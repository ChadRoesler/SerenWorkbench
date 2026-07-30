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
#
#  VALIDATION IS FAIL-CLOSED. The loader is deliberately lenient (Postel) —
#  a malformed manifest is skipped, not fatal. Per-ARGUMENT validation is
#  the opposite, on purpose: these values come from the model and land in
#  argv slots and URL paths. A constraint that silently evaporates because
#  its regex didn't compile is worse than no constraint, because the
#  manifest author believes they have one. So a bad `pattern:` doesn't get
#  dropped with a warning - it refuses the parameter until it's fixed.
# ════════════════════════════════════════════════════════════════════════

from __future__ import annotations

import json
import re
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
        # Compile every `pattern:` ONCE, here, rather than per call. Failures
        # are kept (not raised) so one broken regex disables one parameter
        # instead of taking the whole registry down at startup.
        self._patterns, self._pattern_errors = self._compile_patterns(entry)
        self._is_process = bool(
            entry.invoke and (entry.invoke.kind or "").strip().lower() == "process"
        )

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
            # PUBLISH the string constraints. A rule the caller can't read is
            # a rule it breaks blind; in the schema it's a contract instead.
            if p.pattern:
                prop["pattern"] = p.pattern
            if p.enum:
                prop["enum"] = list(p.enum)
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
                result = await invoke_process(
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

            # A pattern that didn't compile disables the parameter outright.
            # Fail loud and closed - see the fail-closed note in the header.
            bad_pattern = self._pattern_errors.get(param.name)
            if bad_pattern is not None:
                return {}, (
                    f"parameter '{param.name}' has an invalid pattern in the "
                    f"manifest ({bad_pattern}); refusing to run until it is fixed."
                )

            supplied = param.name in raw and raw[param.name] is not None
            source = "" if supplied else " default"
            value = raw[param.name] if supplied else param.default

            if not supplied and value is None:
                if param.required:
                    return {}, f"parameter '{param.name}' is required."
                continue  # optional + no default + not supplied => omit

            coerced, err = self._try_coerce(value, param.type or "string")
            if err:
                return {}, f"parameter '{param.name}'{source}: {err}"

            err = self._constraint_error(param, coerced)
            if err:
                return {}, f"parameter '{param.name}'{source}: {err}"

            result[param.name] = coerced

        return result, None

    # -- Constraint gate --

    def _constraint_error(self, param: ToolParameter, value: object) -> Optional[str]:
        """Every per-value rule, in one place. None means the value is fine.

        One function rather than the old check/report pair: two functions
        computing the same predicate is where drift lives, and the drift
        would land on the permissive side without anything going red.
        """
        # -- enum: closed set, compared on the coerced value --
        if param.enum:
            if value not in param.enum:
                allowed = ", ".join(repr(v) for v in param.enum)
                return f"value {value!r} is not one of the permitted values: {allowed}"

        # -- numeric bounds --
        if param.min is not None or param.max is not None:
            try:
                numeric = float(value)  # type: ignore[arg-type]
            except (ValueError, TypeError):
                numeric = None
            if numeric is not None:
                if param.min is not None and numeric < param.min:
                    return f"value {numeric} below min {param.min}"
                if param.max is not None and numeric > param.max:
                    return f"value {numeric} above max {param.max}"

        # -- string rules --
        if isinstance(value, str):
            rx = self._patterns.get(param.name or "")
            if rx is not None and not rx.fullmatch(value):
                return (
                    f"value does not match the required pattern "
                    f"{param.pattern!r} (the whole value must match)"
                )

            # CWE-88: a value that becomes a flag. Only argv is at risk -
            # a web query string starting with '-' is inert, and refusing it
            # there would break legitimate tools for no gain.
            if (
                self._is_process
                and value.startswith("-")
                and not param.allow_leading_dash
            ):
                return (
                    "value starts with '-', which the spawned program would read "
                    "as a flag rather than data. Put a literal '--' in argv before "
                    "this slot, or set allow_leading_dash: true on the parameter "
                    "if it is genuinely meant to carry flags."
                )

        return None

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

    # -- Protocol tool construction --

    @staticmethod
    def _compile_patterns(
        entry: ToolEntry,
    ) -> Tuple[Dict[str, "re.Pattern[str]"], Dict[str, str]]:
        """Compile each parameter's `pattern:` once at construction.

        Returns (compiled, errors) keyed by parameter name. A regex that
        won't compile lands in `errors` and is enforced as a hard refusal at
        call time, NOT quietly discarded.
        """
        compiled: Dict[str, "re.Pattern[str]"] = {}
        errors: Dict[str, str] = {}
        for p in (entry.parameters or []):
            if not p.name or not p.pattern:
                continue
            try:
                compiled[p.name] = re.compile(p.pattern)
            except re.error as ex:
                errors[p.name] = str(ex)
                print(
                    f"[mcp-registry] tool '{entry.name}': parameter '{p.name}' has an "
                    f"uncompilable pattern {p.pattern!r} ({ex}); the parameter is "
                    "refused until the manifest is fixed",
                    file=sys.stderr,
                )
        return compiled, errors

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
