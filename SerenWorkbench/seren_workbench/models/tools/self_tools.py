# ════════════════════════════════════════════════════════════════════════
#  SelfTool - "who am I?" for the LLM.
#
#  When a fresh instance comes online, it has the system prompt and
#  whatever the user just typed. This tool collapses all grounding info
#  into one call.
# ════════════════════════════════════════════════════════════════════════

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

import httpx


TOOL_DEFINITION = {
    "name": "get_self_context",
    "description": (
        "Returns a snapshot of who/where you are: which model you're running "
        "as, which node, cluster health, current capabilities, time. Call "
        "this at the start of conversations where you need to orient "
        "yourself, or any time the user asks about your state ('how are "
        "you', 'what can you do right now', 'where are you running'). "
        "Returns both structured JSON and a 'narrative' field with a "
        "short prose self-description suitable for thinking-out-loud."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}


async def get_self_context(runtime_host: httpx.AsyncClient, **kwargs) -> str:
    try:
        resp = await runtime_host.get("/api/v1/system/status")
        if not resp.is_success:
            return _err(
                f"Could not orient - RuntimeHost returned HTTP {resp.status_code}.",
                "The cluster head isn't reachable.",
            )

        root = resp.json()
        narrative, structured = _synthesize_self_context(root)

        return json.dumps({
            "narrative": narrative,
            "structured": structured,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }, indent=2)

    except httpx.RequestError as ex:
        return _err(f"RuntimeHost unreachable: {ex}", "Operating blind.")
    except httpx.TimeoutException:
        return _err("Self-context query timed out.", "Cluster head is slow or down.")
    except (json.JSONDecodeError, KeyError) as ex:
        return _err(f"Self-context malformed: {ex}", "Schema mismatch.")


def _synthesize_self_context(root: dict) -> tuple:
    node_count = root.get("node_count", 0)
    online_count = root.get("online_count", 0)

    node_summaries = []
    available_capabilities = set()
    degraded_services = []
    max_temp_c = None
    hottest_node = None

    nodes = root.get("nodes", [])
    for node in nodes:
        node_name = node.get("name", "?")
        node_nickname = node.get("nickname", node_name)
        online = node.get("online", False)
        is_host = node.get("is_host", False)

        # Thermal
        node_max_temp = None
        thermal = node.get("thermal")
        if isinstance(thermal, dict):
            node_max_temp = thermal.get("max_temp_c")
            if node_max_temp is not None:
                if max_temp_c is None or node_max_temp > max_temp_c:
                    max_temp_c = node_max_temp
                    hottest_node = node_nickname

        # Memory
        mem_pct = None
        agent_node = node.get("agent_node")
        if isinstance(agent_node, dict):
            runtime = agent_node.get("runtime")
            if isinstance(runtime, dict):
                mem_pct = runtime.get("memory_pct_used")

        # Services
        node_services = []
        services_detail = node.get("services_detail")
        if online and isinstance(services_detail, dict):
            for svc_name, svc_info in services_detail.items():
                node_services.append(svc_name)
                available_capabilities.add(svc_name)
                status = svc_info.get("status", {}) if isinstance(svc_info, dict) else {}
                running = status.get("running", False)
                library_mode = status.get("library_mode", False)
                if not running and not library_mode:
                    degraded_services.append(f"{svc_name}@{node_name}")

        node_summaries.append({
            "name": node_name,
            "nickname": node_nickname,
            "online": online,
            "is_host": is_host,
            "max_temp_c": node_max_temp,
            "memory_pct_used": int(mem_pct) if mem_pct is not None else None,
            "services": node_services,
        })

    # Build narrative
    parts = [f"You're running across the Seren cluster — {online_count} of {node_count} nodes online."]

    if online_count == 0:
        parts.append("The cluster is unreachable; you're operating from MCP alone.")
    elif online_count < node_count:
        offline = [n["name"] for n in node_summaries if not n["online"]]
        parts.append(f"Offline: {', '.join(offline)}.")

    if available_capabilities:
        parts.append(f"You currently have access to: {', '.join(sorted(available_capabilities))}.")

    if degraded_services:
        parts.append(f"Degraded (installed but not running): {', '.join(degraded_services)}.")

    if max_temp_c is not None and hottest_node is not None:
        feeling = (
            "cool and comfortable" if max_temp_c < 50 else
            "warm but fine" if max_temp_c < 65 else
            "running hot — noticeable load" if max_temp_c < 75 else
            "uncomfortably warm, watch for throttling" if max_temp_c < 85 else
            "very hot, likely thermally throttled"
        )
        parts.append(f"Thermally you're {feeling} ({max_temp_c:.1f}°C max, on {hottest_node}).")

    now = datetime.now()
    parts.append(f"Local time on the cluster head is {now.strftime('%A, %B %d at %I:%M %p')}.")

    return (" ".join(parts), {
        "node_count": node_count,
        "online_count": online_count,
        "nodes": node_summaries,
        "capabilities": sorted(available_capabilities),
        "degraded": degraded_services,
        "max_temp_c": max_temp_c,
        "hottest_node": hottest_node,
    })


def _err(error: str, hint: str) -> str:
    return json.dumps({"error": error, "hint": hint}, indent=2)
