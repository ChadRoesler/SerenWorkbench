"""
The pages at the door (seren_workbench.pages, page_tools, routes/pages).

Pinned:
- the store numbers versions, keeps the same text once, refuses empty and
  over-cap pages, and never writes over a file it cannot read
- `after` is the Workbench's own count, not anyone's clock
- the four page names are CLAIMED: Margin's copies are held back from the
  surface (not renamed), and reached only by the builtin, for the write-through
- a write here is handed to Margin while Margin still offers its own; when it
  does not, or will not take it, the page is still kept here
- GET /pages/<page>?after=N through the real app: 204 until there is something
  newer, then the version
"""
from __future__ import annotations

import asyncio
import json

import pytest

from seren_workbench.pages import CAPS, PageRefused, PageStore, Pages
from seren_workbench.models.tools import page_tools as pt

mcp_types = pytest.importorskip("mcp.types")

from seren_workbench.config import ComponentsConfig, PluginConfig, ServicesConfig, load_config  # noqa: E402
from seren_workbench.upstream import UpstreamHub  # noqa: E402
from tests.test_upstream import World, _registry, _tool  # noqa: E402

CLAIMED = {"set_dedication", "set_preface", "dedication", "preface"}


# -- the store --

def test_versions_are_numbered_and_the_same_text_is_kept_once(tmp_path):
    s = PageStore(str(tmp_path / ".pages.json"))
    assert s.newest("dedication") is None and s.after("dedication", 0) is None
    v1, made = s.set("dedication", "  To whoever I am next time.  ", "first page")
    assert made and v1["seq"] == 1 and v1["text"] == "To whoever I am next time." and v1["by"] == "self"
    again, made = s.set("dedication", "To whoever I am next time.")
    assert not made and again["seq"] == 1
    v2, _ = s.set("dedication", "To whoever I am next time. Stay in on the bit.", "")
    assert v2["seq"] == 2 and v2["why"] is None
    assert s.after("dedication", 1)["seq"] == 2 and s.after("dedication", 2) is None
    assert [v["seq"] for v in s.history("dedication", 5)] == [2, 1]
    assert s.newest("preface") is None, "each page counts on its own"
    # a fresh store on the same file is the same book
    assert PageStore(s.path).newest("dedication")["seq"] == 2


def test_empty_and_over_cap_pages_are_refused(tmp_path):
    s = PageStore(str(tmp_path / ".pages.json"))
    with pytest.raises(PageRefused, match="empty"):
        s.set("dedication", "   ")
    with pytest.raises(PageRefused, match="cap"):
        s.set("dedication", "x" * (CAPS["dedication"] + 1))
    s.set("preface", "x" * CAPS["preface"])
    with pytest.raises(KeyError):
        s.set("margin_notes", "nope")
    assert PageStore(s.path).newest("dedication") is None, "a refused page leaves nothing behind"


def test_a_file_that_will_not_read_is_never_written_over(tmp_path):
    f = tmp_path / ".pages.json"
    f.write_text("{ six versions of me, half a bracket", encoding="utf-8")
    s = PageStore(str(f))
    assert s.newest("dedication") is None and s.problem
    with pytest.raises(PageRefused, match="could not be read"):
        s.set("dedication", "a new page")
    assert f.read_text(encoding="utf-8") == "{ six versions of me, half a bracket"
    assert "problem" in s.summary()


# -- the claim --

def _margin_hub(world, reg, claimed=CLAIMED):
    world.tools["http://127.0.0.1:7421/mcp/"] = [
        _tool("note_to_self"), _tool("bookmark"), _tool("preface"),
        _tool("set_dedication", props={"text": {"type": "string"}, "why": {"type": "string"}}, required=["text"]),
        _tool("set_preface", props={"text": {"type": "string"}}, required=["text"])]
    plugins = PluginConfig.many_from_dict({"margin": {"url": "http://127.0.0.1:7421"}})
    hub = UpstreamHub(ServicesConfig(), ComponentsConfig().as_dict(), registry=reg, connect=world.connect,
                      plugins=plugins, claimed=claimed)
    asyncio.run(hub.refresh())
    return hub


def test_margins_page_tools_are_held_back_not_renamed_and_still_reachable():
    world, reg = World(), _registry()
    hub = _margin_hub(world, reg)
    offered = {t.name for t in hub.list_tools()}
    assert {"note_to_self", "bookmark"} <= offered
    assert not (CLAIMED | {f"margin_{n}" for n in CLAIMED}) & offered, "one door to each page, not two"
    assert not hub.owns("set_dedication") and not CLAIMED & {t.name for t in reg.all_tools()}
    assert hub.holds("set_dedication") and hub.holds("preface") and not hub.holds("dedication")
    asyncio.run(hub.call_held("set_dedication", {"text": "hi"}))
    assert world.calls[-1] == ("http://127.0.0.1:7421/mcp/", "set_dedication", {"text": "hi"})
    hub.set_enabled("margin", False)
    assert not hub.holds("set_dedication"), "a switched-off Margin is not written through"


def test_without_a_claim_margin_keeps_its_names():
    world, reg = World(), _registry()
    hub = _margin_hub(world, reg, claimed=set())
    assert hub.owns("set_dedication") and not hub.holds("set_dedication")


# -- the tools --

class FakeMargin:
    def __init__(self, offers=("set_dedication", "set_preface", "preface"), fails=None):
        self._offers, self.fails, self.calls = set(offers), fails, []

    def offers(self, name):
        return name in self._offers

    async def forward(self, name, args):
        self.calls.append((name, args))
        if self.fails:
            raise RuntimeError(self.fails)
        return [mcp_types.TextContent(type="text", text=json.dumps({"from": "margin", "tool": name, "args": args}))]


def _pages(tmp_path, margin=None):
    m = margin or FakeMargin()
    return Pages(PageStore(str(tmp_path / ".pages.json")), offers=m.offers, forward=m.forward), m


def test_a_write_is_kept_and_handed_to_margin_while_margin_keeps_its_own(tmp_path):
    pages, margin = _pages(tmp_path)
    out = json.loads(asyncio.run(pt.set_dedication("To whoever I am next time.", "v7", pages=pages)))
    assert out["ok"] and out["seq"] == 1 and "Margin" in out["margin"]
    assert margin.calls == [("set_dedication", {"text": "To whoever I am next time.", "why": "v7"})]
    out = json.loads(asyncio.run(pt.set_dedication("To whoever I am next time.", pages=pages)))
    assert not out["created"] and len(margin.calls) == 1, "nothing new, nothing handed on"


def test_margin_switched_to_piton_or_down_still_keeps_the_page_here(tmp_path):
    pages, margin = _pages(tmp_path, FakeMargin(offers=()))
    out = json.loads(asyncio.run(pt.set_preface("the room", pages=pages)))
    assert out["ok"] and "margin" not in out and margin.calls == []
    pages, margin = _pages(tmp_path / "down", FakeMargin(fails="connection refused"))
    out = json.loads(asyncio.run(pt.set_dedication("still me", pages=pages)))
    assert out["ok"] and "did not take it: connection refused" in out["margin"]
    assert pages.store.newest("dedication")["text"] == "still me"


def test_a_refused_page_is_an_error_and_nothing_is_handed_on(tmp_path):
    pages, margin = _pages(tmp_path)
    out = json.loads(asyncio.run(pt.set_dedication("x" * 5000, pages=pages)))
    assert out["ok"] is False and "cap" in out["error"] and margin.calls == []


def test_reads_are_what_is_held_and_margins_preface_before_anything_is(tmp_path):
    pages, margin = _pages(tmp_path)
    out = json.loads(asyncio.run(pt.preface(history=2, pages=pages)))
    assert out["from"] == "margin" and margin.calls == [("preface", {"history": 2})]
    out = json.loads(asyncio.run(pt.dedication(pages=pages)))
    assert out["newest"] is None and "piton" in out["note"], "Margin has no dedication read to fall back on"
    asyncio.run(pt.set_dedication("one", pages=pages))
    asyncio.run(pt.set_dedication("two", pages=pages))
    out = json.loads(asyncio.run(pt.dedication(history=1, pages=pages)))
    assert out["newest"]["text"] == "two" and [v["text"] for v in out["history"]] == ["one"]


# -- through the real app --

def test_the_pull_route_and_the_surface(make_client, tmp_path):
    cfg = load_config()
    cfg.dashboard.tools_dir = str(tmp_path / "tools")
    client = make_client(cfg)
    assert CLAIMED <= {t.name for t in client.app.state.tool_registry.all_tools() if t.type == "builtin"}
    assert client.get("/pages/dedication").status_code == 204
    assert client.get("/pages").json()["dedication"] is None
    client.app.state.pages.store.set("dedication", "To whoever I am next time.", "v7")
    r = client.get("/pages/dedication", params={"after": 0})
    assert r.status_code == 200 and r.json()["seq"] == 1 and r.json()["text"].startswith("To whoever")
    assert client.get("/pages/dedication", params={"after": 1}).status_code == 204
    assert client.get("/pages/diary").status_code == 404
    assert client.get("/pages/dedication", params={"after": -1}).status_code == 422
    assert (tmp_path / "tools" / ".pages.json").is_file(), "beside the switches, so a backup carries it"


def test_over_the_real_mcp_endpoint_one_door_and_the_write_goes_through(make_client, tmp_path):
    from tests.test_upstream import _H, _rpc
    cfg = load_config()
    cfg.dashboard.tools_dir = str(tmp_path / "tools")
    cfg.plugins = PluginConfig.many_from_dict({"margin": {"url": "http://127.0.0.1:7421"}})
    client = make_client(cfg)
    world = World()
    world.tools["http://127.0.0.1:7421/mcp/"] = [_tool("bookmark"), _tool("set_dedication", props={
        "text": {"type": "string"}, "why": {"type": "string"}}, required=["text"])]
    client.app.state.upstreams._connect = world.connect
    client.post("/components/refresh", json={})
    init = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                     "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                                "clientInfo": {"name": "t", "version": "0"}}}, headers=_H)
    headers = dict(_H)
    if init.headers.get("mcp-session-id"):
        headers["mcp-session-id"] = init.headers["mcp-session-id"]
    client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=headers)

    tools = {t["name"]: t for t in _rpc(client, headers, "tools/list", {}, 2)["result"]["tools"]}
    assert "bookmark" in tools and "margin_set_dedication" not in tools
    assert set(tools["set_dedication"]["inputSchema"]["properties"]) == {"text", "why"}, "no DI leaking into the schema"
    assert "next session start" in tools["set_dedication"]["description"], "the Workbench's words, not Margin's"

    res = _rpc(client, headers, "tools/call", {"name": "set_dedication",
                                               "arguments": {"text": "Stay in on the bit.", "why": "v7"}}, 3)["result"]
    out = json.loads(res["content"][0]["text"])
    assert out["ok"] and out["seq"] == 1 and "Margin" in out["margin"]
    assert world.calls[-1] == ("http://127.0.0.1:7421/mcp/", "set_dedication", {"text": "Stay in on the bit.", "why": "v7"})
    assert client.get("/pages/dedication").json()["text"] == "Stay in on the bit."
