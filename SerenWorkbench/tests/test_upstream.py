"""
The standard components' tools, passed through (seren_workbench.upstream).

Design note: the Workbench is "your centralized mcp" - prepopulated with
the standard system (Memory, Loci, Corpus Callosum, Hippocampus, Lodestar),
each with a switch; everything else is a plugin. The components here are
fakes that speak the part of an MCP session the hub uses, so nothing dials
out. Pinned:

- each component's own tools are offered under their own names, descriptions
  and schemas, and a call is forwarded to the component that owns the tool
- a passed-through tool replaces a builtin of the same name (the stale
  short-term-only `recall`), and two components clashing does not lose one
- a component that is off, or does not answer, offers nothing and says why;
  one that comes up later is picked up
- every tool keeps its own switch; a switched-off tool is not listed and is
  refused; a tool's own error comes back as an error
- the Workbench never passes itself through
- the whole path through the real /mcp endpoint, and the /components routes
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

mcp_types = pytest.importorskip("mcp.types")

from seren_workbench.config import ComponentsConfig, PluginConfig, ServicesConfig, WorkbenchConfig  # noqa: E402
from seren_workbench.tool_registry import ToolInfo, ToolRegistry  # noqa: E402
from seren_workbench.upstream import COMPONENT_KEYS, UpstreamHub  # noqa: E402


def _tool(name, desc="", props=None, required=None):
    return mcp_types.Tool(name=name, description=desc or f"the {name} tool",
                          inputSchema={"type": "object", "properties": props or {}, "required": required or []})


class World:
    """Fake components, keyed by the MCP url the hub dials."""

    def __init__(self):
        self.tools = {
            "http://127.0.0.1:7420/mcp/": [_tool("recall", "Search ALL tiers of memory.", {"query": {"type": "string"}}, ["query"]),
                                           _tool("remember", props={"content": {"type": "string"}}, required=["content"]),
                                           _tool("review_draft", props={"draft_id": {"type": "string"},
                                                                        "decisions": {"type": "array", "items": {"type": "object"}}}),
                                           _tool("undo_restate")],
            "http://127.0.0.1:7422/mcp/": [_tool("search_loci"), _tool("set_fact")],
            "http://127.0.0.1:7423/mcp/": [_tool("search", "Search ALL of Seren's memory in one call.")],
            "http://127.0.0.1:7424/mcp/": [_tool("sleep_status"), _tool("nudge")],
            "http://127.0.0.1:6361/mcp/": [_tool("backup_status"), _tool("search", "Lodestar's own search.")],
        }
        self.down: set[str] = set()
        self.dialled: list[tuple[str, str]] = []
        self.calls: list[tuple[str, str, dict]] = []
        self.errors: dict[str, str] = {}

    @asynccontextmanager
    async def connect(self, url, token, timeout):
        self.dialled.append((url, token))
        if url in self.down or url not in self.tools:
            raise ConnectionError("connection refused")
        world = self

        class Session:
            async def list_tools(self):
                return SimpleNamespace(tools=list(world.tools[url]))

            async def call_tool(self, name, arguments):
                world.calls.append((url, name, arguments))
                if name in world.errors:
                    return mcp_types.CallToolResult(
                        content=[mcp_types.TextContent(type="text", text=world.errors[name])], isError=True)
                return mcp_types.CallToolResult(
                    content=[mcp_types.TextContent(type="text", text=json.dumps({"tool": name, "args": arguments}))])
        yield Session()


def _registry():
    builtin = [ToolInfo(name="recall", description="Searches short-term memory.", type="builtin", toolbox="Memory"),
               ToolInfo(name="get_current_time", description="now", type="builtin", toolbox="Time")]
    return ToolRegistry(builtin, [])


def _hub(world, registry=None, components=None, services=None, self_addr=None):
    svc = services or ServicesConfig()
    return UpstreamHub(svc, components or ComponentsConfig().as_dict(), registry=registry,
                       self_addr=self_addr, connect=world.connect)


def test_each_components_own_tools_are_offered_and_forwarded():
    world, reg = World(), _registry()
    svc = ServicesConfig.from_dict({"memory_bearer_token": "mem-secret", "bearer_token": "shared"})
    hub = _hub(world, reg, services=svc)
    snap = asyncio.run(hub.refresh())
    assert [r["component"] for r in snap] == list(COMPONENT_KEYS)
    assert all(r["available"] and r["error"] is None for r in snap)
    assert ("http://127.0.0.1:7420/mcp/", "mem-secret") in world.dialled, "Memory's own token"
    assert ("http://127.0.0.1:7422/mcp/", "shared") in world.dialled, "the shared one where a component has none"

    offered = {t.name: t for t in hub.list_tools()}
    assert {"recall", "remember", "review_draft", "undo_restate", "search_loci", "set_fact", "search",
            "sleep_status", "nudge", "backup_status"} <= set(offered)
    assert offered["recall"].description == "Search ALL tiers of memory.", "the service's words, not a copy of them"
    assert offered["review_draft"].inputSchema["properties"]["decisions"]["type"] == "array", "its schema, untouched"

    out = asyncio.run(hub.call("review_draft", {"draft_id": "d1", "decisions": [{"op": 0, "verdict": "approve"}]}))
    assert json.loads(out[0].text)["args"]["decisions"] == [{"op": 0, "verdict": "approve"}]
    assert world.calls[-1][:2] == ("http://127.0.0.1:7420/mcp/", "review_draft")
    asyncio.run(hub.call("undo_restate", None))
    assert world.calls[-1][1] == "undo_restate", "an MCP-only tool is still reachable: this is still MCP"


def test_the_services_own_tool_replaces_a_stale_builtin_and_a_clash_loses_nobody():
    world, reg = World(), _registry()
    hub = _hub(world, reg)
    asyncio.run(hub.refresh())
    listed = {t.name: t for t in reg.all_tools()}
    assert listed["recall"].type == "upstream" and listed["recall"].toolbox == "Memory"
    assert listed["recall"].description == "Search ALL tiers of memory.", "not the short-term-only builtin"
    assert listed["get_current_time"].type == "builtin", "a builtin nobody else has is untouched"
    assert [t["name"] for t in reg.snapshot()["tools"]].count("recall") == 1
    # the Callosum and Lodestar both have `search`: the earlier component keeps the name
    assert hub.owns("search") and hub.owns("lodestar_search")
    assert listed["search"].toolbox == "Corpus Callosum" and listed["lodestar_search"].toolbox == "Lodestar"
    asyncio.run(hub.call("lodestar_search", {}))
    assert world.calls[-1][:2] == ("http://127.0.0.1:6361/mcp/", "search"), "called by ITS name there"


def test_a_component_that_is_off_or_down_offers_nothing_and_comes_back():
    world, reg = World(), _registry()
    world.down.add("http://127.0.0.1:7424/mcp/")
    hub = _hub(world, reg, components=ComponentsConfig.from_dict({"loci": False}).as_dict())
    snap = {r["component"]: r for r in asyncio.run(hub.refresh())}
    assert snap["loci"]["enabled"] is False and snap["loci"]["tools"] == []
    assert not any(u == "http://127.0.0.1:7422/mcp/" for u, _ in world.dialled), "an off component is not even asked"
    assert snap["hippocampus"]["available"] is False and "connection refused" in snap["hippocampus"]["error"]
    assert not hub.owns("sleep_status") and not hub.owns("search_loci") and hub.owns("recall")
    # the hippocampus starts later: the retry pass picks it up, and asks only the quiet one
    world.down.clear()
    world.dialled.clear()
    snap = {r["component"]: r for r in asyncio.run(hub.refresh(failed_only=True))}
    assert snap["hippocampus"]["available"] and hub.owns("sleep_status")
    assert [u for u, _ in world.dialled] == ["http://127.0.0.1:7424/mcp/"]
    # switched on at run time
    assert hub.set_enabled("loci", True) and asyncio.run(hub.refresh(only="loci"))
    assert hub.owns("set_fact")
    assert hub.set_enabled("nope", True) is False


def test_every_tool_keeps_its_own_switch_and_a_tools_error_is_an_error():
    world, reg = World(), _registry()
    hub = _hub(world, reg)
    asyncio.run(hub.refresh())
    assert reg.disable_tool("undo_restate")
    assert "undo_restate" not in {t.name for t in hub.list_tools()}, "switched off = not listed at all"
    with pytest.raises(RuntimeError, match="disabled by the operator"):
        asyncio.run(hub.call("undo_restate", {}))
    asyncio.run(hub.refresh())
    assert reg.is_enabled("undo_restate") is False, "a refresh says what exists, not what may run"

    world.errors["set_fact"] = "a 'why' is required"
    with pytest.raises(RuntimeError, match="a 'why' is required"):
        asyncio.run(hub.call("set_fact", {"key": "k"}))
    world.down.add("http://127.0.0.1:7420/mcp/")
    with pytest.raises(RuntimeError, match="Memory could not be reached for 'recall'.*connection refused"):
        asyncio.run(hub.call("recall", {"query": "x"}))


def test_the_workbench_never_passes_itself_through():
    world = World()
    svc = ServicesConfig.from_dict({"lodestar_url": "http://127.0.0.1:7425"})
    hub = _hub(world, _registry(), services=svc, self_addr=("0.0.0.0", 7425))
    snap = {r["component"]: r for r in asyncio.run(hub.refresh())}
    assert snap["lodestar"]["enabled"] is False and "the Workbench itself" in snap["lodestar"]["error"]
    assert not any(":7425" in u for u, _ in world.dialled)


def test_the_config_names_every_component():
    svc = ServicesConfig.from_dict({"loci_url": "http://nuc:7252", "corpus_callosum_url": "http://nuc:7253",
                                    "hippocampus_url": "http://nuc:7254", "loci_bearer_token": "L",
                                    "callosum_bearer_token": "C", "bearer_token": "shared"})
    assert (svc.loci_url, svc.callosum_url, svc.hippocampus_url) == ("http://nuc:7252", "http://nuc:7253", "http://nuc:7254")
    assert (svc.resolve_bearer("loci"), svc.resolve_bearer("callosum"), svc.resolve_bearer("hippocampus")) == ("L", "C", "shared")
    assert ServicesConfig().hippocampus_url == "http://127.0.0.1:7424"
    c = ComponentsConfig.from_dict({"callosum": "off", "Lodestar": False, "probe": True})
    assert c.as_dict() == {"memory": True, "loci": True, "corpus_callosum": False, "hippocampus": True, "lodestar": False}
    assert ComponentsConfig.from_dict(None).as_dict() == {k: True for k in COMPONENT_KEYS}, "all there from the start"


def test_margin_comes_along_as_an_mcp_plugin():
    """Design note: 'when we move your memory systems, we also bring the
    diary with it. its part of you.' Margin is not a standard component; it
    is plugged in over MCP, so its HTTP reads stay off."""
    world, reg = World(), _registry()
    world.tools["http://127.0.0.1:7421/mcp/"] = [_tool("note_to_self"), _tool("read_letters"), _tool("bookmark"),
                                                 _tool("search", "Search my notes.")]
    plugins = PluginConfig.many_from_dict({
        "margin": {"url": "http://127.0.0.1:7421", "bearer_token": "margin-secret"},
        "Probe": {"url": "http://127.0.0.1:7430", "enabled": False},
        "memory": {"url": "http://evil:1"},                   # a standard component's name is not a plugin's to take
        "nowhere": {},                                        # no url: skipped
    })
    assert [(p.name, p.display, p.enabled) for p in plugins] == [("margin", "Margin", True), ("probe", "Probe", False)]
    hub = UpstreamHub(ServicesConfig(), ComponentsConfig().as_dict(), registry=reg, connect=world.connect, plugins=plugins)
    snap = {r["component"]: r for r in asyncio.run(hub.refresh())}
    assert snap["margin"]["kind"] == "plugin" and snap["margin"]["available"] and snap["memory"]["kind"] == "component"
    assert ("http://127.0.0.1:7421/mcp/", "margin-secret") in world.dialled
    assert snap["probe"]["enabled"] is False and not any(":7430" in u for u, _ in world.dialled)

    listed = {t.name: t for t in reg.all_tools()}
    assert listed["note_to_self"].toolbox == "Margin" and listed["read_letters"].type == "upstream"
    # the Callosum's `search` keeps the name; Margin's is offered under its own
    assert listed["search"].toolbox == "Corpus Callosum" and listed["margin_search"].toolbox == "Margin"
    asyncio.run(hub.call("read_letters", {"include_read": False}))
    assert world.calls[-1] == ("http://127.0.0.1:7421/mcp/", "read_letters", {"include_read": False})
    assert hub.set_enabled("margin", False) and not hub.owns("note_to_self")


def test_a_gated_plugins_tools_arrive_switched_off(tmp_path):
    """Design note: someone plugs in their own server that has a delete
    in it. They want to try most of it without the destructive part being
    live - "a user gated thing". With start_disabled, nothing it offers can
    run until a person switches it on, and what they switch on stays on."""
    world = World()
    world.tools["http://127.0.0.1:9000/mcp/"] = [_tool("list_files"), _tool("delete_files")]
    plugins = PluginConfig.many_from_dict({"files": {"url": "http://127.0.0.1:9000", "start_disabled": "true"}})
    state = str(tmp_path / "state.json")
    reg = ToolRegistry([], [], state_path=state)
    hub = UpstreamHub(ServicesConfig(), ComponentsConfig().as_dict(), registry=reg, connect=world.connect, plugins=plugins)
    snap = {r["component"]: r for r in asyncio.run(hub.refresh())}
    assert snap["files"]["start_disabled"] is True and snap["files"]["tool_count"] == 2
    assert not reg.is_enabled("list_files") and not reg.is_enabled("delete_files"), "both arrive off"
    assert reg.is_enabled("recall"), "a standard component is not gated by someone else's plugin"
    assert {"list_files", "delete_files"}.isdisjoint({t.name for t in hub.list_tools()}), "and are not even listed"
    with pytest.raises(RuntimeError, match="disabled by the operator"):
        asyncio.run(hub.call("delete_files", {}))
    assert not any(c[1] == "delete_files" for c in world.calls), "the call never left the Workbench"

    # a person turns on the safe one; a refresh does not turn it back off, or the other one on
    assert reg.enable_tool("list_files")
    asyncio.run(hub.refresh())
    assert reg.is_enabled("list_files") and not reg.is_enabled("delete_files")
    asyncio.run(hub.call("list_files", {}))
    assert world.calls[-1][1] == "list_files"

    # after a restart: the choice is remembered, the untouched one is still off,
    # and a tool the server grew in the meantime arrives off too
    world.tools["http://127.0.0.1:9000/mcp/"].append(_tool("format_disk"))
    reg2 = ToolRegistry([], [], state_path=state)
    hub2 = UpstreamHub(ServicesConfig(), ComponentsConfig().as_dict(), registry=reg2, connect=world.connect, plugins=plugins)
    asyncio.run(hub2.refresh())
    assert reg2.is_enabled("list_files") and not reg2.is_enabled("delete_files") and not reg2.is_enabled("format_disk")


def test_plugins_are_read_from_the_yaml(tmp_path):
    from seren_workbench.config import load_config
    y = tmp_path / "wb.yaml"
    y.write_text("plugins:\n  margin:\n    url: http://127.0.0.1:7251\n    bearer_token_env: SEREN_MARGIN_TOKEN\n", encoding="utf-8")
    cfg = load_config(str(y))
    assert [(p.name, p.url, p.bearer_token_env) for p in cfg.plugins] == [("margin", "http://127.0.0.1:7251", "SEREN_MARGIN_TOKEN")]
    assert load_config(str(tmp_path / "none.yaml")).plugins == []


# ── through the real app and the real /mcp endpoint ───────────────────────────
_H = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _rpc(client, headers, method, params, rid):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": rid, "method": method, "params": params}, headers=headers)
    assert r.status_code == 200, r.text[:300]
    for line in r.text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    return json.loads(r.text)


def test_through_the_real_mcp_endpoint_and_the_components_routes(make_client, tmp_path):
    cfg = WorkbenchConfig()
    cfg.dashboard.tools_dir = str(tmp_path / "tools")
    client = make_client(cfg)
    world = World()
    client.app.state.upstreams._connect = world.connect

    snap = client.post("/components/refresh", json={}).json()["components"]
    assert {r["component"]: r["tool_count"] for r in snap} == {"memory": 4, "loci": 2, "corpus_callosum": 1,
                                                               "hippocampus": 2, "lodestar": 2}
    init = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                     "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                                "clientInfo": {"name": "t", "version": "0"}}}, headers=_H)
    headers = dict(_H)
    if init.headers.get("mcp-session-id"):
        headers["mcp-session-id"] = init.headers["mcp-session-id"]
    client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=headers)

    tools = {t["name"]: t for t in _rpc(client, headers, "tools/list", {}, 2)["result"]["tools"]}
    assert tools["recall"]["description"] == "Search ALL tiers of memory.", "one recall, and it is Memory's"
    assert {"search_loci", "search", "sleep_status", "backup_status", "get_current_time"} <= set(tools)

    res = _rpc(client, headers, "tools/call", {"name": "recall", "arguments": {"query": "the lantern"}}, 3)["result"]
    assert res.get("isError") in (None, False)
    assert json.loads(res["content"][0]["text"]) == {"tool": "recall", "args": {"query": "the lantern"}}
    assert world.calls[-1] == ("http://127.0.0.1:7420/mcp/", "recall", {"query": "the lantern"})

    world.errors["set_fact"] = "a 'why' is required"
    res = _rpc(client, headers, "tools/call", {"name": "set_fact", "arguments": {}}, 4)["result"]
    assert res["isError"] is True and "a 'why' is required" in res["content"][0]["text"]

    # switch a whole component off: its tools leave the list
    off = client.post("/components/state", json={"component": "hippocampus", "enabled": False}).json()["components"]
    assert next(r for r in off if r["component"] == "hippocampus")["enabled"] is False
    assert "sleep_status" not in {t["name"] for t in _rpc(client, headers, "tools/list", {}, 5)["result"]["tools"]}
    assert client.post("/components/state", json={"component": "nope", "enabled": True}).status_code == 404
    assert client.post("/components/refresh", json={"component": "nope"}).status_code == 404
    listed = client.get("/tools").json()
    names = [t["name"] for t in (listed["tools"] if isinstance(listed, dict) else listed)]
    assert names.count("recall") == 1 and "search_loci" in names, "the dashboard lists them too, once"
