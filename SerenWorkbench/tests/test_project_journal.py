"""
Life after approval: status and the journal.

THE CLAIMS: the model writes the journal and may move a project between
in_progress and paused; it can only ASK for done; a person confirms done;
a revision agreed against a done project reopens it; and none of this
touches what runs.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from seren_workbench.config import WorkbenchConfig, DashboardConfig, ServicesConfig
from seren_meninges import ServerConfig, TlsConfig



def _call(client, fn, **kw):
    return json.loads(asyncio.run(fn(proposals=client.app.state.proposals, **kw)))


def _project(client, **kw):
    from seren_workbench.models.tools.project_tools import propose_project
    kw.setdefault("name", "board-eyes")
    kw.setdefault("title", "Board eyes")
    kw.setdefault("plan", "# Board eyes: export the board as JSON.")
    kw.setdefault("rationale", "Numbers beat pixels.")
    kw.setdefault("workspace", "/home/alice/mods/inscryption")
    kw.setdefault("downloads", [{"url": "https://github.com/BepInEx/BepInEx/releases", "purpose": "BepInEx"}])
    kw.setdefault("needs", [])
    return _call(client, propose_project, **kw)


def _revision(client, **kw):
    from seren_workbench.models.tools.project_tools import propose_revision
    kw.setdefault("project", "board-eyes")
    kw.setdefault("note", "a change")
    return _call(client, propose_revision, **kw)


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


def _agreed(client, **kw):
    p = _project(client, needs=[], **kw)
    r = client.post(f"/proposals/{p['proposal_id']}/approve").json()
    assert r["ok"] is True
    return "board-eyes"


def _log(client, **kw):
    from seren_workbench.models.tools.project_tools import project_log
    kw.setdefault("project", "board-eyes")
    return _call(client, project_log, **kw)


def test_the_first_entry_starts_the_work(client, dirs):
    name = _agreed(client)
    assert client.get(f"/projects/{name}").json()["state"]["status"] == "agreed"
    r = _log(client, entry="Installed BepInEx 5 x86. Game launches clean.",
             files=["winhttp.dll", "doorstop_config.ini", "BepInEx/"])
    assert r["ok"] is True and r["status"] == "in_progress" and r["entry_id"] == 1
    p = client.get(f"/projects/{name}").json()
    assert p["state"]["status"] == "in_progress"
    assert p["journal"][0]["by"] == "model" and p["journal"][0]["files"] == ["winhttp.dll", "doorstop_config.ini", "BepInEx/"]
    assert p["files_touched"] == ["BepInEx/", "doorstop_config.ini", "winhttp.dll"]
    assert (dirs / "projects" / name / "journal.json").is_file()


def test_files_are_relative_to_the_workspace(client):
    _agreed(client)
    assert "relative" in _log(client, entry="x", files=["C:/Windows/system32/evil.dll"])["error"]
    assert "relative" in _log(client, entry="x", files=["../other-game/thing"])["error"]
    assert "relative" in _log(client, entry="x", files=["/etc/passwd"])["error"]
    assert _log(client, entry="x", files=["BepInEx\\plugins\\WrensEyes\\WrensEyes.dll"])["ok"] is True


def test_the_model_may_pause_and_resume_but_not_finish(client):
    name = _agreed(client)
    assert _log(client, entry="pausing for the night", status="paused")["status"] == "paused"
    assert _log(client, entry="back at it", status="in_progress")["status"] == "in_progress"
    r = _log(client, entry="board.json matches the screen on every play", status="done")
    assert r["status"] == "in_progress" and r["done_requested"] is True
    st = client.get(f"/projects/{name}").json()["state"]
    assert st["status"] == "in_progress" and st["done_requested"] is True
    assert "agreed" in _log(client, entry="x", status="agreed")["error"]


def test_a_person_confirms_done(client):
    name = _agreed(client)
    _log(client, entry="finished", status="done")
    ok = client.post(f"/projects/{name}/done", json={"note": "played three fights, numbers matched"}).json()
    assert ok["ok"] is True and ok["state"]["status"] == "done" and ok["state"]["done_requested"] is False
    p = client.get(f"/projects/{name}").json()
    assert p["journal"][-1]["by"] == "operator" and "three fights" in p["journal"][-1]["text"]
    # Done is closed to the model.
    assert "reopens only through an approved revision" in _log(client, entry="one more thing", status="in_progress")["error"]


def test_done_is_not_reachable_from_the_tool_surface(client):
    names = {t["name"] for t in client.get("/tools").json()["tools"]}
    assert "project_log" in names
    for forbidden in ("confirm_done", "project_done", "set_project_status"):
        assert forbidden not in names


def test_a_revision_reopens_a_done_project_and_only_a_done_one(client):
    name = _agreed(client)
    _log(client, entry="working")
    r = _revision(client, workspace="/home/alice/mods/a")
    client.post(f"/proposals/{r['proposal_id']}/approve")
    assert client.get(f"/projects/{name}").json()["state"]["status"] == "in_progress", \
        "a revision against live work leaves the status alone"

    client.post(f"/projects/{name}/done", json={})
    assert client.get(f"/projects/{name}").json()["state"]["status"] == "done"
    r = _revision(client, note="Adrenaline steal can't pick an item; the item picker needs the board too.",
                  workspace="/home/alice/mods/b")
    client.post(f"/proposals/{r['proposal_id']}/approve")
    p = client.get(f"/projects/{name}").json()
    assert p["state"]["status"] == "in_progress" and p["state"]["reopened_by"] == r["proposal_id"]
    assert p["journal"][-1]["by"] == "workbench" and "Reopened" in p["journal"][-1]["text"]


def test_an_operator_can_reopen_by_hand(client):
    name = _agreed(client)
    client.post(f"/projects/{name}/done", json={})
    r = client.post(f"/projects/{name}/status", json={"status": "in_progress", "note": "not quite"}).json()
    assert r["ok"] is True and r["state"]["status"] == "in_progress"
    bad = client.post(f"/projects/{name}/status", json={"status": "done"}).json()
    assert bad["ok"] is False


def test_the_model_reads_its_own_cabinet(client):
    from seren_workbench.models.tools.project_tools import list_my_projects
    name = _agreed(client)
    _log(client, entry="first", commit="deadbeefcafe")
    _log(client, entry="second")
    mine = _call(client, list_my_projects)
    assert mine["projects"][0]["status"] == "in_progress" and mine["projects"][0]["journal_count"] == 2
    one = _call(client, list_my_projects, name=name, limit=1)
    assert [e["text"] for e in one["journal"]] == ["second"]
    assert one["state"]["status"] == "in_progress"
    everything = _call(client, list_my_projects, name=name, limit=0)
    assert everything["journal"][0]["commit"] == "deadbeefcafe"


def test_a_commit_must_look_like_one(client):
    _agreed(client)
    assert "git hash" in _log(client, entry="x", commit="main")["error"]


def test_the_viewer_has_a_cabinet(client):
    html = client.get("/viewer").text
    assert 'data-tab="projects"' in html, "no Projects tab"
    assert 'id="projects-body"' in html
    assert "refreshProjects" in html and "/projects" in html
    assert "confirm done" in html
