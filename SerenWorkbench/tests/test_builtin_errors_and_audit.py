"""
Two things about a model's own calls.

A BUILTIN'S FAILURE IS AN ERROR. Every builtin reports failure by returning
{"error", "hint"} as its text. Over MCP that used to arrive as a successful
call whose content was bad news - and was counted as a success in the record.
Now it is isError, like a passed-through tool's failure or a manifest tool's.

THE RECORD IS READABLE BY WHOEVER MADE THE CALLS (list_my_tool_calls). It was
only ever shown to the operator; a session woken to do a job leaves the next
one a letter saying what it believes it did, and this is what it did.
"""
from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("mcp")

from seren_workbench.config import WorkbenchConfig
from seren_workbench.dynamic_tools.tool_audit_log import AuditEntry, ToolAuditLog
from seren_workbench.mcp.server import _error_payload
from seren_workbench.models.tools.audit_tools import list_my_tool_calls

from .test_upstream import World, _H, _rpc


def _client(make_client, tmp_path):
    cfg = WorkbenchConfig()
    cfg.dashboard.tools_dir = str(tmp_path / "tools")
    # nothing listens here: every Lodestar-backed builtin fails, quickly
    cfg.services.runtime_host_url = "http://127.0.0.1:9"
    cfg.services.timeout_seconds = 2.0
    client = make_client(cfg)
    client.app.state.upstreams._connect = World().connect
    return client


def _session(client):
    init = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                     "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                                "clientInfo": {"name": "t", "version": "0"}}}, headers=_H)
    headers = dict(_H)
    if init.headers.get("mcp-session-id"):
        headers["mcp-session-id"] = init.headers["mcp-session-id"]
    client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=headers)
    return headers


def test_only_a_bare_error_payload_counts_as_a_failure():
    assert _error_payload(json.dumps({"error": "Lodestar unreachable", "hint": "Check it."})) == \
        "Lodestar unreachable\nCheck it."
    assert _error_payload({"error": "no implementation", "hint": ""}) == "no implementation"
    assert _error_payload('  {"error": "x"}') == "x"
    # an answer that merely HAS an error field is an answer
    assert _error_payload(json.dumps({"error": "node 2 is down", "nodes": [1, 3]})) == ""
    assert _error_payload(json.dumps({"error": None, "hint": "fine"})) == ""
    assert _error_payload("Error: this is prose") == ""
    assert _error_payload('{"error": broken json') == ""
    assert _error_payload(json.dumps(["error"])) == ""
    assert _error_payload(None) == ""


def test_a_builtins_failure_is_an_mcp_error_and_a_failure_in_the_record(make_client, tmp_path):
    client = _client(make_client, tmp_path)
    headers = _session(client)

    ok = _rpc(client, headers, "tools/call", {"name": "get_current_time", "arguments": {}}, 2)["result"]
    assert ok.get("isError") in (None, False)

    bad = _rpc(client, headers, "tools/call", {"name": "get_cluster_status", "arguments": {}}, 3)["result"]
    assert bad["isError"] is True, bad
    text = bad["content"][0]["text"]
    assert "Lodestar" in text and '{"error"' not in text, "the words, not a JSON blob to parse"

    refused = _rpc(client, headers, "tools/call",
                   {"name": "ensure_service_running", "arguments": {"service": "nope"}}, 4)["result"]
    assert refused["isError"] is True and "allowed-services" in refused["content"][0]["text"]

    # the model reads its own record, through the tool
    mine = _rpc(client, headers, "tools/call", {"name": "list_my_tool_calls", "arguments": {}}, 5)["result"]
    assert mine.get("isError") in (None, False)
    record = json.loads(mine["content"][0]["text"])
    assert [c["tool"] for c in record["calls"]] == ["ensure_service_running", "get_cluster_status", "get_current_time"]
    assert [c["ok"] for c in record["calls"]] == [False, False, True], "a returned failure is recorded as one"
    assert "allowed-services" in record["calls"][0]["error"]
    assert record["by_tool"]["get_cluster_status"] == {"calls": 1, "failed": 1}
    assert list(record["by_tool"])[-1] == "get_current_time", "what is failing comes first"
    assert record["kept_since"] and record["kept"] == 3

    only = json.loads(_rpc(client, headers, "tools/call", {
        "name": "list_my_tool_calls", "arguments": {"failed_only": True, "limit": 1}}, 6)["result"]["content"][0]["text"])
    assert only["matching"] == 2 and only["shown"] == 1 and only["calls"][0]["ok"] is False

    # a passed-through call is in the same record
    client.post("/components/refresh", json={})
    _rpc(client, headers, "tools/call", {"name": "recall", "arguments": {"query": "secret words"}}, 7)
    last = json.loads(_rpc(client, headers, "tools/call", {
        "name": "list_my_tool_calls", "arguments": {"tool": "recall"}}, 8)["result"]["content"][0]["text"])
    assert last["calls"][0]["kind"] == "upstream" and last["calls"][0]["arguments"] == 1
    assert "secret words" not in json.dumps(last), "content-blind: how many arguments, never what they were"


def test_the_record_is_content_blind_and_bounded():
    log = ToolAuditLog()
    for i in range(30):
        log.record(AuditEntry(timestamp=1_000_000.0 + i, tool="remember" if i % 3 else "recall", kind="upstream",
                              duration_ms=5, success=bool(i % 5), arg_count=2,
                              error_message=None if i % 5 else "a 'why' is required"))
    out = json.loads(asyncio.run(list_my_tool_calls(limit=10_000, audit_log=log)))
    assert out["kept"] == 30 and out["shown"] == 30
    assert out["calls"][0]["at"] == "1970-01-12T13:47:09Z", "UTC, and newest first"
    assert set(out["calls"][0]) <= {"tool", "when", "at", "ok", "ms", "arguments", "kind", "error"}
    few = json.loads(asyncio.run(list_my_tool_calls(limit="nonsense", audit_log=log)))
    assert few["shown"] == 20
    none = json.loads(asyncio.run(list_my_tool_calls(audit_log=None)))
    assert "error" in none
