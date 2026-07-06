# ════════════════════════════════════════════════════════════════════════
#  MemoryTools - LLM-managed short- and long-term memory.
#
#  Remember(), Recall(), Forget(). The LLM can save strings into the
#  short-term store, search recent memory, and discard entries.
# ════════════════════════════════════════════════════════════════════════

from __future__ import annotations

import json
import sys
from typing import Optional

import httpx


REMEMBER_TOOL_DEF = {
    "name": "remember",
    "description": (
        "Saves a string to short-term memory. Use this to record "
        "facts about the user, the conversation, or the environment. "
        "Returns JSON with an entry_id (e.g. 'short_0001'). Short-term "
        "entries age out after ~200 messages or the consolidator's "
        "threshold; use preserve_memory_verbatim or promote_memory_now "
        "to keep important ones."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "The text to save as a short-term memory entry.",
            },
        },
        "required": ["content"],
    },
}

RECALL_TOOL_DEF = {
    "name": "recall",
    "description": (
        "Searches short-term memory. Returns up to 10 entries matching "
        "the query text, with their entry_id, content, and created_at. "
        "Use this when the user says 'do you remember when I said X' or "
        "when you need to pull something you saved earlier. Entries are "
        "ordered by recency (newest first). Returns JSON array."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search string to match against memory content.",
            },
        },
        "required": ["query"],
    },
}

FORGET_TOOL_DEF = {
    "name": "forget",
    "description": (
        "Deletes a short-term memory entry by its entry_id. Use this when "
        "the user says 'forget that' or 'delete that'. Returns JSON with "
        "the deleted entry_id. Irreversible."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "entry_id": {
                "type": "string",
                "description": "The short-term entry ID to delete, e.g. 'short_0001'.",
            },
        },
        "required": ["entry_id"],
    },
}


async def remember(
    content: str,
    memory: httpx.AsyncClient = None,
    **kwargs,
) -> str:
    if not content:
        return _err("Empty content.", "Provide text to remember.")

    try:
        resp = await memory.post("/short", json={"content": content})
        if not resp.is_success:
            body = resp.text
            return _err(f"SerenMemory returned HTTP {resp.status_code}.", body[:500] + "…")

        data = resp.json()
        entry_id = data.get("entry_id") or data.get("id", "?")
        print(f"[mcp-audit] Remember: id={entry_id}", file=sys.stderr)
        return json.dumps({"entry_id": entry_id}, indent=2)

    except httpx.RequestError as ex:
        return _err(f"SerenMemory unreachable: {ex}", "Check seren-memory.service is running.")
    except httpx.TimeoutException:
        return _err("SerenMemory timed out.", "Try again.")
    except (json.JSONDecodeError, KeyError) as ex:
        return _err(f"Malformed response: {ex}", "Schema mismatch.")


async def recall(
    query: str,
    memory: httpx.AsyncClient = None,
    **kwargs,
) -> str:
    if not query:
        return _err("Empty query.", "Provide a search string.")

    from urllib.parse import quote
    path = f"/short/search?q={quote(query)}"

    try:
        resp = await memory.get(path)
        if not resp.is_success:
            body = resp.text
            return _err(f"SerenMemory returned HTTP {resp.status_code}.", body[:500] + "…")

        results = resp.json()
        return json.dumps(results, indent=2)

    except httpx.RequestError as ex:
        return _err(f"SerenMemory unreachable: {ex}", "Check seren-memory.service is running.")
    except httpx.TimeoutException:
        return _err("SerenMemory timed out.", "Try again.")
    except (json.JSONDecodeError, KeyError) as ex:
        return _err(f"Malformed response: {ex}", "Schema mismatch.")


async def forget(
    entry_id: str,
    memory: httpx.AsyncClient = None,
    **kwargs,
) -> str:
    if not entry_id:
        return _err("Empty entry_id.", "Provide the entry ID from remember() or recall().")

    from urllib.parse import quote
    path = f"/short/{quote(entry_id.strip())}"

    try:
        resp = await memory.delete(path)
        if resp.status_code == 404:
            return _err(f"Entry '{entry_id}' not found.", "May have already aged out.")
        if not resp.is_success:
            body = resp.text
            return _err(f"SerenMemory returned HTTP {resp.status_code}.", body[:500] + "…")

        print(f"[mcp-audit] Forget: id={entry_id}", file=sys.stderr)
        return json.dumps({"entry_id": entry_id, "deleted": True}, indent=2)

    except httpx.RequestError as ex:
        return _err(f"SerenMemory unreachable: {ex}", "Check seren-memory.service is running.")
    except httpx.TimeoutException:
        return _err("SerenMemory timed out.", "Try again.")


def _err(error: str, hint: str) -> str:
    return json.dumps({"error": error, "hint": hint}, indent=2)
