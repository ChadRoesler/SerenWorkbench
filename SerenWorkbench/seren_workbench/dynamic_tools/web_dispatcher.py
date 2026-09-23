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

import ipaddress
import socket
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse

import httpx

from .manifest_models import ManifestConfiguration, ToolInvoke
from .param_subst import substitute_scalar, substitute_json_body

MAX_RESPONSE_CHARS = 16_000
DEFAULT_TIMEOUT_SECONDS = 30

_LOOPBACK_NAMES = {"localhost", "localhost.localdomain", "0.0.0.0", "::", "::1", "127.0.0.1"}


def targets_this_workbench(url: str, own_host: str, own_port: int) -> bool:
    """True when *url* points at the Workbench's own listener.

    A web tool that calls back into this process is the model reaching the
    operator-only routes through a tool: POST /proposals/{id}/approve from
    a "helper" a reviewer waved through is the model approving its own
    next proposal. Builtin tools are how the model reaches the Workbench;
    a manifest tool never needs to.

    The test is the port plus "is that host me": a loopback or unspecified
    address, the bound host, or this machine's own name. Not a full
    interface enumeration - it does not have to be, because a manifest
    pointing at this box's LAN address on this port is the same author's
    same tool and the operator-side check at propose time catches it by
    the same rule.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return False
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if int(port) != int(own_port):
        return False
    if host in _LOOPBACK_NAMES or host == (own_host or "").strip().lower():
        return True
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_loopback or ip.is_unspecified:
            return True
    except ValueError:
        pass
    try:
        return host in {socket.gethostname().lower(), socket.getfqdn().lower()}
    except OSError:
        return False


def _has_header(headers: Optional[Dict[str, str]], name: str) -> bool:
    return any(k.lower() == name.lower() for k in (headers or {}))


async def invoke_web(
    invoke: ToolInvoke,
    file_config: ManifestConfiguration | None,
    tool_name: str,
    args: Dict[str, object],
    param_types: Dict[str, str],
    http_client: httpx.AsyncClient,
    self_addr: Optional[Tuple[str, int]] = None,
) -> dict:
    """Make an HTTP call per the tool's invoke config.

    Returns a dict suitable as an MCP CallToolResult. *self_addr* is this
    Workbench's own (host, port); a target that resolves to it is refused.
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

    if self_addr is not None and targets_this_workbench(full_url, *self_addr):
        return _error(
            f"tool '{tool_name}' points at this Workbench itself ({full_url}); refused.",
            hint="A manifest tool may not call back into the Workbench - that is the "
                 "model reaching the operator routes through a tool. The builtin "
                 "tools are the model's way to the Workbench.",
        )

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
    #
    # Header VALUES substitute too. They used to be the one templated field
    # that didn't, so `Authorization: "Bearer {token}"` sent the four
    # characters "{tok..." literally - no error, no warning, just a 401 from
    # the far end and nothing anywhere to explain it. Header names are left
    # alone deliberately: a templated header NAME is not a real use case and
    # allowing it would let a parameter value inject a header.
    headers: Dict[str, str] = {}
    if body_json is not None:
        headers["Content-Type"] = "application/json"
    if invoke.headers:
        for hname, hvalue in invoke.headers.items():
            headers[hname] = substitute_scalar(str(hvalue), args)
    # The file's credential, resolved NOW rather than at load: nothing holds
    # the secret between calls, and rotating the env var takes effect on
    # the next call. A tool that sets its own Authorization header keeps it.
    if file_config is not None and file_config.has_bearer and not _has_header(headers, "Authorization"):
        from seren_meninges.credentials import resolve_token
        token = resolve_token(inline=file_config.bearer_token or None,
                              keyring_ref=file_config.bearer_token_keyring or None,
                              env_var=file_config.bearer_token_env or None)
        if token:
            headers["Authorization"] = f"Bearer {token}"

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
