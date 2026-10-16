"""
seren_workbench.projects
════════════════════════════════════════════════════════════════════════

The pure parts of a PROJECT proposal and a REVISION of one: validation,
the on-disk shape, the diff a reviewer reads. The staging, the review
record and the approve/reject gates live in proposals.py, where every
kind of proposal goes through the same door.

WHAT A PROJECT IS (16 Oct 2026)

A tool proposal asks for one capability. A project asks for a piece of
WORK: "I want to build a BepInEx mod for Inscryption that exports the
board state", with everything that work will need laid out up front so a
person can say yes to the whole shape before any of it starts:

  - a PLAN, as Markdown, rendered for the reviewer rather than shown raw;
  - TESTING notes: where the work is tried out and how it is checked;
  - a WORKSPACE: the one directory the work may touch;
  - DOWNLOADS: an explicit allowlist of sites, each with a reason, because
    "I need to fetch something" is the step most worth reading twice;
  - NEEDS: the tools and plugins the work will use. Each one is staged as
    its own proposal, a child of the project, and goes through its own
    approve-then-enable gates. Approving a project never turns a tool on.

A project can GROW, but growth is a review: a revision is its own kind of
proposal, against an approved project, carrying the full resulting state
plus a note on what changes and why. The reviewer sees the diff - sites
added, workspace moved, needs added - and the same approve/reject-with-
critique loop applies.

WHERE THE GATES ARE, HONESTLY

The Workbench holds the approved contract and shows it to whoever asks
(GET /projects/{name}/grants). It does not itself run the work: a mod is
built on the box the game lives on, by whatever harness the model acts
through there. That harness is where the workspace and the allowlist are
enforced; this is where they are agreed and recorded. A plan in a folder
nobody loads is as inert as a manifest in one.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

import yaml

MAX_PLAN_CHARS = 64_000
MAX_DOWNLOADS = 32
MAX_NEEDS = 12
_NAME_RE = re.compile(r"[a-z][a-z0-9_-]{2,40}")


class ProjectError(ValueError):
    """Rejected at propose time. Written for the proposer."""


# ── Pieces ─────────────────────────────────────────────────────────────

def check_name(name: str) -> str:
    name = (name or "").strip().lower()
    if not _NAME_RE.fullmatch(name):
        raise ProjectError(
            f"project name '{name}' must be lower-case letters, digits, '_' or '-', "
            "3-41 chars, starting with a letter. It becomes the folder the project lives in."
        )
    return name


def check_title(title: str, fallback: str) -> str:
    title = (title or "").strip()
    if len(title) > 80:
        raise ProjectError("the title is over 80 characters; put the rest in the plan.")
    return title or fallback


def check_text(text: str, what: str, required: bool) -> str:
    text = (text or "").strip()
    if required and not text:
        raise ProjectError(f"the {what} is empty. A project with no {what} is a wish, not a proposal.")
    if len(text) > MAX_PLAN_CHARS:
        raise ProjectError(f"the {what} is {len(text)} chars, over the {MAX_PLAN_CHARS} limit.")
    return text


def check_workspace(path: str) -> str:
    """One directory the work may touch. Absolute, with no way back up.
    Advisory here (the Workbench does not execute the work); enforced by
    the harness that does, which reads it from the approved contract."""
    path = (path or "").strip()
    if not path:
        return ""
    is_posix = path.startswith("/")
    is_windows = bool(re.match(r"^[A-Za-z]:[\\/]", path)) or path.startswith("\\\\")
    if not (is_posix or is_windows):
        raise ProjectError(
            f"workspace '{path}' is not an absolute path. Name the one directory the work "
            "may touch, in full."
        )
    parts = re.split(r"[\\/]+", path)
    if any(p == ".." for p in parts):
        raise ProjectError("workspace must not contain '..' - name the directory directly.")
    if len(path) > 512:
        raise ProjectError("workspace path is over 512 characters.")
    return path


def check_downloads(items: Optional[List[Any]]) -> List[Dict[str, str]]:
    """An allowlist of sites with reasons. URL prefixes, https preferred,
    nothing that could carry a credential or be re-pointed later."""
    out: List[Dict[str, str]] = []
    for raw in (items or []):
        if isinstance(raw, str):
            raw = {"url": raw, "purpose": ""}
        if not isinstance(raw, dict):
            raise ProjectError("each download is {url, purpose}.")
        url = str(raw.get("url") or "").strip()
        purpose = str(raw.get("purpose") or "").strip()
        try:
            u = urlsplit(url)
            _ = u.port
        except ValueError as ex:
            raise ProjectError(f"download '{url}' is not a usable address: {ex}")
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ProjectError(f"download '{url}' must be an absolute http(s) URL prefix.")
        if u.username or u.password:
            raise ProjectError(f"download '{url}' carries a username or password; it must not.")
        if u.query or u.fragment:
            raise ProjectError(f"download '{url}' must not carry a query string or a fragment.")
        if not purpose:
            raise ProjectError(f"download '{url}' needs a purpose - what will be fetched from there and why.")
        if len(purpose) > 200:
            raise ProjectError(f"the purpose for '{url}' is over 200 characters.")
        out.append({"url": url.rstrip("/"), "purpose": purpose})
    if len(out) > MAX_DOWNLOADS:
        raise ProjectError(f"{len(out)} download sites is over the {MAX_DOWNLOADS} limit.")
    seen = set()
    for d in out:
        if d["url"] in seen:
            raise ProjectError(f"download '{d['url']}' is listed twice.")
        seen.add(d["url"])
    return out


def check_needs(items: Optional[List[Any]]) -> List[Dict[str, Any]]:
    """Shape-check only. Each need is staged as a real tool or plugin
    proposal by the store, which runs the real validators on it."""
    out: List[Dict[str, Any]] = []
    for raw in (items or []):
        if not isinstance(raw, dict):
            raise ProjectError("each need is an object with a `kind` of 'tool' or 'plugin'.")
        kind = str(raw.get("kind") or "").strip().lower()
        if kind == "tool":
            if not str(raw.get("manifest") or "").strip():
                raise ProjectError("a need of kind 'tool' carries a `manifest` (YAML, same as propose_tool).")
        elif kind == "plugin":
            if not str(raw.get("name") or "").strip() or not str(raw.get("url") or "").strip():
                raise ProjectError("a need of kind 'plugin' carries a `name` and a `url` (same as propose_plugin).")
        else:
            raise ProjectError(f"need kind '{kind}' is not one of 'tool', 'plugin'.")
        if not str(raw.get("rationale") or "").strip():
            raise ProjectError("every need carries its own `rationale` - it is reviewed on its own.")
        out.append(dict(raw))
    if len(out) > MAX_NEEDS:
        raise ProjectError(f"{len(out)} needs is over the {MAX_NEEDS} limit. Split the project.")
    return out


# ── The contract on disk ───────────────────────────────────────────────

def contract_yaml(name: str, title: str, workspace: str, downloads: List[Dict[str, str]],
                  needs: List[Dict[str, Any]]) -> str:
    """project.yaml: the reviewed bytes. Needs are named here by the
    proposal ids they were staged as, so the contract points at the gates
    rather than restating what is behind them."""
    doc = {
        "schema_version": 1,
        "project": {
            "name": name,
            "title": title,
            "workspace": workspace or None,
            "downloads": downloads,
            "needs": [{"proposal": n.get("proposal"), "kind": n.get("kind"),
                       "name": n.get("name")} for n in needs],
        },
    }
    head = ("# A project agreed through the Workbench's proposal gate.\n"
            "# The plan is plan.md beside this file; testing.md is how it is checked.\n"
            "# `workspace` is the one directory the work may touch and `downloads`\n"
            "# the only sites it may fetch from. Each need is its own proposal with\n"
            "# its own switches; this file approves none of them.\n")
    return head + yaml.safe_dump(doc, sort_keys=False, default_flow_style=False, allow_unicode=True)


def parse_contract(text: str) -> Dict[str, Any]:
    raw = yaml.safe_load(text or "") or {}
    proj = raw.get("project") if isinstance(raw, dict) else None
    if not isinstance(proj, dict):
        raise ProjectError("project.yaml has no `project:` mapping.")
    return proj


def diff_contracts(before: Dict[str, Any], after: Dict[str, Any],
                   plan_changed: bool, testing_changed: bool) -> Dict[str, Any]:
    """What a revision changes, in the terms a reviewer reads."""
    b_dl = {d["url"]: d for d in (before.get("downloads") or [])}
    a_dl = {d["url"]: d for d in (after.get("downloads") or [])}
    b_needs = {str(n.get("proposal")): n for n in (before.get("needs") or [])}
    a_needs = {str(n.get("proposal")): n for n in (after.get("needs") or [])}
    return {
        "title": ({"from": before.get("title"), "to": after.get("title")}
                  if before.get("title") != after.get("title") else None),
        "workspace": ({"from": before.get("workspace"), "to": after.get("workspace")}
                      if (before.get("workspace") or None) != (after.get("workspace") or None) else None),
        "downloads_added": [a_dl[u] for u in a_dl if u not in b_dl],
        "downloads_removed": [b_dl[u] for u in b_dl if u not in a_dl],
        "needs_added": [a_needs[k] for k in a_needs if k not in b_needs],
        "needs_removed": [b_needs[k] for k in b_needs if k not in a_needs],
        "plan_changed": plan_changed,
        "testing_changed": testing_changed,
    }


# ── Life after approval: status and the journal ────────────────────────
#
# Approved is where the work STARTS. The journal is the no-repo git: an
# append-only log the model writes as it works - what it did, which files it
# touched, what it checked, what is next - and the one thing a person reads
# when they come back. Status is the model's to move, except "done", which
# the model can only ask for: done means the plan's "what done looks like"
# was met, and the person who played the fight confirms that. A revision
# approved against a done project reopens it (design note, 16 Oct 2026: "the only
# time a revision would reset, is if it was previously set to done").

STATUSES = ("agreed", "in_progress", "paused", "done")
MODEL_MAY_SET = ("in_progress", "paused")
MAX_ENTRY_CHARS = 8_000
MAX_FILES_PER_ENTRY = 64


def check_entry(text: str) -> str:
    text = (text or "").strip()
    if not text:
        raise ProjectError("a journal entry needs words: what you did, or what you found.")
    if len(text) > MAX_ENTRY_CHARS:
        raise ProjectError(f"the entry is {len(text)} chars, over the {MAX_ENTRY_CHARS} limit; split it.")
    return text


def check_files(items: Optional[List[Any]]) -> List[str]:
    out: List[str] = []
    for raw in (items or []):
        f = str(raw or "").strip().replace("\\", "/")
        if not f:
            continue
        if f.startswith("/") or re.match(r"^[A-Za-z]:", f) or any(p == ".." for p in f.split("/")):
            raise ProjectError(
                f"file '{raw}' must be relative to the workspace, with no '..': the "
                "workspace is the one directory the work may touch."
            )
        if len(f) > 512:
            raise ProjectError(f"file path '{f[:40]}...' is over 512 characters.")
        out.append(f)
    if len(out) > MAX_FILES_PER_ENTRY:
        raise ProjectError(f"{len(out)} files in one entry is over the {MAX_FILES_PER_ENTRY} limit.")
    return out


def check_commit(commit: str) -> str:
    commit = (commit or "").strip()
    if commit and not re.fullmatch(r"[0-9a-f]{7,64}", commit):
        raise ProjectError("commit must be a git hash (7-64 hex chars), or empty.")
    return commit


def check_status_request(status: Optional[str]) -> Optional[str]:
    if status is None or not str(status).strip():
        return None
    status = str(status).strip().lower()
    if status == "done":
        return "done"                      # becomes a request, not a state
    if status not in MODEL_MAY_SET:
        raise ProjectError(
            f"status '{status}' is not one you can set. You may mark in_progress or "
            "paused, or ask for done; agreed is where a project starts."
        )
    return status


def files_touched(journal: List[Dict[str, Any]]) -> List[str]:
    seen: Dict[str, None] = {}
    for e in journal:
        for f in e.get("files") or []:
            seen[f] = None
    return sorted(seen)


def grants(proj: Dict[str, Any], needs_status: Dict[str, str]) -> Dict[str, Any]:
    """The contract in the form a harness consumes: where the work may
    happen, what it may fetch, which needs are actually live."""
    return {
        "name": proj.get("name"),
        "title": proj.get("title"),
        "workspace": proj.get("workspace") or None,
        "allowed_download_prefixes": [d["url"] for d in (proj.get("downloads") or [])],
        "downloads": proj.get("downloads") or [],
        "needs": [{**n, "status": needs_status.get(str(n.get("proposal")), "unknown")}
                  for n in (proj.get("needs") or [])],
    }
