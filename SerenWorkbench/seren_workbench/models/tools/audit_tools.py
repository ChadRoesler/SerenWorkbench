# ═══════════════════════════════════════════════════════════════
#  AuditTools - a model reading the record of its own tool calls.
#
#  list_my_tool_calls.
#
#  The Workbench has always kept this record (dynamic_tools/tool_audit_log)
#  and shown it to the operator at GET /logs. The one party who could not
#  read it was the one whose calls it is. That matters more for a model
#  than for a person: a session that was woken to do a job and ended leaves
#  the next session nothing but a letter saying what it believes it did.
#  This is the other half - what was actually called, when, and whether it
#  worked - so "I reviewed the draft" can be checked against "review_draft,
#  ok, 02:14".
#
#  CONTENT-BLIND, like the log itself: tool names, times, durations, how
#  many arguments, and the error text of a failure. Never an argument's
#  value and never a result. It is a record of what was done, not of what
#  was said.
# ═══════════════════════════════════════════════════════════════

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Optional

from ...dynamic_tools.tool_audit_log import ToolAuditLog

TOOLBOX = "Time & Self"

MAX_LIMIT = 200

LIST_MY_TOOL_CALLS_TOOL_DEF = {
    "name": "list_my_tool_calls",
    "description": (
        "The record of tool calls made through this Workbench, newest first: "
        "which tool, when, how long it took, whether it worked, and the error "
        "if it did not. Use it to check what a session actually did (yours, "
        "or one that was woken while nobody was there), or to see what has "
        "been failing.\n"
        "\n"
        "Content-blind: it holds names, times and outcomes - never an "
        "argument's value or a result. It covers every caller of this "
        "Workbench, kept in memory since the Workbench last started (the "
        "answer says when that was), the most recent 500 calls."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": f"How many calls to return. Default 20, at most {MAX_LIMIT}.",
                "default": 20,
            },
            "tool": {
                "type": "string",
                "description": "Optional. Only calls of this tool, by its exact name.",
            },
            "failed_only": {
                "type": "boolean",
                "description": "Optional. Only the calls that failed.",
                "default": False,
            },
        },
        "required": [],
    },
}


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _age(ts: float, now: float) -> str:
    secs = max(0, int(now - (ts or 0)))
    if secs < 90:
        return f"{secs}s ago"
    if secs < 5400:
        return f"{secs // 60}m ago"
    if secs < 172800:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


async def list_my_tool_calls(
    limit: int = 20,
    tool: Optional[str] = None,
    failed_only: bool = False,
    audit_log: Optional[ToolAuditLog] = None,
    **kwargs,
) -> str:
    if audit_log is None:
        return json.dumps({"error": "This Workbench is not keeping a record of tool calls.", "hint": ""}, indent=2)
    try:
        n = max(1, min(int(limit), MAX_LIMIT))
    except (TypeError, ValueError):
        n = 20
    now = time.time()
    everything = audit_log.snapshot(limit=ToolAuditLog.MAX_ENTRIES)
    rows = [e for e in everything
            if (not tool or e.tool == tool) and (not failed_only or not e.success)]

    by_tool: dict[str, dict[str, int]] = {}
    for e in everything:
        t = by_tool.setdefault(e.tool, {"calls": 0, "failed": 0})
        t["calls"] += 1
        t["failed"] += 0 if e.success else 1

    started = getattr(audit_log, "started_at", None)
    return json.dumps({
        "kept_since": _iso(started) if started else None,
        "kept": len(everything),
        "matching": len(rows),
        "shown": min(n, len(rows)),
        "calls": [
            {
                "tool": e.tool,
                "when": _age(e.timestamp, now),
                "at": _iso(e.timestamp),
                "ok": e.success,
                "ms": e.duration_ms,
                "arguments": e.arg_count,
                "kind": e.kind,
                **({"error": e.error_message} if e.error_message else {}),
            }
            for e in rows[:n]
        ],
        # Failures first, then the busiest: what is going wrong is what a
        # reader of this is usually looking for.
        "by_tool": dict(sorted(by_tool.items(), key=lambda kv: (-kv[1]["failed"], -kv[1]["calls"], kv[0]))),
    }, indent=2)
