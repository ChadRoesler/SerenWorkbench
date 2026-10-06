"""
Telling a client the tool list changed (mcp.server.ToolListWatch).

The Workbench's list moves under a connected client: a component that was
down at startup comes up, a switch is flipped, a proposal is approved. A
client that listed once and was never told keeps a picture that is no longer
true - worst for a model woken as the Workbench starts, which sees the
builtins and none of its memory.

Proves: initialize promises it (capabilities.tools.listChanged), every way
the list can change reaches the watch, a notice goes out only when the list
really differs, and a switched-off tool is not offered at all.
"""
from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("mcp")

from seren_workbench.config import WorkbenchConfig
from seren_workbench.mcp.server import ToolListWatch

from .test_upstream import World, _H, _rpc


class FakeSession:
    def __init__(self, gone: bool = False):
        self.told = 0
        self.gone = gone

    async def send_tool_list_changed(self):
        if self.gone:
            raise RuntimeError("stream closed")
        self.told += 1


def _client(make_client, tmp_path):
    cfg = WorkbenchConfig()
    cfg.dashboard.tools_dir = str(tmp_path / "tools")
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
    for line in init.text.splitlines():
        if line.startswith("data:"):
            return headers, json.loads(line[5:].strip())
    return headers, json.loads(init.text)


def _settle(client, seconds: float = 0.6):
    """Let the watch's debounce run on the app's own loop."""
    client.portal.call(asyncio.sleep, seconds)


def test_initialize_promises_list_changed(make_client, tmp_path):
    client = _client(make_client, tmp_path)
    _, init = _session(client)
    assert init["result"]["capabilities"]["tools"]["listChanged"] is True


def test_a_listing_client_is_remembered_and_told_when_the_list_really_changes(make_client, tmp_path):
    client = _client(make_client, tmp_path)
    watch = client.app.state.tool_watch
    headers, _ = _session(client)
    before = {t["name"] for t in _rpc(client, headers, "tools/list", {}, 2)["result"]["tools"]}
    assert watch.sessions == 1, "the session that listed is the one to tell"
    ears, dead = FakeSession(), FakeSession(gone=True)
    watch.see(ears)
    watch.see(dead)

    # the components come up after the client connected: twenty tools it has never seen
    client.post("/components/refresh", json={})
    _settle(client)
    after = {t["name"] for t in _rpc(client, headers, "tools/list", {}, 3)["result"]["tools"]}
    assert "recall" in after and "recall" not in before
    assert ears.told == 1 and watch.changes == 1
    assert watch.sessions == 2, "the session that had gone was dropped, the live ones kept"

    # asked again, same answer: nothing to say
    client.post("/components/refresh", json={})
    _settle(client)
    assert ears.told == 1

    # a switch flipped on the dashboard
    assert client.post("/tools/state", json={"tool": "recall", "enabled": False}).json()["ok"] is True
    _settle(client)
    assert ears.told == 2
    # a whole component switched off
    client.post("/components/state", json={"component": "hippocampus", "enabled": False})
    _settle(client)
    assert ears.told == 3
    shown = client.get("/components").json()["tool_list"]
    assert shown["changes"] == 3 and shown["sessions"] == 2 and shown["notices"] >= 3


def test_a_switched_off_tool_is_not_offered_whoever_it_belongs_to(make_client, tmp_path):
    client = _client(make_client, tmp_path)
    headers, _ = _session(client)
    assert "fetch_url" in {t["name"] for t in _rpc(client, headers, "tools/list", {}, 2)["result"]["tools"]}
    client.post("/tools/state", json={"tool": "fetch_url", "enabled": False})
    assert "fetch_url" not in {t["name"] for t in _rpc(client, headers, "tools/list", {}, 3)["result"]["tools"]}
    # the operator still sees it, and calling it by name still refuses
    assert "fetch_url" in {t["name"] for t in client.get("/tools").json()["tools"]}
    res = _rpc(client, headers, "tools/call", {"name": "fetch_url", "arguments": {"url": "http://example.com"}}, 4)
    assert res["result"]["isError"] is True and "disabled by the operator" in json.dumps(res)


def test_many_pokes_are_one_check_and_a_poke_outside_a_loop_is_nothing():
    class FakeMCP:
        def __init__(self):
            self.listed = 0
            self.names = ["a"]

        async def list_tools(self):
            from types import SimpleNamespace
            self.listed += 1
            return [SimpleNamespace(name=n, description="", inputSchema={}) for n in self.names]

    mcp = FakeMCP()
    watch = ToolListWatch(mcp)
    watch.poke()                                        # no loop running: nothing, and no error
    assert mcp.listed == 0

    async def go():
        await watch.prime()
        ears = FakeSession()
        watch.see(ears)
        for _ in range(25):
            watch.poke()
        await asyncio.sleep(watch.DEBOUNCE_SECONDS + 0.3)
        assert ears.told == 0 and mcp.listed == 2, "25 pokes, one look, nothing changed, nobody told"
        mcp.names = ["a", "b"]
        watch.poke()
        await asyncio.sleep(watch.DEBOUNCE_SECONDS + 0.3)
        assert ears.told == 1
    asyncio.run(go())
