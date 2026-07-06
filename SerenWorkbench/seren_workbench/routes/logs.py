"""
Logs route — GET /logs.

Returns recent audit-log entries from the in-memory ring buffer.
Content-blind: only metadata, never argument values or result text.
"""
from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter(tags=["logs"])


@router.get("/logs")
async def get_logs(request: Request, limit: int = 100):
    """Return recent audit-log entries from the in-memory ring buffer.

    Query param:
        limit (int, default 100): max entries to return.
    """
    audit = getattr(request.app.state, "audit_log", None)
    if audit is None:
        return {"entries": []}
    return {
        "count": len(audit._entries),
        "entries": list(audit._entries)[-limit:],
    }
