"""
seren_workbench.upstream
════════════════════════════════════════════════════════════════════════

The standard components' own tools, passed through.

The Workbench is the ONE place a model connects: its workbench, its
centralized MCP. The standard system - Memory, Loci, the
Corpus Callosum, the Hippocampus, Lodestar - is there from the start, each
component with a switch; Probe, Theatre, Margin and anything else arrive as
plugins (YAML manifests, `from:` imports), and a model can propose tools of
its own.

HOW THE STANDARD COMPONENTS GET HERE: each of them already serves its tools
over MCP at <its url>/mcp. The Workbench connects to that endpoint as a
client, asks what tools it has, and offers exactly those - same names, same
descriptions, same input schemas - forwarding each call.

WHY PASS-THROUGH AND NOT A COPY. The Workbench used to carry hand-written
memory tools. By October 2026 they covered five of Memory's twenty-one tools,
searched only short-term, and still described a consolidator that had been
retired: a second description of someone else's tools is out of date the day
after it is written, and nothing fails when it is. Passed through, the
Workbench shows what each service offers NOW - a tool added to Memory appears
here with no Workbench release - and a tool that is deliberately MCP-only
(Memory's undo_restate has no HTTP route, by design) is still reachable,
because this is still MCP.

    components:                    # seren-workbench.yaml; all on by default
      memory: true
      loci: true
      corpus_callosum: true
      hippocampus: true
      lodestar: true

    services:                      # where each one is, and its token
      memory_url: http://127.0.0.1:7420
      loci_url: http://127.0.0.1:7422
      callosum_url: http://127.0.0.1:7423
      hippocampus_url: http://127.0.0.1:7424
      lodestar_url: http://127.0.0.1:6361

MCP PLUGINS. Anything else that speaks MCP is plugged in the same way, from
`plugins:` in the config - Margin first of all:

    plugins:
      margin:
        url: http://127.0.0.1:7421
        bearer_token_env: SEREN_MARGIN_TOKEN     # or bearer_token / _keyring

A plugin is listed after the standard components, shows under its own toolbox,
and has the same switch, the same retry and the same GET /components line
(kind "plugin"). Margin comes in this way rather than through its HTTP
manifest because that path needs Margin's http_reads turned on - see
config.PluginConfig.

RULES
  - A component that is off, or does not answer, contributes no tools. It is
    asked again every RETRY_SECONDS until it does, so a service that starts
    after the Workbench appears on its own.
  - A passed-through tool WINS over a builtin of the same name: the service's
    own `recall` replaces the old short-term-only one.
  - Two components with the same tool name: the first in COMPONENTS order
    keeps the name, the other is offered as <component>_<name>.
  - Every tool keeps its own switch in the registry (the dashboard's toggles),
    and a switched-off tool is not listed at all.
  - The Workbench never passes ITSELF through: a component whose url is this
    Workbench's own address is skipped.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Optional
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

# A session per call means the MCP client and httpx would each narrate every
# call at INFO - a dozen lines for one `recall`. Quietened unless someone has
# deliberately set a level on them; a failure still surfaces, as the call's
# own error.
for _noisy in ("mcp.client.streamable_http", "httpx"):
    if logging.getLogger(_noisy).level == logging.NOTSET:
        logging.getLogger(_noisy).setLevel(logging.WARNING)

RETRY_SECONDS = 60.0       # how often a component that did not answer is asked again
LIST_TIMEOUT = 8.0         # asking a component what tools it has
CALL_TIMEOUT = 300.0       # one tool call; a sleep or a backup pull takes minutes


@dataclass(frozen=True)
class ComponentSpec:
    key: str               # the name in `components:` (or `plugins:`) and in the API
    display: str           # the toolbox it shows under on the dashboard
    service: str           # which `services:` credential it presents ("" for a plugin)
    url_attr: str          # which `services:` url it lives at ("" for a plugin)
    kind: str = "component"   # "component" (the standard system) or "plugin"


# Order matters: on a name clash the earlier component keeps the plain name.
COMPONENTS: tuple[ComponentSpec, ...] = (
    ComponentSpec("memory", "Memory", "memory", "memory_url"),
    ComponentSpec("loci", "Loci", "loci", "loci_url"),
    ComponentSpec("corpus_callosum", "Corpus Callosum", "callosum", "callosum_url"),
    ComponentSpec("hippocampus", "Hippocampus", "hippocampus", "hippocampus_url"),
    ComponentSpec("lodestar", "Lodestar", "runtime_host", "runtime_host_url"),
)
COMPONENT_KEYS = tuple(c.key for c in COMPONENTS)


@dataclass
class ComponentState:
    spec: ComponentSpec
    url: str = ""
    token: str = ""
    enabled: bool = True
    start_disabled: bool = False                        # a plugin whose tools arrive switched off
    tools: list[Any] = field(default_factory=list)      # mcp.types.Tool, as the component listed them
    error: str = ""
    checked_at: float = 0.0
    listed_at: float = 0.0

    @property
    def mcp_url(self) -> str:
        # The trailing slash is the mount; without it, a 307. An address that
        # already ends in /mcp (a plugin someone pasted whole) is not doubled.
        base = self.url.rstrip("/")
        return (base if base.endswith("/mcp") else base + "/mcp") + "/"


@asynccontextmanager
async def _connect(url: str, token: str, timeout: float) -> AsyncIterator[Any]:
    """One MCP session with a component, initialized. A session per call: no
    connection to keep alive across a service restart, and a component that
    went away fails THIS call with its reason instead of a stale socket."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    headers = {"Authorization": f"Bearer {token}"} if token else None
    async with streamablehttp_client(url, headers=headers, timeout=timeout,
                                     sse_read_timeout=timeout) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


def _is_self(url: str, self_addr: Optional[tuple[str, int]]) -> bool:
    if not self_addr or not url:
        return False
    u = urlsplit(url)
    port = u.port or (443 if u.scheme == "https" else 80)
    if port != int(self_addr[1]):
        return False
    host = (u.hostname or "").lower()
    mine = str(self_addr[0]).lower()
    if host == mine:
        return True
    try:
        return host == "localhost" or ipaddress.ip_address(host).is_loopback or mine in ("0.0.0.0", "::")
    except ValueError:
        return False


class UpstreamHub:
    """The standard components' tools, listed and forwarded."""

    def __init__(self, services: Any, components: dict[str, bool], registry: Any = None,
                 self_addr: Optional[tuple[str, int]] = None,
                 connect: Optional[Callable[..., Any]] = None, audit_log: Any = None,
                 plugins: Optional[list[Any]] = None) -> None:
        self._registry = registry
        self._connect = connect or _connect
        self._audit = audit_log
        self._lock = asyncio.Lock()
        self._owner: dict[str, tuple[str, str]] = {}     # exposed name -> (component key, the component's own name)
        self._exposed: dict[str, Any] = {}               # exposed name -> mcp.types.Tool (renamed if it clashed)
        self._self_addr = self_addr
        self.states: dict[str, ComponentState] = {}
        for spec in COMPONENTS:
            url = str(getattr(services, spec.url_attr, "") or "")
            st = ComponentState(spec=spec, url=url, enabled=bool(components.get(spec.key, True)))
            try:
                st.token = services.resolve_bearer(spec.service) or ""
            except Exception as exc:  # noqa: BLE001 - a keyring that will not open is a reason, not a crash
                st.error = f"could not read its token: {exc}"
            if _is_self(url, self_addr):
                st.enabled = False
                st.error = "this url is the Workbench itself; not passed through"
            self.states[spec.key] = st
        # MCP plugins (config.PluginConfig), after the standard components.
        for plug in plugins or []:
            self.add_plugin(plug)

    def add_plugin(self, plug: Any) -> bool:
        """Plug one MCP server in: at startup (the config's, then the ones an
        approved proposal installed) and live, when a proposal is approved.
        False when the name is taken - nothing is ever replaced. The caller
        asks it for its tools with refresh(only=name)."""
        if not plug.name or plug.name in self.states:
            return False
        spec = ComponentSpec(plug.name, plug.display or plug.name, "", "", kind="plugin")
        st = ComponentState(spec=spec, url=plug.url, enabled=bool(plug.enabled),
                            start_disabled=bool(getattr(plug, "start_disabled", False)))
        try:
            st.token = plug.resolve_bearer() or ""
        except Exception as exc:  # noqa: BLE001
            st.error = f"could not read its token: {exc}"
        if _is_self(plug.url, self._self_addr):
            st.enabled = False
            st.error = "this url is the Workbench itself; not passed through"
        self.states[spec.key] = st
        return True

    # ── what is offered ───────────────────────────────────────────────────
    def owns(self, name: str) -> bool:
        return name in self._owner

    def names(self) -> set[str]:
        return set(self._owner)

    def list_tools(self) -> list[Any]:
        """Every passed-through tool that is switched on, as MCP tools."""
        out = []
        for name, tool in self._exposed.items():
            key, _ = self._owner[name]
            if not self.states[key].enabled:
                continue
            if self._registry is not None and not self._registry.is_enabled(name):
                continue
            out.append(tool)
        return out

    def snapshot(self) -> list[dict[str, Any]]:
        out = []
        for st in self.states.values():
            mine = sorted(n for n, (k, _) in self._owner.items() if k == st.spec.key)
            out.append({"component": st.spec.key, "kind": st.spec.kind, "display": st.spec.display, "enabled": st.enabled,
                        "start_disabled": st.start_disabled,
                        "url": st.url, "has_token": bool(st.token),
                        "available": bool(st.enabled and st.listed_at and not st.error),
                        "tools": mine, "tool_count": len(mine), "error": st.error or None,
                        "checked_at": st.checked_at or None, "listed_at": st.listed_at or None})
        return out

    # ── asking the components ─────────────────────────────────────────────
    async def _list_one(self, st: ComponentState) -> None:
        st.checked_at = time.time()
        if not st.url:
            st.tools, st.error = [], "no url configured"
            return
        try:
            async with self._connect(st.mcp_url, st.token, LIST_TIMEOUT) as session:
                res = await asyncio.wait_for(session.list_tools(), timeout=LIST_TIMEOUT)
            st.tools = list(getattr(res, "tools", None) or [])
            st.error, st.listed_at = "", time.time()
        except BaseException as exc:  # noqa: BLE001 - anyio wraps connection failures in exception groups
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            st.tools = []
            st.error = _why(exc)

    async def refresh(self, only: Optional[str] = None, failed_only: bool = False) -> list[dict[str, Any]]:
        """Ask the enabled components (or one) what tools they have, then
        rebuild what is offered. Never raises: a component that does not
        answer is in the snapshot with its reason."""
        async with self._lock:
            todo = [st for st in self.states.values()
                    if st.enabled and (only is None or st.spec.key == only)
                    and not (failed_only and st.listed_at and not st.error)]
            if todo:
                await asyncio.gather(*(self._list_one(st) for st in todo))
            self._rebuild()
        return self.snapshot()

    def _rebuild(self) -> None:
        owner: dict[str, tuple[str, str]] = {}
        exposed: dict[str, Any] = {}
        infos = []
        gated: set[str] = set()                                # first-seen tools of a start_disabled plugin
        can_gate = self._registry is not None and hasattr(self._registry, "has_state")
        for st in self.states.values():                        # the standard components first, then the plugins
            spec = st.spec
            if not st.enabled:
                continue
            for tool in st.tools:
                theirs = name = tool.name                      # what the component calls it
                if name in owner:                              # an earlier component has this name
                    name = f"{spec.key}_{theirs}"
                    log.warning("[seren-workbench] %s and %s both have a tool named '%s'; the second is offered as '%s'",
                                self.states[owner[theirs][0]].spec.display, spec.display, theirs, name)
                    tool = tool.model_copy(update={"name": name})
                owner[name] = (spec.key, theirs)
                exposed[name] = tool
                if st.start_disabled and can_gate and not self._registry.has_state(name):
                    gated.add(name)
                infos.append(_tool_info(name, tool, spec, st))
        self._owner, self._exposed = owner, exposed
        if gated:
            # Before they are handed over, so they ARRIVE off rather than
            # being switched off a moment after they could have been called.
            self._registry.seed_disabled(gated)
        if self._registry is not None and hasattr(self._registry, "replace_upstream"):
            self._registry.replace_upstream(infos)

    def set_enabled(self, key: str, enabled: bool) -> bool:
        st = self.states.get(key)
        if st is None:
            return False
        st.enabled = bool(enabled)
        if not st.enabled:
            st.tools = []
        self._rebuild()
        return True

    async def run(self) -> None:
        """The hub's whole life, as one background task: ask every component
        now, then keep asking the quiet ones. In the background on purpose -
        a component that is down takes seconds to say so, and the Workbench
        should be listening while it finds out."""
        try:
            for row in await self.refresh():
                if row["enabled"]:
                    log.info("[seren-workbench] %s: %s", row["display"],
                             f"{row['tool_count']} tool(s) passed through" if row["available"]
                             else f"not available ({row['error']})")
        except Exception as exc:  # noqa: BLE001 - the Workbench runs without its components
            log.info("[seren-workbench] could not ask the components: %r", exc)
        await self.retry_loop()

    async def retry_loop(self) -> None:
        """Ask again, every RETRY_SECONDS, any enabled component that has not
        answered yet: a service that starts after the Workbench shows up
        without anyone restarting anything."""
        while True:
            await asyncio.sleep(RETRY_SECONDS)
            try:
                if any(st.enabled and st.url and (st.error or not st.listed_at) for st in self.states.values()):
                    await self.refresh(failed_only=True)
            except Exception as exc:  # noqa: BLE001 - the loop outlives one bad pass
                log.info("[seren-workbench] component retry failed: %r", exc)

    # ── forwarding a call ─────────────────────────────────────────────────
    async def call(self, name: str, arguments: Optional[dict[str, Any]]) -> Any:
        """Forward one call to the component that owns the tool. Returns the
        component's content blocks; raises with its own words when the tool
        reported an error, so the caller sees an MCP error and not a success
        whose text happens to be bad news."""
        key, upstream_name = self._owner[name]
        st = self.states[key]
        t0 = time.time()
        if not st.enabled:
            raise RuntimeError(f"the {st.spec.display} component is switched off in this Workbench")
        if self._registry is not None and not self._registry.is_enabled(name):
            self._record(name, st, t0, arguments, False, "tool disabled by operator")
            raise RuntimeError(f"tool '{name}' is currently disabled by the operator "
                               "(see the Workbench dashboard's Tool State tab).")
        try:
            async with self._connect(st.mcp_url, st.token, CALL_TIMEOUT) as session:
                res = await session.call_tool(upstream_name, arguments or {})
        except BaseException as exc:  # noqa: BLE001 - see _list_one
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            why = _why(exc)
            self._record(name, st, t0, arguments, False, why)
            raise RuntimeError(f"{st.spec.display} could not be reached for '{name}': {why}") from None
        content = list(getattr(res, "content", None) or [])
        if getattr(res, "isError", False):
            text = " ".join(getattr(c, "text", "") for c in content if getattr(c, "text", "")) or f"'{name}' failed"
            self._record(name, st, t0, arguments, False, text)
            raise RuntimeError(text)
        self._record(name, st, t0, arguments, True)
        return content

    def _record(self, name: str, st: ComponentState, t0: float, arguments: Optional[dict[str, Any]],
                success: bool, error: str = "") -> None:
        if self._audit is None:
            return
        try:
            from .dynamic_tools.tool_audit_log import AuditEntry
            self._audit.record(AuditEntry(
                timestamp=t0, tool=name, kind="upstream", source_file=f"{st.spec.key} ({st.url})",
                duration_ms=int((time.time() - t0) * 1000), success=success,
                error_message=error[:500] or None, arg_count=len(arguments or {})))
        except Exception:  # noqa: BLE001 - an audit line never fails a call
            pass


def _why(exc: BaseException) -> str:
    """The innermost reason. anyio hands connection failures back wrapped in
    exception groups; 'unhandled errors in a TaskGroup' tells nobody anything."""
    seen = 0
    while getattr(exc, "exceptions", None) and seen < 6:
        exc = exc.exceptions[0]
        seen += 1
    if isinstance(exc, asyncio.TimeoutError):
        return "it did not answer in time"
    text = str(exc).strip()
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status == 401:
        return "401 unauthorized - the token this Workbench presents is not the one that service expects"
    if status == 404:
        return "404 - no MCP endpoint there (was that service installed with its MCP extra?)"
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _tool_info(name: str, tool: Any, spec: ComponentSpec, st: ComponentState) -> Any:
    from .tool_registry import ToolInfo
    schema = getattr(tool, "inputSchema", None) or {}
    required = set(schema.get("required") or [])
    params = []
    for pname, p in (schema.get("properties") or {}).items():
        p = p if isinstance(p, dict) else {}
        ptype = p.get("type") or next((a.get("type") for a in p.get("anyOf") or []
                                       if isinstance(a, dict) and a.get("type") != "null"), "string")
        params.append({"name": pname, "type": ptype, "required": pname in required,
                       "description": p.get("description") or p.get("title") or ""})
    return ToolInfo(name=name, description=(getattr(tool, "description", "") or "").strip(),
                    type="upstream", source=f"{spec.display} ({st.url})", parameters=params,
                    display_name=name.replace("_", " "), toolbox=spec.display)
