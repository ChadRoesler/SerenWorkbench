"""
seren_workbench.proposals
════════════════════════════════════════════════════════════════════════

The tool-proposal gate: how a model asks for a capability it does not have.

THE SHAPE, AND WHY IT IS THIS SHAPE

A proposal is a manifest written into a staging directory that NOTHING
LOADS. It is inert text. The only path from "proposed" to "callable" is an
operator calling approve, which moves the file into tools_dir and triggers
a reload. There is no code path in this module that registers a tool.

That mirrors the consolidator's draft gate one layer up: the model
synthesises, a reviewer approves or rejects with a critique, a rejected
proposal can be revised and re-proposed, and the chain is inspectable
afterwards. Same ethos, different surface — the gate that protects what
gets remembered also protects what gets to run.

WHAT A PROPOSAL MAY NOT CONTAIN

  - a name that collides with a builtin, a live tool, or another pending
    proposal. Approval must never be a way to REPLACE something.
  - a `from:` remote import. The whole point of review is that a human read
    the thing being approved; a remote import means the actual content
    lives on some other host and can change after the approval, which makes
    the review a review of a pointer.
  - anything that fails the real loader's parse. Validation runs through
    the SAME parser the loader uses, so "it validated" and "it will load"
    cannot drift apart.

ON DISK, per proposal:
    <proposals_dir>/<id>.yaml   the manifest, byte-for-byte what would be
                                installed - so approval is literally a move
                                and the thing reviewed is the thing that runs
    <proposals_dir>/<id>.json   the review record: status, rationale,
                                critique, attempt chain
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

MAX_MANIFEST_CHARS = 32_000
VALID_KINDS = ("process", "web")
_ID_RE = re.compile(r"^prop_[0-9a-f]{10}$")


@dataclass
class Proposal:
    id: str = ""
    status: str = "pending"          # pending | approved | rejected | superseded
    tool_names: List[str] = field(default_factory=list)
    rationale: str = ""
    created_at: float = 0.0
    reviewed_at: Optional[float] = None
    critique: Optional[str] = None
    attempt: int = 1
    supersedes: Optional[str] = None
    installed_as: Optional[str] = None
    # Filled on read, not stored twice.
    manifest: str = ""
    # Convenience for the reviewer: what each tool would actually DO.
    effects: List[Dict[str, Any]] = field(default_factory=list)


class ProposalError(ValueError):
    """Rejected at propose time. The message is written for the proposer."""


class ProposalStore:
    """File-backed proposal staging. Never registers anything."""

    def __init__(
        self,
        proposals_dir: str,
        tools_dir: str,
        live_names: Optional[Any] = None,
    ) -> None:
        self._dir = Path(proposals_dir)
        self._tools_dir = Path(tools_dir)
        # A CALLABLE, not a snapshot. The live tool set changes underneath a
        # proposal that's sitting in review - a reload can add the very name
        # being proposed - so the collision check has to ask at the moment it
        # matters rather than compare against a set captured at startup.
        self._live_names = live_names or (lambda: set())

    @property
    def directory(self) -> str:
        return str(self._dir)

    # ── Propose ────────────────────────────────────────────────────────

    def propose(
        self,
        manifest_yaml: str,
        rationale: str,
        supersedes: Optional[str] = None,
    ) -> Proposal:
        """Validate and stage a manifest. Raises ProposalError on refusal."""
        if not manifest_yaml or not manifest_yaml.strip():
            raise ProposalError("manifest is empty - nothing to propose.")
        if len(manifest_yaml) > MAX_MANIFEST_CHARS:
            raise ProposalError(
                f"manifest is {len(manifest_yaml)} chars, over the "
                f"{MAX_MANIFEST_CHARS} limit. Propose one tool at a time."
            )
        if not rationale or not rationale.strip():
            raise ProposalError(
                "rationale is required. A reviewer needs to know what you "
                "wanted this for, not just what it does."
            )

        names, effects = self._validate(manifest_yaml, set(self._live_names()))

        prior = self.get(supersedes) if supersedes else None
        if supersedes and prior is None:
            raise ProposalError(f"proposal '{supersedes}' not found to supersede.")

        pid = "prop_" + secrets.token_hex(5)
        p = Proposal(
            id=pid,
            status="pending",
            tool_names=names,
            rationale=rationale.strip(),
            created_at=time.time(),
            attempt=(prior.attempt + 1) if prior else 1,
            supersedes=supersedes,
            manifest=manifest_yaml,
            effects=effects,
        )

        self._dir.mkdir(parents=True, exist_ok=True)
        (self._dir / f"{pid}.yaml").write_text(manifest_yaml, encoding="utf-8")
        self._write_record(p)

        if prior is not None and prior.status == "rejected":
            prior.status = "superseded"
            self._write_record(prior)

        return p

    def _validate(
        self, manifest_yaml: str, taken_names: set[str]
    ) -> tuple[List[str], List[Dict[str, Any]]]:
        """Parse through the REAL loader path, so validation can't drift."""
        try:
            raw = yaml.safe_load(manifest_yaml)
        except Exception as ex:
            raise ProposalError(f"manifest is not valid YAML: {ex}")
        if not isinstance(raw, dict):
            raise ProposalError(
                "manifest must be a mapping with a top-level `tools:` list."
            )

        from .dynamic_tools.manifest_loader import _dict_to_manifest

        mf = _dict_to_manifest(raw)
        if not mf.tools:
            raise ProposalError("manifest declares no tools.")

        names: List[str] = []
        effects: List[Dict[str, Any]] = []
        pending = self._pending_names()

        for entry in mf.tools:
            if entry.is_remote:
                raise ProposalError(
                    "remote imports (`from:`) can't be proposed. Review has to "
                    "be review of the actual tool, and a remote manifest can "
                    "change after it's approved. Inline the definition."
                )
            if not entry.name:
                raise ProposalError("every tool needs a `name`.")
            if not re.fullmatch(r"[a-z][a-z0-9_]{2,63}", entry.name):
                raise ProposalError(
                    f"tool name '{entry.name}' must be lower_snake_case, "
                    "3-64 chars, starting with a letter."
                )
            if entry.name in taken_names:
                raise ProposalError(
                    f"'{entry.name}' is already the name of a live tool. "
                    "Approval must never replace something that exists - "
                    "pick a different name."
                )
            if entry.name in pending:
                raise ProposalError(
                    f"'{entry.name}' is already awaiting review in another "
                    "proposal. Supersede that one instead of duplicating it."
                )
            if entry.name in names:
                raise ProposalError(f"'{entry.name}' is declared twice.")

            if entry.invoke is None or not entry.invoke.kind:
                raise ProposalError(f"tool '{entry.name}' has no `invoke.kind`.")
            kind = entry.invoke.kind.strip().lower()
            if kind not in VALID_KINDS:
                raise ProposalError(
                    f"tool '{entry.name}' has invoke.kind '{kind}'; "
                    f"must be one of {', '.join(VALID_KINDS)}."
                )
            if kind == "process" and not entry.invoke.argv:
                raise ProposalError(f"tool '{entry.name}' is process but has no argv.")
            if kind == "web" and not entry.invoke.path:
                raise ProposalError(f"tool '{entry.name}' is web but has no path.")
            if not entry.description or not entry.description.strip():
                raise ProposalError(
                    f"tool '{entry.name}' needs a description - it's what a "
                    "reviewer reads and what a model selects on."
                )

            names.append(entry.name)
            effects.append(self._describe_effect(entry, kind))

        return names, effects

    @staticmethod
    def _describe_effect(entry, kind: str) -> Dict[str, Any]:
        """The blunt summary a reviewer actually needs.

        Spelling out the argv or the URL means the reviewer sees what would
        RUN, not just a name and a friendly description. `process` carries
        the loudest flag because it is the one that executes a binary.
        """
        eff: Dict[str, Any] = {"tool": entry.name, "kind": kind}
        if kind == "process":
            eff["runs"] = list(entry.invoke.argv or [])
            eff["executes_a_binary"] = True
            eff["review_note"] = (
                "This spawns a program. Read the argv. Check every {param} "
                "slot for what a caller could put there."
            )
        else:
            base = entry.invoke.base_url or "(from manifest configuration)"
            eff["calls"] = f"{(entry.invoke.method or 'GET').upper()} {base}{entry.invoke.path}"
            eff["executes_a_binary"] = False
        params = entry.parameters or []
        eff["parameters"] = [
            {
                "name": p.name,
                "type": p.type or "string",
                "constrained": bool(p.pattern or p.enum or p.min is not None
                                    or p.max is not None),
            }
            for p in params if p.name
        ]
        return eff

    # ── Review ─────────────────────────────────────────────────────────

    def approve(self, pid: str) -> Proposal:
        p = self._require(pid, "pending")

        # RE-CHECK the collision. A proposal can sit in review for days while
        # the live surface moves under it - a reload could have added the very
        # name being approved. Checking only at propose time would let the
        # approval silently install a shadow.
        live = set(self._live_names())
        clash = [n for n in p.tool_names if n in live]
        if clash:
            raise ProposalError(
                f"can't approve: {', '.join(clash)} became a live tool while "
                "this proposal was in review. Reject it and have the proposer "
                "rename."
            )

        self._tools_dir.mkdir(parents=True, exist_ok=True)

        dest = self._tools_dir / f"proposed-{pid}.yaml"
        if dest.exists():
            raise ProposalError(f"{dest.name} already exists in the tools dir.")
        # MOVE, not regenerate. The bytes that were reviewed are the bytes
        # that get installed; anything else makes the review advisory.
        shutil.move(str(self._dir / f"{pid}.yaml"), str(dest))

        p.status = "approved"
        p.reviewed_at = time.time()
        p.installed_as = dest.name
        self._write_record(p)
        return p

    def reject(self, pid: str, critique: str) -> Proposal:
        if not critique or not critique.strip():
            raise ProposalError(
                "a critique is required. A bare 'no' gives the proposer "
                "nothing to revise against."
            )
        p = self._require(pid, "pending")
        p.status = "rejected"
        p.reviewed_at = time.time()
        p.critique = critique.strip()
        self._write_record(p)
        return p

    # ── Read ───────────────────────────────────────────────────────────

    def get(self, pid: str) -> Optional[Proposal]:
        if not pid or not _ID_RE.match(pid):
            return None                      # also blocks path traversal
        rec = self._dir / f"{pid}.json"
        if not rec.is_file():
            return None
        try:
            data = json.loads(rec.read_text(encoding="utf-8"))
        except Exception:
            return None
        p = Proposal(**{k: v for k, v in data.items() if k in Proposal.__annotations__})
        man = self._dir / f"{pid}.yaml"
        if man.is_file():
            p.manifest = man.read_text(encoding="utf-8")
        return p

    def list(self, status: Optional[str] = None) -> List[Proposal]:
        out: List[Proposal] = []
        if not self._dir.is_dir():
            return out
        for rec in sorted(self._dir.glob("prop_*.json")):
            p = self.get(rec.stem)
            if p is None:
                continue
            if status and p.status != status:
                continue
            out.append(p)
        out.sort(key=lambda x: x.created_at, reverse=True)
        return out

    # ── Internals ──────────────────────────────────────────────────────

    def _pending_names(self) -> set[str]:
        return {n for p in self.list("pending") for n in p.tool_names}

    def _require(self, pid: str, status: str) -> Proposal:
        p = self.get(pid)
        if p is None:
            raise ProposalError(f"proposal '{pid}' not found.")
        if p.status != status:
            raise ProposalError(
                f"proposal '{pid}' is '{p.status}', not '{status}'."
            )
        return p

    def _write_record(self, p: Proposal) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        data = asdict(p)
        data.pop("manifest", None)          # lives in the .yaml, not twice
        tmp = self._dir / f".{p.id}.json.tmp"
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self._dir / f"{p.id}.json")
