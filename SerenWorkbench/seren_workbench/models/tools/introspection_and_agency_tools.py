# ════════════════════════════════════════════════════════════════════════
#  IntrospectionAndAgencyTools - Wave 1 small enablers.
#
#  TimeSinceLastMessage, PreserveMemoryVerbatim, PromoteMemoryNow.
# ════════════════════════════════════════════════════════════════════════

from __future__ import annotations

import json
import sys
from typing import Optional

import httpx


# TimeSinceLastMessage
TIME_TOOL_DEF = {
    "name": "time_since_last_message",
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

# PreserveMemoryVerbatim
PRESERVE_TOOL_DEF = {
    "name": "preserve_memory_verbatim",
    "description": (
        "Mark a short-term memory entry for VERBATIM promotion on the "
        "next consolidator cycle - no summarization, no fusion with "
        "other entries. Requires the entry ID returned by Remember() or recall(). "
        "Returns JSON: {ok, id, verbatim:true, pinned:true}."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "entry_id": {
                "type": "string",
                "description": "The short-term entry ID, as returned by Remember() or recall().",
            },
        },
        "required": ["entry_id"],
    },
}

# PromoteMemoryNow
PROMOTE_TOOL_DEF = {
    "name": "promote_memory_now",
    "description": (
        "Promote a short-term memory to durable long-term IMMEDIATELY, "
        "without waiting for the consolidator's next cycle. Requires the entry ID "
        "from Remember() or recall(). Returns JSON: {ok, long_term_id, removed_short_id}."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "entry_id": {
                "type": "string",
                "description": "The short-term entry ID to promote immediately.",
            },
        },
        "required": ["entry_id"],
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
                f"RuntimeHost returned HTTP {resp.status_code}.",
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
        return _err(f"RuntimeHost unreachable: {ex}", "Check RuntimeHost is running.")
    except httpx.TimeoutException:
        return _err("RuntimeHost timed out.", "Try again.")


async def preserve_memory_verbatim(
    entry_id: str,
    memory: httpx.AsyncClient = None,
    **kwargs,
) -> str:
    if not entry_id:
        return _err("Empty entry_id.", "Provide the ID from a Remember() or recall() result.")

    from urllib.parse import quote
    path = f"/short/{quote(entry_id.strip())}/preserve"

    try:
        resp = await memory.post(path, content=None)
        if resp.status_code == 404:
            return _err(f"Short-term entry '{entry_id}' not found.", "May have aged out.")
        if not resp.is_success:
            body = resp.text
            return _err(f"SerenMemory returned HTTP {resp.status_code}.", body[:500] + "…")

        print(f"[mcp-audit] PreserveMemoryVerbatim: id={entry_id}", file=sys.stderr)
        return resp.text

    except httpx.RequestError as ex:
        return _err(f"SerenMemory unreachable: {ex}", "Check seren-memory.service is running.")
    except httpx.TimeoutException:
        return _err("Preserve request timed out.", "Try again.")


async def promote_memory_now(
    entry_id: str,
    memory: httpx.AsyncClient = None,
    **kwargs,
) -> str:
    if not entry_id:
        return _err("Empty entry_id.", "Provide the ID from a Remember() or recall() result.")

    from urllib.parse import quote
    path = f"/short/{quote(entry_id.strip())}/promote"

    try:
        resp = await memory.post(path, content=None)
        if resp.status_code == 404:
            return _err(f"Short-term entry '{entry_id}' not found.", "May have aged out.")
        if not resp.is_success:
            body = resp.text
            return _err(f"SerenMemory returned HTTP {resp.status_code}.", body[:500] + "…")

        print(f"[mcp-audit] PromoteMemoryNow: id={entry_id}", file=sys.stderr)
        return resp.text

    except httpx.RequestError as ex:
        return _err(f"SerenMemory unreachable: {ex}", "Check seren-memory.service is running.")
    except httpx.TimeoutException:
        return _err("Promote request timed out.", "Try again.")


def _err(error: str, hint: str) -> str:
    return json.dumps({"error": error, "hint": hint}, indent=2)
