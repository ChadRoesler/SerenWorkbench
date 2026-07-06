# ════════════════════════════════════════════════════════════════════════
#  LogsTool - let the LLM debug itself.
#
#  When the user says "you're being slow" or "you returned garbage," the
#  LLM can directly inspect the tail of any service's log file.
# ════════════════════════════════════════════════════════════════════════

from __future__ import annotations

import json
import re
from typing import Optional

import httpx


ALLOWED_SERVICES = {"llama", "kokoro", "comfy", "whisper", "chroma", "searxng"}
DEFAULT_LINES = 50
MAX_LINES = 200

TOOL_DEFINITION = {
    "name": "get_recent_logs",
    "description": (
        "Fetches the last N lines of a service's log file. Use this when "
        "the user reports something wrong with a specific service ('you're "
        "slow', 'image gen failed', 'kokoro broke') - read the logs first, "
        "then explain what you found. Allowed services: llama, kokoro, "
        "comfy, whisper, chroma, searxng. Default 50 lines, max 200. "
        "Returns JSON with 'lines' array. Auth tokens are redacted."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "service": {
                "type": "string",
                "description": "Service name to read logs from. One of: llama, kokoro, comfy, whisper, chroma, searxng.",
            },
            "lines": {
                "type": "integer",
                "description": "How many lines from the tail. Default 50, max 200.",
                "default": 50,
            },
        },
        "required": ["service"],
    },
}


async def get_recent_logs(
    service: str,
    lines: int = DEFAULT_LINES,
    runtime_host: httpx.AsyncClient = None,
    **kwargs,
) -> str:
    if not service:
        return _err("Missing 'service' argument.", "Provide a service name.")

    if service.lower() not in ALLOWED_SERVICES:
        return _err(
            f"Service '{service}' is not in the readable-logs allowlist.",
            f"Allowed: {', '.join(sorted(ALLOWED_SERVICES))}",
        )

    n = max(1, min(lines if lines > 0 else DEFAULT_LINES, MAX_LINES))

    try:
        resp = await runtime_host.get(f"/api/v1/service/{service}/logs?lines={n}")
        if not resp.is_success:
            return _err(
                f"RuntimeHost returned HTTP {resp.status_code} for {service} logs.",
                "Service may not be installed, or log file may be missing.",
            )

        data = resp.json()
        logs = data.get("logs", {})
        node = data.get("node")
        log_path = logs.get("log_path")

        raw_lines = logs.get("lines", [])
        redacted = [_redact_secrets(str(line)) for line in raw_lines]

        return json.dumps({
            "service": service,
            "node": node,
            "log_path": log_path,
            "line_count": len(redacted),
            "lines": redacted,
        }, indent=2)

    except httpx.RequestError as ex:
        return _err(f"RuntimeHost unreachable: {ex}", "Check RuntimeHost is running.")
    except httpx.TimeoutException:
        return _err("RuntimeHost timed out fetching logs.", "Try again.")
    except (json.JSONDecodeError, KeyError) as ex:
        return _err(f"Malformed response: {ex}", "Schema mismatch.")


# -- Redaction --

_BEARER_RE = re.compile(r"(?i)(bearer\s+)([A-Za-z0-9_\-\.]{16,})")
_AUTH_HEADER_RE = re.compile(r"(?i)(authorization\s*[:=]\s*)([A-Za-z0-9_\-\.]{16,})")
_LONG_HEX_RE = re.compile(r"\b[A-Fa-f0-9]{32,}\b")


def _redact_secrets(line: str) -> str:
    line = _BEARER_RE.sub(r"\1[REDACTED]", line)
    line = _AUTH_HEADER_RE.sub(r"\1[REDACTED]", line)
    line = _LONG_HEX_RE.sub("[REDACTED-HEX]", line)
    return line


def _err(error: str, hint: str) -> str:
    return json.dumps({"error": error, "hint": hint}, indent=2)
