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

import inspect
import logging
import os
import time
from typing import Any, Dict, Optional

import httpx

from fastapi import FastAPI

from ..tool_config.mcp_config import McpConfig

logger = logging.getLogger(__name__)

# Annotation types that are dependency-injected, never exposed in schemas.
_DI_TYPES = (httpx.AsyncClient, McpConfig)


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
    """Mount the SerenMcp MCP server onto an existing FastAPI app.

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

    mcp = FastMCP("seren-workbench")

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
            _record_call(_t0, len(kwargs), True)
            return result
        except Exception as exc:
            _record_call(_t0, len(kwargs), False, str(exc))
            raise

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
        return {
            "error": f"tool '{name}' has no registered implementation",
            "hint": f"This tool is defined but not yet wired. {desc}",
        }

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


def _register_dynamic_tools(mcp, registry, di_registry, audit_log=None) -> None:
    """Register every dynamic tool via YamlDispatchedTool.

    The registry carries each dynamic tool's ToolEntry + owning manifest.
    We build the dispatcher once per tool, then register a wrapper whose
    signature mirrors the manifest's parameter list (so FastMCP generates
    the right schema) and whose body routes through YamlDispatchedTool.call
    — validation, coercion, range checks, audit, process/web dispatch.
    """
    from ..dynamic_tools.yaml_dispatched_tool import YamlDispatchedTool
    from ..dynamic_tools.tool_audit_log import ToolAuditLog

    # A general-purpose async client for kind=web dynamic tools (their
    # base_url comes from the manifest, so no client base_url here).
    web_client = di_registry.get("_dynamic_web_client")
    if web_client is None:
        web_client = httpx.AsyncClient()

    for tool in registry.all_tools():
        if tool.type != "dynamic" or tool.entry is None:
            continue

        dispatched = YamlDispatchedTool(
            entry=tool.entry,
            owner=tool.owner,
            source_path=tool.source,
            http_client=web_client,
            audit_log=audit_log if audit_log is not None else ToolAuditLog(),
        )
        _register_dispatched(mcp, dispatched, tool, registry)


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
