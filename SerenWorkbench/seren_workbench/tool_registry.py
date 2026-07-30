"""
seren_workbench.tool_registry
════════════════════════════════════════════════════════════════════════

Central registry for all tools — both builtin (Python modules in models/tools/)
and dynamic (YAML manifests loaded from disk). Provides the combined list for
the viewer and the MCP server, plus enable/disable state management.

The viewer's toggles feed into this registry; the MCP server checks it at
CALL TIME to decide whether a tool may run (registration happens once at
startup, so the toggle gate lives in the call path, not the tool list).

Startup enable state is seeded from DashboardConfig:
  - tools_disabled entries start disabled (survives restarts, unlike the
    in-memory toggles).
  - tools_enabled non-empty = allowlist: everything NOT named starts disabled.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

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
    # Presentation. `name` stays the identifier the model calls; these two
    # exist so a person scanning the dashboard isn't reading snake_case.
    # Both are DERIVED unless something declares otherwise — see
    # resolve_toolbox/humanise below.
    display_name: str = ""
    toolbox: str = ""
    # For toggles: some tools have multiple actions that can be individually
    # disabled. E.g. memory_tools has remember/recall/forget as sub-actions.
    actions: list[dict] = field(default_factory=list)
    # Dynamic tools carry their manifest entry + owning manifest so the MCP
    # layer can build a YamlDispatchedTool. Builtins leave these None.
    # (Not serialized: /tools and snapshot() build their dicts explicitly.)
    entry: Any = None
    owner: Any = None


@dataclass
class ToolAction:
    """A sub-action within a tool that can be toggled independently."""
    name: str
    description: str
    enabled: bool = True


class ToolRegistry:
    """Holds every tool definition and tracks enable/disable state.

    The viewer calls enable_tool()/disable_tool()/enable_action()/disable_action()
    via POST /tools/state. The MCP server queries is_enabled() at call time
    before letting a tool run.

    Concurrency note: state lives in plain dicts mutated by single assignments
    — atomic under CPython — and every access path runs on the app's event
    loop, so no lock is needed. Revisit if toggling ever moves off-loop.
    """

    def __init__(self, builtin_tools: list[ToolInfo],
                 dynamic_tools: list[ToolInfo],
                 start_disabled: Optional[set[str]] = None) -> None:
        self._builtin = builtin_tools
        self._dynamic = dynamic_tools
        # name -> enabled state
        self._enabled: dict[str, bool] = {}
        # "name.action" -> enabled state for sub-actions
        self._action_enabled: dict[str, bool] = {}
        # Kept so a later replace_dynamic() can seed NEW tools the same way
        # startup did — otherwise a tool named in tools_disabled would come
        # back enabled the first time someone hit reload.
        self._start_disabled: set[str] = set(start_disabled or ())

        for t in builtin_tools + dynamic_tools:
            self._enabled[t.name] = t.name not in self._start_disabled
            for a in t.actions:
                self._action_enabled[f"{t.name}.{a['name']}"] = True

    def builtin_names(self) -> set[str]:
        """Names owned by builtin tools — the set a manifest must never take."""
        return {t.name for t in self._builtin}

    def seed_disabled(self, names: set[str]) -> None:
        """Mark names so that when they NEXT appear they start disabled.

        This is how an approved proposal arrives switched off. Approving a
        tool and running it are two different decisions — the first says "I
        read this and it's not malicious", the second says "and I want it
        live right now". Collapsing them means the only moment to change
        your mind is before you've seen it in the list.

        Seeding only bites on first appearance (replace_dynamic won't touch
        a name it already has state for), so enabling the tool later isn't
        undone by the next reload.
        """
        self._start_disabled |= set(names)

    def dynamic_tools(self) -> list[ToolInfo]:
        return list(self._dynamic)

    def replace_dynamic(self, new_dynamic: list[ToolInfo]) -> None:
        """Swap the dynamic tool set, PRESERVING operator toggle state.

        An operator who disabled a tool and then reloaded the directory did
        not thereby re-enable it; a reload is a statement about what exists
        on disk, not about what is permitted to run. Tools that survive the
        reload keep their current state, tools that vanish drop theirs, and
        genuinely new tools are seeded from the startup denylist.
        """
        surviving = {t.name for t in new_dynamic}
        gone = {t.name for t in self._dynamic} - surviving

        for name in gone:
            self._enabled.pop(name, None)
            for key in [k for k in self._action_enabled if k.startswith(f"{name}.")]:
                self._action_enabled.pop(key, None)

        for t in new_dynamic:
            if t.name not in self._enabled:          # new since last load
                self._enabled[t.name] = t.name not in self._start_disabled
            for a in t.actions:
                self._action_enabled.setdefault(f"{t.name}.{a['name']}", True)

        self._dynamic = new_dynamic

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
                "display_name": t.display_name or t.name,
                "toolbox": t.toolbox or "Other",
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

# A tool-definition global is discovered when its attr name ends with the
# suffix (REMEMBER_TOOL_DEF, FETCH_TOOL_DEFINITION, ...) OR is exactly the
# bare form. The bare forms are belt-and-suspenders: 8 modules shipped with
# a bare TOOL_DEFINITION that "endswith('_TOOL_DEFINITION')" silently missed
# (no underscore boundary), which vanished a third of the builtin surface.
_DEF_SUFFIXES = ("_TOOL_DEF", "_TOOL_DEFINITION")
_DEF_BARE_NAMES = ("TOOL_DEF", "TOOL_DEFINITION")


def _is_tool_def_attr(attr_name: str) -> bool:
    return attr_name.endswith(_DEF_SUFFIXES) or attr_name in _DEF_BARE_NAMES


# ── Presentation: human names and toolbox grouping ─────────────────────
#
# Both are DERIVED by default and DECLARABLE when derivation can't know.
# That ordering matters: a scheme that required every tool to declare its
# own label would drift the moment someone added a tool and forgot, and a
# scheme that ONLY derived couldn't put wait_for_service_tool.py and
# service_control_tools.py in the same box — which is the actual grouping
# an operator wants. So: per-tool key beats module constant beats derived.

# Words that look wrong in Title Case. Small on purpose — this is a
# readability nicety, not a linguistics project.
_ACRONYMS = {
    "url": "URL", "urls": "URLs", "id": "ID", "ids": "IDs", "api": "API",
    "mcp": "MCP", "llm": "LLM", "tts": "TTS", "ui": "UI", "os": "OS",
    "cpu": "CPU", "gpu": "GPU", "ram": "RAM", "http": "HTTP", "json": "JSON",
    "yaml": "YAML", "sql": "SQL", "ok": "OK",
}


def humanise(name: str) -> str:
    """get_cluster_status -> 'Get Cluster Status'."""
    parts = [p for p in re.split(r"[_\-\s]+", (name or "").strip()) if p]
    if not parts:
        return name or ""
    return " ".join(_ACRONYMS.get(p.lower(), p[:1].upper() + p[1:]) for p in parts)


def toolbox_from_module(module_name: str) -> str:
    """cluster_tools -> 'Cluster'; wait_for_service_tool -> 'Wait For Service'.

    The trailing _tool/_tools is stripped because it's noise once the label
    is displayed as a toolbox — 'Cluster Toolbox' beats 'Cluster Tools Toolbox'.
    """
    stem = re.sub(r"_tools?$", "", (module_name or "").strip())
    return humanise(stem) or "Other"


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
        # Module-level default, used when a tool doesn't name its own box.
        module_box = getattr(mod, "TOOLBOX", None) or toolbox_from_module(name)
        for attr_name in dir(mod):
            if not _is_tool_def_attr(attr_name):
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
                display_name=val.get("display_name") or humanise(tname),
                toolbox=val.get("toolbox") or module_box,
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
    tools_enabled: Optional[list[str]] = None,
    tools_disabled: Optional[list[str]] = None,
    exclude: Optional[set[str]] = None,
) -> ToolRegistry:
    """Factory: gather builtin + dynamic tools, return a populated registry.

    Args:
        mcp_config:     optional McpConfig for tool-level knob overrides
                        (reserved — knobs are injected per-call via DI).
        tools_dir:      path to the YAML manifest directory for dynamic tools.
                        Defaults to /opt/seren/tools — empty if absent.
        tools_enabled:  allowlist from DashboardConfig — if non-empty, every
                        tool NOT named here starts disabled.
        tools_disabled: denylist from DashboardConfig — these start disabled.
    """
    builtin = _builtin_tool_info()
    dynamic = _dynamic_tool_info(tools_dir, mcp_config)

    # A feature switched off should be ABSENT, not present-and-erroring. A
    # tool that lists in the schema and then reports "not enabled" on every
    # call spends the model's attention to teach it a lesson the operator
    # already knew.
    if exclude:
        builtin = [t for t in builtin if t.name not in exclude]
        dynamic = [t for t in dynamic if t.name not in exclude]

    all_names = {t.name for t in builtin} | {t.name for t in dynamic}
    start_disabled: set[str] = set(tools_disabled or [])
    if tools_enabled:
        start_disabled |= all_names - set(tools_enabled)

    return ToolRegistry(builtin, dynamic, start_disabled=start_disabled)


def _dynamic_tool_info(tools_dir: str,
                        mcp_config: Optional[McpConfig] = None) -> list[ToolInfo]:
    """Load YAML tool manifests from *tools_dir* and build ToolInfo entries.

    Uses ManifestLoader (lenient — missing dir = empty result, malformed
    files are skipped with warnings). Each ToolEntry becomes a ToolInfo
    with parameters extracted from the entry's parameter list, CARRYING the
    entry + owning manifest so the MCP layer can build its dispatcher.
    """
    # Short-circuit if the directory doesn't exist
    if not os.path.isdir(tools_dir):
        return []

    from .dynamic_tools.manifest_loader import ManifestLoader

    loader = ManifestLoader()
    return tool_info_from_load_result(loader.load_directory(tools_dir))


def tool_info_from_load_result(result) -> list[ToolInfo]:
    """Turn a ManifestLoader LoadResult into ToolInfo entries.

    Split out of _dynamic_tool_info so the reload path builds its tool list
    through the EXACT code startup used. Two functions constructing ToolInfo
    from the same LoadResult is precisely the duplicate-source-of-truth shape
    that keeps costing this project days.
    """
    info: list[ToolInfo] = []
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
            entry=entry,
            owner=_manifest,
            display_name=getattr(entry, "display_name", None) or humanise(name),
            toolbox=_dynamic_toolbox(entry, _manifest, _source),
        ))

    return info


def _dynamic_toolbox(entry, manifest, source) -> str:
    """Which custom toolbox a manifest tool belongs to.

    Order: the tool says so > the manifest's metadata says so > the file
    name. The filename fallback means a operator who just drops
    `hotdog-math.yaml` in gets a "Hotdog Math" box for free without
    learning a new key.
    """
    declared = getattr(entry, "toolbox", None)
    if declared:
        return str(declared)
    meta = getattr(manifest, "metadata", None)
    if meta is not None and getattr(meta, "toolbox", None):
        return str(meta.toolbox)
    stem = os.path.splitext(os.path.basename(str(source or "")))[0]
    return humanise(stem) or "Custom"


def _extract_dynamic_params(params: list) -> list[dict]:
    """Convert ToolParameter objects to the viewer-friendly param dict list."""
    out = []
    for p in params:
        pname = p.name if hasattr(p, "name") else ""
        ptype = p.type if hasattr(p, "type") else "string"
        preq = p.required if hasattr(p, "required") else False
        pdesc = p.description if hasattr(p, "description") else ""
        pdefault = p.default if hasattr(p, "default") else None
        entry = {
            "name": pname,
            "type": ptype,
            "required": preq,
            "description": pdesc,
            "default": pdefault,
        }
        # Surface the string constraints to the dashboard too — an operator
        # reading /tools should see the same rules the model is held to.
        if getattr(p, "pattern", None):
            entry["pattern"] = p.pattern
        if getattr(p, "enum", None):
            entry["enum"] = list(p.enum)
        out.append(entry)
    return out
