# ════════════════════════════════════════════════════════════════════════
#  ModelsTool - "what can you do" for the LLM.
#
#  Wraps the agent's per-service /models endpoints (llama, comfy, whisper).
# ════════════════════════════════════════════════════════════════════════

from __future__ import annotations

import json

import httpx


MODEL_CAPABLE_SERVICES = {"llama", "comfy", "whisper"}

TOOL_DEFINITION = {
    "name": "list_models",
    "description": (
        "Lists the models available for a given service on whichever node "
        "hosts it. Pass the service name: 'llama' (GGUF models for "
        "inference), 'comfy' (checkpoints for image generation), or "
        "'whisper' (transcription models). Returns JSON with the service's "
        "model list. Use this when the user asks 'what models do you have' "
        "or 'can you use X' - don't guess your own capabilities."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "service": {
                "type": "string",
                "description": "Service to list models for. One of: llama, comfy, whisper",
            },
        },
        "required": ["service"],
    },
}


async def list_models(
    service: str,
    runtime_host: httpx.AsyncClient = None,
    **kwargs,
) -> str:
    if not service:
        return _err("Missing 'service' argument.", "Provide one of: llama, comfy, whisper.")

    if service.lower() not in MODEL_CAPABLE_SERVICES:
        return _err(
            f"Service '{service}' doesn't have a /models endpoint.",
            f"Try one of: {', '.join(sorted(MODEL_CAPABLE_SERVICES))}",
        )

    try:
        resp = await runtime_host.get(f"/api/v1/service/{service}/manifest")
        if not resp.is_success:
            return _err(
                f"RuntimeHost returned HTTP {resp.status_code} for {service}.",
                "Service may not be installed on any online node.",
            )

        data = resp.json()
        node_name = data.get("node")

        if not node_name:
            return _err("Could not determine which node hosts the service.", "Missing 'node' field.")

        models_resp = await runtime_host.get(f"/api/v1/node/{node_name}/service/{service}/models")
        if models_resp.is_success:
            return json.dumps({
                "service": service,
                "node": node_name,
                "models": models_resp.json(),
            }, indent=2)

        return _err(
            f"Could not enumerate models for {service} on {node_name}.",
            f"/models endpoint returned HTTP {models_resp.status_code}.",
        )

    except httpx.RequestError as ex:
        return _err(f"RuntimeHost unreachable: {ex}", "Check RuntimeHost is running.")
    except httpx.TimeoutException:
        return _err("RuntimeHost timed out.", "Try again.")
    except (json.JSONDecodeError, KeyError) as ex:
        return _err(f"Malformed response: {ex}", "Schema mismatch.")


def _err(error: str, hint: str) -> str:
    return json.dumps({"error": error, "hint": hint}, indent=2)
