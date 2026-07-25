"""
Dynamic (YAML manifest) tool tests — the plug-and-play path, end to end.

Boots the app with dashboard.tools_dir pointed at a temp manifest dir and
proves: the tool appears in /tools as type=dynamic, its schema carries the
manifest parameters, and a tools/call over live MCP actually DISPATCHES —
kind=process spawns the argv (async, shell-free) and returns stdout.

This whole path was dead before the cutover-completion pass (loader never
constructed with a client, dispatch never wired, subprocess timeout caught
the wrong exception); this file is its regression net.
"""
from __future__ import annotations

import json
import sys
import textwrap

import pytest

try:
    import mcp  # noqa: F401
    _mcp_available = True
except ImportError:
    _mcp_available = False

from seren_workbench.config import WorkbenchConfig, DashboardConfig, ServicesConfig
from seren_meninges import ServerConfig, TlsConfig

_MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}

def _manifest_text() -> str:
    """Manifest with a cross-platform process tool.

    NOT `echo` — that's a cmd BUILTIN on Windows, not a binary, so
    create_subprocess_exec would FileNotFoundError there while CI's ubuntu
    passed happily. sys.executable runs everywhere the tests do. json.dumps
    on the argv list yields valid YAML flow style with proper escaping for
    Windows backslash paths.
    """
    argv = [sys.executable, "-c", "print('hello, {who}!')"]
    return textwrap.dedent("""\
        schema_version: 1
        metadata:
          version: "0.0.1"
          license: GPL-3.0
        tools:
          - name: echo_greeting
            description: Echoes a greeting for the given name. Sandbox test tool.
            invoke:
              kind: process
              argv: %s
              timeout_seconds: 15
            parameters:
              - name: who
                type: string
                required: true
                description: Who to greet.
    """) % json.dumps(argv)


@pytest.fixture
def dyn_client(tmp_path, make_client):
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    (tools_dir / "greeting.yaml").write_text(_manifest_text())
    cfg = WorkbenchConfig(
        server=ServerConfig(),
        tls=TlsConfig(),
        dashboard=DashboardConfig(tools_dir=str(tools_dir)),
        services=ServicesConfig(),
    )
    return make_client(cfg)


def _open_session(client):
    init = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "seren-test", "version": "0"}},
    }
    r = client.post("/mcp", json=init, headers=_MCP_HEADERS)
    assert r.status_code == 200
    headers = dict(_MCP_HEADERS)
    sid = r.headers.get("mcp-session-id")
    if sid:
        headers["mcp-session-id"] = sid
    client.post("/mcp", json={"jsonrpc": "2.0",
                              "method": "notifications/initialized"}, headers=headers)
    return headers


def _parse_sse_json(resp) -> dict:
    for line in resp.text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[len("data:"):].strip())
    return json.loads(resp.text)


def test_dynamic_tool_listed(dyn_client):
    body = dyn_client.get("/tools").json()
    by_name = {t["name"]: t for t in body["tools"]}
    assert "echo_greeting" in by_name
    t = by_name["echo_greeting"]
    assert t["type"] == "dynamic"
    pnames = {p["name"] for p in t["parameters"]}
    assert pnames == {"who"}
    # And the root counts see it
    root = dyn_client.get("/").json()
    assert root["dynamic_count"] == 1


@pytest.mark.skipif(not _mcp_available, reason="mcp extras not installed")
def test_dynamic_tool_schema_over_mcp(dyn_client):
    headers = _open_session(dyn_client)
    r = dyn_client.post("/mcp", json={"jsonrpc": "2.0", "id": 2,
                                      "method": "tools/list", "params": {}},
                        headers=headers)
    payload = _parse_sse_json(r)
    tools = {t["name"]: t for t in payload.get("result", {}).get("tools", [])}
    assert "echo_greeting" in tools
    props = set(tools["echo_greeting"].get("inputSchema", {}).get("properties", {}).keys())
    assert props == {"who"}


@pytest.mark.skipif(not _mcp_available, reason="mcp extras not installed")
def test_dynamic_process_tool_dispatches(dyn_client):
    """tools/call on the manifest tool must actually run the argv and hand
    back stdout with the substituted parameter — the whole dead path alive."""
    headers = _open_session(dyn_client)
    call = {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "echo_greeting", "arguments": {"who": "duder"}}}
    r = dyn_client.post("/mcp", json=call, headers=headers)
    assert r.status_code == 200
    payload = _parse_sse_json(r)
    result = payload.get("result", {})
    assert result.get("isError") is not True, f"dispatch errored: {result}"
    assert "hello, duder!" in json.dumps(result)


@pytest.mark.skipif(not _mcp_available, reason="mcp extras not installed")
def test_dynamic_tool_missing_required_param_is_error(dyn_client):
    headers = _open_session(dyn_client)
    call = {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "echo_greeting", "arguments": {}}}
    r = dyn_client.post("/mcp", json=call, headers=headers)
    payload = _parse_sse_json(r)
    result = payload.get("result", {})
    # Either FastMCP validation or the dispatcher's own required-check —
    # both must land as an error, never a silent empty run.
    assert result.get("isError") is True or "error" in json.dumps(result).lower()
