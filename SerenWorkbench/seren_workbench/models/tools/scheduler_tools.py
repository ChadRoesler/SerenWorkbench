# ════════════════════════════════════════════════════════════════════════
#  SchedulerTools - let the LLM schedule recurring actions.
#
#  ScheduleAction, ListScheduled, UnscheduleAction.
# ════════════════════════════════════════════════════════════════════════

from __future__ import annotations

import json
import sys
from typing import Optional

import httpx


SCHEDULE_TOOL_DEF = {
    "name": "schedule_action",
    "description": (
        "Schedules an action to be taken later. Requires: a name "
        "(human-readable), a tool name (which MCP tool to call), "
        "arguments (JSON dict for the tool), and an execution time in "
        "ISO 8601 format (UTC). The scheduler service will call the "
        "tool at that time. Returns JSON with schedule_id."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "A human-readable name for this scheduled action.",
            },
            "tool": {
                "type": "string",
                "description": "The MCP tool name to call when the time comes.",
            },
            "arguments": {
                "type": "object",
                "description": "JSON dict of arguments to pass to the tool.",
            },
            "execute_at": {
                "type": "string",
                "description": "ISO 8601 UTC datetime string for when to execute, e.g. '2025-06-01T14:00:00Z'.",
            },
        },
        "required": ["name", "tool", "arguments", "execute_at"],
    },
}

LIST_SCHEDULED_TOOL_DEF = {
    "name": "list_scheduled",
    "description": (
        "Lists all currently scheduled actions. Returns JSON array of "
        "schedule entries with their id, name, tool, execute_at, and "
        "status (pending/executed/failed)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

UNSCHEDULE_TOOL_DEF = {
    "name": "unschedule_action",
    "description": (
        "Cancels a scheduled action by its schedule_id. Returns JSON "
        "with the cancelled id. Use when the user says 'cancel that' or "
        "'don't do that later'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "schedule_id": {
                "type": "string",
                "description": "The schedule ID returned by schedule_action.",
            },
        },
        "required": ["schedule_id"],
    },
}


async def schedule_action(
    name: str,
    tool: str,
    arguments: dict,
    execute_at: str,
    scheduler: httpx.AsyncClient = None,
    **kwargs,
) -> str:
    if not name:
        return _err("Missing 'name'.", "Provide a human-readable name.")
    if not tool:
        return _err("Missing 'tool'.", "Provide a valid MCP tool name.")
    if not execute_at:
        return _err("Missing 'execute_at'.", "Provide an ISO 8601 UTC datetime.")

    try:
        resp = await scheduler.post(
            "/schedule",
            json={
                "name": name,
                "tool": tool,
                "arguments": arguments,
                "execute_at": execute_at,
            },
        )
        if not resp.is_success:
            body = resp.text
            return _err(f"Scheduler returned HTTP {resp.status_code}.", body[:500] + "…")

        data = resp.json()
        schedule_id = data.get("schedule_id") or data.get("id", "?")
        print(
            f"[mcp-audit] ScheduleAction: {name} tool={tool} "
            f"at={execute_at} id={schedule_id}",
            file=sys.stderr,
        )
        return json.dumps({"schedule_id": schedule_id}, indent=2)

    except httpx.RequestError as ex:
        return _err(f"Scheduler unreachable: {ex}", "Check scheduler service is running.")
    except httpx.TimeoutException:
        return _err("Scheduler timed out.", "Try again.")
    except (json.JSONDecodeError, KeyError) as ex:
        return _err(f"Malformed response: {ex}", "Schema mismatch.")


async def list_scheduled(
    scheduler: httpx.AsyncClient = None,
    **kwargs,
) -> str:
    try:
        resp = await scheduler.get("/schedule")
        if not resp.is_success:
            body = resp.text
            return _err(f"Scheduler returned HTTP {resp.status_code}.", body[:500] + "…")

        return json.dumps(resp.json(), indent=2)

    except httpx.RequestError as ex:
        return _err(f"Scheduler unreachable: {ex}", "Check scheduler service is running.")
    except httpx.TimeoutException:
        return _err("Scheduler timed out.", "Try again.")
    except (json.JSONDecodeError, KeyError) as ex:
        return _err(f"Malformed response: {ex}", "Schema mismatch.")


async def unschedule_action(
    schedule_id: str,
    scheduler: httpx.AsyncClient = None,
    **kwargs,
) -> str:
    if not schedule_id:
        return _err("Missing 'schedule_id'.", "Provide the ID from schedule_action.")

    from urllib.parse import quote
    path = f"/schedule/{quote(schedule_id.strip())}"

    try:
        resp = await scheduler.delete(path)
        if resp.status_code == 404:
            return _err(f"Schedule '{schedule_id}' not found.", "May have already executed.")
        if not resp.is_success:
            body = resp.text
            return _err(f"Scheduler returned HTTP {resp.status_code}.", body[:500] + "…")

        print(f"[mcp-audit] UnscheduleAction: id={schedule_id}", file=sys.stderr)
        return json.dumps({"schedule_id": schedule_id, "cancelled": True}, indent=2)

    except httpx.RequestError as ex:
        return _err(f"Scheduler unreachable: {ex}", "Check scheduler service is running.")
    except httpx.TimeoutException:
        return _err("Scheduler timed out.", "Try again.")


def _err(error: str, hint: str) -> str:
    return json.dumps({"error": error, "hint": hint}, indent=2)
