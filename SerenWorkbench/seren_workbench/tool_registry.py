"""
seren_workbench.tool_registry
════════════════════════════════════════════════════════════════════════

Central registry for all tools — both builtin (Python modules in models/tools/)
and dynamic (YAML manifests loaded from disk). Provides the combined list for
the viewer and the MCP server, plus enable/disable state management.

The viewer's toggles feed into this registry; the MCP server reads it to decide
which tools to expose to the LLM.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

from .tool_config.mcp_config import McpConfig


@dataclass
class ToolInfo:
    """Serialisable info about one tool — for the viewer and the /tools endpoint."""
    name: str
    description: str
    type: str  # "builtin" or "dynamic"
    source: str = ""
    enabled: bool = True
    parameters: list[dict] = field(default_factory=list)
    # For toggles: some tools have multiple actions that can be individually
    # disabled. E.g. memory_tools has remember/recall/forget as sub-actions.
    actions: list[dict] = field(default_factory=list)


@dataclass
class ToolAction:
    """A sub-action within a tool that can be toggled independently."""
    name: str
    description: str
    enabled: bool = True


class ToolRegistry:
    """Holds every tool definition and tracks enable/disable state.

    The viewer calls enable_tool()/disable_tool()/enable_action()/disable_action()
    via POST /tools/state. The MCP server queries is_enabled() before exposing
    a tool to the LLM.
    """

    def __init__(self, builtin_tools: list[ToolInfo],
                 dynamic_tools: list[ToolInfo]) -> None:
        self._builtin = builtin_tools
        self._dynamic = dynamic_tools
        # name -> enabled state
        self._enabled: dict[str, bool] = {}
        # "name.action" -> enabled state for sub-actions
        self._action_enabled: dict[str, bool] = {}
        self._lock = False  # will be asyncio.Lock in production

        # Initialise defaults: everything enabled
        for t in builtin_tools:
            self._enabled[t.name] = True
            for a in t.actions:
                self._action_enabled[f"{t.name}.{a['name']}"] = True
        for t in dynamic_tools:
            self._enabled[t.name] = True
            for a in t.actions:
                self._action_enabled[f"{t.name}.{a['name']}"] = True

    def all_tools(self) -> list[ToolInfo]:
        """Return combined list, with current enabled states applied."""
        result = []
        for t in self._builtin + self._dynamic:
            t.enabled = self._enabled.get(t.name, True)
            for a in t.actions:
                key = f"{t.name}.{a['name']}"
                a["enabled"] = self._action_enabled.get(key, True)
            result.append(t)
        return result

    def get_tool(self, name: str) -> Optional[ToolInfo]:
        for t in self._builtin + self._dynamic:
            if t.name == name:
                return t
        return None

    def is_enabled(self, tool_name: str) -> bool:
        return self._enabled.get(tool_name, True)

    def is_action_enabled(self, tool_name: str, action: str) -> bool:
        return self._action_enabled.get(f"{tool_name}.{action}", True)

    def enable_tool(self, name: str) -> bool:
        if name not in self._enabled:
            return False
        self._enabled[name] = True
        return True

    def disable_tool(self, name: str) -> bool:
        if name not in self._enabled:
            return False
        self._enabled[name] = False
        return True

    def enable_action(self, tool_name: str, action: str) -> bool:
        key = f"{tool_name}.{action}"
        if key not in self._action_enabled:
            return False
        self._action_enabled[key] = True
        return True

    def disable_action(self, tool_name: str, action: str) -> bool:
        key = f"{tool_name}.{action}"
        if key not in self._action_enabled:
            return False
        self._action_enabled[key] = False
        return True

    def snapshot(self) -> dict:
        return {
            "tools": [{
                "name": t.name,
                "description": t.description,
                "type": t.type,
                "source": t.source,
                "enabled": self._enabled.get(t.name, True),
                "actions": [
                    {
                        "name": a["name"],
                        "description": a.get("description", ""),
                        "enabled": self._action_enabled.get(
                            f"{t.name}.{a['name']}", True),
                    }
                    for a in t.actions
                ],
            } for t in self._builtin + self._dynamic],
        }


# ── Build the registry from the builtin tool modules ───────────────────

def _builtin_tool_info() -> list[ToolInfo]:
    """Gather tool definitions from models/tools/ modules.

    Each module exports one or more `*_TOOL_DEF` dicts with name/description/
    input_schema. We extract those and build ToolInfo entries.
    """
    import importlib
    import pkgutil

    info = []
    # Discover all modules in models/tools
    pkg = importlib.import_module(".models.tools", package=__package__)
    for _, name, _ in pkgutil.iter_modules(pkg.__path__):
        mod = importlib.import_module(f".models.tools.{name}", package=__package__)
        # Collect every global whose name ends with _TOOL_DEF or _TOOL_DEFINITION
        for attr_name in dir(mod):
            if not (attr_name.endswith("_TOOL_DEF") or attr_name.endswith("_TOOL_DEFINITION")):
                continue
            val = getattr(mod, attr_name)
            if not isinstance(val, dict):
                continue
            tname = val.get("name", attr_name)
            desc = val.get("description", "(no description)")
            schema = val.get("input_schema", {})
            params = _extract_params(schema)
            info.append(ToolInfo(
                name=tname,
                description=desc,
                type="builtin",
                source=f"models/tools/{name}.py",
                enabled=True,
                parameters=params,
            ))
    return info


def _extract_params(schema: dict) -> list[dict]:
    """Turn an MCP input_schema into a param list for the viewer."""
    props = schema.get("properties", {})
    required = set(schema.get("required", []))
    params = []
    for pname, pdef in props.items():
        params.append({
            "name": pname,
            "type": pdef.get("type", "string"),
            "required": pname in required,
            "description": pdef.get("description", ""),
            "default": pdef.get("default"),
        })
    return params


def build_registry(
    mcp_config: Optional[McpConfig] = None,
    tools_dir: str = "/opt/seren/tools",
) -> ToolRegistry:
    """Factory: gather builtin + dynamic tools, return a populated registry.

    Args:
        mcp_config: optional McpConfig for tool-level knob overrides.
        tools_dir:  path to the YAML manifest directory for dynamic tools.
                    Defaults to /opt/seren/tools — empty if absent.
    """
    builtin = _builtin_tool_info()
    dynamic = _dynamic_tool_info(tools_dir, mcp_config)
    return ToolRegistry(builtin, dynamic)


def _dynamic_tool_info(tools_dir: str,
                        mcp_config: Optional[McpConfig] = None) -> list[ToolInfo]:
    """Load YAML tool manifests from *tools_dir* and build ToolInfo entries.

    Uses ManifestLoader (lenient — missing dir = empty result, malformed
    files are skipped with warnings). Each ToolEntry becomes a ToolInfo
    with parameters extracted from the entry's parameter list.
    """
    info: list[ToolInfo] = []

    # Short-circuit if the directory doesn't exist
    if not os.path.isdir(tools_dir):
        return info

    from .dynamic_tools.manifest_loader import ManifestLoader
    from .dynamic_tools.manifest_models import ToolParameter

    loader = ManifestLoader()
    result = loader.load_directory(tools_dir)

    for entry, _manifest, _source in result.resolved_inline_tools:
        name = entry.name or "unnamed"
        desc = entry.description or "(no description)"
        params = _extract_dynamic_params(entry.parameters or [])
        info.append(ToolInfo(
            name=name,
            description=desc,
            type="dynamic",
            source=_source if isinstance(_source, str) else "",
            enabled=True,
            parameters=params,
        ))

    return info


def _extract_dynamic_params(params: list) -> list[dict]:
    """Convert ToolParameter objects to the viewer-friendly param dict list."""
    out = []
    for p in params:
        pname = p.name if hasattr(p, "name") else ""
        ptype = p.type if hasattr(p, "type") else "string"
        preq = p.required if hasattr(p, "required") else False
        pdesc = p.description if hasattr(p, "description") else ""
        pdefault = p.default if hasattr(p, "default") else None
        out.append({
            "name": pname,
            "type": ptype,
            "required": preq,
            "description": pdesc,
            "default": pdefault,
        })
    return out
