"""
Config route — GET /config.

Returns the current server config as JSON — used by the dashboard Config tab.
"""
from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Request

router = APIRouter(tags=["config"])


def _mask(value: str | None) -> str:
    """Mask a secret for display: empty stays empty, set becomes '••• (set)'."""
    return "••• (set)" if value else ""


@router.get("/config")
async def get_config(request: Request):
    """Return the current server config as JSON — used by the dashboard
    Config tab to display operator-tunable knobs.

    SECRETS ARE MASKED: bearer_token never leaves the process in the clear
    (the viewer note promises this; asdict() alone would leak it raw).
    """
    cfg = request.app.state.config
    mcp_cfg = getattr(request.app.state, "mcp_config", None)
    tool_overrides = mcp_cfg.snapshot() if mcp_cfg else {}

    server = asdict(cfg.server)
    if "bearer_token" in server:
        server["bearer_token"] = _mask(server["bearer_token"])

    return {
        "server": server,
        "tls": asdict(cfg.tls),
        "dashboard": asdict(cfg.dashboard),
        "services": asdict(cfg.services),
        "tool_overrides": tool_overrides,
    }
