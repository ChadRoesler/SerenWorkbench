"""
Info routes — GET /, GET /health.

Service info + liveness endpoint.

`/` also carries the update status. It's a PUBLIC route (no bearer), which is
fine because it already published the running version — `updates` adds the
comparison, not the disclosure. If you'd rather not advertise "and it's out of
date" to an unauthenticated caller, set `updates.enabled: false` and read the
badge from the dashboard instead.
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Request

from .._version import __version__ as _fallback_version
from seren_meninges import get_version
from seren_meninges.updates import updates_payload

APP_VERSION = get_version("seren-workbench", fallback=_fallback_version)

router = APIRouter(tags=["info"])



@router.get("/")
async def root(request: Request):
    reg = request.app.state.tool_registry
    all_tools = reg.all_tools()

    # Pending proposals ride along on the summary so the dashboard header can
    # show a waiting count. Something asked you for a capability; that should
    # be visible without opening the tab to go looking for it.
    pending = 0
    store = getattr(request.app.state, "proposals", None)
    if store is not None:
        try:
            pending = len(store.list("pending"))
        except Exception:  # noqa: BLE001 — a summary must never 500
            pending = 0

    return {
        "service": "SerenWorkbench",
        "version": APP_VERSION,
        "tools_count": len(all_tools),
        "builtin_count": sum(1 for t in all_tools if t.type == "builtin"),
        "dynamic_count": sum(1 for t in all_tools if t.type == "dynamic"),
        "disabled_count": sum(1 for t in all_tools if not t.enabled),
        "pending_proposals": pending,
        "updates": await updates_payload(
            getattr(request.app.state, "updates", None),
            distribution="seren-workbench", installed=APP_VERSION),
    }


@router.get("/health")
async def health():
    return {"ok": True, "ts": time.time()}
