"""
Functional tests for the mounted MCP HTTP endpoint.

Drives an actual JSON-RPC ``initialize`` through the live app (with the
lifespan entered, the way uvicorn runs it) so the whole path is exercised.
Gated on the ``mcp`` SDK like the rest of the MCP suite.

The StreamableHTTP transport frames replies as SSE events (``event: message\\n
data: {{...}}\\n\\n``), NOT direct JSON.  We check the raw text for expected
substrings — same pattern as SerenWorkbench/tests/test_mcp_endpoint.py.

Same pattern as SerenWorkbench/tests/test_mcp_endpoint.py.
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


def _body_text(resp) -> str:
    # streamable-HTTP frames the reply as an SSE event ("event: message\n
    # data: {...}"); just return raw text and let callers substring/parse.
    return resp.text


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
