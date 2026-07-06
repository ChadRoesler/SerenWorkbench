# ════════════════════════════════════════════════════════════════════════
#  WaitForServiceTool - wait for a service to be fully online.
#
#  Polls RuntimeHost until the requested service reports 'running' on at
#  least one node, or a timeout is reached. This prevents the LLM from
#  acting before the cluster has settled.
# ════════════════════════════════════════════════════════════════════════

from __future__ import annotations

import asyncio
import json
import sys
from typing import Optional

import httpx
from ...tool_config.mcp_config import McpConfig


TOOL_DEFINITION = {
    "name": "wait_for_service",
    "description": (
        "Polls RuntimeHost until the given service is running on at least "
        "one node. Returns when the service comes online or after a "
        "timeout (default 30s, configurable per-tool). Use this when the "
        "user says 'start service X' — call wait_for_service afterward to "
        "confirm readiness before doing anything that depends on it. "
        "Returns JSON with service name, online nodes, elapsed seconds, "
        "and whether it timed out."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "service": {
                "type": "string",
                "description": "Service name to wait for (e.g. 'llama', 'kokoro', 'comfy').",
            },
            "timeout": {
                "type": "integer",
                "description": "Max seconds to wait before giving up. Default 30.",
                "default": 30,
            },
        },
        "required": ["service"],
    },
}


async def wait_for_service(
    service: str,
    timeout: int = 30,
    runtime_host: httpx.AsyncClient = None,
    config: Optional[McpConfig] = None,
    **kwargs,
) -> str:
    if not service:
        return _err("Missing 'service' argument.", "Provide a service name.")

    section = config.for_tool("wait_for_service") if config else None
    max_timeout = section.get_int("timeout", 120) if section else 120
    poll_interval = section.get_float("poll_interval", 1.0) if section else 1.0

    n = min(timeout, max_timeout)
    deadline = asyncio.get_event_loop().time() + n
    polled = 0

    while True:
        try:
            resp = await runtime_host.get(f"/api/v1/service/{service}/status")
            if resp.is_success:
                data = resp.json()
                node = data.get("node")
                status = data.get("status", {})
                running = status.get("running", False)
                library_mode = status.get("library_mode", False)

                if running or library_mode:
                    elapsed = asyncio.get_event_loop().time() - (deadline - n)
                    print(
                        f"[mcp-audit] WaitForService: {service} online "
                        f"after {elapsed:.1f}s on node {node}",
                        file=sys.stderr,
                    )
                    return json.dumps({
                        "service": service,
                        "node": node,
                        "running": running,
                        "library_mode": library_mode,
                        "elapsed_seconds": round(elapsed, 1),
                        "timed_out": False,
                    }, indent=2)

        except (httpx.RequestError, httpx.TimeoutException, json.JSONDecodeError, KeyError):
            pass

        if asyncio.get_event_loop().time() >= deadline:
            elapsed = n
            return json.dumps({
                "service": service,
                "timed_out": True,
                "elapsed_seconds": elapsed,
                "note": f"Service '{service}' did not report running within {n}s.",
            }, indent=2)

        polled += 1
        await asyncio.sleep(poll_interval)


def _err(error: str, hint: str) -> str:
    return json.dumps({"error": error, "hint": hint}, indent=2)
