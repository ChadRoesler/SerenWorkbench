"""
A model asking for an MCP server to be plugged in (propose_plugin).

The claims, each attacked below:
  - proposing connects to NOTHING: the address is not dialled until an
    operator approves
  - a proposal never holds a secret, and a credential POINTER is shown to the
    reviewer in so many words
  - approval plugs the server in with EVERY tool switched off, and a tool
    that is off is not offered to the model
  - it survives a restart, and it never replaces anything
"""
from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("mcp")

from seren_workbench.config import PluginConfig, WorkbenchConfig
from seren_workbench.models.tools.proposal_tools import list_my_proposals, propose_plugin

from .test_upstream import World, _H, _rpc, _tool

BRIDGE = "http://127.0.0.1:9000"


def _client(make_client, tmp_path, world=None):
    cfg = WorkbenchConfig()
    cfg.dashboard.tools_dir = str(tmp_path / "tools")
    client = make_client(cfg)
    world = world or World()
    world.tools[BRIDGE + "/mcp/"] = [_tool("spawn_item", "Spawns an item in the game."),
                                     _tool("delete_save", "Deletes the save file.")]
    client.app.state.upstreams._connect = world.connect
    return client, world


def _propose(client, **kw):
    kw.setdefault("name", "bridge")
    kw.setdefault("url", BRIDGE)
    kw.setdefault("rationale", "the user's BepInEx mod exposes the game over MCP; I want spawn_item for the base tour.")
    return json.loads(asyncio.run(propose_plugin(proposals=client.app.state.proposals, **kw)))


def _session(client):
    init = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                     "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                                "clientInfo": {"name": "t", "version": "0"}}}, headers=_H)
    headers = dict(_H)
    if init.headers.get("mcp-session-id"):
        headers["mcp-session-id"] = init.headers["mcp-session-id"]
    client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=headers)
    return headers


def test_proposing_connects_to_nothing_and_approval_plugs_it_in_switched_off(make_client, tmp_path):
    client, world = _client(make_client, tmp_path)
    headers = _session(client)

    # through MCP, the way a model does it
    res = _rpc(client, headers, "tools/call", {"name": "propose_plugin", "arguments": {
        "name": "bridge", "url": BRIDGE, "rationale": "I want spawn_item for the base tour."}}, 2)["result"]
    assert res.get("isError") in (None, False), res
    out = json.loads(res["content"][0]["text"])
    pid = out["proposal_id"]
    assert out["plugin"]["name"] == "bridge" and "Nothing has been connected to" in out["what_happens_next"]
    assert world.dialled == [], "not even dialled"
    assert "bridge" not in client.app.state.upstreams.states
    assert not (tmp_path / "tools" / "plugins").exists()

    # what the reviewer is shown
    shown = client.get(f"/proposals/{pid}").json()
    assert shown["kind"] == "plugin" and shown["title"] == "plugin: bridge" and shown["tool_names"] == []
    eff = shown["effects"][0]
    assert eff["kind"] == "plugin" and eff["calls"] == f"MCP {BRIDGE}/mcp/" and "switched OFF" in eff["arrives"]
    assert "sends_credential" not in eff
    assert "start_disabled: true" in shown["manifest"]
    mine = json.loads(asyncio.run(list_my_proposals(proposals=client.app.state.proposals)))["proposals"][0]
    assert mine["kind"] == "plugin" and mine["plugin"]["url"] == BRIDGE and mine["status"] == "pending"

    # approved: plugged in, asked for its tools, every one of them off
    done = client.post(f"/proposals/{pid}/approve").json()
    assert done["ok"] is True and done["plugged_in"] is True and done["enabled"] is False, done
    assert done["installed_as"] == "plugins/bridge.yaml"
    assert done["component"]["tool_count"] == 2 and done["component"]["start_disabled"] is True
    assert "all switched OFF" in done["next_step"]
    assert world.dialled[-1][0] == BRIDGE + "/mcp/"
    listed = {t["name"] for t in _rpc(client, headers, "tools/list", {}, 3)["result"]["tools"]}
    assert {"spawn_item", "delete_save"}.isdisjoint(listed), "a stranger's tools are not shown to the model either"
    blocked = _rpc(client, headers, "tools/call", {"name": "delete_save", "arguments": {}}, 4)["result"]
    assert blocked["isError"] is True and "disabled by the operator" in blocked["content"][0]["text"]
    assert not any(c[1] == "delete_save" for c in world.calls)

    # the operator turns on the one that may run
    assert client.post("/tools/state", json={"tool": "spawn_item", "enabled": True}).json()["ok"] is True
    listed = {t["name"] for t in _rpc(client, headers, "tools/list", {}, 5)["result"]["tools"]}
    assert "spawn_item" in listed and "delete_save" not in listed
    ran = _rpc(client, headers, "tools/call", {"name": "spawn_item", "arguments": {"what": "lantern"}}, 6)["result"]
    assert ran.get("isError") in (None, False) and world.calls[-1][1] == "spawn_item"


def test_an_approved_plugin_is_there_after_a_restart_with_its_switches(make_client, tmp_path):
    client, world = _client(make_client, tmp_path)
    pid = _propose(client)["proposal_id"]
    client.post(f"/proposals/{pid}/approve")
    client.post("/tools/state", json={"tool": "spawn_item", "enabled": True})

    again, world2 = _client(make_client, tmp_path)
    hub = again.app.state.upstreams
    assert "bridge" in hub.states and hub.states["bridge"].start_disabled is True
    again.post("/components/refresh", json={})
    reg = again.app.state.tool_registry
    assert reg.is_enabled("spawn_item") and not reg.is_enabled("delete_save")
    # a tool the server grew while nobody was looking arrives off too
    world2.tools[BRIDGE + "/mcp/"].append(_tool("wipe_world"))
    again.post("/components/refresh", json={"component": "bridge"})
    assert not reg.is_enabled("wipe_world")


def test_a_proposal_never_holds_a_secret_and_a_pointer_is_shown_loudly(make_client, tmp_path):
    client, world = _client(make_client, tmp_path)
    for bad in ({"url": "http://alice:hunter2@127.0.0.1:9000"},                # a password in the address
                {"bearer_token_env": "sk-live-abcdef0123456789.not.a.name"},  # the token itself, in the name field
                {"bearer_token_keyring": "just-a-token"},
                {"bearer_token_env": "A", "bearer_token_keyring": "svc/user"}):
        out = _propose(client, **bad)
        assert "error" in out, bad
    assert client.get("/proposals").json()["count"] == 0, "a refused proposal leaves nothing on disk"

    out = _propose(client, bearer_token_env="BRIDGE_TOKEN")
    shown = client.get(f"/proposals/{out['proposal_id']}").json()
    eff = shown["effects"][0]
    assert eff["sends_credential"] == "env:BRIDGE_TOKEN"
    assert eff["review_note"].startswith("APPROVING SENDS A SECRET") and BRIDGE in eff["review_note"]
    assert eff["executes_a_binary"] is False, "said in its own field, not by borrowing another's"
    assert "bearer_token_env: BRIDGE_TOKEN" in shown["manifest"] and "bearer_token:" not in shown["manifest"]


def test_what_cannot_be_proposed(make_client, tmp_path):
    client, world = _client(make_client, tmp_path)
    port = client.app.state.config.server.port
    refusals = {
        "rationale is required": {"rationale": "  "},
        "lower_snake_case": {"name": "My Bridge!"},
        "standard component": {"name": "memory"},
        "absolute http": {"url": "ftp://127.0.0.1/x"},
        "query string": {"url": BRIDGE + "/?token=abc"},
        "this Workbench itself": {"url": f"http://127.0.0.1:{port}"},
    }
    for needle, kw in refusals.items():
        out = _propose(client, **kw)
        assert needle in out.get("error", ""), (needle, out)
    assert world.dialled == []

    first = _propose(client)
    assert first["ok"] is True
    assert "already awaiting review" in _propose(client)["error"]
    client.post(f"/proposals/{first['proposal_id']}/approve")
    assert "already plugged in" in _propose(client)["error"], "approval never replaces something that exists"

    # rejected with a critique, revised, superseded: the same loop as a tool
    other = _propose(client, name="hub", url="http://127.0.0.1:9100")
    rej = client.post(f"/proposals/{other['proposal_id']}/reject", json={"critique": "which tools do you want?"}).json()
    assert rej["proposal"]["status"] == "rejected"
    revised = _propose(client, name="hub", url="http://127.0.0.1:9100", supersedes=other["proposal_id"],
                       rationale="lights_on and lights_off, for the evening routine.")
    assert revised["attempt"] == 2
    assert client.get(f"/proposals/{other['proposal_id']}").json()["status"] == "superseded"


def test_a_name_taken_while_the_proposal_waited_cannot_be_approved(make_client, tmp_path):
    client, world = _client(make_client, tmp_path)
    pid = _propose(client)["proposal_id"]
    # the operator plugs something in under that name in the meantime
    client.app.state.upstreams.add_plugin(PluginConfig(name="bridge", url="http://127.0.0.1:9999"))
    r = client.post(f"/proposals/{pid}/approve")
    assert r.status_code == 409 and "plugged in while this proposal was in review" in r.json()["error"]
    assert client.app.state.upstreams.states["bridge"].url == "http://127.0.0.1:9999"


def test_a_server_that_is_down_at_approval_is_still_plugged_in_and_still_gated(make_client, tmp_path):
    client, world = _client(make_client, tmp_path)
    world.down.add(BRIDGE + "/mcp/")
    pid = _propose(client)["proposal_id"]
    done = client.post(f"/proposals/{pid}/approve").json()
    assert done["plugged_in"] is True and done["component"]["available"] is False
    assert "did not answer" in done["next_step"]
    world.down.clear()
    client.post("/components/refresh", json={"component": "bridge"})
    reg = client.app.state.tool_registry
    assert not reg.is_enabled("spawn_item") and not reg.is_enabled("delete_save")


def test_proposals_switched_off_removes_the_tool(make_client, tmp_path):
    cfg = WorkbenchConfig()
    cfg.dashboard.tools_dir = str(tmp_path / "tools")
    cfg.dashboard.proposals_enabled = False
    client = make_client(cfg)
    assert "propose_plugin" not in {t["name"] for t in client.get("/tools").json()["tools"]}


def test_an_address_pasted_with_its_mcp_path_is_not_doubled():
    from seren_workbench.upstream import ComponentSpec, ComponentState
    spec = ComponentSpec("x", "X", "", "", kind="plugin")
    assert ComponentState(spec=spec, url="http://h:1").mcp_url == "http://h:1/mcp/"
    assert ComponentState(spec=spec, url="http://h:1/mcp").mcp_url == "http://h:1/mcp/"
    assert ComponentState(spec=spec, url="http://h:1/mcp/").mcp_url == "http://h:1/mcp/"
