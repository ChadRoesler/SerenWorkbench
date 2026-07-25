"""
seren_workbench.app
════════════════════════════════════════════════════════════════════════

The FastAPI application for the Seren Workbench MCP server. Wires the
builtin tools, dynamic tool registry, optional bearer auth, the operator
dashboard, and the MCP transport for LLMs to connect to.

Serves:
    GET  /              — service info + tool counts
    GET  /health        — liveness
    GET  /tools         — JSON list of all registered tools (for the LLM)
    GET  /viewer        — the operator dashboard HTML
    POST /tools/state   — enable/disable a tool or action (viewer toggles)
    GET  /tools/state   — current enable/disable snapshot
    GET  /config        — server config JSON
    GET  /logs          — audit log entries
    /mcp                — the MCP transport endpoint

Integrates seren_meninges (config/auth/viewer baseplate) and seren_sinew
(request logging) — following the same pattern as the rest of the Seren family.

DEPENDENCY INJECTION: the lifespan builds one httpx.AsyncClient per Seren
service (base URLs from cfg.services) and registers them BY PARAMETER NAME
in app.state.di_registry. The MCP layer injects them into builtin tool
impls whose params are annotated httpx.AsyncClient / McpConfig. Without
this the impls' DI defaults are None and every service call explodes —
the half-cutover state this port started in.
"""
from __future__ import annotations

import time
from contextlib import asynccontextmanager, AsyncExitStack
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from .config import WorkbenchConfig, load_config
from .tool_registry import build_registry
from .routes import info as info_routes
from .routes import tools as tools_routes
from .routes import config as config_routes
from .routes import logs as logs_routes

from seren_meninges import get_version
from seren_meninges.auth import bearer_auth_middleware
from seren_meninges.viewer import render_from_dir
from seren_sinew.request_log import RequestLoggingMiddleware

from . import __version__ as _fallback_version
APP_VERSION = get_version("seren-workbench", fallback=_fallback_version)


def create_app(config: Optional[WorkbenchConfig] = None) -> FastAPI:
    cfg = config or load_config()
    bearer = cfg.server.resolve_bearer()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.config = cfg

        # Load McpConfig (tool-level knobs) from the SAME yaml load_config
        # resolved — no CWD-vs-argv[0] split brain between the server block
        # and the tools block.
        from .tool_config.mcp_config import McpConfig as _McpConfig
        mcp_config = _McpConfig.load(cfg.source_path)
        app.state.mcp_config = mcp_config

        app.state.tool_registry = build_registry(
            mcp_config=mcp_config,
            tools_dir=cfg.dashboard.tools_dir,
            tools_enabled=cfg.dashboard.tools_enabled,
            tools_disabled=cfg.dashboard.tools_disabled,
        )

        # Wire the tool audit log
        from .dynamic_tools.tool_audit_log import ToolAuditLog
        app.state.audit_log = ToolAuditLog()

        async with AsyncExitStack() as _stack:
            # ── DI clients: one AsyncClient per Seren service ───────────
            svc = cfg.services
            timeout = httpx.Timeout(svc.timeout_seconds)

            async def _client(base_url: str) -> httpx.AsyncClient:
                c = httpx.AsyncClient(base_url=base_url, timeout=timeout)
                await _stack.enter_async_context(c)
                return c

            # A base_url-less client for fetch_url absolute gets and for
            # kind=web dynamic tools (their base_url comes from the manifest).
            _general = await _stack.enter_async_context(
                httpx.AsyncClient(timeout=timeout))

            app.state.di_registry = {
                "memory": await _client(svc.memory_url),
                "runtime_host": await _client(svc.runtime_host_url),
                "searxng": await _client(svc.searxng_url),
                "scheduler": await _client(svc.scheduler_url),
                "config": mcp_config,
                "_dynamic_web_client": _general,
            }

            # Mount the MCP surface — conditionally, so a missing `mcp`
            # package doesn't crash startup.
            try:
                from .mcp.server import mount_mcp_routes
                mcp_server = mount_mcp_routes(app)
            except ImportError as exc:
                mcp_server = None
                print(f"[seren-workbench] MCP surface not available; HTTP-only mode ({exc})")
            except Exception as exc:
                mcp_server = None
                print(f"[seren-workbench] MCP mount failed: {exc!r} — continuing without MCP")

            # The streamable-HTTP transport needs its session manager's task
            # group entered explicitly.
            session_manager = getattr(mcp_server, "session_manager", None)
            if session_manager is not None:
                await _stack.enter_async_context(session_manager.run())
                print("[seren-workbench] MCP session manager running")
            yield

        print("[seren-workbench] shut down")

    app = FastAPI(
        title="SerenWorkbench",
        description="MCP (Model Context Protocol) server for the Seren stack — "
                    "the tool surface LLMs reach through.",
        version=APP_VERSION,
        lifespan=lifespan,
    )

    # ── Auth + logging stack ───────────────────────────────────────────
    app.add_middleware(bearer_auth_middleware(bearer))
    app.add_middleware(
        RequestLoggingMiddleware,
        service_name="seren-workbench",
        env_prefix="SEREN_WORKBENCH",
    )

    viewer_dir = Path(__file__).resolve().parent / "viewer" / "ui"

    # ── The operator dashboard viewer ──────────────────────────────────
    @app.get("/viewer")
    async def viewer():
        """The operator dashboard — carded tool list with enable/disable toggles.

        Renders the shared SerenMeninges baseplate with cool-grey accent and
        the leaf fragment files from viewer/ui/.
        """
        html = render_from_dir(
            viewer_dir,
            title="SerenWorkbench",
            brand="Seren<b>Workbench</b> · Tool Surface",
            subtitle=f"v{APP_VERSION} · the MCP tool layer",
            accent="#8e9aaf",  # cool grey — slate with a hint of blue
        )
        return HTMLResponse(html)

    # ── Route subpackage mounts ────────────────────────────────────────
    app.include_router(info_routes.router)
    app.include_router(tools_routes.router)
    app.include_router(config_routes.router)
    app.include_router(logs_routes.router)

    return app
