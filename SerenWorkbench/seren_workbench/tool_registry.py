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
  - tools_disabled entries start disabled.
  - tools_enabled non-empty = allowlist: everything NOT named starts disabled.

And then REMEMBERED. Every toggle the dashboard makes, and every proposal
approved-but-left-off, is written to a small state file and read back at
the next start. Before that file existed the dashboard's toggles were
in-memory only: reboot the Jetson and the tool you deliberately switched
off was live again, which is the opposite of what a switch is for. A name
the yaml lists explicitly is the operator's written word and beats the
file; everything else is whatever the dashboard last said.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)
STATE_FORMAT = 1

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
                 start_disabled: Optional[set[str]] = None,
                 state_path: Optional[str] = None,
                 pinned: Optional[set[str]] = None,
                 named: Optional[set[str]] = None) -> None:
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
        # Names the yaml spoke for. The file never overrides these. `named`
        # is the subset the yaml LITERALLY lists (not the ones an allowlist
        # implies), which is the only set worth a conflict warning.
        self._pinned: set[str] = set(pinned or ())
        self._named: set[str] = set(named if named is not None else self._pinned)
        # Where toggles are remembered between restarts. None = in-memory
        # only (bare registries in tests). The file is read once here and
        # written on every change; a write that fails leaves the toggle in
        # force for this process and says so in persist_error.
        self._state_path = state_path
        self.persist_error: str = ""
        self._persisted_tools: dict[str, bool] = {}
        self._persisted_actions: dict[str, bool] = {}
        self._load_state()

        for t in builtin_tools + dynamic_tools:
            self._enabled[t.name] = self._initial_state(t.name)
            for a in t.actions:
                key = f"{t.name}.{a['name']}"
                self._action_enabled[key] = self._persisted_actions.get(key, True)

    # ── Remembering toggles ────────────────────────────────────────────

    @property
    def state_path(self) -> Optional[str]:
        return self._state_path

    @property
    def persisted(self) -> bool:
        """True when the last change was written down somewhere it will be
        read back from. False for an in-memory registry or after a failed
        write - the route reports it so a toggle that will not survive a
        restart is never mistaken for one that will."""
        return bool(self._state_path) and not self.persist_error

    def _initial_state(self, name: str) -> bool:
        """yaml first, then the remembered toggle, then on."""
        if name in self._pinned:
            return name not in self._start_disabled
        if name in self._persisted_tools:
            return self._persisted_tools[name]
        return name not in self._start_disabled

    def _load_state(self) -> None:
        if not self._state_path or not os.path.isfile(self._state_path):
            return
        try:
            with open(self._state_path, encoding="utf-8") as f:
                raw = json.load(f)
            tools = raw.get("tools") if isinstance(raw, dict) else None
            actions = raw.get("actions") if isinstance(raw, dict) else None
            self._persisted_tools = {str(k): bool(v) for k, v in (tools or {}).items()}
            self._persisted_actions = {str(k): bool(v) for k, v in (actions or {}).items()}
        except Exception as exc:  # noqa: BLE001 - a bad file must not stop boot
            log.warning("tool state file %s unreadable (%s); starting from the yaml",
                        self._state_path, exc)
            return
        clashes = sorted(
            name for name, remembered in self._persisted_tools.items()
            if name in self._named and remembered != (name not in self._start_disabled)
        )
        if clashes:
            log.warning("the yaml and the dashboard's remembered state disagree about %s; "
                        "the yaml wins - drop a name from tools_enabled/tools_disabled to "
                        "let the dashboard decide it", ", ".join(clashes))

    def _persist(self) -> None:
        """Write the remembered toggles. Only DELIBERATE ones are in the file:
        a dashboard click, or a proposal approved and left off. Tools that
        merely took their default are not written, so the file is a record
        of decisions, not a dump - and a conflict with the yaml is always
        about something a person actually chose. Atomic (tmp + replace) so
        a crash mid-write leaves the previous file, not half of one."""
        if not self._state_path:
            return
        payload = {"format": STATE_FORMAT, "saved_at": time.time(),
                   "tools": dict(self._persisted_tools),
                   "actions": dict(self._persisted_actions)}
        try:
            parent = os.path.dirname(self._state_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            tmp = self._state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, sort_keys=True)
            os.replace(tmp, self._state_path)
            self.persist_error = ""
        except OSError as exc:
            self.persist_error = f"{exc}"
            log.warning("could not write tool state to %s: %s - the toggle holds "
                        "until restart", self._state_path, exc)

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
        # A deliberate decision, so it is remembered: the tool must still be
        # off after a restart, not just until one.
        for name in names:
            self._persisted_tools[name] = False

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
                self._enabled[t.name] = self._initial_state(t.name)
            for a in t.actions:
                key = f"{t.name}.{a['name']}"
                self._action_enabled.setdefault(key, self._persisted_actions.get(key, True))

        self._dynamic = new_dynamic
        self._persist()

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
        self._persisted_tools[name] = True
        self._persist()
        return True

    def disable_tool(self, name: str) -> bool:
        if name not in self._enabled:
            return False
        self._enabled[name] = False
        self._persisted_tools[name] = False
        self._persist()
        return True

    def enable_action(self, tool_name: str, action: str) -> bool:
        key = f"{tool_name}.{action}"
        if key not in self._action_enabled:
            return False
        self._action_enabled[key] = True
        self._persisted_actions[key] = True
        self._persist()
        return True

    def disable_action(self, tool_name: str, action: str) -> bool:
        key = f"{tool_name}.{action}"
        if key not in self._action_enabled:
            return False
        self._action_enabled[key] = False
        self._persisted_actions[key] = False
        self._persist()
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
    tools_dir: str = "",
    tools_enabled: Optional[list[str]] = None,
    tools_disabled: Optional[list[str]] = None,
    exclude: Optional[set[str]] = None,
    state_path: Optional[str] = None,
) -> ToolRegistry:
    """Factory: gather builtin + dynamic tools, return a populated registry.

    Args:
        mcp_config:     optional McpConfig for tool-level knob overrides
                        (reserved — knobs are injected per-call via DI).
        tools_dir:      path to the YAML manifest directory for dynamic tools.
                        Empty or absent = no dynamic tools.
        tools_enabled:  allowlist from DashboardConfig — if non-empty, every
                        tool NOT named here starts disabled.
        tools_disabled: denylist from DashboardConfig — these start disabled.
        state_path:     where the dashboard's toggles are remembered between
                        restarts. None = this registry forgets on exit.
    """
    builtin = _builtin_tool_info()
    dynamic = _dynamic_tool_info(tools_dir, mcp_config) if tools_dir else []

    # A feature switched off should be ABSENT, not present-and-erroring. A
    # tool that lists in the schema and then reports "not enabled" on every
    # call spends the model's attention to teach it a lesson the operator
    # already knew.
    if exclude:
        builtin = [t for t in builtin if t.name not in exclude]
        dynamic = [t for t in dynamic if t.name not in exclude]

    all_names = {t.name for t in builtin} | {t.name for t in dynamic}
    start_disabled: set[str] = set(tools_disabled or [])
    # What the yaml said in so many words. With an allowlist every name is
    # spoken for (in it or not); otherwise only the denylist is.
    pinned: set[str] = set(tools_disabled or [])
    if tools_enabled:
        start_disabled |= all_names - set(tools_enabled)
        pinned |= all_names

    return ToolRegistry(builtin, dynamic, start_disabled=start_disabled,
                        state_path=state_path, pinned=pinned,
                        named=set(tools_disabled or []) | set(tools_enabled or []))


def _dynamic_tool_info(tools_dir: str,
                        mcp_config: Optional[McpConfig] = None) -> list[ToolInfo]:
    """Load YAML tool manifests from *tools_dir* and build ToolInfo entries.

    Uses ManifestLoader (lenient — missing dir = empty result, malformed
    files are skipped with warnings). Each ToolEntry becomes a ToolInfo
    with parameters extracted from the entry's parameter list, CARRYING the
    entry + owning manifest so the MCP layer can build its dispatcher.
    """
    # Short-circuit if the directory doesn't exist
    if not tools_dir or not os.path.isdir(tools_dir):
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
