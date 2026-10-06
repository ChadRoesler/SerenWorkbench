# ════════════════════════════════════════════════════════════════════════
#  IntrospectionAndAgencyTools - Wave 1 small enablers.
#
#  TimeSinceLastMessage. (PreserveMemoryVerbatim and PromoteMemoryNow lived
#  here until 6 Oct 2026; they are Memory's own tools, passed through with
#  the rest of that component - see seren_workbench/upstream.py.)
# ════════════════════════════════════════════════════════════════════════

from __future__ import annotations

import json
import sys
from typing import Optional

import httpx


# Declares its own toolbox: reading the temporal posture of a conversation
# is a self-awareness thing.

# TimeSinceLastMessage
TIME_TOOL_DEF = {
    "name": "time_since_last_message",
    "toolbox": "Time & Self",
    "description": (
        "Returns how long since the user last sent a message, in seconds. "
        "Use this to read the temporal posture of the conversation: "
        "30 seconds quiet means active back-and-forth, 3 hours means "
        "they've stepped away. Returns JSON with seconds_since_last_message, "
        "last_message_at_unix, posture."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}



async def time_since_last_message(
    runtime_host: httpx.AsyncClient = None,
    **kwargs,
) -> str:
    try:
        resp = await runtime_host.get("/api/v1/chat/last_user_at")
        if not resp.is_success:
            body = resp.text
            return _err(
                f"Lodestar returned HTTP {resp.status_code}.",
                body[:500] + "…" if len(body) > 500 else body,
            )

        data = resp.json()
        last_at = data.get("last_user_at_unix")

        if last_at is None or last_at <= 0:
            return json.dumps({
                "seconds_since_last_message": None,
                "last_message_at_unix": None,
                "posture": "unknown",
                "note": "No user message has been recorded yet this session.",
            }, indent=2)

        import time
        now = int(time.time())
        seconds = now - int(last_at)
        posture = (
            "active" if seconds < 120 else
            "brief_pause" if seconds < 600 else
            "away" if seconds < 3600 else
            "long_away"
        )
        return json.dumps({
            "seconds_since_last_message": seconds,
            "last_message_at_unix": last_at,
            "posture": posture,
        }, indent=2)

    except httpx.RequestError as ex:
        return _err(f"Lodestar unreachable: {ex}", "Check Lodestar is running.")
    except httpx.TimeoutException:
        return _err("Lodestar timed out.", "Try again.")


def _err(error: str, hint: str) -> str:
    return json.dumps({"error": error, "hint": hint}, indent=2)
