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
    """Return recent audit-log entries (NEWEST first) from the ring buffer.

    Uses ToolAuditLog.snapshot() — the lock-holding, newest-first accessor.
    (The old code sliced the private deque tail directly, which returned the
    OLDEST entries and skipped the lock.)

    Query param:
        limit (int, default 100): max entries to return.
    """
    audit = getattr(request.app.state, "audit_log", None)
    if audit is None:
        return {"count": 0, "entries": []}
    entries = audit.snapshot(limit=limit)
    return {
        "count": audit.count,
        "entries": entries,
    }
