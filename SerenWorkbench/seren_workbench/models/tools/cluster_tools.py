# ════════════════════════════════════════════════════════════════════════
#  ClusterTools - let the LLM manage the cluster topology.
#
#  Enumerate, inspect, and query nodes.
# ════════════════════════════════════════════════════════════════════════

from __future__ import annotations

import json
from typing import Optional

import httpx


TOOL_DEFINITION = {
    "name": "get_cluster_status",
    "description": (
        "Returns the full cluster topology from RuntimeHost. Use this "
        "when the user asks about the cluster ('what nodes are there', "
        "'is everything online', 'show me the cluster'). Returns JSON "
        "with node_count, online_count, and a 'nodes' array - each node "
        "has its name, nickname, online flag, services, thermal, and "
        "memory info. This is the same data get_self_context uses."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}


async def get_cluster_status(
    runtime_host: httpx.AsyncClient = None,
    **kwargs,
) -> str:
    try:
        resp = await runtime_host.get("/api/v1/system/status")
        if not resp.is_success:
            return _err(
                f"RuntimeHost returned HTTP {resp.status_code}.",
                "Cluster head may be down.",
            )

        data = resp.json()

        # Summarize each node
        nodes = data.get("nodes", [])
        summaries = []
        for node in nodes:
            node_name = node.get("name", "?")
            node_nickname = node.get("nickname", node_name)
            online = node.get("online", False)
            is_host = node.get("is_host", False)

            # Services
            services_detail = node.get("services_detail")
            services = []
            if online and isinstance(services_detail, dict):
                services = list(services_detail.keys())

            # Thermal
            thermal = node.get("thermal")
            max_temp_c = None
            if isinstance(thermal, dict):
                max_temp_c = thermal.get("max_temp_c")

            # Memory
            mem_pct = None
            agent_node = node.get("agent_node")
            if isinstance(agent_node, dict):
                runtime = agent_node.get("runtime")
                if isinstance(runtime, dict):
                    mem_pct = runtime.get("memory_pct_used")

            summaries.append({
                "name": node_name,
                "nickname": node_nickname,
                "online": online,
                "is_host": is_host,
                "services": services,
                "max_temp_c": max_temp_c,
                "memory_pct_used": int(mem_pct) if mem_pct is not None else None,
            })

        return json.dumps({
            "node_count": data.get("node_count", 0),
            "online_count": data.get("online_count", 0),
            "nodes": summaries,
        }, indent=2)

    except httpx.RequestError as ex:
        return _err(f"RuntimeHost unreachable: {ex}", "Check RuntimeHost is running.")
    except httpx.TimeoutException:
        return _err("RuntimeHost timed out.", "Try again.")
    except (json.JSONDecodeError, KeyError) as ex:
        return _err(f"Malformed response: {ex}", "Schema mismatch.")


def _err(error: str, hint: str) -> str:
    return json.dumps({"error": error, "hint": hint}, indent=2)
