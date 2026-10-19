# ════════════════════════════════════════════════════════════════════════
#  ProjectTools - how a model asks to START A PIECE OF WORK, and to grow
#  one it was allowed to start.
#
#  propose_project / propose_revision / list_my_projects.
#
#  Written for the instance that will read them. A project is not a tool:
#  it is a plan, a place, an allowlist and a list of needs, and saying
#  yes to it is saying yes to the shape of the work - not to any tool in
#  it. Each need goes through its own gate. "I want to play a game with
#  you" starts here.
# ════════════════════════════════════════════════════════════════════════

from __future__ import annotations

import json
from typing import Any, List, Optional

from ...proposals import ProposalStore, ProposalError

TOOLBOX = "Proposals"


_NEED_SCHEMA = {
    "type": "object",
    "description": (
        "A tool or plugin the work will use. {kind: 'tool', manifest, rationale} "
        "stages a tool proposal exactly like propose_tool; {kind: 'plugin', name, "
        "url, rationale, display?, bearer_token_env?, bearer_token_keyring?} "
        "stages a plugin proposal exactly like propose_plugin. Each is reviewed "
        "and switched on separately from the project."
    ),
    "properties": {
        "kind": {"type": "string", "enum": ["tool", "plugin"]},
        "rationale": {"type": "string"},
        "manifest": {"type": "string"},
        "name": {"type": "string"},
        "url": {"type": "string"},
        "display": {"type": "string"},
        "bearer_token_env": {"type": "string"},
        "bearer_token_keyring": {"type": "string"},
    },
    "required": ["kind", "rationale"],
}

_DOWNLOAD_SCHEMA = {
    "type": "object",
    "description": "A site the work may fetch from, as a URL prefix, and why.",
    "properties": {
        "url": {"type": "string", "description": "https://host/path-prefix. No query, no credentials."},
        "purpose": {"type": "string", "description": "What will be fetched from there and why."},
    },
    "required": ["url", "purpose"],
}


PROPOSE_PROJECT_TOOL_DEF = {
    "name": "propose_project",
    "description": (
        "Propose a piece of WORK for a human to agree to before any of it "
        "starts: a mod for a game, a tool chain, anything with more than one "
        "step and more than one need. This stages nothing that runs. What is "
        "reviewed is the plan (Markdown, rendered for the reviewer), the one "
        "directory the work may touch, an explicit list of sites it may fetch "
        "from with a reason for each, and the tools or plugins it will need.\n"
        "\n"
        "Every need is staged as its own proposal, a child of this one, and "
        "goes through its own approve-then-enable gates. Approving the project "
        "approves the plan and the limits, never a tool. So write the plan as "
        "the thing a person says yes to: what you will build, how you will "
        "test it, and what you will not touch.\n"
        "\n"
        "A project can grow later with propose_revision; it cannot grow "
        "quietly. If this comes back rejected you get the critique and can "
        "revise citing supersedes."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": (
                    "A short slug (lower-case, 3-41 chars). It names the project's folder, "
                    "and its drawer in the cabinet: where the work's own files will live."
                ),
            },
            "title": {
                "type": "string",
                "description": "A human title, up to 80 chars.",
            },
            "plan": {
                "type": "string",
                "description": (
                    "The development plan as Markdown: goal, approach, steps, what "
                    "done looks like, what is out of scope. The reviewer reads this "
                    "rendered."
                ),
            },
            "rationale": {
                "type": "string",
                "description": "Why this project, in plain language. What made you want to start it.",
            },
            "workspace": {
                "type": "string",
                "description": (
                    "The ONE absolute directory the work acts on, on the box where "
                    "the work happens (a game folder, say). Not where your own files "
                    "go: once agreed, the project gets a drawer in the cabinet for "
                    "source, notes and builds, so reinstalling the thing you modify "
                    "cannot take the work with it. Leave empty if the work acts on "
                    "nothing outside its drawer."
                ),
            },
            "testing": {
                "type": "string",
                "description": "Markdown notes on the testing workspace: where it is tried out and how it is checked.",
            },
            "downloads": {
                "type": "array",
                "items": _DOWNLOAD_SCHEMA,
                "description": "Every site the work may fetch from. An empty list means it fetches nothing.",
            },
            "needs": {
                "type": "array",
                "items": _NEED_SCHEMA,
                "description": "The tools and plugins the work will use, each staged as its own proposal.",
            },
            "supersedes": {
                "type": "string",
                "description": "Optional. The id of a rejected project proposal this one revises.",
            },
        },
        "required": ["name", "plan", "rationale"],
    },
}


PROPOSE_REVISION_TOOL_DEF = {
    "name": "propose_revision",
    "description": (
        "Ask to change an APPROVED project: a new site to fetch from, a tool "
        "it turns out to need, a moved workspace, a rewritten plan. It is its "
        "own proposal through the same gate. Fields you give replace the "
        "project's; fields you leave out stay as agreed. `needs` here are "
        "additions, each staged as its own child proposal.\n"
        "\n"
        "The reviewer sees the DIFF: what was added, removed or moved, and your "
        "note on why. Write the note for that reader. One revision of a project "
        "at a time; supersede a rejected one rather than stacking."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "project": {"type": "string", "description": "The approved project's name."},
            "note": {"type": "string", "description": "What changes and why. Required."},
            "plan": {"type": "string", "description": "Optional. A replacement plan (Markdown)."},
            "testing": {"type": "string", "description": "Optional. Replacement testing notes."},
            "workspace": {"type": "string", "description": "Optional. A replacement workspace path."},
            "downloads": {
                "type": "array", "items": _DOWNLOAD_SCHEMA,
                "description": "Optional. The FULL replacement allowlist (not just additions).",
            },
            "needs": {
                "type": "array", "items": _NEED_SCHEMA,
                "description": "Optional. Needs to ADD, each staged as its own proposal.",
            },
            "supersedes": {"type": "string", "description": "Optional. A rejected revision this one revises."},
        },
        "required": ["project", "note"],
    },
}


LIST_PROJECTS_TOOL_DEF = {
    "name": "list_my_projects",
    "description": (
        "Your cabinet of plans: the projects a person has agreed to, with what "
        "each one grants (workspace, download allowlist, which needs are "
        "approved and enabled), its status, and the tail of its journal. Read "
        "this before touching a project's files or fetching anything for it - "
        "the grants are the agreement, a need that is still pending is a tool "
        "you do not have yet, and the journal is where the last you stopped.\n"
        "\n"
        "Each project has a `drawer`: its folder in the cabinet (`cabinet`, set "
        "by the operator), where the work's OWN files live - source, notes, "
        "builds. Keep them there, not in the workspace: the workspace is what "
        "the work acts on and can be wiped by a reinstall, the drawer is kept. "
        "The path is for the box the work is done on; `~` is that box's home."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Optional. One project: full plan, testing notes, history, journal."},
            "limit": {"type": "integer", "description": "With a name: how many journal entries, newest last. Default 20; 0 for all."},
        },
        "required": [],
    },
}


PROJECT_LOG_TOOL_DEF = {
    "name": "project_log",
    "description": (
        "Write an entry in a project's journal: what you did, which files you "
        "touched (relative to the workspace, or to the project's drawer), what "
        "you checked, what is next. "
        "The journal is the no-repo git, and it is what the person reads when "
        "they come back - so write it as you go, not at the end. Your first "
        "entry moves the project from agreed to in_progress.\n"
        "\n"
        "`status` may be in_progress or paused, or done - which is a REQUEST: "
        "done means the plan's 'what done looks like' was met, and the person "
        "who checked confirms it. A done project reopens only through an "
        "approved revision. With a commit hash, the entry links to it."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "project": {"type": "string", "description": "The approved project's name."},
            "entry": {"type": "string", "description": "What you did and found, in plain language."},
            "files": {"type": "array", "items": {"type": "string"},
                      "description": "Paths touched, relative to the workspace or the drawer. No '..'."},
            "commit": {"type": "string", "description": "Optional git hash this entry corresponds to."},
            "status": {"type": "string", "enum": ["in_progress", "paused", "done"],
                       "description": "Optional status move; done is a request for confirmation."},
        },
        "required": ["project", "entry"],
    },
}


def _err(error: str, hint: str) -> str:
    return json.dumps({"error": error, "hint": hint}, indent=2)


def _needs_summary(items: List[dict]) -> List[dict]:
    return [{"proposal": n.get("proposal"), "kind": n.get("kind"), "name": n.get("name")} for n in items]


async def propose_project(
    name: str,
    plan: str,
    rationale: str,
    title: str = "",
    workspace: str = "",
    testing: str = "",
    downloads: Optional[List[Any]] = None,
    needs: Optional[List[Any]] = None,
    supersedes: Optional[str] = None,
    proposals: ProposalStore = None,
    **kwargs,
) -> str:
    if proposals is None:
        return _err("Tool proposals are not enabled on this Workbench.",
                    "The operator sets dashboard.proposals_enabled to turn this on.")
    try:
        p = proposals.propose_project(name=name, title=title, plan=plan, rationale=rationale,
                                      workspace=workspace, testing=testing, downloads=downloads,
                                      needs=needs, supersedes=supersedes)
    except ProposalError as ex:
        return _err(str(ex), "Fix it and propose again.")
    except Exception as ex:  # noqa: BLE001
        return _err(f"could not stage the project: {ex}", "")
    return json.dumps({
        "ok": True,
        "proposal_id": p.id,
        "status": p.status,
        "project": (p.project or {}).get("name"),
        "needs": _needs_summary((p.project or {}).get("needs") or []),
        "attempt": p.attempt,
        "staged_at": proposals.directory,
        "what_happens_next": (
            "The plan, workspace, download allowlist and needs are staged for review. "
            "Approval agrees to the plan and the limits; each need listed is its own "
            "proposal and is approved and switched on separately. Check "
            "list_my_proposals for the children and list_my_projects for what is "
            "agreed."
        ),
    }, indent=2)


async def propose_revision(
    project: str,
    note: str,
    plan: Optional[str] = None,
    testing: Optional[str] = None,
    workspace: Optional[str] = None,
    downloads: Optional[List[Any]] = None,
    needs: Optional[List[Any]] = None,
    supersedes: Optional[str] = None,
    proposals: ProposalStore = None,
    **kwargs,
) -> str:
    if proposals is None:
        return _err("Tool proposals are not enabled on this Workbench.",
                    "The operator sets dashboard.proposals_enabled to turn this on.")
    try:
        p = proposals.propose_revision(project=project, note=note, plan=plan, testing=testing,
                                       workspace=workspace, downloads=downloads, needs=needs,
                                       supersedes=supersedes)
    except ProposalError as ex:
        return _err(str(ex), "Fix it and propose again.")
    except Exception as ex:  # noqa: BLE001
        return _err(f"could not stage the revision: {ex}", "")
    rev = p.revision or {}
    return json.dumps({
        "ok": True,
        "proposal_id": p.id,
        "status": p.status,
        "project": rev.get("project"),
        "changes": rev.get("changes"),
        "needs_added": _needs_summary(rev.get("needs_added") or []),
        "attempt": p.attempt,
        "what_happens_next": (
            "The revision is staged with its diff. Until it is approved the project "
            "stays as last agreed; needs added here are their own proposals."
        ),
    }, indent=2)


async def project_log(
    project: str,
    entry: str,
    files: Optional[List[Any]] = None,
    commit: str = "",
    status: Optional[str] = None,
    proposals: ProposalStore = None,
    **kwargs,
) -> str:
    if proposals is None:
        return _err("Tool proposals are not enabled on this Workbench.", "")
    try:
        out = proposals.log(project, entry, files=files, commit=commit, status=status)
    except ProposalError as ex:
        return _err(str(ex), "Fix it and write the entry again.")
    except Exception as ex:  # noqa: BLE001
        return _err(f"could not write the entry: {ex}", "")
    st = out["state"]
    return json.dumps({
        "ok": True,
        "project": project,
        "entry_id": out["entry"]["id"],
        "status": st.get("status"),
        **({"done_requested": True,
            "what_happens_next": "You asked for done. The person who checks the result confirms it "
                                 "from the Projects tab; until then the status stays as it is."}
           if st.get("done_requested") else {}),
    }, indent=2)


async def list_my_projects(
    name: Optional[str] = None,
    limit: int = 20,
    proposals: ProposalStore = None,
    **kwargs,
) -> str:
    if proposals is None:
        return _err("Tool proposals are not enabled on this Workbench.", "")
    if name:
        p = proposals.project(name)
        if p is None:
            return _err(f"no approved project named '{name}'.", "list_my_projects with no name lists them.")
        return json.dumps({"project": p["grants"], "cabinet": proposals.cabinet().get("path"),
                           "state": p["state"], "plan": p["plan"],
                           "testing": p["testing"], "history": p["history"],
                           "files_touched": p["files_touched"],
                           "journal": p["journal"][-limit:] if limit else p["journal"]}, indent=2)
    items = proposals.projects()
    cab = proposals.cabinet()
    return json.dumps({
        "count": len(items),
        "cabinet": cab.get("path"),
        **({"cabinet_error": cab["error"]} if cab.get("error") else {}),
        "projects": [{**p["grants"], "status": p["state"].get("status"),
                      "done_requested": bool(p["state"].get("done_requested")),
                      "journal_count": len(p["journal"]),
                      "last_entry": (p["journal"][-1] if p["journal"] else None)} for p in items],
    }, indent=2)
