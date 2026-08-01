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

APP_VERSION = get_version("seren-workbench", fallback=_fallback_version)

router = APIRouter(tags=["info"])

# What `/` reports when no checker was wired at all — a seren-meninges older
# than 2.0.0, so the import in the lifespan didn't take.
#
# This is a full payload rather than a null or an absent key ON PURPOSE: an
# absent key reads as "fine" to whatever renders it, and "I could not check"
# is not the same fact as "you are current".
_NOT_WIRED = {
    "status": "unavailable",
    "distribution": "seren-workbench",
    "latest": None,
    "update_available": False,
    "detail": "update checking not installed — "
              "pip install 'seren-workbench[updates]'",
    "checked_at": None,
}


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

    checker = getattr(request.app.state, "updates", None)
    if checker is None:
        updates = {**_NOT_WIRED, "installed": APP_VERSION}
    else:
        updates = (await checker.get()).as_dict()

    return {
        "service": "SerenWorkbench",
        "version": APP_VERSION,
        "tools_count": len(all_tools),
        "builtin_count": sum(1 for t in all_tools if t.type == "builtin"),
        "dynamic_count": sum(1 for t in all_tools if t.type == "dynamic"),
        "disabled_count": sum(1 for t in all_tools if not t.enabled),
        "pending_proposals": pending,
        "updates": updates,
    }


@router.get("/health")
async def health():
    return {"ok": True, "ts": time.time()}
