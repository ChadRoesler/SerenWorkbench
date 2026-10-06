"""
What the Workbench keeps, and snapshots of it (seren_workbench.keeping).

The tools folder is a model's own surface: the manifests it proposed and got,
the plugins, the proposals with their critiques, and the switches. None of it
can be rebuilt from anywhere else, and losing only the switches is the worst
case - every tool that was deliberately off comes back on. So:

  - the same routes as the rest of the family (GET /stores, POST
    /stores/snapshot, the archive, the rehearsal), so a Lodestar pulls these
    with everything else
  - a rehearsal that reads the restored copy the way the Workbench would
  - a restore: two config keys and a restart, into an empty folder only,
    with no route and no tool in front of it
"""
from __future__ import annotations

import io
import json
import sys
import tarfile
import textwrap

import pytest

from seren_workbench.app import create_app
from seren_workbench.config import BackupConfig, WorkbenchConfig, load_config


def _manifest(name: str) -> str:
    return textwrap.dedent("""\
        schema_version: 1
        tools:
          - name: %s
            description: Says a number.
            invoke:
              kind: process
              argv: %s
            parameters: []
    """) % (name, json.dumps([sys.executable, "-c", "print(7)"]))


def _cfg(tmp_path, **backup) -> WorkbenchConfig:
    cfg = WorkbenchConfig()
    cfg.dashboard.tools_dir = str(tmp_path / "tools")
    cfg.backup = BackupConfig(**backup)
    return cfg


def _furnish(tools) -> None:
    """A Workbench that has been lived in."""
    tools.mkdir(parents=True, exist_ok=True)
    (tools / "widgets.yaml").write_text(_manifest("count_widgets"), encoding="utf-8")
    (tools / "gadgets.yaml").write_text(_manifest("count_gadgets"), encoding="utf-8")
    (tools / "plugins").mkdir()
    (tools / "plugins" / "bridge.yaml").write_text(
        "plugins:\n  bridge:\n    url: http://127.0.0.1:9000\n    start_disabled: true\n", encoding="utf-8")


def test_the_tools_folder_is_declared_snapshotted_and_rehearsed(make_client, tmp_path):
    tools = tmp_path / "tools"
    _furnish(tools)
    client = make_client(_cfg(tmp_path))

    # a deliberate switch, and a proposal: both are things only this folder holds
    assert client.post("/tools/state", json={"tool": "count_gadgets", "enabled": False}).json()["ok"] is True
    import asyncio
    from seren_workbench.models.tools.proposal_tools import propose_tool
    asyncio.run(propose_tool(manifest=_manifest("count_sprockets"), rationale="needed it twice",
                             proposals=client.app.state.proposals))

    kept = client.get("/stores").json()
    assert kept["ok"] is True
    assert [s["name"] for s in kept["stores"]] == ["tools"], "one store: everything lives in the tools folder"
    assert kept["stores"][0]["exists"] is True

    snap = client.post("/stores/snapshot", json={"reason": "test"}).json()
    assert snap["ok"] is True, snap
    sid = snap["snapshot"]["id"]
    root = tmp_path / "backups" / "seren-workbench" / sid
    assert root.is_dir(), "beside the tools folder, not inside it"
    raw = root / "raw" / "tools"
    assert (raw / "widgets.yaml").is_file() and (raw / ".tool-state.json").is_file()
    assert (raw / "plugins" / "bridge.yaml").is_file()
    assert len(list((raw / "proposed").glob("prop_*.json"))) == 1
    man = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert man["counts"] == {"manifests": 2, "tools": 2, "unreadable_manifests": 0, "plugins": 1,
                             "proposals": 1, "switches": 1}

    rep = client.post(f"/stores/snapshots/{sid}/rehearse").json()
    assert rep["ok"] is True and rep["dry_run"] is True and rep["live_store_touched"] is False, rep
    assert rep["check"]["counts"] == man["counts"], "the restored copy reads as what was kept"

    # the archive a Lodestar pulls, and the same rehearsal from the stash side
    arc = client.get(f"/stores/snapshots/{sid}/archive")
    assert arc.status_code == 200 and arc.headers["x-seren-service"] == "seren-workbench"
    with tarfile.open(fileobj=io.BytesIO(arc.content)) as tf:
        assert any(n.endswith("raw/tools/.tool-state.json") for n in tf.getnames())
    sent = client.post("/stores/rehearse", content=arc.content).json()
    assert sent["ok"] is True, sent


def test_a_rehearsal_fails_a_snapshot_whose_switches_do_not_read(make_client, tmp_path):
    tools = tmp_path / "tools"
    _furnish(tools)
    client = make_client(_cfg(tmp_path))
    client.post("/tools/state", json={"tool": "count_gadgets", "enabled": False})
    sid = client.post("/stores/snapshot", json={}).json()["snapshot"]["id"]
    # the kept copy rots: verify catches a changed file by its hash
    state = tmp_path / "backups" / "seren-workbench" / sid / "raw" / "tools" / ".tool-state.json"
    state.write_text("{ not json", encoding="utf-8")
    rep = client.post(f"/stores/snapshots/{sid}/rehearse").json()
    assert rep["ok"] is False and rep["problems"], rep


def test_state_and_proposals_kept_elsewhere_are_stores_of_their_own(tmp_path):
    from seren_workbench import keeping
    cfg = _cfg(tmp_path)
    cfg.dashboard.state_file = str(tmp_path / "elsewhere" / "switches.json")
    cfg.dashboard.proposals_dir = str(tmp_path / "staging")
    assert [(s.name, s.kind) for s in keeping.workbench_stores(cfg)] == [
        ("tools", "dir"), ("proposals", "dir"), ("tool-state", "file")]


def test_snapshots_can_be_switched_off(make_client, tmp_path):
    client = make_client(_cfg(tmp_path, enabled=False))
    assert client.get("/stores").status_code == 404
    assert client.post("/stores/snapshot", json={}).status_code == 404


def test_restore_at_startup_into_an_empty_folder_only(make_client, tmp_path):
    # the old box
    old = tmp_path / "old"
    _furnish(old / "tools")
    a = make_client(_cfg(old))
    a.post("/tools/state", json={"tool": "count_gadgets", "enabled": False})
    sid = a.post("/stores/snapshot", json={"reason": "moving house"}).json()["snapshot"]["id"]
    snapshot = old / "backups" / "seren-workbench" / sid

    # no reason: refused, and the Workbench does not start
    new = tmp_path / "new"
    from seren_sinew.stores import RestoreRefused
    with pytest.raises(RestoreRefused):
        create_app(_cfg(new, restore_from=str(snapshot)))
    assert not (new / "tools" / "widgets.yaml").exists()

    # the new box: empty, so the snapshot is put back before anything reads the folder
    b = make_client(_cfg(new, restore_from=str(snapshot), restore_reason="moving to the NUC"))
    names = {t["name"]: t for t in b.get("/tools").json()["tools"]}
    assert {"count_widgets", "count_gadgets"} <= set(names)
    assert names["count_gadgets"]["enabled"] is False, "the switch came with it: still off"
    assert names["count_widgets"]["enabled"] is True
    assert "bridge" in b.app.state.upstreams.states, "and the plugin an approved proposal installed"
    receipts = (new / "backups" / "seren-workbench" / "restores.jsonl").read_text(encoding="utf-8")
    assert "moving to the NUC" in receipts

    # a folder that holds something is never overwritten: the key is passed by
    (new / "tools" / "mine.yaml").write_text(_manifest("count_mine"), encoding="utf-8")
    c = make_client(_cfg(new, restore_from=str(snapshot), restore_reason="again"))
    assert "count_mine" in {t["name"] for t in c.get("/tools").json()["tools"]}


def test_there_is_no_restore_route_and_no_restore_tool(make_client, tmp_path):
    client = make_client(_cfg(tmp_path))
    paths = {getattr(r, "path", "") for r in client.app.routes}
    assert not any("restore" in p for p in paths), paths
    assert not any("restore" in t["name"] for t in client.get("/tools").json()["tools"])


def test_backup_block_is_read_from_the_yaml(tmp_path):
    y = tmp_path / "seren-workbench.yaml"
    y.write_text("backup:\n  every_hours: 6\n  keep_daily: 3\n  dir: %s\n  restore_from: ''\n"
                 % json.dumps(str(tmp_path / "b")), encoding="utf-8")
    cfg = load_config(str(y))
    assert cfg.backup.enabled is True and cfg.backup.every_hours == 6.0 and cfg.backup.keep_daily == 3
    from seren_workbench import keeping
    assert keeping.backup_dir(cfg) == (tmp_path / "b").resolve()
