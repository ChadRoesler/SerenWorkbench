"""
seren_workbench.mcp.server
════════════════════════════════════════════════════════════════════════

Wires the FastMCP server INTO the existing FastAPI app at /mcp.

Same process, same port. The MCP tools read from the ToolRegistry — which
respects the operator dashboard's enable/disable toggles — and call the
builtin tool implementations (httpx-based HTTP calls to the Seren services)
or the dynamic tool dispatchers.

DESIGN: This is a near-exact sibling of seren_memory.mcp.server and
seren_loci.mcp.server — the same three transport footguns bite any
FastMCP-into-FastAPI mount, so the same three fixes apply.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import httpx

from fastapi import FastAPI

logger = logging.getLogger(__name__)


def mount_mcp_routes(app: FastAPI):
    """Mount the SerenMcp MCP server onto an existing FastAPI app.

    Reads app.state.tool_registry (set by the lifespan handler) to wire
    tools to the MCP surface. Returns the FastMCP instance; the caller MUST
    enter `mcp.session_manager.run()` for the app's lifetime.
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

    # Grab the audit log from app state for tool-call recording
    audit_log = getattr(app.state, "audit_log", None)

    mcp = FastMCP("seren-workbench")

    # Register tools from the registry — only enabled tools are exposed.
    # Each builtin tool's implementation function is looked up by name and
    # wrapped as an MCP tool. Dynamic tools are registered separately via
    # the dynamic tool dispatchers.
    _register_enabled_tools(mcp, registry, audit_log)

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


def _register_enabled_tools(mcp, registry, audit_log=None) -> None:
    """Register every enabled tool from the registry onto the FastMCP instance.

    Each builtin tool has a corresponding async implementation function in
    models/tools/*.py. We import the module, look up the function by matching
    the tool name to the function name, and wrap it.

    WRAPPING: Many tool functions have dependency-injection parameters
    (e.g. httpx.AsyncClient) that FastMCP cannot generate JSON schemas for.
    We create a wrapper that accepts only JSON-serializable parameters and
    injects the DI defaults when calling the real implementation.

    Dynamic tools (YAML-defined) are dispatched via the dynamic tool dispatchers
    (web_dispatcher, process_dispatcher) and registered here when active.

    *audit_log*: optional ToolAuditLog instance — if set, every tool call
    is recorded as an AuditEntry.
    """
    import importlib
    import inspect
    import pkgutil

    # Build a name -> (module, func) lookup from models/tools/
    impl_map = {}
    pkg = importlib.import_module(".models.tools", package=__package__)
    for _, mod_name, _ in pkgutil.iter_modules(pkg.__path__):
        mod = importlib.import_module(f".models.tools.{mod_name}", package=__package__)
        for attr_name in dir(mod):
            val = getattr(mod, attr_name)
            if not callable(val) and not (hasattr(val, "__wrapped__") or inspect.iscoroutinefunction(val)):
                continue
            # Skip _err helper and non-tool functions
            if attr_name.startswith("_") or attr_name in ("escapeHtml",):
                continue
            impl_map[attr_name] = (mod, val)

    for tool in registry.all_tools():
        if not tool.enabled:
            continue
        # Try to find the implementation function by name
        func_name = tool.name.replace("-", "_").replace(" ", "_")
        if func_name in impl_map:
            fn = impl_map[func_name][1]
            _register_wrapped(mcp, fn, tool, audit_log)
        else:
            # For tools without a direct implementation (e.g. dynamic tools
            # that haven't been wired yet), register a stub that returns
            # an error message.
            @mcp.tool()
            async def _stub(name=tool.name, desc=tool.description, **kwargs):
                return {
                    "error": f"tool '{name}' has no registered implementation",
                    "hint": f"This tool is defined but not yet wired. {desc}",
                }


def _register_wrapped(mcp, fn, tool, audit_log=None) -> None:
    """Register *fn* as an MCP tool, stripping non-JSON-serializable params.

    FastMCP's ``@mcp.tool()`` generates a JSON schema from the function's
    signature. Parameters typed as ``httpx.AsyncClient`` (or any class not
    trivially serializable) cause a ``PydanticInvalidForJsonSchema`` error
    even when they have a default of ``None``.

    We create a wrapper that exposes ONLY the clean parameters, then calls
    ``fn`` with all original params — the DI defaults are left in place.

    Each wrapper gets a UNIQUE function name so FastMCP doesn't overwrite
    previously registered tools.

    *audit_log*: optional ToolAuditLog — if set, every call is recorded.
    """
    import inspect
    import types
    import time

    sig = inspect.signature(fn)
    # Types that FastMCP cannot schema-generate — anything that isn't a
    # plain JSON type (str, int, float, bool, list, dict, None).
    _NON_SCHEMA_TYPES = (httpx.AsyncClient,)

    clean_params = []
    di_params = {}  # name -> default value, to be injected when calling fn
    for pname, p in sig.parameters.items():
        if pname == "kwargs" or pname.startswith("_"):
            continue
        # Check the type annotation (or the default's type) for non-schema types
        ptype = p.annotation if p.annotation is not inspect.Parameter.empty else None
        if ptype is not None and ptype in _NON_SCHEMA_TYPES:
            # This is a DI param — keep it out of the MCP schema
            di_params[pname] = p.default if p.default is not inspect.Parameter.empty else None
            continue
        clean_params.append(p)

    # Generate a unique wrapper name from the tool name
    safe_name = tool.name.replace("-", "_").replace(" ", "_")
    wrapper_name = f"_mcp_{safe_name}"

    # ── Helper to record audit entry ────────────────────────────────────
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

    if not clean_params:
        # No clean params at all — register a no-arg wrapper
        async def _di_wrapper():
            _t0 = time.time()
            try:
                result = await fn(**di_params)
                _record_call(_t0, 0, True)
                return result
            except Exception as exc:
                _record_call(_t0, 0, False, str(exc))
                raise
        _di_wrapper.__name__ = wrapper_name
        _di_wrapper.__qualname__ = wrapper_name
        _di_wrapper.__module__ = __name__
        mcp.tool(name=tool.name, description=tool.description)(_di_wrapper)
    else:
        # Build the wrapper dynamically with the clean parameter names.
        # We use **kwargs so FastMCP can generate the schema from the
        # param list but the wrapper only forwards known clean params.
        clean_names = [p.name for p in clean_params]
        async def _clean_wrapper(**kwargs):
            _t0 = time.time()
            # Forward only the params that the original function expects
            filtered = {k: v for k, v in kwargs.items() if k in di_params or k in clean_names}
            # Merge DI defaults (caller may override via env/config)
            args = {**di_params, **filtered}
            try:
                result = await fn(**args)
                _record_call(_t0, len(kwargs), True)
                return result
            except Exception as exc:
                _record_call(_t0, len(kwargs), False, str(exc))
                raise
        _clean_wrapper.__name__ = wrapper_name
        _clean_wrapper.__qualname__ = wrapper_name
        _clean_wrapper.__module__ = __name__
        mcp.tool(name=tool.name, description=tool.description)(_clean_wrapper)


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
