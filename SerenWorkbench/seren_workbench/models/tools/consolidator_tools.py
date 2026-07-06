# ════════════════════════════════════════════════════════════════════════
#  ConsolidatorTools - manage memory consolidation.
#
#  Trigger a consolidator cycle manually, check its status.
# ════════════════════════════════════════════════════════════════════════

from __future__ import annotations

import json
import sys
from typing import Optional

import httpx


CONSOLIDATE_TOOL_DEF = {
    "name": "trigger_consolidation",
    "description": (
        "Triggers an immediate consolidator cycle. The consolidator "
        "summarizes and fuses short-term entries into long-term durable "
        "memory. Use this when the user says 'consolidate now' or when "
        "you've saved a lot of Remember() calls and want them preserved. "
        "Returns JSON with cycle_id, entries_processed, and new_long_term_count."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

CONSOLIDATION_STATUS_TOOL_DEF = {
    "name": "consolidation_status",
    "description": (
        "Returns the consolidator's current state: idle, running, or "
        "last cycle info. Use this to check if consolidation is in "
        "progress or to see how the last cycle went. Returns JSON."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}


async def trigger_consolidation(
    memory: httpx.AsyncClient = None,
    **kwargs,
) -> str:
    try:
        resp = await memory.post("/consolidate/trigger", content=None)
        if not resp.is_success:
            body = resp.text
            return _err(
                f"SerenMemory returned HTTP {resp.status_code}.",
                body[:500] + "…" if len(body) > 500 else body,
            )

        data = resp.json()
        cycle_id = data.get("cycle_id", "?")
        processed = data.get("entries_processed", 0)
        new_long = data.get("new_long_term_count", 0)

        print(
            f"[mcp-audit] Consolidation triggered: cycle_id={cycle_id} "
            f"processed={processed} new_long={new_long}",
            file=sys.stderr,
        )
        return json.dumps(data, indent=2)

    except httpx.RequestError as ex:
        return _err(f"SerenMemory unreachable: {ex}", "Check seren-memory.service is running.")
    except httpx.TimeoutException:
        return _err("SerenMemory timed out.", "Try again.")
    except (json.JSONDecodeError, KeyError) as ex:
        return _err(f"Malformed response: {ex}", "Schema mismatch.")


async def consolidation_status(
    memory: httpx.AsyncClient = None,
    **kwargs,
) -> str:
    try:
        resp = await memory.get("/consolidate/status")
        if not resp.is_success:
            body = resp.text
            return _err(
                f"SerenMemory returned HTTP {resp.status_code}.",
                body[:500] + "…" if len(body) > 500 else body,
            )

        return json.dumps(resp.json(), indent=2)

    except httpx.RequestError as ex:
        return _err(f"SerenMemory unreachable: {ex}", "Check seren-memory.service is running.")
    except httpx.TimeoutException:
        return _err("SerenMemory timed out.", "Try again.")
    except (json.JSONDecodeError, KeyError) as ex:
        return _err(f"Malformed response: {ex}", "Schema mismatch.")


def _err(error: str, hint: str) -> str:
    return json.dumps({"error": error, "hint": hint}, indent=2)
