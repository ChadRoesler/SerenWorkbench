# ════════════════════════════════════════════════════════════════════════
#  WebDispatcher - runs a kind=web tool by making an HTTP call.
#
#  TYPE-AWARE PARAMETER SUBSTITUTION
#
#  body_template is JSON. {param} substitution has to respect the param's
#  declared type or it'll produce invalid JSON:
#    String params  -> JSON-escape the value, substitute INSIDE the quotes
#                      the template already provides
#    Non-string     -> substitute literal (true/false/123/3.14)
#
#  PATH SUBSTITUTION is always scalar (URL-encode each param value).
#
#  BASE URL resolves: invoke.base_url > configuration.base_url > error.
#
#  HTTPX NOTE: AsyncClient.send() takes NO timeout kwarg (proven TypeError
#  on httpx 0.28) — per-request timeout goes through client.request(...).
#  We also build headers ONCE up front instead of poking req._content and
#  rebuilding the Request (a private attr that reads b'' — not None — on a
#  bodyless GET, which used to sneak Content-Type onto GETs).
# ════════════════════════════════════════════════════════════════════════

from __future__ import annotations

from typing import Dict, Optional

import httpx

from .manifest_models import ManifestConfiguration, ToolInvoke
from .param_subst import substitute_scalar, substitute_json_body

MAX_RESPONSE_CHARS = 16_000
DEFAULT_TIMEOUT_SECONDS = 30


async def invoke_web(
    invoke: ToolInvoke,
    file_config: ManifestConfiguration | None,
    tool_name: str,
    args: Dict[str, object],
    param_types: Dict[str, str],
    http_client: httpx.AsyncClient,
) -> dict:
    """Make an HTTP call per the tool's invoke config.

    Returns a dict suitable as an MCP CallToolResult.
    """
    # Resolve base URL
    base_url = invoke.base_url or (file_config.base_url if file_config else None)
    if not base_url:
        return _error("no base_url set on tool or file configuration.")

    if not invoke.path:
        return _error("no invoke.path set.")

    method = (invoke.method or "GET").strip().upper()

    # Path substitution - URL-encode each scalar value
    resolved_path = substitute_scalar(invoke.path, args, url_encode=True)

    # Build full URI
    from urllib.parse import urljoin
    full_url = urljoin(base_url.rstrip("/") + "/", resolved_path.lstrip("/"))

    # Body for verbs that take one
    body_json: Optional[str] = None
    if method in ("POST", "PUT", "PATCH") and invoke.body_template:
        try:
            body_json = substitute_json_body(invoke.body_template, args, param_types)
        except Exception as ex:
            return _error(
                f"body_template substitution failed: {ex}",
                hint="Check that string params live inside quotes in the template, "
                "and non-string params live outside quotes.",
            )

    # Headers - built once: JSON content type only when a body exists,
    # per-tool headers layered on top (they may override Content-Type).
    headers: Dict[str, str] = {}
    if body_json is not None:
        headers["Content-Type"] = "application/json"
    if invoke.headers:
        headers.update(invoke.headers)

    # Make the call
    try:
        resp = await http_client.request(
            method,
            full_url,
            content=body_json,
            headers=headers or None,
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )
    except httpx.TimeoutException:
        return _error(
            f"tool '{tool_name}' web call timed out.",
            hint=f"target: {method} {full_url}",
        )
    except httpx.RequestError as ex:
        return _error(
            f"tool '{tool_name}' web call failed: {ex}",
            hint=f"target: {method} {full_url}",
        )

    body = resp.text
    if len(body) > MAX_RESPONSE_CHARS:
        body = body[:MAX_RESPONSE_CHARS] + "\n…[response truncated]"

    if not resp.is_success:
        return _error(
            f"tool '{tool_name}' got HTTP {resp.status_code} from {method} {full_url}.",
            hint=f"body: {body}",
        )

    return {
        "content": [{"type": "text", "text": body if body else "(empty response)"}],
    }


def _error(msg: str, hint: str | None = None) -> dict:
    text = f"{msg}\nhint: {hint}" if hint else msg
    return {"is_error": True, "content": [{"type": "text", "text": text}]}
