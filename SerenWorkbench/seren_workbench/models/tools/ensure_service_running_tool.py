# ════════════════════════════════════════════════════════════════════════
#  EnsureServiceRunningTool - start a service if it's not running.
#
#  Combines check + start + wait into a single tool call, so the LLM
#  doesn't need to chain start_service + wait_for_service manually.
# ════════════════════════════════════════════════════════════════════════

from __future__ import annotations

import asyncio
import json
import sys
from typing import Optional

import httpx
from ...tool_config.mcp_config import McpConfig


ALLOWED_SERVICES = {"llama", "kokoro", "comfy", "whisper", "chroma", "searxng"}

TOOL_DEFINITION = {
    "name": "ensure_service_running",
    "description": (
        "Ensures a service is running: checks status, starts it if "
        "stopped, and waits for it to report online. This is a combined "
        "operation that replaces the pattern of check + start_service + "
        "wait_for_service. Returns JSON with the service name, whether "
        "it was already running, and the wait time. "
        f"Allowed services: {', '.join(sorted(ALLOWED_SERVICES))}."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "service": {
                "type": "string",
                "description": "Service to ensure running. One of: " + ", ".join(sorted(ALLOWED_SERVICES)),
            },
            "timeout": {
                "type": "integer",
                "description": "Max seconds to wait for the service to come online. Default 30.",
                "default": 30,
            },
        },
        "required": ["service"],
    },
}


async def ensure_service_running(
    service: str,
    timeout: int = 30,
    runtime_host: httpx.AsyncClient = None,
    config: Optional[McpConfig] = None,
    **kwargs,
) -> str:
    if not service:
        return _err("Missing 'service' argument.", "Provide a service name.")

    if service.lower() not in ALLOWED_SERVICES:
        return _err(
            f"Service '{service}' is not in the allowed-services list.",
            f"Allowed: {', '.join(sorted(ALLOWED_SERVICES))}",
        )

    section = config.for_tool("ensure_service_running") if config else None
    max_timeout = section.get_int("timeout", 60) if section else 60
    n = min(timeout, max_timeout)

    try:
        # Check current status
        status_resp = await runtime_host.get(f"/api/v1/service/{service}/status")
        if not status_resp.is_success:
            return _err(
                f"RuntimeHost returned HTTP {status_resp.status_code} checking {service}.",
                "Cluster head may be down.",
            )

        status_data = status_resp.json()
        current_status = status_data.get("status", {})
        running = current_status.get("running", False)
        library_mode = current_status.get("library_mode", False)

        if running or library_mode:
            return json.dumps({
                "service": service,
                "action": "already_running",
                "node": status_data.get("node"),
                "running": running,
                "library_mode": library_mode,
                "elapsed_seconds": 0,
            }, indent=2)

        # Start it
        start_resp = await runtime_host.post(f"/api/v1/service/{service}/start")
        if not start_resp.is_success:
            body = start_resp.text
            return _err(
                f"RuntimeHost returned HTTP {start_resp.status_code} starting {service}.",
                body[:500] + "…" if len(body) > 500 else body,
            )

        start_data = start_resp.json()
        started_node = start_data.get("node", "unknown")

        # Wait for it
        poll_interval = section.get_float("poll_interval", 1.0) if section else 1.0
        deadline = asyncio.get_event_loop().time() + n

        while True:
            check_resp = await runtime_host.get(f"/api/v1/service/{service}/status")
            if check_resp.is_success:
                check_data = check_resp.json()
                s = check_data.get("status", {})
                if s.get("running") or s.get("library_mode"):
                    elapsed = asyncio.get_event_loop().time() - (deadline - n)
                    print(
                        f"[mcp-audit] EnsureRunning: {service} started on "
                        f"{check_data.get('node')} after {elapsed:.1f}s",
                        file=sys.stderr,
                    )
                    return json.dumps({
                        "service": service,
                        "action": "started",
                        "node": check_data.get("node"),
                        "running": s.get("running", False),
                        "library_mode": s.get("library_mode", False),
                        "elapsed_seconds": round(elapsed, 1),
                    }, indent=2)

            if asyncio.get_event_loop().time() >= deadline:
                return json.dumps({
                    "service": service,
                    "action": "started_but_not_ready",
                    "node": started_node,
                    "timed_out": True,
                    "elapsed_seconds": n,
                    "note": f"Service '{service}' started but did not report running within {n}s.",
                }, indent=2)

            await asyncio.sleep(poll_interval)

    except httpx.RequestError as ex:
        return _err(f"RuntimeHost unreachable: {ex}", "Check RuntimeHost is running.")
    except httpx.TimeoutException:
        return _err("RuntimeHost timed out.", "Try again.")
    except (json.JSONDecodeError, KeyError) as ex:
        return _err(f"Malformed response: {ex}", "Schema mismatch.")


def _err(error: str, hint: str) -> str:
    return json.dumps({"error": error, "hint": hint}, indent=2)
