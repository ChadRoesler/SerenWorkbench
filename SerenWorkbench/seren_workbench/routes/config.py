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

    return {
        "server": _masked(asdict(cfg.server)),
        "tls": asdict(cfg.tls),
        "dashboard": asdict(cfg.dashboard),
        # The services block carries OUTBOUND token literals now (what we
        # present to Memory / Lodestar); same rule, same mask.
        "services": _masked(asdict(cfg.services)),
        "tool_overrides": tool_overrides,
    }


def _masked(block: dict) -> dict:
    """Every key that IS a secret (ends in `bearer_token`) is masked. The
    `_env` and `_keyring` keys are pointers and stay readable - knowing
    which env var holds the token is not knowing the token."""
    return {k: (_mask(v) if k.endswith("bearer_token") else v) for k, v in block.items()}
