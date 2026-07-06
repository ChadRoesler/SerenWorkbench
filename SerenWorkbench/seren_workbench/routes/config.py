"""
Config route — GET /config.

Returns the current server config as JSON — used by the dashboard Config tab.
"""
from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Request

router = APIRouter(tags=["config"])


@router.get("/config")
async def get_config(request: Request):
    """Return the current server config as JSON — used by the dashboard
    Config tab to display operator-tunable knobs."""
    cfg = request.app.state.config
    mcp_cfg = getattr(request.app.state, "mcp_config", None)
    tool_overrides = mcp_cfg.snapshot() if mcp_cfg else {}
    return {
        "server": asdict(cfg.server),
        "tls": asdict(cfg.tls),
        "dashboard": asdict(cfg.dashboard),
        "tool_overrides": tool_overrides,
    }
