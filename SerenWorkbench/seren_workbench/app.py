"""
seren_workbench.app
════════════════════════════════════════════════════════════════════════

The FastAPI application for the Seren Workbench MCP server. Wires the
builtin tools, dynamic tool registry, optional bearer auth, the operator
dashboard, and the MCP transport for LLMs to connect to.

Serves:
    GET  /              — service info + tool counts + update status
    GET  /health        — liveness
    GET  /tools         — JSON list of all registered tools (for the LLM)
    GET  /viewer        — the operator dashboard HTML
    POST /tools/state   — enable/disable a tool or action (viewer toggles)
    GET  /tools/state   — current enable/disable snapshot
    GET  /config        — server config JSON
    GET  /logs          — audit log entries
    GET  /stores        — what the Workbench keeps, and its snapshots
    POST /stores/snapshot — take one now (and the rest of seren_sinew.stores'
                          routes: list, archive, rehearse; there is no restore
                          route - see keeping.py)
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

import os
import time
import logging
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
log = logging.getLogger("seren_workbench")

def create_app(config: Optional[WorkbenchConfig] = None) -> FastAPI:
    cfg = config or load_config()
    bearer = cfg.server.resolve_bearer()

    # A restore, when the config asks for one (backup.restore_from +
    # restore_reason): into an empty tools folder only, before anything reads
    # it. Refused = the Workbench does not start. No route does this.
    from . import keeping
    keeping.restore_if_asked(cfg, log=lambda m: log.info("[seren-workbench] %s", m))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.config = cfg

        # Load McpConfig (tool-level knobs) from the SAME yaml load_config
        # resolved — no CWD-vs-argv[0] split brain between the server block
        # and the tools block.
        from .tool_config.mcp_config import McpConfig as _McpConfig
        mcp_config = _McpConfig.load(cfg.source_path)
        app.state.mcp_config = mcp_config

        from .models.tools.proposal_tools import (
            PROPOSE_TOOL_DEF, PROPOSE_PLUGIN_TOOL_DEF, LIST_PROPOSALS_TOOL_DEF,
        )
        proposal_tool_names = {
            PROPOSE_TOOL_DEF["name"], PROPOSE_PLUGIN_TOOL_DEF["name"],
            LIST_PROPOSALS_TOOL_DEF["name"],
        }

        app.state.tool_registry = build_registry(
            mcp_config=mcp_config,
            tools_dir=cfg.dashboard.tools_dir,
            tools_enabled=cfg.dashboard.tools_enabled,
            tools_disabled=cfg.dashboard.tools_disabled,
            exclude=set() if cfg.dashboard.proposals_enabled else proposal_tool_names,
            # Where a toggle is remembered so it is still in force after a
            # restart. Beside the manifests by default.
            state_path=cfg.dashboard.resolve_state_file(),
        )
        # Our own listener, handed to everything that must refuse to point a
        # tool back at it (the web dispatcher, the proposal validator).
        self_addr = (cfg.server.host, int(cfg.server.port))

        # ── Tool proposals ──────────────────────────────────────────────
        # The staging store. Note what it is NOT given: any way to register
        # a tool. It writes files into a directory the loader doesn't read,
        # and approval lives behind an operator HTTP route with no MCP tool
        # in front of it. live_names is a callable so the collision check
        # asks the registry at the moment it matters.
        if cfg.dashboard.proposals_enabled:
            from .proposals import ProposalStore
            app.state.proposals = ProposalStore(
                proposals_dir=cfg.dashboard.resolve_proposals_dir(),
                tools_dir=cfg.dashboard.tools_dir,
                live_names=lambda: {t.name for t in app.state.tool_registry.all_tools()},
                self_addr=self_addr,
                plugins_dir=cfg.dashboard.resolve_plugins_dir(),
                # Every name the hub has: the standard components and what
                # is plugged in. Asked at the moment it matters.
                live_plugins=lambda: set(getattr(getattr(app.state, "upstreams", None), "states", {}) or {}),
            )
        else:
            app.state.proposals = None

        # Wire the tool audit log
        from .dynamic_tools.tool_audit_log import ToolAuditLog
        app.state.audit_log = ToolAuditLog()

        # ── Update checker ───────────────────────────────────────
        # "is there a newer seren-workbench". Cosmetic: it polls on a TTL,
        # never in the request path, and every failure mode is a status string
        # rather than an exception.
        #
        # The try/except guards the IMPORT, because a Meninges older than 2.0.0
        # has no updates module. The gate is DELIBERATELY VISIBLE - state stays
        # None and GET / reports status="unavailable" with a reason. A silent
        # fallback would render as "you're up to date", which is the exact
        # failure shape that let mcp 2.0.0 quietly delete this service's /mcp
        # endpoint without anything going red.
        try:
            from seren_meninges.updates import UpdateChecker
            app.state.updates = UpdateChecker(
                "seren-workbench",
                enabled=cfg.updates.enabled,
                index_url=cfg.updates.index_url,
                ttl_seconds=cfg.updates.check_interval_hours * 3600.0,
                allow_prerelease=cfg.updates.allow_prerelease,
                fallback_version=APP_VERSION,
            )
        # Catch EVERYTHING, not just ImportError. This whole feature is cosmetic -
        # seren_meninges/version.py states the contract: a version read must never
        # crash startup. A too-narrow catch here already bit us: cfg.updates was
        # missing, the AttributeError sailed past `except ImportError`, and five
        # services failed to boot on a feature that only draws a badge.
        except Exception as exc:
            app.state.updates = None
            log.info("update checking unavailable (%s)", exc)

        async with AsyncExitStack() as _stack:
            # ── DI clients: one AsyncClient per Seren service ───────────
            svc = cfg.services
            timeout = httpx.Timeout(svc.timeout_seconds)

            async def _client(base_url: str, service: str = "") -> httpx.AsyncClient:
                # The bearer this Workbench PRESENTS to a Seren service, when
                # one is configured. Resolved once at startup through the same
                # resolver the leaves use inbound. A stack installed with
                # --gen-token had no way to do this before, so every builtin
                # 401'd with no config key to fix it.
                token = svc.resolve_bearer(service) if service else ""
                headers = {"Authorization": f"Bearer {token}"} if token else None
                c = httpx.AsyncClient(base_url=base_url, timeout=timeout, headers=headers)
                await _stack.enter_async_context(c)
                return c

            # A base_url-less client for fetch_url absolute gets and for
            # kind=web dynamic tools (their base_url comes from the manifest).
            _general = await _stack.enter_async_context(
                httpx.AsyncClient(timeout=timeout))

            # The standard components, passed through (upstream.py). Built
            # before the MCP mount reads it; asked for their tools below, once
            # the mount exists, and never in the way of startup.
            from .upstream import UpstreamHub
            from .config import PluginConfig
            # The config's plugins, then the ones an approved proposal
            # installed (<tools_dir>/plugins/). The config's win on a name.
            plugins = list(cfg.plugins) + PluginConfig.many_from_dir(
                cfg.dashboard.resolve_plugins_dir(), taken={p.name for p in cfg.plugins})
            app.state.upstreams = UpstreamHub(
                svc, cfg.components.as_dict(), registry=app.state.tool_registry,
                self_addr=self_addr, audit_log=app.state.audit_log, plugins=plugins)

            # The services this Workbench holds up through Lodestar's leases
            # (holds.py): remembered, and let go of when a session forgets.
            from .holds import DEFAULT_HOLD_MINUTES, DEFAULT_HOLDER, Holds
            _ensure_cfg = mcp_config.for_tool("ensure_service_running")
            app.state.holds = Holds(
                holder=_ensure_cfg.get_string("holder", DEFAULT_HOLDER),
                hold_minutes=_ensure_cfg.get_float("hold_minutes", DEFAULT_HOLD_MINUTES))

            app.state.di_registry = {
                "memory": await _client(svc.memory_url, "memory"),
                "runtime_host": await _client(svc.runtime_host_url, "runtime_host"),
                "holds": app.state.holds,
                "audit_log": app.state.audit_log,
                "searxng": await _client(svc.searxng_url),
                "scheduler": await _client(svc.scheduler_url, "scheduler"),
                "config": mcp_config,
                "proposals": app.state.proposals,
                "_dynamic_web_client": _general,
                "_self_addr": self_addr,
            }

            # Mount the MCP surface — conditionally, so a missing `mcp`
            # package doesn't crash startup.
            try:
                from .mcp.server import mount_mcp_routes
                mcp_server = mount_mcp_routes(app)
            except ImportError as exc:
                mcp_server = None
                log.info(f"[seren-workbench] MCP surface not available; HTTP-only mode ({exc})")
            except Exception as exc:
                mcp_server = None
                log.info(f"[seren-workbench] MCP mount failed: {exc!r} — continuing without MCP")

            # ── Live tool reload ────────────────────────────────────────
            # The initial LoadResult is rebuilt from the ToolInfos the
            # registry already holds rather than re-running the loader: a
            # second load_directory() here would re-fetch every remote
            # `from:` manifest at boot, doubling startup network work to
            # recover data we already have in hand. Startup skip/warning
            # detail is logged by build_registry's own load; the first
            # reload repopulates it in the snapshot.
            try:
                from .dynamic_tools.dynamic_tool_registry import DynamicToolRegistry
                from .dynamic_tools.manifest_loader import LoadResult
                from .mcp.server import make_dynamic_registrar

                seed = LoadResult()
                seed.resolved_inline_tools = [
                    (t.entry, t.owner, t.source)
                    for t in app.state.tool_registry.dynamic_tools()
                    if t.entry is not None
                ]
                app.state.dynamic_registry = DynamicToolRegistry(
                    tools_dir=cfg.dashboard.tools_dir,
                    initial_load=seed,
                    tool_registry=app.state.tool_registry,
                    mcp_server=mcp_server,
                    register=make_dynamic_registrar(
                        app.state.tool_registry,
                        app.state.di_registry,
                        app.state.audit_log,
                    ),
                )
            except Exception as exc:
                app.state.dynamic_registry = None
                log.info(f"[seren-workbench] live tool reload unavailable: {exc!r}")

            # The streamable-HTTP transport needs its session manager's task
            # group entered explicitly.
            session_manager = getattr(mcp_server, "session_manager", None)
            if session_manager is not None:
                await _stack.enter_async_context(session_manager.run())
                log.info("[seren-workbench] MCP session manager running")

            # The tool list as it stands is what a client is told about
            # changes FROM (mcp.server.ToolListWatch).
            _watch = getattr(app.state, "tool_watch", None)
            if _watch is not None:
                await _watch.prime()

            # Ask the components what they offer - in the background, so the
            # Workbench is listening while a component that is down takes its
            # seconds to say so. One that is not up yet is asked again every
            # minute. SEREN_WORKBENCH_COMPONENTS=off skips it entirely (tests,
            # a box with none of them).
            import asyncio as _asyncio
            _retry = None
            if mcp_server is not None and os.environ.get("SEREN_WORKBENCH_COMPONENTS", "").lower() not in ("0", "off", "false", "no"):
                _retry = _asyncio.create_task(app.state.upstreams.run())
            # The Workbench's own snapshot schedule: one whenever the newest
            # is older than backup.every_hours (seren_sinew.stores).
            _snaps = None
            if app.state.stores is not None and cfg.backup.every_hours > 0:
                from seren_sinew.stores import snapshot_loop
                _snaps = _asyncio.create_task(snapshot_loop(lambda: app.state.stores, cfg.backup.every_hours))
            _lodestar = app.state.di_registry["runtime_host"]
            _letting_go = _asyncio.create_task(app.state.holds.run(_lodestar))
            try:
                yield
            finally:
                for _task in (_retry, _snaps, _letting_go):
                    if _task is not None:
                        _task.cancel()
                # What this Workbench still holds goes with it: its memory of
                # the holds ends here, and a lease nobody remembers is never
                # released. Bounded - a Lodestar that is down must not hang
                # the shutdown.
                try:
                    await _asyncio.wait_for(
                        app.state.holds.release_all(_lodestar, "the Workbench is shutting down"), timeout=20)
                except Exception as exc:  # noqa: BLE001
                    log.info("[seren-workbench] could not let go of held services at shutdown: %r", exc)

        log.info("[seren-workbench] shut down")

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

    # ── What the Workbench keeps, and snapshots of it ──────────────────
    # The tools folder: manifests, approved plugins, proposals, the switches.
    # Same keeper and same routes as Memory, Loci, Margin and the Hippocampus,
    # so a Lodestar pulls and rehearses these with the rest. See keeping.py.
    from seren_sinew.stores import add_store_routes
    app.state.stores = keeping.make_keeper(cfg, APP_VERSION, log=lambda m: log.info("[seren-workbench] %s", m))
    add_store_routes(app, lambda: app.state.stores)

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
    from .routes import components as component_routes
    app.include_router(component_routes.router)
    from .routes import proposals as proposal_routes
    app.include_router(proposal_routes.router)
    app.include_router(config_routes.router)
    app.include_router(logs_routes.router)

    return app
