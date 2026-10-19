"""
The cabinet: where projects live (17 Oct 2026).

THE CLAIMS UNDER TEST:
  - each agreed project gets a drawer, <cabinet>/<name>, for its own files,
    reported in its grants and to the model, apart from its workspace;
  - the cabinet is an operator setting kept AS WRITTEN (`~` and all),
    because it is a path on the box the work is done on, which may not be
    this one; blank means `projects` beside the tools folder;
  - it is never the contract folder, and no reviewed contract names it;
  - a cabinet that cannot be used says why and gives no drawers, rather
    than quietly sending the work somewhere nobody chose.
"""
from __future__ import annotations

import json
import os

import pytest

from seren_meninges import ServerConfig, TlsConfig
from seren_workbench import projects as P
from seren_workbench.config import DashboardConfig, ServicesConfig, WorkbenchConfig

from .test_projects import _call, _project

DESK = r"~\seren\wren\stores\workbench\projects"


def _client(make_client, tools, cabinet: str = ""):
    return make_client(WorkbenchConfig(
        server=ServerConfig(), tls=TlsConfig(),
        dashboard=DashboardConfig(tools_dir=str(tools), cabinet=cabinet),
        services=ServicesConfig(),
    ))


@pytest.fixture
def tools(tmp_path):
    t = tmp_path / "stores" / "workbench" / "tools"
    (t / "proposed").mkdir(parents=True)
    return t


def _agree(client):
    r = _project(client)
    assert client.post(f"/proposals/{r['proposal_id']}/approve").json()["ok"] is True
    return r


# ── the pure parts ─────────────────────────────────────────────────────

def test_the_default_is_projects_beside_the_tools_folder(tmp_path):
    d = DashboardConfig(tools_dir=str(tmp_path / "stores" / "workbench" / "tools"))
    assert d.resolve_cabinet() == str(tmp_path / "stores" / "workbench" / "projects")
    # a trailing separator on tools_dir does not move it
    d2 = DashboardConfig(tools_dir=str(tmp_path / "stores" / "workbench" / "tools") + os.sep)
    assert d2.resolve_cabinet() == d.resolve_cabinet()


def test_a_written_cabinet_is_kept_as_written():
    d = DashboardConfig.from_dict({"tools_dir": "/srv/wb/tools", "cabinet": DESK})
    assert d.cabinet == DESK and d.resolve_cabinet() == DESK   # `~` NOT expanded here
    assert DashboardConfig.from_dict({"tools_dir": "/srv/wb/tools"}).cabinet == ""


def test_the_env_can_set_it(monkeypatch, tmp_path):
    from seren_workbench.config import load_config
    monkeypatch.setenv("SEREN_WORKBENCH_CABINET", "~/elsewhere/projects")
    cfg = load_config(str(tmp_path / "missing.yaml"))
    assert cfg.dashboard.resolve_cabinet() == "~/elsewhere/projects"


@pytest.mark.parametrize("path", [
    "/mnt/nvme/seren/wren/stores/workbench/projects", r"C:\Users\Caesar\seren\wren\projects",
    "C:/Users/Caesar/projects", r"\\nas\share\projects", "~", "~/projects", DESK,
])
def test_check_cabinet_takes_absolute_and_home_paths(path):
    assert P.check_cabinet(path) == path


@pytest.mark.parametrize("path,why", [
    ("", "empty"), ("projects", "absolute"), ("./projects", "absolute"),
    ("~user/projects", "absolute"), ("/srv/../etc", ".."), (r"C:\a\..\b", ".."),
])
def test_check_cabinet_refuses_what_could_point_anywhere(path, why):
    with pytest.raises(P.ProjectError, match=why if why != "empty" else "empty"):
        P.check_cabinet(path)


def test_a_drawer_is_joined_the_way_the_cabinet_is_written():
    assert P.drawer("/mnt/x/projects", "board-eyes") == "/mnt/x/projects/board-eyes"
    assert P.drawer("/mnt/x/projects/", "board-eyes") == "/mnt/x/projects/board-eyes"
    assert P.drawer(r"C:\seren\projects", "board-eyes") == r"C:\seren\projects\board-eyes"
    assert P.drawer(DESK, "board-eyes") == DESK + r"\board-eyes"
    assert P.drawer("~/projects", "board-eyes") == "~/projects/board-eyes"
    assert P.drawer("~", "board-eyes") == "~/board-eyes"
    assert P.drawer("/", "board-eyes") == "/board-eyes"
    assert P.drawer(None, "board-eyes") is None


# ── through the Workbench ──────────────────────────────────────────────

def test_an_agreed_project_has_a_drawer_in_the_default_cabinet(make_client, tools):
    client = _client(make_client, tools)
    _agree(client)
    cab = tools.parent / "projects"

    listing = client.get("/projects").json()
    assert listing["cabinet"]["path"] == str(cab)
    assert listing["cabinet"]["configured"] is False and listing["cabinet"]["error"] is None
    assert listing["cabinet"]["on_this_box"] is False          # nothing made it yet
    assert listing["cabinet"]["contracts"] == str(tools / "projects")
    assert listing["projects"][0]["drawer"] == P.drawer(str(cab), "board-eyes")

    g = client.get("/projects/board-eyes/grants").json()
    assert g["drawer"] == P.drawer(str(cab), "board-eyes")
    assert g["workspace"].endswith("Inscryption")             # the workspace is still its own thing

    # Approval wrote the contract where it always did, and made no drawer:
    # the Workbench records where the work lives, the harness keeps it there.
    assert (tools / "projects" / "board-eyes" / "project.yaml").is_file()
    assert not cab.exists()
    cab.mkdir()
    assert client.get("/projects").json()["cabinet"]["on_this_box"] is True


def test_a_cabinet_on_another_box_is_reported_as_written(make_client, tools):
    client = _client(make_client, tools, cabinet=DESK)
    _agree(client)
    cab = client.get("/projects").json()["cabinet"]
    assert cab == {**cab, "path": DESK, "configured": True, "on_this_box": False, "error": None}
    assert client.get("/projects/board-eyes/grants").json()["drawer"] == DESK + r"\board-eyes"

    # The reviewed contract names no drawer: the cabinet is the operator's
    # setting, so moving it moves every drawer and no proposal can change it.
    text = (tools / "projects" / "board-eyes" / "project.yaml").read_text(encoding="utf-8")
    assert "drawer" not in text and "cabinet" not in text


def test_the_model_is_told_where_its_files_go(make_client, tools):
    from seren_workbench.models.tools.project_tools import LIST_PROJECTS_TOOL_DEF, list_my_projects
    client = _client(make_client, tools, cabinet=DESK)
    _agree(client)
    many = _call(client, list_my_projects)
    assert many["cabinet"] == DESK and many["projects"][0]["drawer"] == DESK + r"\board-eyes"
    one = _call(client, list_my_projects, name="board-eyes")
    assert one["cabinet"] == DESK and one["project"]["drawer"] == DESK + r"\board-eyes"
    assert "drawer" in LIST_PROJECTS_TOOL_DEF["description"]
    assert "not in the workspace" in LIST_PROJECTS_TOOL_DEF["description"]


def test_an_unusable_cabinet_says_why_and_gives_no_drawer(make_client, tools):
    client = _client(make_client, tools, cabinet="projects")      # relative: unusable
    _agree(client)                                                 # the Workbench still works
    cab = client.get("/projects").json()["cabinet"]
    assert cab["path"] is None and "not an absolute path" in cab["error"]
    assert client.get("/projects/board-eyes/grants").json()["drawer"] is None
    from seren_workbench.models.tools.project_tools import list_my_projects
    many = _call(client, list_my_projects)
    assert many["cabinet"] is None and "not an absolute path" in many["cabinet_error"]


def test_the_config_tab_shows_it(make_client, tools):
    client = _client(make_client, tools, cabinet=DESK)
    assert client.get("/config").json()["dashboard"]["cabinet"] == DESK


def test_the_viewer_has_the_location_and_the_count_bar(make_client, tools):
    html = _client(make_client, tools).get("/viewer").text
    assert 'id="projects-bar"' in html and 'id="projects-info"' in html
    assert "cabinetLineHtml" in html and "data-proj-view" in html
    assert "completed" in html and "drawer:" in html
