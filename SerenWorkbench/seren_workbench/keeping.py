"""
seren_workbench.keeping
════════════════════════════════════════════════════════════════════════

What a Workbench keeps on disk, and snapshots of it (seren_sinew.stores).

A Workbench looked like it kept nothing: the memory is in Memory, the facts
in Loci. But the tools folder is where a model's own surface lives -

    *.yaml              the tool manifests, including every approved proposal
    plugins/            the MCP servers a proposal plugged in
    proposed/           proposals awaiting review, and the record of every
                        one that was approved or rejected, with its critique
    .tool-state.json    the switches: what a person turned off, what arrived
                        off and was never turned on

- and none of it can be rebuilt from anywhere else. A tool a model asked for,
argued for and got is as much theirs as a note in the margin: it is their
workbench. Lose the box and the memory comes back from its
snapshots while the hands do not; worse, lose the state file and every tool
that was deliberately switched off comes back ON.

So the Workbench keeps snapshots the way the rest of the family does: the
same keeper, the same routes (GET /stores, POST /stores/snapshot, the
archive a Lodestar pulls, the rehearsal), the same restore - two config keys
and a restart, into an empty store only, with no route and no tool in front
of it.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Optional

SERVICE = "seren-workbench"
PLUGINS_DIR_NAME = "plugins"
# Half-written files (the state file and a proposal record are both written
# tmp-then-replace). Never part of a snapshot, never counted.
_EXCLUDE = ("*.tmp",)


def _inside(child: Path, parent: Path) -> bool:
    return child == parent or parent in child.parents


def workbench_stores(cfg: Any) -> list:
    """The Store list for this config. One store - the tools folder - unless
    the proposals folder or the state file was pointed somewhere else, in
    which case each of those is a store of its own: a snapshot that silently
    left out the switches would restore every disabled tool as enabled."""
    from seren_sinew.stores import Store
    d = cfg.dashboard
    tools = Path(d.tools_dir).expanduser().resolve()
    out = [Store("tools", "dir", str(tools),
                 "tool manifests, approved plugins, proposals and the switches", exclude=_EXCLUDE)]
    proposals = Path(d.resolve_proposals_dir()).expanduser().resolve()
    if not _inside(proposals, tools):
        out.append(Store("proposals", "dir", str(proposals),
                         "tool proposals: pending, approved and rejected, with their critiques", exclude=_EXCLUDE))
    state = Path(d.resolve_state_file()).expanduser().resolve()
    if not _inside(state, tools):
        out.append(Store("tool-state", "file", str(state), "the switches: which tools are on and off"))
    return out


def backup_dir(cfg: Any) -> Path:
    """Where snapshots go: backup.dir, else `backups` BESIDE the tools folder
    (the way Margin keeps its beside the database). Beside, not inside, so the
    manifest loader never walks past a snapshot's copies of old manifests."""
    if (cfg.backup.dir or "").strip():
        return Path(cfg.backup.dir).expanduser().resolve()
    return Path(cfg.dashboard.tools_dir).expanduser().resolve().parent / "backups"


def _count(tools: Path, proposals: Path, state: Path, skip: Optional[Path] = None) -> dict[str, int]:
    """What is in a tools folder, as numbers. The same function counts the
    live folder for a snapshot's manifest and a restored COPY in a rehearsal,
    so the two can only disagree when the copy is not what was kept.

    Manifests are parsed, never loaded: no remote `from:` is fetched and no
    tool is registered. A rehearsal reads; it does not reach out."""
    import yaml

    from .dynamic_tools.manifest_loader import _dict_to_manifest

    def _yamls(folder: Path) -> list[Path]:
        if not folder.is_dir():
            return []
        return sorted(f for f in folder.glob("*.yaml")
                      if f.is_file() and not (skip is not None and skip in f.resolve().parents))

    manifests = _yamls(tools)
    tool_count = unreadable = 0
    for f in manifests:
        try:
            raw = yaml.safe_load(f.read_text(encoding="utf-8"))
            tool_count += len(_dict_to_manifest(raw if isinstance(raw, dict) else {}).tools or [])
        except Exception:  # noqa: BLE001 - a file the loader would skip too
            unreadable += 1
    switches = 0
    if state.is_file():
        try:
            raw = json.loads(state.read_text(encoding="utf-8"))
            switches = len(raw.get("tools") or {}) + len(raw.get("actions") or {})
        except Exception:  # noqa: BLE001
            switches = -1                                   # there and unreadable: not the same as none
    return {
        "manifests": len(manifests),
        "tools": tool_count,
        "unreadable_manifests": unreadable,
        "plugins": len(_yamls(tools / PLUGINS_DIR_NAME)),
        "proposals": len(list(proposals.glob("prop_*.json"))) if proposals.is_dir() else 0,
        "switches": switches,
    }


def live_counts(cfg: Any) -> dict[str, int]:
    d = cfg.dashboard
    return _count(Path(d.tools_dir).expanduser().resolve(),
                  Path(d.resolve_proposals_dir()).expanduser().resolve(),
                  Path(d.resolve_state_file()).expanduser().resolve(),
                  skip=backup_dir(cfg))


def make_check(cfg: Any) -> Callable[[dict[str, Path], dict[str, Any], Path], dict[str, Any]]:
    """A rehearsal's look at a restored COPY: every manifest parses the way
    the loader would parse it, the state file reads, the proposals are there.
    The counts are compared with the snapshot's own by the rehearsal."""
    d = cfg.dashboard
    live_tools = Path(d.tools_dir).expanduser().resolve()
    live_proposals = Path(d.resolve_proposals_dir()).expanduser().resolve()
    live_state = Path(d.resolve_state_file()).expanduser().resolve()

    def _check(restored: dict[str, Path], manifest: dict[str, Any], snapshot_dir: Path) -> dict[str, Any]:
        tools = restored["tools"]
        # Where each piece sits in the COPY: inside the tools folder at the
        # same relative place, or in its own store when it lives elsewhere.
        proposals = (tools / live_proposals.relative_to(live_tools) if _inside(live_proposals, live_tools)
                     else restored.get("proposals", tools / "__none__"))
        state = (tools / live_state.relative_to(live_tools) if _inside(live_state, live_tools)
                 else restored.get("tool-state", tools) / live_state.name)
        counts = _count(tools, proposals, state)
        problems = []
        if counts["switches"] < 0:
            problems.append(f"{state.name} is in the snapshot and does not read as JSON: the switches would be lost")
        return {"counts": counts, "problems": problems}

    return _check


def restore_if_asked(cfg: Any, log: Callable[[str], None] = lambda m: None) -> Optional[dict[str, Any]]:
    """backup.restore_from + backup.restore_reason: put a snapshot back before
    anything reads the tools folder. Into an empty store only; refused = the
    Workbench does not start (seren_sinew.stores.restore_at_startup). Called
    from create_app and from nowhere else - there is no route and no tool."""
    if not (cfg.backup.restore_from or "").strip():
        return None
    from seren_sinew.stores import restore_at_startup
    return restore_at_startup(SERVICE, workbench_stores(cfg), cfg.backup.restore_from,
                              cfg.backup.restore_reason, backup_dir(cfg) / SERVICE, log=log)


def make_keeper(cfg: Any, version: str, log: Callable[[str], None] = lambda m: None) -> Any:
    """The keeper, or None when backup.enabled is off."""
    if not cfg.backup.enabled:
        return None
    from seren_sinew.stores import StoreKeeper

    def _extra() -> dict[str, Any]:
        try:
            counts: Any = live_counts(cfg)
        except Exception:  # noqa: BLE001 - a snapshot is still worth taking without its counts
            counts = None
        return {"version": version, "counts": counts}

    return StoreKeeper(SERVICE, lambda: workbench_stores(cfg), backup_dir(cfg), extra=_extra,
                       check=make_check(cfg), keep_daily=cfg.backup.keep_daily,
                       keep_weekly=cfg.backup.keep_weekly, log=log)
