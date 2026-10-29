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

PLUGIN PROPOSALS (6 Oct 2026)

A model can also ask for an MCP SERVER to be plugged in (propose_plugin):
"there is a server at this address, here is why I want its tools". Same
gate, same loop, and two things a tool proposal does not need:

  - what is reviewed is an ADDRESS, not the tools behind it. Nobody can read
    a server's tools into a proposal and trust they are still the same
    tomorrow. So an approved plugin always arrives with start_disabled: every
    tool it offers, now or later, is switched OFF until a person turns it on.
    Approval says "connect and show me"; each switch says "and this one may
    run". A switched-off tool is not listed to the model either, so a
    stranger's tool descriptions do not reach it by being plugged in.
  - a proposal never carries a secret. A plugin that needs a token names
    where the token IS on this box (an environment variable, a keyring
    entry), the review shows that name loudly - approving sends that secret
    to that address - and an inline token is not a thing that can be
    proposed.

An approved plugin is one small yaml in <tools_dir>/plugins/, read at startup
beside the `plugins:` in the config, and plugged in live at approval.

ON DISK, per proposal:
    <proposals_dir>/<id>.yaml   the manifest, byte-for-byte what would be
                                installed - so approval is literally a move
                                and the thing reviewed is the thing that runs
    <proposals_dir>/<id>.json   the review record: status, rationale,
                                critique, attempt chain
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from seren_sinew.atomic import write_json

from . import projects as _proj

log = logging.getLogger("seren_workbench.proposals")

MAX_MANIFEST_CHARS = 32_000
VALID_KINDS = ("process", "web")
_ID_RE = re.compile(r"^prop_[0-9a-f]{10}$")
_PLUGIN_NAME_RE = re.compile(r"[a-z][a-z0-9_]{2,31}")
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
_KEYRING_REF_RE = re.compile(r"[\w.\-@]{1,128}/[\w.\-@]{1,128}")
# The standard components' names: a plugin may not take one (config.PluginConfig).
RESERVED_PLUGIN_NAMES = frozenset({"memory", "loci", "corpus_callosum", "callosum", "hippocampus",
                                   "lodestar", "runtime_host", "workbench"})


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
    # A rejection the reviewer closed for good: no revision may supersede
    # it, and a later proposal that asks for the same tool (or plugin) name
    # is refused with this critique. "Not like this" is `final=False`;
    # "not at all" is `final=True`.
    final: bool = False
    # "tool" (a manifest) or "plugin" (an MCP server to plug in). For a
    # plugin, `plugin` holds its name / url / display / credential pointer
    # and `title` is what a list shows in place of tool names.
    kind: str = "tool"
    plugin: Optional[Dict[str, Any]] = None
    title: str = ""
    # "project": a piece of work with a plan, a workspace, a download
    # allowlist and needs; "revision": a change to an approved project.
    # `project` holds the contract fields; `revision` the diff and the
    # project it is against. A need staged by a project or a revision is an
    # ordinary tool/plugin proposal that carries `parent` (the proposal that
    # staged it) and `for_project` (the project's name), so the reviewer can
    # see the family and the contract can point at the gates.
    project: Optional[Dict[str, Any]] = None
    revision: Optional[Dict[str, Any]] = None
    parent: Optional[str] = None
    for_project: Optional[str] = None
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
        self_addr: Optional[tuple[str, int]] = None,
        plugins_dir: Optional[str] = None,
        live_plugins: Optional[Any] = None,
        projects_dir: Optional[str] = None,
        cabinet: Optional[str] = None,
        cabinet_set: bool = False,
    ) -> None:
        self._dir = Path(proposals_dir)
        self._tools_dir = Path(tools_dir)
        # Where an approved project's contract lives: <tools_dir>/projects/<name>/
        # with project.yaml, plan.md, testing.md and history.json. Nothing
        # loads it; the loader's glob is non-recursive and these are not
        # manifests anyway.
        self._projects_dir = Path(projects_dir) if projects_dir else self._tools_dir / "projects"
        # The cabinet: where the projects' own FILES live, one drawer each
        # (seren_workbench.projects, "The cabinet"). Not the contract folder
        # above, ever: that one only the approval gate writes. A cabinet that
        # cannot be used is reported with its reason and gives no drawers,
        # rather than quietly falling back to somewhere nobody chose.
        raw = cabinet if cabinet is not None else str(self._tools_dir.parent / "projects")
        self._cabinet_set = bool(cabinet_set)
        try:
            self._cabinet: Optional[str] = _proj.check_cabinet(raw)
            self._cabinet_error: Optional[str] = None
        except _proj.ProjectError as ex:
            self._cabinet, self._cabinet_error = None, str(ex)
            log.warning("projects: %s", ex)
        # Where an approved plugin is installed, and a callable for the names
        # plugged in right now (the hub's components and plugins) - a
        # callable for the same reason live_names is one.
        self._plugins_dir = Path(plugins_dir) if plugins_dir else self._tools_dir / "plugins"
        self._live_plugins = live_plugins or (lambda: set())
        # This Workbench's own (host, port). A proposed web tool that points
        # here is refused before a reviewer ever sees it - see _validate.
        self._self_addr = self_addr
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
        parent: Optional[str] = None,
        for_project: Optional[str] = None,
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

        prior = self._prior(supersedes)
        self._refuse_if_closed(names, "tool")

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
            parent=parent,
            for_project=for_project,
        )

        self._dir.mkdir(parents=True, exist_ok=True)
        (self._dir / f"{pid}.yaml").write_text(manifest_yaml, encoding="utf-8")
        self._write_record(p)

        if prior is not None and prior.status == "rejected":
            prior.status = "superseded"
            self._write_record(prior)

        return p

    # ── Propose a plugin ───────────────────────────────────────────────

    def propose_plugin(
        self,
        name: str,
        url: str,
        rationale: str,
        display: str = "",
        bearer_token_env: str = "",
        bearer_token_keyring: str = "",
        supersedes: Optional[str] = None,
        parent: Optional[str] = None,
        for_project: Optional[str] = None,
    ) -> Proposal:
        """Validate and stage a request to plug in an MCP server. Nothing is
        connected to: the address is not even dialled until an operator
        approves. Raises ProposalError on refusal."""
        if not rationale or not rationale.strip():
            raise ProposalError(
                "rationale is required. A reviewer needs to know what you "
                "want this server's tools for, and how you came by its address."
            )
        name = (name or "").strip().lower().replace("-", "_").replace(" ", "_")
        if not _PLUGIN_NAME_RE.fullmatch(name):
            raise ProposalError(
                f"plugin name '{name}' must be lower_snake_case, 3-32 chars, "
                "starting with a letter. It becomes the toolbox its tools show under."
            )
        if name in RESERVED_PLUGIN_NAMES:
            raise ProposalError(f"'{name}' is the name of a standard component; pick another.")
        if name in set(self._live_plugins()):
            raise ProposalError(
                f"'{name}' is already plugged in. Approval must never replace "
                "something that exists - pick a different name."
            )
        if name in self._pending_plugins():
            raise ProposalError(
                f"a plugin named '{name}' is already awaiting review. "
                "Supersede that proposal instead of duplicating it."
            )
        url = self._check_plugin_url(url)

        env, ring = (bearer_token_env or "").strip(), (bearer_token_keyring or "").strip()
        if env and not _ENV_NAME_RE.fullmatch(env):
            raise ProposalError(
                "bearer_token_env is the NAME of an environment variable on the "
                "Workbench's box (letters, digits, underscores) - never the token itself."
            )
        if ring and not _KEYRING_REF_RE.fullmatch(ring):
            raise ProposalError(
                "bearer_token_keyring is a keyring reference, 'service/user' - "
                "never the token itself."
            )
        if env and ring:
            raise ProposalError("name one place the token is kept, not two.")

        prior = self._prior(supersedes)
        self._refuse_if_closed([name], "plugin")

        entry: Dict[str, Any] = {"url": url}
        if (display or "").strip():
            entry["display"] = display.strip()[:60]
        # ALWAYS. What was reviewed is an address; the tools behind it are
        # each a person's decision, made on the dashboard, one switch at a time.
        entry["start_disabled"] = True
        if env:
            entry["bearer_token_env"] = env
        if ring:
            entry["bearer_token_keyring"] = ring
        text = ("# An MCP server plugged in by an approved proposal (propose_plugin).\n"
                "# Every tool it offers arrives switched off; turn on the ones you want\n"
                "# from the dashboard. Delete this file and restart to unplug it.\n"
                + yaml.safe_dump({"plugins": {name: entry}}, sort_keys=False, default_flow_style=False))

        connects = url.rstrip("/")
        connects = (connects if connects.endswith("/mcp") else connects + "/mcp") + "/"
        effect: Dict[str, Any] = {
            "tool": f"{name} (MCP plugin)",
            "kind": "plugin",
            "calls": f"MCP {connects}",
            "executes_a_binary": False,
            "arrives": "every tool this server offers, now or later, switched OFF",
            "review_note": (
                "Approving connects this Workbench to that address and lists what it "
                "offers. Nothing it offers can run, or is shown to the model, until you "
                "switch that tool on - read each one's description before you do."
            ),
            "parameters": [],
        }
        credential = f"env:{env}" if env else (f"keyring:{ring}" if ring else "")
        if credential:
            effect["sends_credential"] = credential
            effect["review_note"] = (
                f"APPROVING SENDS A SECRET: the bearer token kept at {credential} on this box "
                f"is presented to {connects} on every connection. Check that the address "
                "deserves it. " + effect["review_note"]
            )

        pid = "prop_" + secrets.token_hex(5)
        p = Proposal(
            id=pid,
            status="pending",
            tool_names=[],
            rationale=rationale.strip(),
            created_at=time.time(),
            attempt=(prior.attempt + 1) if prior else 1,
            supersedes=supersedes,
            manifest=text,
            effects=[effect],
            kind="plugin",
            plugin={"name": name, "url": url, "display": entry.get("display", ""),
                    "credential": credential or None},
            title=f"plugin: {name}",
            parent=parent,
            for_project=for_project,
        )
        self._dir.mkdir(parents=True, exist_ok=True)
        (self._dir / f"{pid}.yaml").write_text(text, encoding="utf-8")
        self._write_record(p)
        if prior is not None and prior.status == "rejected":
            prior.status = "superseded"
            self._write_record(prior)
        return p

    # ── Propose a project ──────────────────────────────────────────────

    def propose_project(
        self,
        name: str,
        title: str,
        plan: str,
        rationale: str,
        workspace: str = "",
        testing: str = "",
        downloads: Optional[List[Any]] = None,
        needs: Optional[List[Any]] = None,
        supersedes: Optional[str] = None,
    ) -> Proposal:
        """Stage a piece of work for review: a plan, where it happens, what
        it may fetch, what it needs. Each need is staged as a child proposal
        of its own kind, through the same validators, so approving the
        project approves the plan and the limits - never a tool."""
        if not rationale or not rationale.strip():
            raise ProposalError(
                "rationale is required. Say what the project is for and what "
                "made you want to start it."
            )
        try:
            name = _proj.check_name(name)
            title = _proj.check_title(title, name)
            plan = _proj.check_text(plan, "plan", required=True)
            testing = _proj.check_text(testing, "testing notes", required=False)
            workspace = _proj.check_workspace(workspace)
            dl = _proj.check_downloads(downloads)
            needs_in = _proj.check_needs(needs)
        except _proj.ProjectError as ex:
            raise ProposalError(str(ex))
        if name in self._live_projects():
            raise ProposalError(
                f"a project named '{name}' is already approved. Propose a revision "
                "of it (propose_revision) rather than a second project."
            )
        if name in self._pending_projects():
            raise ProposalError(
                f"a project named '{name}' is already awaiting review. Supersede "
                "that proposal instead of duplicating it."
            )
        prior = self._prior(supersedes)
        self._refuse_if_closed([name], "project")

        pid = "prop_" + secrets.token_hex(5)
        children = self._stage_needs(needs_in, parent=pid, for_project=name)
        text = _proj.contract_yaml(name, title, workspace, dl, children)
        effect = self._project_effect(name, title, workspace, dl, children, "project")
        p = Proposal(
            id=pid,
            status="pending",
            tool_names=[],
            rationale=rationale.strip(),
            created_at=time.time(),
            attempt=(prior.attempt + 1) if prior else 1,
            supersedes=supersedes,
            manifest=text,
            effects=[effect],
            kind="project",
            project={"name": name, "title": title, "workspace": workspace or None,
                     "downloads": dl, "needs": children},
            title=f"project: {title}",
        )
        self._dir.mkdir(parents=True, exist_ok=True)
        (self._dir / f"{pid}.yaml").write_text(text, encoding="utf-8")
        (self._dir / f"{pid}.plan.md").write_text(plan, encoding="utf-8")
        (self._dir / f"{pid}.testing.md").write_text(testing, encoding="utf-8")
        self._write_record(p)
        if prior is not None and prior.status == "rejected":
            prior.status = "superseded"
            self._write_record(prior)
        return p

    def propose_revision(
        self,
        project: str,
        note: str,
        plan: Optional[str] = None,
        testing: Optional[str] = None,
        workspace: Optional[str] = None,
        downloads: Optional[List[Any]] = None,
        needs: Optional[List[Any]] = None,
        supersedes: Optional[str] = None,
    ) -> Proposal:
        """A change to an APPROVED project, as its own proposal. Fields given
        replace; fields left out stay. `needs` here are additions: each is
        staged as a child and joins the contract on approval. The reviewed
        bytes are the full resulting project.yaml; the record carries the
        diff the reviewer actually reads."""
        if not note or not note.strip():
            raise ProposalError(
                "a note is required: what changes and why. A revision with no "
                "reason reads as drift."
            )
        try:
            name = _proj.check_name(project)
        except _proj.ProjectError as ex:
            raise ProposalError(str(ex))
        live = self.project(name)
        if live is None:
            raise ProposalError(
                f"no approved project named '{name}'. A revision is against something "
                "that was agreed; propose the project first."
            )
        if any(p.revision and p.revision.get("project") == name for p in self.list("pending")):
            raise ProposalError(
                f"a revision of '{name}' is already awaiting review. Supersede it "
                "rather than stacking another."
            )
        before = live["contract"]
        try:
            new_plan = _proj.check_text(plan, "plan", required=True) if plan is not None else None
            new_testing = _proj.check_text(testing, "testing notes", required=False) if testing is not None else None
            new_ws = _proj.check_workspace(workspace) if workspace is not None else (before.get("workspace") or "")
            new_dl = _proj.check_downloads(downloads) if downloads is not None else list(before.get("downloads") or [])
            adds = _proj.check_needs(needs)
        except _proj.ProjectError as ex:
            raise ProposalError(str(ex))
        prior = self._prior(supersedes)
        self._refuse_if_closed([name], "revision")

        pid = "prop_" + secrets.token_hex(5)
        children = self._stage_needs(adds, parent=pid, for_project=name)
        all_needs = list(before.get("needs") or []) + children
        title = before.get("title") or name
        text = _proj.contract_yaml(name, title, new_ws, new_dl, all_needs)
        after = _proj.parse_contract(text)
        changes = _proj.diff_contracts(before, after, new_plan is not None, new_testing is not None)
        changes["note"] = note.strip()
        effect = self._project_effect(name, title, new_ws, new_dl, all_needs, "revision", changes)
        p = Proposal(
            id=pid,
            status="pending",
            tool_names=[],
            rationale=note.strip(),
            created_at=time.time(),
            attempt=(prior.attempt + 1) if prior else 1,
            supersedes=supersedes,
            manifest=text,
            effects=[effect],
            kind="revision",
            revision={"project": name, "note": note.strip(), "changes": changes,
                      "needs_added": children},
            title=f"revision: {title}",
            for_project=name,
        )
        self._dir.mkdir(parents=True, exist_ok=True)
        (self._dir / f"{pid}.yaml").write_text(text, encoding="utf-8")
        if new_plan is not None:
            (self._dir / f"{pid}.plan.md").write_text(new_plan, encoding="utf-8")
        if new_testing is not None:
            (self._dir / f"{pid}.testing.md").write_text(new_testing, encoding="utf-8")
        self._write_record(p)
        if prior is not None and prior.status == "rejected":
            prior.status = "superseded"
            self._write_record(prior)
        return p

    def _stage_needs(self, needs: List[Dict[str, Any]], parent: str, for_project: str) -> List[Dict[str, Any]]:
        """Each need becomes a real proposal of its kind. All-or-nothing: a
        need that fails validation takes the ones staged before it back out,
        so a half-staged project never sits in the queue."""
        staged: List[Proposal] = []
        out: List[Dict[str, Any]] = []
        try:
            for n in needs:
                if n["kind"] == "tool":
                    c = self.propose(str(n.get("manifest")), str(n.get("rationale")),
                                     parent=parent, for_project=for_project)
                    out.append({"proposal": c.id, "kind": "tool", "name": ", ".join(c.tool_names)})
                else:
                    c = self.propose_plugin(str(n.get("name")), str(n.get("url")), str(n.get("rationale")),
                                            display=str(n.get("display") or ""),
                                            bearer_token_env=str(n.get("bearer_token_env") or ""),
                                            bearer_token_keyring=str(n.get("bearer_token_keyring") or ""),
                                            parent=parent, for_project=for_project)
                    out.append({"proposal": c.id, "kind": "plugin", "name": str((c.plugin or {}).get("name"))})
                staged.append(c)
        except ProposalError as ex:
            for c in staged:
                self._discard(c.id)
            raise ProposalError(f"need could not be staged: {ex}")
        return out

    def _discard(self, pid: str) -> None:
        for suffix in (".yaml", ".json", ".plan.md", ".testing.md"):
            try:
                (self._dir / f"{pid}{suffix}").unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _project_effect(name, title, workspace, downloads, needs, kind, changes=None) -> Dict[str, Any]:
        eff: Dict[str, Any] = {
            "tool": f"{kind}: {name}",
            "kind": kind,
            "title": title,
            "workspace": workspace or None,
            "downloads": downloads,
            "needs": needs,
            "executes_a_binary": False,
            "parameters": [],
            "review_note": (
                "Approving agrees to the plan, the one directory it may touch and the "
                "sites it may fetch from - nothing else. Every need listed is its own "
                "proposal, reviewed and switched on separately; this approves none of them."
                if kind == "project" else
                "Approving changes an agreed project. Read the diff: what was added is "
                "what you are saying yes to. Needs added here are their own proposals, "
                "reviewed and switched on separately."
            ),
        }
        if downloads:
            eff["review_note"] = (
                f"DOWNLOADS: {len(downloads)} site(s) would be allowed. Read each one and its "
                "purpose. " + eff["review_note"]
            )
        if changes is not None:
            eff["changes"] = changes
        return eff

    def _check_plugin_url(self, url: str) -> str:
        from urllib.parse import urlsplit
        url = (url or "").strip()
        try:
            u = urlsplit(url)
            host, _port = u.hostname, u.port          # .port raises on a bad one
        except ValueError as ex:
            raise ProposalError(f"'{url}' is not a usable address: {ex}")
        if u.scheme not in ("http", "https") or not host:
            raise ProposalError(
                "the address must be an absolute http:// or https:// URL - the "
                "server's base address; the Workbench adds /mcp/ itself."
            )
        if u.username or u.password:
            raise ProposalError(
                "the address carries a username or password. A proposal never "
                "holds a secret: name where the token is kept with "
                "bearer_token_env or bearer_token_keyring instead."
            )
        if u.query or u.fragment:
            raise ProposalError("the address must not carry a query string or a fragment.")
        if self._self_addr is not None:
            from .upstream import _is_self
            if _is_self(url, self._self_addr):
                raise ProposalError(
                    "that address is this Workbench itself. It cannot be plugged "
                    "into itself."
                )
        return url.rstrip("/")

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
            if kind == "web" and self._self_addr is not None:
                base = entry.invoke.base_url or (mf.configuration.base_url if mf.configuration else "") or ""
                from .dynamic_tools.web_dispatcher import targets_this_workbench
                if base and targets_this_workbench(base.rstrip("/") + "/" + entry.invoke.path.lstrip("/"),
                                                   *self._self_addr):
                    raise ProposalError(
                        f"tool '{entry.name}' points at this Workbench itself. A tool "
                        "can't call back into the surface that runs it - the approval "
                        "routes stay operator-only. The builtin tools are how you reach "
                        "the Workbench."
                    )
            if not entry.description or not entry.description.strip():
                raise ProposalError(
                    f"tool '{entry.name}' needs a description - it's what a "
                    "reviewer reads and what a model selects on."
                )

            names.append(entry.name)
            effects.append(self._describe_effect(entry, kind, mf.configuration))

        return names, effects

    @staticmethod
    def _describe_effect(entry, kind: str, file_config=None) -> Dict[str, Any]:
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
        # A credential pointer is the one thing a reviewer must not miss: a
        # web tool that sends env:SOME_SECRET to a host of the proposer's
        # choosing is exfiltration with a friendly description. Named here,
        # never valued.
        if file_config is not None and file_config.has_bearer:
            eff["sends_credential"] = file_config.credential_label()
            eff["review_note"] = (eff.get("review_note", "") + " " if eff.get("review_note") else "") + (
                "This tool sends a bearer token from this box's environment to the host "
                "above. Check that the host deserves it."
            )
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
        if p.kind == "plugin":
            return self._approve_plugin(p)
        if p.kind == "project":
            return self._approve_project(p)
        if p.kind == "revision":
            return self._approve_revision(p)

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

    def _approve_plugin(self, p: Proposal) -> Proposal:
        name = str((p.plugin or {}).get("name") or "")
        if not _PLUGIN_NAME_RE.fullmatch(name):
            raise ProposalError(f"proposal '{p.id}' names no usable plugin.")
        # RE-CHECK, for the reason a tool's name is re-checked: the config
        # may have gained a plugin of this name while the proposal waited.
        if name in set(self._live_plugins()):
            raise ProposalError(
                f"can't approve: '{name}' was plugged in while this proposal "
                "was in review. Reject it and have the proposer rename."
            )
        self._plugins_dir.mkdir(parents=True, exist_ok=True)
        dest = self._plugins_dir / f"{name}.yaml"
        if dest.exists():
            raise ProposalError(f"{dest.name} already exists in the plugins folder.")
        # MOVE, not regenerate: the bytes reviewed are the bytes installed.
        shutil.move(str(self._dir / f"{p.id}.yaml"), str(dest))
        p.status = "approved"
        p.reviewed_at = time.time()
        p.installed_as = f"{self._plugins_dir.name}/{dest.name}"
        self._write_record(p)
        return p

    def _approve_project(self, p: Proposal) -> Proposal:
        name = str((p.project or {}).get("name") or "")
        if not name:
            raise ProposalError(f"proposal '{p.id}' names no project.")
        # RE-CHECK: a project of this name may have been approved from another
        # proposal while this one waited.
        if name in self._live_projects():
            raise ProposalError(
                f"can't approve: a project named '{name}' was approved while this "
                "proposal was in review. Reject it and have the proposer rename, "
                "or revise the live one."
            )
        home = self._projects_dir / name
        if home.exists():
            raise ProposalError(f"{home} already exists.")
        home.mkdir(parents=True, exist_ok=False)
        # MOVE, not regenerate: the bytes reviewed are the bytes agreed.
        shutil.move(str(self._dir / f"{p.id}.yaml"), str(home / "project.yaml"))
        shutil.move(str(self._dir / f"{p.id}.plan.md"), str(home / "plan.md"))
        testing = self._dir / f"{p.id}.testing.md"
        if testing.is_file():
            shutil.move(str(testing), str(home / "testing.md"))
        else:
            (home / "testing.md").write_text("", encoding="utf-8")
        p.status = "approved"
        p.reviewed_at = time.time()
        p.installed_as = f"{self._projects_dir.name}/{name}/"
        self._write_record(p)
        self._append_history(home, {"proposal": p.id, "kind": "project", "at": p.reviewed_at,
                                    "note": "agreed"})
        return p

    def _approve_revision(self, p: Proposal) -> Proposal:
        name = str((p.revision or {}).get("project") or "")
        home = self._projects_dir / name
        if not name or not (home / "project.yaml").is_file():
            raise ProposalError(
                f"can't approve: project '{name}' is no longer there to revise."
            )
        # The reviewed yaml replaces the live contract; the previous one is
        # kept beside it so the chain can be read back.
        stamp = int(time.time())
        shutil.copy2(str(home / "project.yaml"), str(home / f"project.{stamp}.yaml"))
        shutil.move(str(self._dir / f"{p.id}.yaml"), str(home / "project.yaml"))
        for part in ("plan", "testing"):
            src = self._dir / f"{p.id}.{part}.md"
            if src.is_file():
                shutil.copy2(str(home / f"{part}.md"), str(home / f"{part}.{stamp}.md"))
                shutil.move(str(src), str(home / f"{part}.md"))
        p.status = "approved"
        p.reviewed_at = time.time()
        p.installed_as = f"{self._projects_dir.name}/{name}/"
        self._write_record(p)
        self._append_history(home, {"proposal": p.id, "kind": "revision", "at": p.reviewed_at,
                                    "note": (p.revision or {}).get("note"),
                                    "changes": (p.revision or {}).get("changes")})
        # A revision against a DONE project reopens it; otherwise the work
        # carries on under the new limits and the status is left alone.
        state = self._read_state(home)
        if state.get("status") == "done":
            self._write_state(home, {"status": "in_progress", "since": p.reviewed_at,
                                     "done_requested": False, "reopened_by": p.id})
            self._append_journal(home, {"at": p.reviewed_at, "by": "workbench",
                                        "text": f"Reopened: revision {p.id} agreed after the project was done. "
                                                f"{(p.revision or {}).get('note') or ''}".strip(),
                                        "files": [], "commit": "", "status": "in_progress"})
        return p

    # ── Life after approval ────────────────────────────────────────────

    def _read_state(self, home: Path) -> Dict[str, Any]:
        f = home / "state.json"
        try:
            if f.is_file():
                return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {"status": "agreed", "since": None, "done_requested": False}

    def _write_state(self, home: Path, state: Dict[str, Any]) -> None:
        (home / "state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")

    def _read_journal(self, home: Path) -> List[Dict[str, Any]]:
        f = home / "journal.json"
        try:
            if f.is_file():
                return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            pass
        return []

    def _append_journal(self, home: Path, entry: Dict[str, Any]) -> Dict[str, Any]:
        journal = self._read_journal(home)
        entry = {"id": len(journal) + 1, **entry}
        journal.append(entry)
        write_json(home / "journal.json", journal)
        return entry

    def _home(self, name: str) -> Path:
        try:
            name = _proj.check_name(name)
        except _proj.ProjectError as ex:
            raise ProposalError(str(ex))
        home = self._projects_dir / name
        if not (home / "project.yaml").is_file():
            raise ProposalError(f"no approved project named '{name}'.")
        return home

    def log(self, name: str, text: str, files: Optional[List[Any]] = None,
            commit: str = "", status: Optional[str] = None) -> Dict[str, Any]:
        """The model's journal entry: what it did, which files (relative to
        the workspace), an optional commit, an optional status move. `done`
        is recorded as a request for the operator to confirm."""
        home = self._home(name)
        try:
            text = _proj.check_entry(text)
            files = _proj.check_files(files)
            commit = _proj.check_commit(commit)
            want = _proj.check_status_request(status)
        except _proj.ProjectError as ex:
            raise ProposalError(str(ex))
        state = self._read_state(home)
        now = time.time()
        if want == "done":
            state["done_requested"] = True
            state["done_requested_at"] = now
            if state.get("status") == "agreed":
                state["status"] = "in_progress"; state["since"] = now
        elif want is not None:
            if state.get("status") == "done":
                raise ProposalError(
                    f"'{name}' is done. A done project reopens only through an approved "
                    "revision; propose one saying what turned out to be unfinished."
                )
            if state.get("status") != want:
                state["status"] = want; state["since"] = now
        elif state.get("status") == "agreed":
            # Writing the first entry is starting the work.
            state["status"] = "in_progress"; state["since"] = now
        self._write_state(home, state)
        entry = self._append_journal(home, {"at": now, "by": "model", "text": text, "files": files,
                                            "commit": commit or "",
                                            "status": state.get("status"),
                                            **({"asks": "done"} if want == "done" else {})})
        return {"entry": entry, "state": state}

    def confirm_done(self, name: str, note: str = "") -> Dict[str, Any]:
        """Operator: the plan's 'what done looks like' was met."""
        home = self._home(name)
        state = self._read_state(home)
        now = time.time()
        state.update({"status": "done", "since": now, "done_requested": False, "done_at": now})
        self._write_state(home, state)
        self._append_journal(home, {"at": now, "by": "operator", "text": f"Done. {note}".strip(),
                                    "files": [], "commit": "", "status": "done"})
        return state

    def set_status(self, name: str, status: str, note: str = "") -> Dict[str, Any]:
        """Operator: pause, resume or reopen by hand. Done goes through confirm_done."""
        home = self._home(name)
        status = (status or "").strip().lower()
        if status not in ("agreed", "in_progress", "paused"):
            raise ProposalError("status must be agreed, in_progress or paused; done is confirm_done.")
        state = self._read_state(home)
        now = time.time()
        reopened = state.get("status") == "done"
        state.update({"status": status, "since": now, "done_requested": False})
        self._write_state(home, state)
        self._append_journal(home, {"at": now, "by": "operator",
                                    "text": f"{'Reopened' if reopened else 'Status'}: {status}. {note}".strip(),
                                    "files": [], "commit": "", "status": status})
        return state

    @staticmethod
    def _append_history(home: Path, entry: Dict[str, Any]) -> None:
        path = home / "history.json"
        try:
            hist = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
        except Exception:
            hist = []
        hist.append(entry)
        path.write_text(json.dumps(hist, indent=2), encoding="utf-8")

    # ── Read: projects ─────────────────────────────────────────────────

    def _live_projects(self) -> set[str]:
        if not self._projects_dir.is_dir():
            return set()
        return {d.name for d in self._projects_dir.iterdir()
                if d.is_dir() and (d / "project.yaml").is_file()}

    def _pending_projects(self) -> set[str]:
        return {str(p.project.get("name")) for p in self.list("pending") if p.kind == "project" and p.project}

    def project(self, name: str) -> Optional[Dict[str, Any]]:
        """An approved project: its contract, plan, testing notes, history,
        and the status of each need right now."""
        try:
            name = _proj.check_name(name)
        except _proj.ProjectError:
            return None
        home = self._projects_dir / name
        if not (home / "project.yaml").is_file():
            return None
        try:
            contract = _proj.parse_contract((home / "project.yaml").read_text(encoding="utf-8"))
        except _proj.ProjectError:
            return None
        def _read(fn: str) -> str:
            f = home / fn
            return f.read_text(encoding="utf-8") if f.is_file() else ""
        hist_f = home / "history.json"
        try:
            history = json.loads(hist_f.read_text(encoding="utf-8")) if hist_f.is_file() else []
        except Exception:
            history = []
        needs_status = {}
        for n in contract.get("needs") or []:
            c = self.get(str(n.get("proposal")))
            needs_status[str(n.get("proposal"))] = c.status if c else "missing"
        journal = self._read_journal(home)
        return {
            "name": name,
            "contract": contract,
            "plan": _read("plan.md"),
            "testing": _read("testing.md"),
            "history": history,
            "grants": _proj.grants(contract, needs_status, self._cabinet),
            "home": str(home),
            "state": self._read_state(home),
            "journal": journal,
            "files_touched": _proj.files_touched(journal),
        }

    def cabinet(self) -> Dict[str, Any]:
        """Where projects live, for the viewer's Location line and the
        model's list: the path as written, whether the operator set it or
        it is the default beside the tools folder, whether that folder is
        on THIS box (a Workbench on the cluster head records a cabinet on
        the desk the work is done at), and why not when it cannot be used."""
        here = False
        if self._cabinet:
            try:
                here = Path(os.path.expanduser(self._cabinet)).is_dir()
            except (OSError, ValueError):
                here = False
        return {"path": self._cabinet, "configured": self._cabinet_set,
                "on_this_box": here, "error": self._cabinet_error,
                "contracts": str(self._projects_dir)}

    def projects(self) -> List[Dict[str, Any]]:
        out = []
        for name in sorted(self._live_projects()):
            p = self.project(name)
            if p is not None:
                out.append(p)
        return out

    def project_texts(self, pid: str) -> Dict[str, str]:
        """The plan and testing notes staged with a project or revision
        proposal, for the reviewer. Empty strings when not staged."""
        out = {}
        for part in ("plan", "testing"):
            f = self._dir / f"{pid}.{part}.md"
            out[part] = f.read_text(encoding="utf-8") if f.is_file() else ""
        return out

    def plugin_file(self, p: Proposal) -> Optional[Path]:
        """Where an approved plugin proposal was installed."""
        name = str((p.plugin or {}).get("name") or "")
        return self._plugins_dir / f"{name}.yaml" if p.kind == "plugin" and name else None

    def reject(self, pid: str, critique: str, final: bool = False) -> Proposal:
        """Refuse a pending proposal. `final` closes it for good: the chain
        cannot be superseded and the names it asked for are refused at
        propose time, with this critique, until the record is removed."""
        if not critique or not critique.strip():
            raise ProposalError(
                "a critique is required. A bare 'no' gives the proposer "
                "nothing to revise against."
            )
        p = self._require(pid, "pending")
        p.status = "rejected"
        p.reviewed_at = time.time()
        p.critique = critique.strip()
        p.final = bool(final)
        self._write_record(p)
        # A need exists only for the project or revision that staged it.
        # Rejecting the parent rejects its pending children with the same
        # critique - not permanently, since the same tool may be wanted
        # honestly by a later project - so a revised project can stage them
        # again rather than colliding with its own orphans.
        if p.kind in ("project", "revision"):
            for c in self.list("pending"):
                if c.parent == p.id:
                    c.status = "rejected"
                    c.reviewed_at = p.reviewed_at
                    c.critique = f"(with {p.kind} {p.id}) {p.critique}"
                    self._write_record(c)
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

    def _prior(self, supersedes: Optional[str]) -> Optional[Proposal]:
        """The proposal a revision cites, checked: it must exist, and it must
        not have been closed for good."""
        if not supersedes:
            return None
        prior = self.get(supersedes)
        if prior is None:
            raise ProposalError(f"proposal '{supersedes}' not found to supersede.")
        if prior.final:
            raise ProposalError(
                f"proposal '{supersedes}' was rejected permanently and cannot be "
                f"revised. The critique: {prior.critique}"
            )
        return prior

    def _closed(self) -> List[Proposal]:
        return [p for p in self.list("rejected") if p.final]

    def _refuse_if_closed(self, names: List[str], kind: str) -> None:
        """A permanently rejected proposal keeps its names. Asking again under
        the same name, with or without `supersedes`, gets the same answer
        and the same critique - the reviewer said 'not at all', not 'not
        like this'."""
        for p in self._closed():
            if p.kind == "tool":
                held = set(p.tool_names)
            elif p.kind == "plugin":
                held = {str((p.plugin or {}).get("name"))} if p.plugin else set()
            elif p.kind == "project":
                held = {str((p.project or {}).get("name"))} if p.project else set()
            else:
                held = {str((p.revision or {}).get("project"))} if p.revision else set()
            if p.kind != kind:
                continue
            hit = sorted(set(names) & held)
            if hit:
                what = kind
                raise ProposalError(
                    f"{what} '{hit[0]}' was rejected permanently in {p.id} and "
                    f"may not be proposed again. The critique: {p.critique}"
                )

    def _pending_plugins(self) -> set[str]:
        return {str(p.plugin.get("name")) for p in self.list("pending") if p.kind == "plugin" and p.plugin}

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
        write_json(self._dir / f"{p.id}.json", data)
