# ════════════════════════════════════════════════════════════════════════
#  ServiceControlTools - start/stop/restart cluster services.
#
#  The LLM can manage its own compute via RuntimeHost's service control
#  endpoints. Allowed: llama, kokoro, comfy, whisper, chroma, searxng.
# ════════════════════════════════════════════════════════════════════════

from __future__ import annotations

import json
import sys
from typing import Optional

import httpx


ALLOWED_SERVICES = {"llama", "kokoro", "comfy", "whisper", "chroma", "searxng"}

START_TOOL_DEF = {
    "name": "start_service",
    "description": (
        "Tells RuntimeHost to start the given service. Use this when the "
        "user asks you to turn something on ('start llama', 'run kokoro', "
        "'enable comfy'). The service must be installed on at least one "
        "node. Returns JSON with the service name and the node it started on. "
        "After calling this, wait_for_service to confirm it's actually online. "
        f"Allowed services: {', '.join(sorted(ALLOWED_SERVICES))}."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "service": {
                "type": "string",
                "description": "Service to start. One of: " + ", ".join(sorted(ALLOWED_SERVICES)),
            },
        },
        "required": ["service"],
    },
}

STOP_TOOL_DEF = {
    "name": "stop_service",
    "description": (
        "Tells RuntimeHost to stop the given service. Use this when the "
        "user asks you to turn something off ('stop llama', 'stop comfy'). "
        "Returns JSON with the service name and the node it stopped on. "
        f"Allowed services: {', '.join(sorted(ALLOWED_SERVICES))}."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "service": {
                "type": "string",
                "description": "Service to stop. One of: " + ", ".join(sorted(ALLOWED_SERVICES)),
            },
        },
        "required": ["service"],
    },
}

RESTART_TOOL_DEF = {
    "name": "restart_service",
    "description": (
        "Tells RuntimeHost to restart the given service. Use this when the "
        "user reports a service is acting up ('llama is broken', 'kokoro "
        "is stuck', 'restart comfy'). Returns JSON with the service name "
        "and the node it restarted on. After calling this, wait_for_service "
        "to confirm it's back online."
        f"Allowed services: {', '.join(sorted(ALLOWED_SERVICES))}."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "service": {
                "type": "string",
                "description": "Service to restart. One of: " + ", ".join(sorted(ALLOWED_SERVICES)),
            },
        },
        "required": ["service"],
    },
}


async def _control_service(service: str, action: str, runtime_host: httpx.AsyncClient = None) -> str:
    if not service:
        return _err("Missing 'service' argument.", "Provide a service name.")

    if service.lower() not in ALLOWED_SERVICES:
        return _err(
            f"Service '{service}' is not in the allowed-services list.",
            f"Allowed: {', '.join(sorted(ALLOWED_SERVICES))}",
        )

    try:
        resp = await runtime_host.post(f"/api/v1/service/{service}/{action}")
        if not resp.is_success:
            body = resp.text
            return _err(
                f"RuntimeHost returned HTTP {resp.status_code} for "
                f"{action} on {service}.",
                body[:500] + "…" if len(body) > 500 else body,
            )

        data = resp.json()
        node_name = data.get("node", "unknown")
        print(
            f"[mcp-audit] ServiceControl: {action} {service} on node {node_name}",
            file=sys.stderr,
        )
        return json.dumps({
            "action": action,
            "service": service,
            "node": node_name,
            "ok": True,
        }, indent=2)

    except httpx.RequestError as ex:
        return _err(f"RuntimeHost unreachable: {ex}", "Check RuntimeHost is running.")
    except httpx.TimeoutException:
        return _err(f"RuntimeHost timed out doing {action} on {service}.", "Try again.")
    except (json.JSONDecodeError, KeyError) as ex:
        return _err(f"Malformed response: {ex}", "Schema mismatch.")


async def start_service(service: str, runtime_host: httpx.AsyncClient = None, **kwargs) -> str:
    return await _control_service(service, "start", runtime_host)


async def stop_service(service: str, runtime_host: httpx.AsyncClient = None, **kwargs) -> str:
    return await _control_service(service, "stop", runtime_host)


async def restart_service(service: str, runtime_host: httpx.AsyncClient = None, **kwargs) -> str:
    return await _control_service(service, "restart", runtime_host)


def _err(error: str, hint: str) -> str:
    return json.dumps({"error": error, "hint": hint}, indent=2)
