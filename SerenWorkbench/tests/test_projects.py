"""
Project proposals - a piece of work asked for all at once, and grown by
review.

THE CLAIM UNDER TEST: approving a project approves the plan and the limits
and NEVER a tool. Every need is its own proposal with its own gates, and a
project's folder is as inert as a staged manifest.
"""
from __future__ import annotations

import asyncio
import json
import sys
import textwrap

import pytest

from seren_workbench.config import WorkbenchConfig, DashboardConfig, ServicesConfig
from seren_meninges import ServerConfig, TlsConfig


def _manifest(name: str = "count_widgets", text: str = "42") -> str:
    argv = [sys.executable, "-c", f"print('{text}')"]
    return textwrap.dedent("""\
        schema_version: 1
        tools:
          - name: %s
            description: Counts the widgets. Use when someone asks how many.
            invoke:
              kind: process
              argv: %s
            parameters: []
    """) % (name, json.dumps(argv))


PLAN = textwrap.dedent("""\
    # Board eyes

    A BepInEx plugin for Inscryption that writes the **board state** out as JSON.

    ## Steps
    1. Install BepInEx into the game folder.
    2. Build the plugin against the game's assemblies.
    3. Write `board.json` on every state change.

    > Out of scope: anything that changes how the game plays.
""")


@pytest.fixture
def dirs(tmp_path):
    tools = tmp_path / "tools"; tools.mkdir()
    (tools / "proposed").mkdir()
    return tools


@pytest.fixture
def client(dirs, make_client):
    return make_client(WorkbenchConfig(
        server=ServerConfig(), tls=TlsConfig(),
        dashboard=DashboardConfig(tools_dir=str(dirs)),
        services=ServicesConfig(),
    ))


def _names(client) -> set[str]:
    return {t["name"] for t in client.get("/tools").json()["tools"]}


def _call(client, fn, **kw):
    return json.loads(asyncio.run(fn(proposals=client.app.state.proposals, **kw)))


def _project(client, **kw):
    from seren_workbench.models.tools.project_tools import propose_project
    kw.setdefault("name", "board-eyes")
    kw.setdefault("title", "Board eyes")
    kw.setdefault("plan", PLAN)
    kw.setdefault("rationale", "Card symbols are unreadable over a screen share; numbers are not.")
    kw.setdefault("workspace", r"D:\SteamLibrary\steamapps\common\Inscryption")
    kw.setdefault("testing", "Run the game, start a Leshy fight, check board.json updates.")
    kw.setdefault("downloads", [
        {"url": "https://github.com/BepInEx/BepInEx/releases", "purpose": "the BepInEx 5 release zip"},
    ])
    kw.setdefault("needs", [
        {"kind": "tool", "manifest": _manifest("count_widgets"),
         "rationale": "the plugin's build needs widgets counted"},
    ])
    return _call(client, propose_project, **kw)


def _revision(client, **kw):
    from seren_workbench.models.tools.project_tools import propose_revision
    kw.setdefault("project", "board-eyes")
    kw.setdefault("note", "Thunderstore hosts the modding API.")
    return _call(client, propose_revision, **kw)


# ══ The negative claim ═════════════════════════════════════════════════

def test_proposing_a_project_creates_no_tool_and_no_folder(client, dirs):
    r = _project(client)
    assert r["ok"] is True and r["status"] == "pending"
    assert "count_widgets" not in _names(client)
    assert not (dirs / "projects").exists()
    client.post("/tools/manifests/reload")
    assert "count_widgets" not in _names(client)


def test_approving_a_project_agrees_to_the_limits_and_turns_nothing_on(client, dirs):
    r = _project(client)
    child = r["needs"][0]["proposal"]

    ok = client.post(f"/proposals/{r['proposal_id']}/approve").json()
    assert ok["ok"] is True and ok["enabled"] is False
    assert "Nothing was switched on" in ok["next_step"]
    assert "1 need(s) still wait" in ok["next_step"]

    home = dirs / "projects" / "board-eyes"
    assert (home / "project.yaml").is_file()
    assert (home / "plan.md").read_text(encoding="utf-8").startswith("# Board eyes")
    assert (home / "testing.md").is_file()
    assert json.loads((home / "history.json").read_text())[0]["kind"] == "project"

    # The need is untouched: still pending, still not a tool.
    assert client.get(f"/proposals/{child}").json()["status"] == "pending"
    assert "count_widgets" not in _names(client)

    # The grants say so too.
    g = client.get("/projects/board-eyes/grants").json()
    assert g["workspace"].endswith("Inscryption")
    assert g["allowed_download_prefixes"] == ["https://github.com/BepInEx/BepInEx/releases"]
    assert g["needs"][0]["status"] == "pending"


def test_the_need_goes_through_its_own_gates(client):
    r = _project(client)
    child = r["needs"][0]["proposal"]
    client.post(f"/proposals/{r['proposal_id']}/approve")
    client.post(f"/proposals/{child}/approve")
    assert "count_widgets" in _names(client)
    tool = {t["name"]: t for t in client.get("/tools").json()["tools"]}["count_widgets"]
    assert tool["enabled"] is False, "approved, still off - the second gate holds"
    g = client.get("/projects/board-eyes/grants").json()
    assert g["needs"][0]["status"] == "approved"


def test_children_carry_their_family(client):
    r = _project(client)
    child = client.get(f"/proposals/{r['needs'][0]['proposal']}").json()
    assert child["parent"] == r["proposal_id"]
    assert child["for_project"] == "board-eyes"
    detail = client.get(f"/proposals/{r['proposal_id']}").json()
    assert detail["texts"]["plan"].startswith("# Board eyes")
    assert [c["id"] for c in detail["children"]] == [child["id"]]


def test_a_bad_need_stages_nothing(client):
    r = _project(client, needs=[
        {"kind": "tool", "manifest": _manifest("good_one"), "rationale": "fine"},
        {"kind": "tool", "manifest": "tools: []", "rationale": "declares nothing"},
    ])
    assert "ok" not in r and "need could not be staged" in r["error"]
    assert client.get("/proposals").json()["count"] == 0, "the good need must not be left behind"


# ══ Refusals ═══════════════════════════════════════════════════════════

def test_downloads_need_a_purpose_and_carry_no_secrets(client):
    r = _project(client, downloads=[{"url": "https://example.com/x", "purpose": ""}])
    assert "purpose" in r["error"]
    r = _project(client, downloads=[{"url": "https://me:pw@example.com/x", "purpose": "a zip"}])
    assert "password" in r["error"]
    r = _project(client, downloads=[{"url": "ftp://example.com/x", "purpose": "a zip"}])
    assert "http(s)" in r["error"]


def test_workspace_must_be_absolute_and_stay_put(client):
    assert "absolute" in _project(client, workspace="mods/here")["error"]
    assert ".." in _project(client, workspace=r"D:\games\..\secrets")["error"]
    assert _project(client, workspace="/home/alice/mods")["ok"] is True


def test_a_plan_is_required(client):
    assert "plan is empty" in _project(client, plan="   ")["error"]


def test_project_names_cannot_collide(client):
    assert _project(client)["ok"] is True
    again = _project(client)
    assert "already awaiting review" in again["error"]


def test_rejecting_a_project_takes_its_pending_needs_with_it(client):
    """A need exists for its project. Leaving it pending after the project
    is refused would block the revised project with its own orphan."""
    p = _project(client)
    child = p["needs"][0]["proposal"]
    client.post(f"/proposals/{p['proposal_id']}/reject", json={"critique": "workspace too wide"})
    c = client.get(f"/proposals/{child}").json()
    assert c["status"] == "rejected" and "workspace too wide" in c["critique"]
    assert c["final"] is False, "the tool may be wanted honestly elsewhere"


def test_a_rejected_project_can_be_revised_and_a_final_one_cannot(client):
    first = _project(client)["proposal_id"]
    client.post(f"/proposals/{first}/reject", json={"critique": "the workspace is the whole drive"})
    second = _project(client, supersedes=first, workspace="/home/alice/mods")
    assert second["ok"] is True and second["attempt"] == 2
    client.post(f"/proposals/{second['proposal_id']}/reject", json={"critique": "no mods.", "final": True})
    third = _project(client, supersedes=second["proposal_id"])
    assert "permanently" in third["error"] and "no mods." in third["error"]


# ══ Revisions ══════════════════════════════════════════════════════════

def test_a_revision_needs_an_approved_project(client):
    r = _revision(client)
    assert "no approved project" in r["error"]
    p = _project(client)
    r = _revision(client)
    assert "no approved project" in r["error"], "pending is not approved"


def test_a_revision_shows_the_diff_and_applies_on_approval(client, dirs):
    p = _project(client)
    client.post(f"/proposals/{p['proposal_id']}/approve")

    r = _revision(client,
                  downloads=[
                      {"url": "https://github.com/BepInEx/BepInEx/releases", "purpose": "the BepInEx 5 release zip"},
                      {"url": "https://thunderstore.io/c/inscryption", "purpose": "the Inscryption modding API"},
                  ],
                  workspace="/home/alice/mods/inscryption",
                  needs=[{"kind": "tool", "manifest": _manifest("count_sprockets"),
                          "rationale": "sprockets too"}])
    assert r["ok"] is True
    ch = r["changes"]
    assert [d["url"] for d in ch["downloads_added"]] == ["https://thunderstore.io/c/inscryption"]
    assert ch["downloads_removed"] == []
    assert ch["workspace"]["to"] == "/home/alice/mods/inscryption"
    assert len(ch["needs_added"]) == 1 and ch["plan_changed"] is False

    # Until approved, the live contract is as agreed.
    g = client.get("/projects/board-eyes/grants").json()
    assert len(g["allowed_download_prefixes"]) == 1

    ok = client.post(f"/proposals/{r['proposal_id']}/approve").json()
    assert ok["ok"] is True
    g = client.get("/projects/board-eyes/grants").json()
    assert "https://thunderstore.io/c/inscryption" in g["allowed_download_prefixes"]
    assert g["workspace"] == "/home/alice/mods/inscryption"
    assert len(g["needs"]) == 2
    home = dirs / "projects" / "board-eyes"
    assert len(list(home.glob("project.*.yaml"))) == 1, "the previous contract is kept beside it"
    hist = json.loads((home / "history.json").read_text())
    assert [h["kind"] for h in hist] == ["project", "revision"]


def test_one_pending_revision_at_a_time_and_a_note_is_required(client):
    p = _project(client)
    client.post(f"/proposals/{p['proposal_id']}/approve")
    assert "note is required" in _revision(client, note=" ")["error"]
    assert _revision(client, workspace="/a/b")["ok"] is True
    assert "already awaiting review" in _revision(client, workspace="/a/c")["error"]


def test_a_rejected_revision_leaves_the_project_as_agreed(client):
    p = _project(client)
    client.post(f"/proposals/{p['proposal_id']}/approve")
    r = _revision(client, downloads=[])
    client.post(f"/proposals/{r['proposal_id']}/reject", json={"critique": "keep the BepInEx site"})
    g = client.get("/projects/board-eyes/grants").json()
    assert g["allowed_download_prefixes"] == ["https://github.com/BepInEx/BepInEx/releases"]


# ══ The model's view ═══════════════════════════════════════════════════

def test_the_model_can_read_what_was_agreed(client):
    from seren_workbench.models.tools.project_tools import list_my_projects
    p = _project(client)
    assert _call(client, list_my_projects)["count"] == 0
    client.post(f"/proposals/{p['proposal_id']}/approve")
    mine = _call(client, list_my_projects)
    assert mine["count"] == 1 and mine["projects"][0]["name"] == "board-eyes"
    one = _call(client, list_my_projects, name="board-eyes")
    assert one["plan"].startswith("# Board eyes") and one["history"][0]["kind"] == "project"


def test_the_project_tools_are_proposal_tools(client):
    names = _names(client)
    assert {"propose_project", "propose_revision", "list_my_projects"} <= names
    for forbidden in ("approve_project", "approve_revision", "approve_proposal"):
        assert forbidden not in names


def test_project_tools_vanish_with_proposals(dirs, make_client):
    c = make_client(WorkbenchConfig(
        server=ServerConfig(), tls=TlsConfig(),
        dashboard=DashboardConfig(tools_dir=str(dirs), proposals_enabled=False),
        services=ServicesConfig(),
    ))
    names = {t["name"] for t in c.get("/tools").json()["tools"]}
    assert not ({"propose_project", "propose_revision", "list_my_projects"} & names)


def test_list_my_proposals_shows_the_family(client):
    from seren_workbench.models.tools.proposal_tools import list_my_proposals
    p = _project(client)
    mine = _call(client, list_my_proposals)
    by_id = {x["id"]: x for x in mine["proposals"]}
    assert by_id[p["proposal_id"]]["kind"] == "project"
    child = by_id[p["needs"][0]["proposal"]]
    assert child["parent"] == p["proposal_id"] and child["for_project"] == "board-eyes"


# ══ The viewer ═════════════════════════════════════════════════════════

def test_the_plan_button_survives_showing_the_contract(client):
    """Found on the live viewer: 'show the contract' replaced the
    detail box's innerHTML and the plan button went with it."""
    html = client.get("/viewer").text
    card = html.split("function proposalCardHtml")[1].split("function projectSummaryHtml")[0]
    assert card.index("${planBtn") < card.index('class="prop-detail"'), (
        "the plan button must be rendered outside the detail box"
    )


def test_the_viewer_renders_a_plan_rather_than_dumping_it(client):
    html = client.get("/viewer").text
    assert 'id="plan-dialog"' in html, "no plan page"
    assert "renderMarkdown" in html
    assert 'id="proposals-kind"' in html, "no kind filter"
    assert "/projects" in html or "grants" in html
