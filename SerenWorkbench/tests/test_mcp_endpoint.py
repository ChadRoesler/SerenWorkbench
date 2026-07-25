"""
Functional tests for the mounted MCP HTTP endpoint.

Drives an actual JSON-RPC ``initialize`` through the live app (with the
lifespan entered, the way uvicorn runs it) so the whole path is exercised —
then goes further: ``tools/list`` with the negotiated session, asserting the
REAL tool surface (all 24 builtins present, schemas carrying true parameter
names — the regression tests for the endswith-discovery bug, the _stub
collapse, and the **kwargs schema bug).

The StreamableHTTP transport frames replies as SSE events (``event: message\n
data: {...}\n\n``), NOT direct JSON. We parse the data: line when we need
structure, substring-check when we don't.
"""
from __future__ import annotations

import json

import pytest

try:
    import mcp  # noqa: F401
    _mcp_available = True
except ImportError:
    _mcp_available = False

pytestmark = pytest.mark.skipif(
    not _mcp_available, reason="mcp extras not installed"
)

from seren_workbench.config import WorkbenchConfig, load_config


# StreamableHTTP requires BOTH content types advertised or it 406s.
_MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}

_INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "seren-test", "version": "0"},
    },
}

_INITIALIZED = {
    "jsonrpc": "2.0",
    "method": "notifications/initialized",
}

_TOOLS_LIST = {
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/list",
    "params": {},
}


def _body_text(resp) -> str:
    # streamable-HTTP frames the reply as an SSE event ("event: message\n
    # data: {...}"); just return raw text and let callers substring/parse.
    return resp.text


def _parse_sse_json(resp) -> dict:
    """Extract the JSON payload from an SSE-framed (or plain JSON) reply."""
    text = resp.text
    for line in text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[len("data:"):].strip())
    return json.loads(text)


def _open_session(client):
    """initialize + initialized-notification; return the session headers."""
    r = client.post("/mcp", json=_INIT, headers=_MCP_HEADERS)
    assert r.status_code == 200, f"initialize failed: {r.status_code}: {r.text[:300]}"
    headers = dict(_MCP_HEADERS)
    session_id = r.headers.get("mcp-session-id")
    if session_id:
        headers["mcp-session-id"] = session_id
    n = client.post("/mcp", json=_INITIALIZED, headers=headers)
    assert n.status_code in (200, 202), f"initialized notification failed: {n.status_code}"
    return headers


# ── Handshake ───────────────────────────────────────────────────────────

def test_initialize_handshake_succeeds_at_mcp(client):
    """POST initialize to /mcp returns 200 with a JSON-RPC result."""
    r = client.post("/mcp", json=_INIT, headers=_MCP_HEADERS)
    assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text[:300]}"
    text = _body_text(r)
    assert "result" in text and "protocolVersion" in text, \
        f"no initialize result in body: {text[:300]}"


def test_mcp_trailing_slash_also_works(client):
    """Trailing-slash form /mcp/ must resolve too, not 404."""
    r = client.post("/mcp/", json=_INIT, headers=_MCP_HEADERS,
                    follow_redirects=True)
    assert r.status_code == 200, f"trailing-slash form 404'd: {r.status_code}"


def test_initialize_includes_capabilities(client):
    """The initialize result advertises tool-list capabilities."""
    r = client.post("/mcp", json=_INIT, headers=_MCP_HEADERS)
    assert r.status_code == 200
    text = _body_text(r)
    # The server advertises its capabilities in the initialize response
    assert "capabilities" in text, f"no capabilities in body: {text[:300]}"


def test_bad_json_returns_error(client):
    r = client.post("/mcp", content="not json", headers=_MCP_HEADERS)
    assert r.status_code in (200, 400), f"unexpected {r.status_code}: {r.text[:300]}"


# ── tools/list: the real surface ────────────────────────────────────────

# Every builtin tool that must be visible over MCP. The second half of this
# list is the 8 recovered by the discovery fix (bare TOOL_DEFINITION names) —
# each one here is a regression tripwire.
_EXPECTED_TOOLS = {
    # suffixed-def tools (the 16 that always registered)
    "remember", "recall", "forget",
    "trigger_consolidation", "consolidation_status",
    "schedule_action", "list_scheduled", "unschedule_action",
    "start_service", "stop_service", "restart_service",
    "search_the_web", "fetch_url",
    "time_since_last_message", "preserve_memory_verbatim", "promote_memory_now",
    # the 8 recovered by the discovery fix
    "get_current_time", "which_model", "get_cluster_status",
    "ensure_service_running", "wait_for_service", "get_recent_logs",
    "list_models", "get_self_context",
}


def test_tools_list_exposes_full_builtin_surface(client):
    headers = _open_session(client)
    r = client.post("/mcp", json=_TOOLS_LIST, headers=headers)
    assert r.status_code == 200, f"tools/list failed: {r.status_code}: {r.text[:300]}"
    payload = _parse_sse_json(r)
    tools = payload.get("result", {}).get("tools", [])
    names = {t["name"] for t in tools}
    missing = _EXPECTED_TOOLS - names
    assert not missing, f"missing tools over MCP: {sorted(missing)}"
    # And NOT one collapsed stub:
    assert "_stub" not in names


def test_tools_list_schemas_have_real_params(client):
    """The **kwargs regression check: get_recent_logs must expose 'service'
    and 'lines' in its schema — not a lone property named 'kwargs' — and DI
    params (runtime_host, config) must NOT leak into it."""
    headers = _open_session(client)
    r = client.post("/mcp", json=_TOOLS_LIST, headers=headers)
    payload = _parse_sse_json(r)
    tools = {t["name"]: t for t in payload.get("result", {}).get("tools", [])}

    logs_schema = tools["get_recent_logs"].get("inputSchema", {})
    props = set(logs_schema.get("properties", {}).keys())
    assert "service" in props and "lines" in props, f"bad schema props: {props}"
    assert "kwargs" not in props
    assert "runtime_host" not in props and "config" not in props

    remember_schema = tools["remember"].get("inputSchema", {})
    rprops = set(remember_schema.get("properties", {}).keys())
    assert rprops == {"content"}, f"remember schema leaked params: {rprops}"
    assert remember_schema.get("required") == ["content"]


def test_disabled_tool_refused_at_call_time(client):
    """The operator gate: disable a tool via the dashboard route, then call
    it over MCP — the call must come back isError with the disabled message."""
    r = client.post("/tools/state", json={"tool": "get_current_time", "enabled": False})
    assert r.status_code == 200

    headers = _open_session(client)
    call = {
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "get_current_time", "arguments": {}},
    }
    resp = client.post("/mcp", json=call, headers=headers)
    assert resp.status_code == 200, f"tools/call failed: {resp.status_code}"
    payload = _parse_sse_json(resp)
    result = payload.get("result", {})
    assert result.get("isError") is True, f"disabled tool ran anyway: {result}"
    text = json.dumps(result)
    assert "disabled" in text

    # Re-enable and call again — get_current_time needs no external service,
    # so it must genuinely succeed end-to-end.
    client.post("/tools/state", json={"tool": "get_current_time", "enabled": True})
    resp2 = client.post("/mcp", json={**call, "id": 4}, headers=headers)
    payload2 = _parse_sse_json(resp2)
    result2 = payload2.get("result", {})
    assert result2.get("isError") is not True, f"re-enabled tool failed: {result2}"
    assert "iso_utc" in json.dumps(result2)
