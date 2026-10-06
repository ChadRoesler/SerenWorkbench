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
    # "tool" (a manifest) or "plugin" (an MCP server to plug in). For a
    # plugin, `plugin` holds its name / url / display / credential pointer
    # and `title` is what a list shows in place of tool names.
    kind: str = "tool"
    plugin: Optional[Dict[str, Any]] = None
    title: str = ""
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
    ) -> None:
        self._dir = Path(proposals_dir)
        self._tools_dir = Path(tools_dir)
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

        prior = self.get(supersedes) if supersedes else None
        if supersedes and prior is None:
            raise ProposalError(f"proposal '{supersedes}' not found to supersede.")

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
        )
        self._dir.mkdir(parents=True, exist_ok=True)
        (self._dir / f"{pid}.yaml").write_text(text, encoding="utf-8")
        self._write_record(p)
        if prior is not None and prior.status == "rejected":
            prior.status = "superseded"
            self._write_record(prior)
        return p

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

    def plugin_file(self, p: Proposal) -> Optional[Path]:
        """Where an approved plugin proposal was installed."""
        name = str((p.plugin or {}).get("name") or "")
        return self._plugins_dir / f"{name}.yaml" if p.kind == "plugin" and name else None

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
        tmp = self._dir / f".{p.id}.json.tmp"
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self._dir / f"{p.id}.json")
