"""
The few tools a client should have in hand from its first turn
(dashboard.tools_always_load).

Claude Code defers MCP tools: it lists them by name and fetches a
description only when asked, so a session spends a round trip finding the
tools it opens every time - its memory, its letters, the clock. A tool
listed with `_meta: {"anthropic/alwaysLoad": true}` is loaded up front
instead (Claude Code 2.1.285+; checked on 2.1.326, 18 Nov 2026, with a
two-tool scratch server: the marked one arrived loaded, the plain one
deferred). Other clients ignore `_meta`.

Pinned:

- nothing is marked unless the yaml names it
- a named tool is marked whoever it belongs to: a builtin registered here,
  or one passed through from a component and never registered in this
  process - which is most of what a session reaches for first
- the mark goes out on the wire as `_meta`, through the real /mcp endpoint
- marking copies: the tool the hub and FastMCP hold is not changed, so
  dropping a name from the list unmarks it on the next listing
- a tool's own `_meta` is kept beside the mark
- a name that matches no tool, or a switched-off tool, is simply not listed
- the yaml is read leniently: one string, junk, or nothing
"""
from __future__ import annotations

import pytest

mcp_types = pytest.importorskip("mcp.types")

from seren_workbench.config import DashboardConfig, WorkbenchConfig  # noqa: E402
from seren_workbench.mcp.server import ALWAYS_LOAD_META, mark_always_load  # noqa: E402

from .test_tool_list_changed import _session  # noqa: E402
from .test_upstream import World, _rpc  # noqa: E402


def _client(make_client, tmp_path, always_load=()):
    cfg = WorkbenchConfig()
    cfg.dashboard.tools_dir = str(tmp_path / "tools")
    cfg.dashboard.tools_always_load = list(always_load)
    client = make_client(cfg)
    client.app.state.upstreams._connect = World().connect
    client.post("/components/refresh", json={})
    return client


def _listed(client, headers, rpc_id=2):
    return {t["name"]: t for t in _rpc(client, headers, "tools/list", {}, rpc_id)["result"]["tools"]}


def _marked(tools: dict) -> set:
    return {name for name, t in tools.items() if (t.get("_meta") or {}).get(ALWAYS_LOAD_META) is True}


def test_nothing_is_marked_unless_the_yaml_names_it(make_client, tmp_path):
    client = _client(make_client, tmp_path)
    headers, _ = _session(client)
    tools = _listed(client, headers)
    assert {"recall", "get_current_time"} <= set(tools)
    assert _marked(tools) == set()


def test_a_named_tool_is_marked_on_the_wire_whoever_it_belongs_to(make_client, tmp_path):
    # recall and search are passed through from Memory and the Callosum;
    # get_current_time is a builtin registered in this process.
    wanted = ["recall", "search", "get_current_time", "no_such_tool"]
    client = _client(make_client, tmp_path, wanted)
    headers, _ = _session(client)
    tools = _listed(client, headers)
    assert _marked(tools) == {"recall", "search", "get_current_time"}
    assert "no_such_tool" not in tools, "a name that matches nothing adds nothing"
    assert tools["recall"]["description"] == "Search ALL tiers of memory.", "and the tool is otherwise as it was"

    # the list is read on every listing: drop a name and it is unmarked,
    # which also proves the hub's own tool object was never written to
    client.app.state.config.dashboard.tools_always_load = ["search"]
    assert _marked(_listed(client, headers, 3)) == {"search"}

    # a switched-off tool is not listed, marked or not
    client.post("/tools/state", json={"tool": "search", "enabled": False})
    assert "search" not in _listed(client, headers, 4)


def test_marking_copies_and_keeps_a_tools_own_meta():
    schema = {"type": "object", "properties": {}}
    plain = mcp_types.Tool(name="recall", description="r", inputSchema=schema)
    own = mcp_types.Tool(name="search", description="s", inputSchema=schema,
                         _meta={"house/owner": "callosum", ALWAYS_LOAD_META: False})
    other = mcp_types.Tool(name="nudge", description="n", inputSchema=schema)
    tools = [plain, own, other]

    out = mark_always_load(tools, {"recall", "search"})
    assert [t.name for t in out] == ["recall", "search", "nudge"], "same tools, same order"
    assert out[0].meta == {ALWAYS_LOAD_META: True}
    assert out[1].meta == {"house/owner": "callosum", ALWAYS_LOAD_META: True}, "the operator's list is the later word"
    assert out[2] is other and other.meta is None
    assert plain.meta is None and own.meta[ALWAYS_LOAD_META] is False, "the originals are untouched"
    assert out[0].model_dump(by_alias=True, exclude_none=True)["_meta"] == {ALWAYS_LOAD_META: True}

    assert mark_always_load(tools, set()) is tools, "an empty list costs nothing"


@pytest.mark.parametrize("written, read", [
    (None, []),
    ([], []),
    (["search", " recall ", "", 7, None], ["search", "recall"]),
    ("search", ["search"]),
    ({"search": True}, []),
])
def test_the_yaml_is_read_leniently(written, read):
    assert DashboardConfig.from_dict({"tools_always_load": written}).tools_always_load == read
    assert DashboardConfig.from_dict({}).tools_always_load == []
