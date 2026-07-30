"""
Toolbox grouping and human-readable names.

The dashboard used to be one flat scroll of snake_case. This is the layer
that makes it `Toolbox > tool` with a readable label on the head and the
callable identifier inside — so a non-technical operator can find a tool,
and an auditor can still see exactly what it's called.

DERIVE, THEN LET IT BE DECLARED. Pure derivation grouped by module and
produced fifteen boxes, seven of them holding a single tool — and it could
never put wait_for_service_tool.py in the same box as service_control_tools.py,
which is the grouping a person actually wants. Pure declaration would drift
the first time someone added a tool and forgot the key. So: per-tool beats
module-level beats derived, and most tools declare nothing.
"""
from __future__ import annotations

import textwrap

import pytest

from seren_workbench.config import WorkbenchConfig, DashboardConfig, ServicesConfig
from seren_workbench.tool_registry import (
    build_registry, humanise, toolbox_from_module,
)
from seren_meninges import ServerConfig, TlsConfig


# ── Derivation ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,pretty", [
    ("get_cluster_status", "Get Cluster Status"),
    ("remember", "Remember"),
    ("list_my_proposals", "List My Proposals"),
    ("fetch_url", "Fetch URL"),          # acronym, not "Url"
    ("get_api_id", "Get API ID"),
    ("", ""),
])
def test_humanise(raw, pretty):
    assert humanise(raw) == pretty


@pytest.mark.parametrize("module,box", [
    ("cluster_tools", "Cluster"),          # trailing _tools is noise on a box
    ("scheduler_tools", "Scheduler"),
    ("wait_for_service_tool", "Wait For Service"),
    ("", "Other"),
])
def test_toolbox_from_module(module, box):
    assert toolbox_from_module(module) == box


# ── Builtins ───────────────────────────────────────────────────────────

def _by_name():
    return {t.name: t for t in build_registry(tools_dir="/nonexistent").all_tools()}


def test_every_builtin_gets_a_box_and_a_label():
    """No tool may fall out of the tree — an ungrouped tool is invisible in
    a grouped view, which is worse than an ugly name."""
    for t in _by_name().values():
        assert t.toolbox, f"{t.name} has no toolbox"
        assert t.display_name, f"{t.name} has no display_name"


def test_a_module_constant_regroups_its_tools():
    """The three service modules land in ONE box. Derivation alone would
    have made three, one of them holding a single tool."""
    tools = _by_name()
    for n in ("start_service", "restart_service",
              "ensure_service_running", "wait_for_service"):
        assert tools[n].toolbox == "Services", n


def test_a_per_tool_key_beats_the_module_constant():
    """introspection_and_agency_tools.py is the case a module-level default
    can't express: its tools belong in two different boxes."""
    tools = _by_name()
    assert tools["time_since_last_message"].toolbox == "Time & Self"
    assert tools["preserve_memory_verbatim"].toolbox == "Memory"
    assert tools["promote_memory_now"].toolbox == "Memory"


def test_grouping_actually_reduces_the_scroll():
    """The whole point. If this ever climbs back toward one-box-per-module
    the view has regressed to a flat list wearing a costume."""
    tools = list(_by_name().values())
    boxes = {t.toolbox for t in tools}
    assert len(boxes) <= 12, f"too many boxes to scan: {sorted(boxes)}"
    singles = [b for b in boxes if sum(1 for t in tools if t.toolbox == b) == 1]
    assert len(singles) <= 2, f"too many one-tool boxes: {singles}"


# ── Custom (manifest) tools ────────────────────────────────────────────

def _client_with(manifest_name: str, body: str, make_client, tmp_path):
    # NOTE: no dedent here. YAML is indentation-sensitive and these fixtures
    # compose fragments, so dedent-on-an-f-string silently produced a
    # manifest that parsed to nothing and a test that failed for the wrong
    # reason. Write the YAML flush-left and keep it literal.
    tools = tmp_path / "tools"; tools.mkdir()
    (tools / manifest_name).write_text(body)
    return make_client(WorkbenchConfig(
        server=ServerConfig(), tls=TlsConfig(),
        dashboard=DashboardConfig(tools_dir=str(tools)),
        services=ServicesConfig(),
    ))


_TOOL = (
    "  - name: count_dogs\n"
    "    description: Counts hot dogs.\n"
    '    invoke: {kind: process, argv: ["/bin/true"]}\n'
)


def test_custom_toolbox_falls_back_to_the_file_name(make_client, tmp_path):
    """Drop in hotdog-math.yaml and get a 'Hotdog Math' box for free — no
    new key to learn for the operator who just wants one tool."""
    c = _client_with("hotdog-math.yaml", f"schema_version: 1\ntools:\n{_TOOL}",
                     make_client, tmp_path)
    t = {x["name"]: x for x in c.get("/tools").json()["tools"]}["count_dogs"]
    assert t["toolbox"] == "Hotdog Math"
    assert t["display_name"] == "Count Dogs"
    assert t["type"] == "dynamic"


def test_manifest_metadata_can_name_the_toolbox(make_client, tmp_path):
    c = _client_with("misc.yaml",
                     "schema_version: 1\n"
                     "metadata:\n"
                     "  toolbox: Costco Science\n"
                     f"tools:\n{_TOOL}",
                     make_client, tmp_path)
    t = {x["name"]: x for x in c.get("/tools").json()["tools"]}["count_dogs"]
    assert t["toolbox"] == "Costco Science"


def test_a_tool_can_override_its_files_toolbox_and_label(make_client, tmp_path):
    c = _client_with("misc.yaml",
                     "schema_version: 1\n"
                     "metadata:\n"
                     "  toolbox: Costco Science\n"
                     "tools:\n"
                     "  - name: count_dogs\n"
                     '    display_name: "Hot Dog Counter (v2)"\n'
                     "    toolbox: Serious Business\n"
                     "    description: Counts hot dogs.\n"
                     '    invoke: {kind: process, argv: ["/bin/true"]}\n',
                     make_client, tmp_path)
    t = {x["name"]: x for x in c.get("/tools").json()["tools"]}["count_dogs"]
    assert t["toolbox"] == "Serious Business"
    assert t["display_name"] == "Hot Dog Counter (v2)"
    assert t["name"] == "count_dogs", "the callable identifier must not change"


# ── Both endpoints carry it ────────────────────────────────────────────

def test_tools_and_tool_state_agree(make_client, tmp_path):
    """The toggle for a tool has to sit where you just saw the tool, so both
    endpoints need the same grouping — not one of them."""
    c = _client_with("hotdog-math.yaml", f"schema_version: 1\ntools:\n{_TOOL}",
                     make_client, tmp_path)
    a = {x["name"]: x for x in c.get("/tools").json()["tools"]}
    b = {x["name"]: x for x in c.get("/tools/state").json()["tools"]}
    assert set(a) == set(b)
    for name in a:
        assert a[name]["toolbox"] == b[name]["toolbox"], name
        assert a[name]["display_name"] == b[name]["display_name"], name


# ── The viewer renders the tree ────────────────────────────────────────

def test_the_viewer_groups_instead_of_listing(client):
    html = client.get("/viewer").text
    assert "groupByToolbox" in html, "no grouping in the view"
    assert "Custom Toolboxes" in html, "custom tools have no container"
    assert "tbox-body" in html and ".tbox .tbox" in html, "no nesting styles"
    # The head shows the human name; the raw identifier lives inside.
    assert "display_name" in html and "raw-name" in html


def test_filtering_opens_the_groups_it_matches(client):
    """A filter whose matches stay hidden inside collapsed groups is worse
    than no filter at all."""
    html = client.get("/viewer").text
    assert "forced" in html, "groupHtml has no force-open path"
