"""
seren_workbench.mcp.server
════════════════════════════════════════════════════════════════════════

Wires the FastMCP server INTO the existing FastAPI app at /mcp.

Same process, same port. The MCP tools read from the ToolRegistry — the
operator dashboard's enable/disable toggles are enforced AT CALL TIME
(registration happens once at startup) — and call the builtin tool
implementations (httpx-based HTTP calls to the Seren services, injected
by parameter name from app.state.di_registry) or the dynamic tool
dispatchers via YamlDispatchedTool.

DESIGN: This is a near-exact sibling of seren_memory.mcp.server and
seren_loci.mcp.server — the same three transport footguns bite any
FastMCP-into-FastAPI mount, so the same three fixes apply.

SCHEMA GENERATION (the proven footgun): FastMCP builds each tool's JSON
schema from the registered function's SIGNATURE. A bare ``**kwargs``
wrapper produces a schema with one property literally named "kwargs" —
the LLM never sees the real parameters. So every wrapper gets a REAL
``__signature__`` (the impl's signature minus DI params) + matching
``__annotations__``. Proven: schema comes out with the true params and
required list, and DI params stay hidden.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import time
import weakref
from typing import Any, Dict, Optional

import httpx

from fastapi import FastAPI

from ..dynamic_tools.tool_audit_log import ERROR_MESSAGE_MAX_CHARS, ToolAuditLog
from ..holds import Holds
from ..tool_config.mcp_config import McpConfig
from ..proposals import ProposalStore

logger = logging.getLogger(__name__)

# Annotation types that are dependency-injected, never exposed in schemas.
# ProposalStore is here for the same reason the httpx clients are: propose_tool
# needs it, and a DI param that ISN'T listed here leaks into the LLM-visible
# schema as a phantom argument the model then tries to supply. ToolAuditLog
# (list_my_tool_calls) and Holds (ensure_service_running) likewise.
_DI_TYPES = (httpx.AsyncClient, McpConfig, ProposalStore, ToolAuditLog, Holds)


class ToolReportedError(RuntimeError):
    """A builtin tool answered with its error payload. Raised so the MCP
    result is marked isError - see _error_payload."""


def _error_payload(result: Any) -> str:
    """The message, when *result* is a builtin's way of saying it failed.

    Every builtin reports failure by RETURNING {"error": ..., "hint": ...}
    as its text. Over MCP that is a successful call whose content happens to
    be bad news: the client shows it as a result, the audit log counts it as
    a success, and a model reading quickly takes "Lodestar unreachable" for
    an answer. The passed-through tools and the manifest tools already raise
    (the component's own isError is carried across); this makes the builtins
    say it the same way, in one place, without each of them changing how it
    is called directly.

    Deliberately narrow: only a payload that is NOTHING BUT an error and an
    optional hint. A tool whose real answer has an "error" field among others
    is answering, not failing."""
    data = result
    if isinstance(result, str):
        text = result.lstrip()
        if not text.startswith("{") or len(text) > 20_000:
            return ""
        try:
            data = json.loads(text)
        except ValueError:
            return ""
    if not isinstance(data, dict) or not data.get("error") or not set(data) <= {"error", "hint"}:
        return ""
    hint = str(data.get("hint") or "").strip()
    return str(data["error"]).strip() + (f"\n{hint}" if hint else "")


class ToolListWatch:
    """Tells connected clients when the tool list has changed
    (notifications/tools/list_changed).

    The Workbench's list moves under a client all the time: a component that
    was down at startup comes up a minute later with twenty tools, a switch
    is flipped on the dashboard, a proposal is approved, a plugin is plugged
    in. A client that listed tools once at connect and was never told keeps
    calling a surface that is no longer there - in particular a model woken
    at the moment the Workbench started sees the builtins and none of its
    memory.

    Anything that might have changed the list calls poke(); a moment later
    (pokes arriving together are one check) the list is fingerprinted - names,
    descriptions, schemas - and ONLY IF IT DIFFERS from the last one is each
    session told. So poke() is safe to call from anywhere and costs nothing
    when nothing changed.

    Sessions are learned as they list or call tools, and held weakly: one
    that has gone is dropped the first time telling it fails."""

    DEBOUNCE_SECONDS = 0.25

    def __init__(self, mcp: Any) -> None:
        self._mcp = mcp
        self._sessions: "weakref.WeakSet[Any]" = weakref.WeakSet()
        self._last: Optional[str] = None
        self._dirty = False
        self._task: Optional[asyncio.Task] = None
        self.changes = 0                    # how many times the list was seen to differ
        self.notices = 0                    # notifications sent, over all sessions
        self.last_change_at: Optional[float] = None

    def see(self, session: Any) -> None:
        try:
            self._sessions.add(session)
        except TypeError:                   # not weak-referenceable: nothing to remember it by
            pass

    @property
    def sessions(self) -> int:
        return len(self._sessions)

    async def _fingerprint(self) -> str:
        h = hashlib.sha256()
        for t in sorted(await self._mcp.list_tools(), key=lambda t: t.name):
            h.update(json.dumps([t.name, t.description, getattr(t, "inputSchema", None)],
                                sort_keys=True, default=str).encode("utf-8"))
        return h.hexdigest()

    async def prime(self) -> None:
        """Take the list as it is now as the starting point (at startup,
        before any client has connected)."""
        try:
            self._last = await self._fingerprint()
        except Exception as exc:  # noqa: BLE001
            logger.info("[seren-workbench] could not fingerprint the tool list: %r", exc)

    async def check(self) -> bool:
        """Compare the list with the last one seen; tell every session if it
        differs. True when it did."""
        fp = await self._fingerprint()
        if fp == self._last:
            return False
        self._last = fp
        self.changes += 1
        self.last_change_at = time.time()
        for session in list(self._sessions):
            try:
                await session.send_tool_list_changed()
                self.notices += 1
            except Exception:  # noqa: BLE001 - a session that has gone
                self._sessions.discard(session)
        return True

    def poke(self) -> None:
        """Something may have changed the list. Never raises, never blocks;
        outside a running loop (a bare registry in a test) it does nothing."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._dirty = True
        if self._task is None or self._task.done():
            self._task = loop.create_task(self._soon())

    async def _soon(self) -> None:
        while self._dirty:
            self._dirty = False
            await asyncio.sleep(self.DEBOUNCE_SECONDS)
            try:
                await self.check()
            except Exception as exc:  # noqa: BLE001 - a notice never takes anything down
                logger.info("[seren-workbench] tool-list check failed: %r", exc)

    def snapshot(self) -> dict:
        return {"sessions": self.sessions, "changes": self.changes, "notices": self.notices,
                "last_change_at": self.last_change_at}


def _is_di_annotation(ann) -> bool:
    """True when *ann* names a DI type — directly or wrapped in Optional[...].

    The tool modules use `from __future__ import annotations`, so a plain
    inspect.signature() hands back STRINGS ("httpx.AsyncClient") that never
    match a class identity check — the callers below use eval_str=True to
    resolve them first. Optional[McpConfig] arrives as Union[McpConfig, None]
    and must be unwrapped.
    """
    import typing
    if ann in _DI_TYPES:
        return True
    if typing.get_origin(ann) is typing.Union:
        return any(a in _DI_TYPES for a in typing.get_args(ann))
    return False


def mount_mcp_routes(app: FastAPI):
    """Mount the SerenWorkbench MCP server onto an existing FastAPI app.

    Reads app.state.tool_registry and app.state.di_registry (set by the
    lifespan handler) to wire tools to the MCP surface. Returns the FastMCP
    instance; the caller MUST enter `mcp.session_manager.run()` for the
    app's lifetime.
    """
    from mcp.server.fastmcp import FastMCP

    mount_path = os.environ.get("SEREN_WORKBENCH_MOUNT", "/mcp").rstrip("/")
    if not mount_path.startswith("/"):
        mount_path = "/" + mount_path

    registry = getattr(app.state, "tool_registry", None)
    if registry is None:
        raise RuntimeError(
            "mount_mcp_routes called before app.state.tool_registry was set. "
            "Mount inside the lifespan handler."
        )

    # DI values by parameter name: {"memory": AsyncClient, "runtime_host": ...,
    # "searxng": ..., "scheduler": ..., "config": McpConfig}. Built by the
    # lifespan from ServicesConfig. Absent (tests, bare create_app) = empty:
    # impls fall back to their signature defaults.
    di_registry: Dict[str, Any] = getattr(app.state, "di_registry", {}) or {}
    audit_log = getattr(app.state, "audit_log", None)

    # The standard components' tools are not registered here at all: they
    # are listed and forwarded live (upstream.py), so what a client sees is
    # what each service offers at that moment. FastMCP wires its protocol
    # handlers to self.list_tools / self.call_tool, so overriding those two
    # is the whole integration - and a builtin whose name a component also
    # has is left out of the list, because the call would go to the component.
    #
    # A SWITCHED-OFF TOOL IS NOT LISTED, whoever it belongs to. That was the
    # rule for passed-through tools only; a builtin or a manifest tool that
    # was off still listed and then refused every call, which spends a
    # model's attention to teach it what the operator already decided. The
    # call path still refuses (the gate is there, not here), and now that
    # clients are told when the list changes, a tool appears the moment its
    # switch is flipped.
    class _WorkbenchMCP(FastMCP):
        tool_watch: Any = None

        def _see_session(self) -> None:
            if self.tool_watch is None:
                return
            try:
                self.tool_watch.see(self._mcp_server.request_context.session)
            except Exception:  # noqa: BLE001 - LookupError outside a request; or an SDK without it
                pass

        async def list_tools(self):  # noqa: ANN202
            self._see_session()
            tools = [t for t in await super().list_tools() if registry.is_enabled(t.name)]
            hub = getattr(app.state, "upstreams", None)
            if hub is None:
                return tools
            theirs = hub.list_tools()
            names = hub.names()
            return [t for t in tools if t.name not in names] + theirs

        async def call_tool(self, name, arguments):  # noqa: ANN001, ANN202
            self._see_session()
            hub = getattr(app.state, "upstreams", None)
            if hub is not None and hub.owns(name):
                return await hub.call(name, arguments)
            return await super().call_tool(name, arguments)

    mcp = _WorkbenchMCP("seren-workbench")

    # Tell clients when the list changes (ToolListWatch). Two halves: say at
    # initialize that we will (capabilities.tools.listChanged - the SDK
    # defaults it to false and a client is entitled to ignore notices it was
    # not promised), and have everything that changes the list poke the watch.
    # The registry is the one place every such change passes through.
    watch = ToolListWatch(mcp)
    mcp.tool_watch = watch
    app.state.tool_watch = watch
    registry.on_change = watch.poke
    _advertise_list_changed(mcp)

    _register_builtin_tools(mcp, registry, di_registry, audit_log)
    _register_dynamic_tools(mcp, registry, di_registry, audit_log)

    # -- Bug 1: the double-/mcp footgun --
    if hasattr(mcp.settings, "streamable_http_path"):
        mcp.settings.streamable_http_path = "/"

    # -- Bug 3: DNS-rebinding host check --
    if hasattr(mcp.settings, "transport_security"):
        _apply_transport_security(mcp)

    asgi_app = _resolve_transport_app(mcp)
    app.mount(mount_path, asgi_app)
    logger.info("[seren-workbench] MCP server mounted at %s (%d tools)",
                mount_path, _count_tools(mcp))

    return mcp


# ── Builtin tools ───────────────────────────────────────────────────────

def _register_builtin_tools(mcp, registry, di_registry, audit_log=None) -> None:
    """Register every builtin tool from the registry onto the FastMCP instance.

    Each builtin tool has a corresponding async implementation function in
    models/tools/*.py. We import the modules, look up functions by matching
    the tool name (functions DEFINED in the module only — dir() also lists
    imports, and an imported same-name callable must not shadow the impl),
    and wrap each with a schema-clean, DI-injecting, call-time-gated wrapper.
    """
    import importlib
    import pkgutil

    impl_map: Dict[str, Any] = {}
    pkg = importlib.import_module("..models.tools", package=__package__)
    for _, mod_name, _ in pkgutil.iter_modules(pkg.__path__):
        mod = importlib.import_module(f"..models.tools.{mod_name}", package=__package__)
        for attr_name in dir(mod):
            if attr_name.startswith("_"):
                continue
            val = getattr(mod, attr_name)
            # Only coroutine functions defined IN this module.
            if not inspect.iscoroutinefunction(val):
                continue
            if getattr(val, "__module__", None) != mod.__name__:
                continue
            impl_map[attr_name] = val

    for tool in registry.all_tools():
        if tool.type != "builtin":
            continue
        func_name = tool.name.replace("-", "_").replace(" ", "_")
        fn = impl_map.get(func_name)
        if fn is not None:
            _register_wrapped(mcp, fn, tool, registry, di_registry, audit_log)
        else:
            _register_stub(mcp, tool)


def _register_wrapped(mcp, fn, tool, registry, di_registry, audit_log=None) -> None:
    """Register *fn* as an MCP tool with a REAL signature minus DI params.

    - DI params (annotation in _DI_TYPES) are stripped from the schema and
      resolved at call time from di_registry by parameter name, falling back
      to the impl's own default.
    - VAR_KEYWORD (**kwargs) is dropped from the exposed signature.
    - The wrapper checks registry.is_enabled() on EVERY call — this is the
      operator gate. Registration is startup-fixed; the toggle must bite in
      the call path or the dashboard is decorative.
    - Every call is recorded in the audit log (content-blind).
    """
    # eval_str resolves the from-__future__ string annotations back into
    # real classes so DI detection and pydantic schema generation both work.
    sig = inspect.signature(fn, eval_str=True)

    clean_params = []
    di_param_names = []
    for pname, p in sig.parameters.items():
        if p.kind in (inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL):
            continue
        ptype = p.annotation if p.annotation is not inspect.Parameter.empty else None
        if ptype is not None and _is_di_annotation(ptype):
            di_param_names.append(pname)
            continue
        clean_params.append(p)

    def _resolve_di() -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for pname in di_param_names:
            if pname in di_registry:
                out[pname] = di_registry[pname]
            else:
                default = sig.parameters[pname].default
                out[pname] = None if default is inspect.Parameter.empty else default
        return out

    def _record_call(start: float, arg_count: int, success: bool,
                     error_msg: str = "") -> None:
        if audit_log is None:
            return
        from ..dynamic_tools.tool_audit_log import AuditEntry
        audit_log.record(AuditEntry(
            timestamp=start,
            tool=tool.name,
            kind="builtin",
            source_file=tool.source,
            duration_ms=int((time.time() - start) * 1000),
            success=success,
            error_message=error_msg or None,
            arg_count=arg_count,
        ))

    clean_names = {p.name for p in clean_params}

    async def _wrapper(**kwargs):
        _t0 = time.time()
        if not registry.is_enabled(tool.name):
            _record_call(_t0, len(kwargs), False, "tool disabled by operator")
            raise RuntimeError(
                f"tool '{tool.name}' is currently disabled by the operator "
                "(see the Workbench dashboard's Tool State tab)."
            )
        args = {**_resolve_di(), **{k: v for k, v in kwargs.items() if k in clean_names}}
        try:
            result = await fn(**args)
        except Exception as exc:
            _record_call(_t0, len(kwargs), False, _short(str(exc) or type(exc).__name__))
            raise
        # A builtin that RETURNED its failure: an error to the client and a
        # failure in the record, like every other kind of tool here.
        failure = _error_payload(result)
        if failure:
            _record_call(_t0, len(kwargs), False, _short(failure))
            raise ToolReportedError(failure)
        _record_call(_t0, len(kwargs), True)
        return result

    safe_name = tool.name.replace("-", "_").replace(" ", "_")
    _wrapper.__name__ = f"_mcp_{safe_name}"
    _wrapper.__qualname__ = _wrapper.__name__
    _wrapper.__module__ = __name__
    # THE SCHEMA FIX: hand FastMCP the clean signature + annotations so it
    # generates the real parameter schema instead of a lone 'kwargs' prop.
    _wrapper.__signature__ = sig.replace(parameters=clean_params)
    _wrapper.__annotations__ = {
        p.name: p.annotation for p in clean_params
        if p.annotation is not inspect.Parameter.empty
    }
    mcp.tool(name=tool.name, description=tool.description)(_wrapper)


def _register_stub(mcp, tool) -> None:
    """Register a stub for a defined-but-unimplemented tool.

    Each stub gets a UNIQUE function name (the old code registered every
    stub as a function literally named `_stub`, so FastMCP collapsed them
    all into ONE tool called `_stub` — proven with a 'Tool already exists'
    warning) and an EMPTY signature so no bookkeeping params leak into the
    LLM-visible schema.
    """
    async def _stub_impl(name=tool.name, desc=tool.description):
        raise ToolReportedError(
            f"tool '{name}' has no registered implementation\n"
            f"This tool is defined but not yet wired. {desc}")

    safe_name = tool.name.replace("-", "_").replace(" ", "_")
    _stub_impl.__name__ = f"_mcp_stub_{safe_name}"
    _stub_impl.__qualname__ = _stub_impl.__name__
    _stub_impl.__module__ = __name__
    _stub_impl.__signature__ = inspect.Signature(parameters=[])
    _stub_impl.__annotations__ = {}
    mcp.tool(name=tool.name, description=tool.description)(_stub_impl)


# ── Dynamic (YAML manifest) tools ───────────────────────────────────────

_JSON_TYPE_MAP = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
}


def make_dynamic_registrar(registry, di_registry, audit_log=None):
    """Return ``callable(mcp, tool_info)`` that registers ONE dynamic tool.

    Both startup and live reload go through this, so a tool added at 3am by
    a reload is built by the exact code that built the ones present at boot.
    The alternative — a second registration path for the reload case — is
    the duplicate-source-of-truth bug waiting to be written.
    """
    from ..dynamic_tools.yaml_dispatched_tool import YamlDispatchedTool
    from ..dynamic_tools.tool_audit_log import ToolAuditLog

    # A general-purpose async client for kind=web dynamic tools (their
    # base_url comes from the manifest, so no client base_url here).
    web_client = di_registry.get("_dynamic_web_client")
    if web_client is None:
        web_client = httpx.AsyncClient()
    log = audit_log if audit_log is not None else ToolAuditLog()
    # (host, port) this Workbench listens on, so a web tool aimed back at it
    # is refused at dispatch. Absent in bare test registries = no guard.
    self_addr = di_registry.get("_self_addr")

    def _register_one(mcp, tool) -> None:
        if tool.type != "dynamic" or tool.entry is None:
            return
        dispatched = YamlDispatchedTool(
            entry=tool.entry,
            owner=tool.owner,
            source_path=tool.source,
            http_client=web_client,
            audit_log=log,
            self_addr=self_addr,
        )
        _register_dispatched(mcp, dispatched, tool, registry)

    return _register_one


def _register_dynamic_tools(mcp, registry, di_registry, audit_log=None) -> None:
    """Register every dynamic tool via YamlDispatchedTool.

    The registry carries each dynamic tool's ToolEntry + owning manifest.
    We build the dispatcher once per tool, then register a wrapper whose
    signature mirrors the manifest's parameter list (so FastMCP generates
    the right schema) and whose body routes through YamlDispatchedTool.call
    — validation, coercion, constraint checks, audit, process/web dispatch.
    """
    register_one = make_dynamic_registrar(registry, di_registry, audit_log)
    for tool in registry.all_tools():
        register_one(mcp, tool)


def _register_dispatched(mcp, dispatched, tool, registry) -> None:
    """Register one YamlDispatchedTool with a manifest-shaped signature."""
    params = []
    annotations: Dict[str, Any] = {}
    for p in (tool.entry.parameters or []):
        if not p.name:
            continue
        ptype = _JSON_TYPE_MAP.get((p.type or "string").strip().lower(), str)
        if p.required:
            default = inspect.Parameter.empty
        else:
            default = p.default  # may be None — fine, optional
        params.append(inspect.Parameter(
            p.name, inspect.Parameter.KEYWORD_ONLY,
            default=default, annotation=ptype,
        ))
        annotations[p.name] = ptype

    async def _dyn_wrapper(**kwargs):
        if not registry.is_enabled(tool.name):
            raise RuntimeError(
                f"tool '{tool.name}' is currently disabled by the operator "
                "(see the Workbench dashboard's Tool State tab)."
            )
        result = await dispatched.call(kwargs)
        content = result.get("content") or []
        text = ""
        if content and isinstance(content[0], dict):
            text = content[0].get("text", "")
        if result.get("is_error"):
            # Raising lets FastMCP mark the CallToolResult isError properly.
            raise RuntimeError(text or f"tool '{tool.name}' failed")
        return text or "(no output)"

    safe_name = tool.name.replace("-", "_").replace(" ", "_")
    _dyn_wrapper.__name__ = f"_mcp_dyn_{safe_name}"
    _dyn_wrapper.__qualname__ = _dyn_wrapper.__name__
    _dyn_wrapper.__module__ = __name__
    _dyn_wrapper.__signature__ = inspect.Signature(parameters=params)
    _dyn_wrapper.__annotations__ = annotations
    mcp.tool(name=tool.name, description=tool.description)(_dyn_wrapper)


def _short(text: str) -> str:
    """An error for the audit log, at the log's own length."""
    text = " ".join(str(text).split())
    return text if len(text) <= ERROR_MESSAGE_MAX_CHARS else text[:ERROR_MESSAGE_MAX_CHARS] + "…"


def _advertise_list_changed(mcp) -> None:
    """Make `initialize` say capabilities.tools.listChanged: true.

    The low-level server builds its capabilities from NotificationOptions,
    which the session manager never passes - so it is always the default,
    all false. Wrapped on the instance: every new session then starts from
    options that say what this server actually does. An SDK that has moved
    this is left alone, with a line in the log - the notices are still sent,
    and a client is free to act on them or not."""
    low = getattr(mcp, "_mcp_server", None)
    original = getattr(low, "create_initialization_options", None)
    if original is None:
        logger.info("[seren-workbench] this mcp SDK has no create_initialization_options to wrap; "
                    "tools.listChanged is not advertised")
        return
    try:
        from mcp.server.lowlevel.server import NotificationOptions
    except Exception as exc:  # noqa: BLE001
        logger.info("[seren-workbench] NotificationOptions unavailable (%s); tools.listChanged is not advertised", exc)
        return

    def _options(notification_options=None, experimental_capabilities=None):
        return original(notification_options or NotificationOptions(tools_changed=True),
                        experimental_capabilities)

    low.create_initialization_options = _options


# ── Transport plumbing (the three family footguns) ──────────────────────

def _apply_transport_security(mcp) -> None:
    """Configure FastMCP's DNS-rebinding host check from env, defaulting OFF."""
    try:
        from mcp.server.transport_security import TransportSecuritySettings
    except Exception as exc:
        logger.info("[seren-workbench] transport_security module unavailable (%s); "
                    "leaving SDK default in place", exc)
        return

    def _split(name: str) -> list[str]:
        return [v.strip() for v in os.environ.get(name, "").split(",") if v.strip()]

    allowed_hosts = _split("SEREN_WORKBENCH_ALLOWED_HOSTS")
    allowed_origins = _split("SEREN_WORKBENCH_ALLOWED_ORIGINS")

    if allowed_hosts or allowed_origins:
        if not allowed_origins:
            allowed_origins = [f"http://{h}" for h in allowed_hosts] + \
                              [f"https://{h}" for h in allowed_hosts]
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts,
            allowed_origins=allowed_origins,
        )
        logger.info("[seren-workbench] MCP host check ON; allowed_hosts=%s", allowed_hosts)
    else:
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=False)
        logger.info("[seren-workbench] MCP host check OFF (trusted-LAN); set "
                    "SEREN_WORKBENCH_ALLOWED_HOSTS to enable an allowlist")


def _resolve_transport_app(mcp) -> object:
    """Return an ASGI app for the MCP HTTP transport, tolerating SDK drift."""
    for attr in ("streamable_http_app", "sse_app"):
        factory = getattr(mcp, attr, None)
        if callable(factory):
            logger.info("[seren-workbench] MCP transport: %s", attr)
            return factory()
    try:
        import mcp as _mcp_pkg
        version = getattr(_mcp_pkg, "__version__", "unknown")
    except Exception:
        version = "unknown"
    raise RuntimeError(
        f"mcp SDK version {version} exposes neither streamable_http_app nor "
        "sse_app on FastMCP - cannot mount HTTP transport."
    )


def _count_tools(mcp) -> int:
    """Best-effort tool count for the startup log line."""
    for attr in ("_tools", "tools", "_tool_manager"):
        obj = getattr(mcp, attr, None)
        if obj is None:
            continue
        if hasattr(obj, "list_tools"):
            try:
                return len(list(obj.list_tools()))
            except Exception:
                continue
        if isinstance(obj, dict):
            return len(obj)
    return 0
