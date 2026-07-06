# ════════════════════════════════════════════════════════════════════════
#  YamlDispatchedTool - MCP tool subclass for a plug-and-play tool loaded
#  from a manifest YAML.
#
#  Overrides the MCP tool interface:
#    name/description      - from the manifest entry
#    input_schema          - JSON Schema built from ToolParameter list
#    call                  - extracts arguments, validates + defaults them,
#                            routes to ProcessDispatcher or WebDispatcher
#
#  AUDIT LOG: every dispatch writes an audit entry.
# ════════════════════════════════════════════════════════════════════════

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .manifest_models import ManifestFile, ToolEntry, ToolParameter
from .tool_audit_log import ToolAuditLog, AuditEntry, ERROR_MESSAGE_MAX_CHARS
from .process_dispatcher import invoke_process
from .web_dispatcher import invoke_web


class YamlDispatchedTool:
    """A plug-and-play MCP tool loaded from a YAML manifest.

    In the Python MCP SDK, tools are registered as callable objects with
    a name, description, input_schema, and a call method.
    """

    def __init__(
        self,
        entry: ToolEntry,
        owner: ManifestFile,
        source_path: str,
        http_client: Any,  # httpx.AsyncClient
        audit_log: ToolAuditLog,
    ) -> None:
        self._entry = entry
        self._owner = owner
        self._source_path = source_path
        self._http_client = http_client
        self._audit_log = audit_log
        self._param_types = self._build_param_types(entry)

    # -- MCP tool interface --

    @property
    def name(self) -> str:
        return self._entry.name or "(unnamed)"

    @property
    def description(self) -> str:
        return self._entry.description or ""

    @property
    def source_path(self) -> str:
        return self._source_path

    def input_schema(self) -> dict:
        """Build JSON Schema representing the parameter contract."""
        properties = {}
        required = []
        params = self._entry.parameters or []

        for p in params:
            if not p.name:
                continue
            prop = {"type": self._map_json_schema_type(p.type or "string")}
            if p.description:
                prop["description"] = p.description
            if p.default is not None:
                prop["default"] = p.default
            if p.min is not None:
                prop["minimum"] = p.min
            if p.max is not None:
                prop["maximum"] = p.max
            properties[p.name] = prop
            if p.required:
                required.append(p.name)

        return {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }

    async def call(self, arguments: Dict[str, Any]) -> dict:
        """Execute the tool with the given arguments.

        Returns a dict suitable as an MCP CallToolResult:
          {"content": [...], "is_error": bool}
        """
        start_ns = time.monotonic_ns()

        args, validation_error = self._resolve_arguments(arguments)
        if validation_error:
            elapsed_ms = (time.monotonic_ns() - start_ns) // 1_000_000
            self._record_audit(elapsed_ms, len(arguments), success=False, error=validation_error)
            return {"is_error": True, "content": [{"type": "text", "text": validation_error}]}

        print(
            f"[mcp-audit] {self.name}: dispatching kind={self._entry.invoke.kind if self._entry.invoke else '?'} "
            f"args=[{', '.join(args.keys())}]",
            file=sys.stderr,
        )

        kind = (self._entry.invoke.kind or "").strip().lower() if self._entry.invoke else ""
        try:
            if kind == "process":
                result = invoke_process(
                    self._entry.invoke,
                    self._owner.configuration,
                    self.name,
                    args,
                )
            elif kind == "web":
                result = await invoke_web(
                    self._entry.invoke,
                    self._owner.configuration,
                    self.name,
                    args,
                    self._param_types,
                    self._http_client,
                )
            else:
                result = {
                    "is_error": True,
                    "content": [{
                        "type": "text",
                        "text": f"tool '{self.name}' has unknown invoke.kind '{self._entry.invoke.kind if self._entry.invoke else None}'",
                    }],
                }
        except Exception as ex:
            result = {"is_error": True, "content": [{"type": "text", "text": str(ex)}]}

        elapsed_ms = (time.monotonic_ns() - start_ns) // 1_000_000
        is_error = result.get("is_error", False)
        error_text = None
        if is_error:
            content = result.get("content", [])
            if content:
                error_text = content[0].get("text", "") if isinstance(content[0], dict) else str(content[0])
        self._record_audit(elapsed_ms, len(args), success=not is_error, error=error_text)
        return result

    # -- Argument resolution --

    def _resolve_arguments(self, raw: Dict[str, Any]) -> Tuple[Dict[str, object], Optional[str]]:
        result: Dict[str, object] = {}
        parameters = self._entry.parameters or []

        for param in parameters:
            if not param.name:
                continue

            if param.name in raw:
                raw_val = raw[param.name]
                if raw_val is not None:
                    coerced, err = self._try_coerce(raw_val, param.type or "string")
                    if err:
                        return {}, f"parameter '{param.name}': {err}"
                    if not self._check_range(param, coerced, err):
                        # _check_range returns False + sets err via closure - recheck
                        range_err = self._range_error(param, coerced)
                        if range_err:
                            return {}, f"parameter '{param.name}': {range_err}"
                    result[param.name] = coerced
                elif param.required:
                    return {}, f"parameter '{param.name}' is required."
                elif param.default is not None:
                    coerced, err = self._try_coerce(param.default, param.type or "string")
                    if err:
                        return {}, f"parameter '{param.name}' default: {err}"
                    result[param.name] = coerced
            elif param.required:
                return {}, f"parameter '{param.name}' is required."
            elif param.default is not None:
                coerced, err = self._try_coerce(param.default, param.type or "string")
                if err:
                    return {}, f"parameter '{param.name}' default: {err}"
                result[param.name] = coerced
            # else: optional + no default + not supplied => omit

        return result, None

    @staticmethod
    def _try_coerce(value: Any, declared_type: str) -> Tuple[object, Optional[str]]:
        declared = declared_type.strip().lower()
        try:
            if declared == "string":
                return str(value), None
            elif declared == "integer":
                if isinstance(value, bool):
                    return (1 if value else 0), None
                return int(value), None
            elif declared == "number":
                return float(value), None
            elif declared == "boolean":
                if isinstance(value, bool):
                    return value, None
                if isinstance(value, str):
                    lower = value.strip().lower()
                    if lower in ("true", "yes", "on", "1"):
                        return True, None
                    if lower in ("false", "no", "off", "0"):
                        return False, None
                return bool(value), None
            else:
                return str(value), None
        except (ValueError, TypeError) as ex:
            return None, str(ex)

    @staticmethod
    def _check_range(param: ToolParameter, value: object, _previous_err: Optional[str]) -> bool:
        # Range check only for numeric types
        if param.min is None and param.max is None:
            return True
        if value is None:
            return True
        try:
            numeric = float(value)  # type: ignore
        except (ValueError, TypeError):
            return True
        if param.min is not None and numeric < param.min:
            return False
        if param.max is not None and numeric > param.max:
            return False
        return True

    @staticmethod
    def _range_error(param: ToolParameter, value: object) -> Optional[str]:
        try:
            numeric = float(value)  # type: ignore
        except (ValueError, TypeError):
            return None
        if param.min is not None and numeric < param.min:
            return f"value {numeric} below min {param.min}"
        if param.max is not None and numeric > param.max:
            return f"value {numeric} above max {param.max}"
        return None

    # -- Protocol tool construction --

    @staticmethod
    def _build_param_types(entry: ToolEntry) -> Dict[str, str]:
        mapping: Dict[str, str] = {}
        if entry.parameters:
            for p in entry.parameters:
                if p.name:
                    mapping[p.name] = (p.type or "string").strip().lower()
        return mapping

    @staticmethod
    def _map_json_schema_type(declared: str) -> str:
        t = declared.strip().lower()
        if t in ("string", "integer", "number", "boolean"):
            return t
        return "string"

    # -- Audit --

    def _record_audit(self, duration_ms: int, arg_count: int, success: bool, error: Optional[str]) -> None:
        truncated = error
        if truncated is not None and len(truncated) > ERROR_MESSAGE_MAX_CHARS:
            truncated = truncated[:ERROR_MESSAGE_MAX_CHARS] + "…"

        self._audit_log.record(AuditEntry(
            timestamp=time.time(),
            tool=self.name,
            kind=(self._entry.invoke.kind or "?").strip().lower() if self._entry.invoke else "?",
            source_file=Path(self._source_path).name,
            duration_ms=duration_ms,
            success=success,
            error_message=truncated,
            arg_count=arg_count,
        ))
