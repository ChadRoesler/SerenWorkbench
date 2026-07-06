# ════════════════════════════════════════════════════════════════════════
#  WhichModelTool - tell the LLM what inference model is loaded.
#
#  When the user asks 'what model are you' or 'which llama are you
#  running', this fetches the active model from the llama service.
# ════════════════════════════════════════════════════════════════════════

from __future__ import annotations

import json
from typing import Optional

import httpx


TOOL_DEFINITION = {
    "name": "which_model",
    "description": (
        "Returns the currently-loaded inference model (the GGUF file "
        "loaded into llama.cpp). Use this when the user asks 'what "
        "model are you' or 'which llama are you running'. Fetches from "
        "the llama service's status. Returns JSON with model_name, "
        "model_path, node, and quant_type if available."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}


async def which_model(
    runtime_host: httpx.AsyncClient = None,
    **kwargs,
) -> str:
    try:
        resp = await runtime_host.get("/api/v1/service/llama/status")
        if not resp.is_success:
            return _err(
                f"RuntimeHost returned HTTP {resp.status_code} for llama.",
                "Llama service may not be running.",
            )

        data = resp.json()
        node = data.get("node")
        status = data.get("status", {})

        model_name = status.get("model_name") or status.get("active_model") or "unknown"
        model_path = status.get("model_path")
        quant_type = status.get("quant_type")

        result = {
            "model_name": model_name,
            "node": node,
        }
        if model_path:
            result["model_path"] = model_path
        if quant_type:
            result["quant_type"] = quant_type

        return json.dumps(result, indent=2)

    except httpx.RequestError as ex:
        return _err(f"RuntimeHost unreachable: {ex}", "Check RuntimeHost is running.")
    except httpx.TimeoutException:
        return _err("RuntimeHost timed out.", "Try again.")
    except (json.JSONDecodeError, KeyError) as ex:
        return _err(f"Malformed response: {ex}", "Schema mismatch.")


def _err(error: str, hint: str) -> str:
    return json.dumps({"error": error, "hint": hint}, indent=2)
