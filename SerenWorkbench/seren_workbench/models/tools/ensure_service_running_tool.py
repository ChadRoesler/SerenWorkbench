# ═══════════════════════════════════════════════════════════════
#  EnsureServiceRunningTool - "I need this service up", and "I am done".
#
#  ensure_service_running / release_service.
#
#  THROUGH LODESTAR'S LEASES (seren_sinew.orchestration, 6 Oct 2026). This
#  tool used to check a service's status, POST /start and poll - which
#  started a service nobody would ever stop, and knew nothing about who
#  else was using it: the hippocampus could finish a sleep and stop the
#  llama a model had just been handed. Now it says what everything else in
#  the stack says: one `ensure` in the Workbench's name, one answer when the
#  service is READY (Lodestar picks the node, the Observatory starts it and
#  waits for its health check), and a `release` when done. The service is
#  stopped when its last holder lets go, if an ensure is what started it.
#
#  A Lodestar from before ensure/release (it answers 404) is driven the old
#  way - status, start, poll - so a Workbench newer than its cluster head
#  still works; the answer says "leased": false.
#
#  A hold the model forgets is let go by the Workbench itself: see holds.py.
# ═══════════════════════════════════════════════════════════════

from __future__ import annotations

import asyncio
import json
import sys
from typing import Optional

import httpx
from ...holds import Holds
from ...tool_config.mcp_config import McpConfig

# Which toolbox these land in on the dashboard. Derivation would put
# this module in a box of its own; this says otherwise. Per-tool
# "toolbox" keys in a TOOL_DEF override even this.
TOOLBOX = "Services"


ALLOWED_SERVICES = {"llama", "kokoro", "comfy", "whisper", "chroma", "searxng"}

DEFAULT_TIMEOUT = 120      # a model loading takes a minute or two, not thirty seconds
DEFAULT_MAX_TIMEOUT = 300  # tools.ensure_service_running.timeout raises or lowers the ceiling

ENSURE_SERVICE_RUNNING_TOOL_DEF = {
    "name": "ensure_service_running",
    "description": (
        "Make a service ready to use, and hold it while you use it. Asks "
        "Lodestar, which picks the node, starts the service if it is not "
        "running and answers once it passes its health check - so one call "
        "replaces check + start + wait. Returns the node and the address to "
        "send requests to.\n"
        "\n"
        "The service is HELD in this Workbench's name until you call "
        "release_service. While it is held nothing else's release will stop "
        "it under you. Release it when you are done: a model left loaded on "
        "a small GPU is memory the next job cannot have. A hold you forget "
        "is let go for you after a while (the answer says when); calling "
        "this again for the same service renews it.\n"
        "\n"
        f"Allowed services: {', '.join(sorted(ALLOWED_SERVICES))}."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "service": {
                "type": "string",
                "description": "Service to make ready. One of: " + ", ".join(sorted(ALLOWED_SERVICES)),
            },
            "timeout": {
                "type": "integer",
                "description": f"Max seconds to wait for it to be ready. Default {DEFAULT_TIMEOUT}; "
                               "a large model on a small board can need more.",
                "default": DEFAULT_TIMEOUT,
            },
            "reason": {
                "type": "string",
                "description": "Optional. What you want it for, in a few words - it goes in the "
                               "cluster's logs beside the lease.",
            },
        },
        "required": ["service"],
    },
}

RELEASE_SERVICE_TOOL_DEF = {
    "name": "release_service",
    "description": (
        "Say you are done with a service you asked for with "
        "ensure_service_running. Lodestar drops this Workbench's hold; the "
        "service is stopped only if nobody else holds it AND an ensure is "
        "what started it - one that was already running when you asked "
        "belongs to whoever started it and is left alone. Releasing "
        "something you do not hold is harmless."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "service": {
                "type": "string",
                "description": "Service to let go of. One of: " + ", ".join(sorted(ALLOWED_SERVICES)),
            },
            "reason": {"type": "string", "description": "Optional. A few words for the logs."},
        },
        "required": ["service"],
    },
}


def _check_service(service: str) -> Optional[str]:
    if not service:
        return _err("Missing 'service' argument.", "Provide a service name.")
    if service.lower() not in ALLOWED_SERVICES:
        return _err(
            f"Service '{service}' is not in the allowed-services list.",
            f"Allowed: {', '.join(sorted(ALLOWED_SERVICES))}",
        )
    return None


async def ensure_service_running(
    service: str,
    timeout: int = DEFAULT_TIMEOUT,
    reason: str = "",
    runtime_host: httpx.AsyncClient = None,
    config: Optional[McpConfig] = None,
    holds: Optional[Holds] = None,
    **kwargs,
) -> str:
    bad = _check_service(service)
    if bad:
        return bad
    service = service.lower()

    section = config.for_tool("ensure_service_running") if config else None
    max_timeout = section.get_int("timeout", DEFAULT_MAX_TIMEOUT) if section else DEFAULT_MAX_TIMEOUT
    try:
        n = max(1, min(int(timeout), max_timeout))
    except (TypeError, ValueError):
        n = min(DEFAULT_TIMEOUT, max_timeout)
    holds = holds if holds is not None else Holds()

    from seren_sinew.orchestration import EnsureRequest, EnsureResult
    req = EnsureRequest(holder=holds.holder, reason=(reason or "asked for through the Workbench")[:200],
                        wait_seconds=float(n))
    try:
        # The injected client's own timeout is a few seconds; this one call
        # waits for a model to load, plus the hops in between.
        resp = await runtime_host.post(f"/api/v1/service/{service}/ensure", json=req.to_dict(),
                                       timeout=n + 20)
        if resp.status_code in (404, 405):
            return await _ensure_without_leases(service, n, runtime_host, section)
        if not resp.is_success:
            body = resp.text
            return _err(f"Lodestar returned HTTP {resp.status_code} for ensure '{service}'.",
                        body[:500] + "…" if len(body) > 500 else body)
        out = EnsureResult.from_dict(resp.json())
    except httpx.TimeoutException:
        return _err(f"Lodestar did not answer within {n + 20}s.",
                    "The service may still be starting: wait_for_service, or ask again.")
    except httpx.RequestError as ex:
        return _err(f"Lodestar unreachable: {ex}", "Check Lodestar is running.")
    except (json.JSONDecodeError, KeyError, TypeError) as ex:
        return _err(f"Malformed response: {ex}", "Schema mismatch.")

    if not (out.ok and out.ready):
        where = f" on {out.node}" if out.node else ""
        return _err(out.error or f"'{service}'{where} is not ready.",
                    "Nothing is held. get_recent_logs shows why a service did not come up; "
                    "get_cluster_status shows which nodes are online.")

    holds.note(service, out.node)
    print(f"[mcp-audit] EnsureRunning: {service} ready on {out.node} after {out.waited_seconds:.1f}s "
          f"(started={out.started}, held as {holds.holder})", file=sys.stderr)
    answer = {
        "service": service,
        "action": "started" if out.started else "already_running",
        "node": out.node,
        "running": True,
        "base_url": out.base_url,
        "elapsed_seconds": out.waited_seconds,
        "leased": True,
        "held_as": holds.holder,
        "holders": out.holders,
        "when_done": f"release_service('{service}')",
    }
    if holds.hold_minutes > 0:
        answer["let_go_after_minutes"] = holds.hold_minutes
    return json.dumps(answer, indent=2)


async def release_service(
    service: str,
    reason: str = "",
    runtime_host: httpx.AsyncClient = None,
    holds: Optional[Holds] = None,
    **kwargs,
) -> str:
    bad = _check_service(service)
    if bad:
        return bad
    service = service.lower()
    holds = holds if holds is not None else Holds()

    got = await holds.release(runtime_host, service, reason or "released through the Workbench")
    if not got.get("leases", True):
        return json.dumps({
            "service": service, "released": False, "stopped": False,
            "note": "This Lodestar keeps no leases (it is older than ensure/release), so there was "
                    "nothing to let go of and the service is left running.",
        }, indent=2)
    if not got.get("ok"):
        return _err(got.get("error") or f"Lodestar could not release '{service}'.",
                    "This Workbench no longer counts it as held. get_cluster_status shows whether it is still up.")
    return json.dumps({
        "service": service,
        "released": True,
        "stopped": bool(got.get("stopped")),
        "node": got.get("node") or None,
        "still_held_by": got.get("holders") or [],
    }, indent=2)


async def _ensure_without_leases(service: str, n: int, runtime_host: httpx.AsyncClient, section) -> str:
    """A Lodestar from before ensure/release: status, start, poll. Nothing is
    held, and nothing will stop the service afterwards."""
    try:
        # Check current status
        status_resp = await runtime_host.get(f"/api/v1/service/{service}/status")
        if not status_resp.is_success:
            return _err(
                f"Lodestar returned HTTP {status_resp.status_code} checking {service}.",
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
                "leased": False,
            }, indent=2)

        # Start it
        start_resp = await runtime_host.post(f"/api/v1/service/{service}/start")
        if not start_resp.is_success:
            body = start_resp.text
            return _err(
                f"Lodestar returned HTTP {start_resp.status_code} starting {service}.",
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
                        f"{check_data.get('node')} after {elapsed:.1f}s (no leases)",
                        file=sys.stderr,
                    )
                    return json.dumps({
                        "service": service,
                        "action": "started",
                        "node": check_data.get("node"),
                        "running": s.get("running", False),
                        "library_mode": s.get("library_mode", False),
                        "elapsed_seconds": round(elapsed, 1),
                        "leased": False,
                    }, indent=2)

            if asyncio.get_event_loop().time() >= deadline:
                return _err(
                    f"'{service}' was started on {started_node} and did not report running within {n}s.",
                    "It may still be loading: wait_for_service, or get_recent_logs to see why.",
                )

            await asyncio.sleep(poll_interval)

    except httpx.TimeoutException:
        return _err("Lodestar timed out.", "Try again.")
    except httpx.RequestError as ex:
        return _err(f"Lodestar unreachable: {ex}", "Check Lodestar is running.")
    except (json.JSONDecodeError, KeyError) as ex:
        return _err(f"Malformed response: {ex}", "Schema mismatch.")


def _err(error: str, hint: str) -> str:
    return json.dumps({"error": error, "hint": hint}, indent=2)
