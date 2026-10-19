"""
Proposal routes — the operator half of the tool-proposal gate.

GET  /proposals                 list, newest first
GET  /proposals/{id}            one, including the full manifest text
POST /proposals/{id}/approve    install it and reload the live surface
POST /proposals/{id}/reject     refuse it, with a critique the proposer reads
GET  /projects                  the projects agreed so far, with their grants, and the cabinet they live in
GET  /projects/{name}           one, with its plan, testing notes and history
GET  /projects/{name}/grants    the contract as a harness consumes it
POST /projects/{name}/done      operator: confirm the plan's "done" was met
POST /projects/{name}/status    operator: {status: agreed|in_progress|paused}, a reopen by hand

THE ASYMMETRY IS THE POINT. The model can write a proposal and read its own
proposals. It cannot approve one. There is no MCP tool behind any route in
this file and there should never be — the moment approval is reachable from
the tool surface, the gate is a formality and "hands on the surface" is a
comment rather than a mechanism.

ROUTE ORDER: literal paths ahead of parameterised ones. Same
specific-before-generic rule that bit SerenMargin's /notes/stats.
"""
from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from ..proposals import ProposalError

router = APIRouter(tags=["proposals"])


def _store(request: Request):
    store = getattr(request.app.state, "proposals", None)
    if store is None:
        raise HTTPException(
            status_code=503,
            detail="tool proposals are disabled (dashboard.proposals_enabled)",
        )
    return store


def _public(p, include_manifest: bool = False) -> dict:
    d = asdict(p)
    if not include_manifest:
        d.pop("manifest", None)
    return d


@router.get("/proposals")
async def list_proposals(request: Request, status: str | None = None):
    store = _store(request)
    items = store.list(status)
    return {
        "count": len(items),
        "pending": sum(1 for p in items if p.status == "pending"),
        "proposals_dir": store.directory,
        "proposals": [_public(p) for p in items],
    }


@router.get("/proposals/{pid}")
async def get_proposal(request: Request, pid: str):
    """One proposal WITH its manifest text.

    The manifest is the thing being reviewed, so it is returned in full and
    verbatim — never summarised. `effects` beside it spells out what each
    tool would actually run, because a friendly description is not what you
    should be approving on.
    """
    store = _store(request)
    p = store.get(pid)
    if p is None:
        raise HTTPException(status_code=404, detail=f"no proposal '{pid}'")
    out = _public(p, include_manifest=True)
    if p.kind in ("project", "revision"):
        # The plan is the thing being agreed to; it travels with the record
        # so the viewer renders it rather than asking the reviewer to go
        # and find a file.
        out["texts"] = store.project_texts(pid)
        out["children"] = [_public(c) for c in store.list() if c.parent == pid]
    return out


@router.post("/proposals/{pid}/approve")
async def approve_proposal(request: Request, pid: str):
    """Install the proposal — REGISTERED BUT SWITCHED OFF.

    Approval MOVES the reviewed file into the tools directory (the bytes that
    were read are the bytes that run) and reloads, so the tool appears in the
    list immediately. It arrives DISABLED.

    That's deliberate, and it's the second gate. "I read this and it isn't
    malicious" and "I want this callable right now" are two different
    decisions, and collapsing them means the last chance to change your mind
    is before you've ever seen the thing in context. Flip it on from the Tool
    State tab, or POST /tools/state, when you're ready.
    """
    store = _store(request)
    try:
        p = store.approve(pid)
    except ProposalError as ex:
        return JSONResponse({"ok": False, "error": str(ex)}, status_code=409)

    if p.kind == "plugin":
        return await _plug_in(request, store, p)
    if p.kind in ("project", "revision"):
        # Nothing to reload: a project is a contract on disk, not a tool.
        name = (p.project or p.revision or {}).get("name") or (p.revision or {}).get("project")
        live = store.project(str(name)) if name else None
        pending = [n for n in (live or {}).get("grants", {}).get("needs", [])
                   if n.get("status") == "pending"]
        return {
            "ok": True,
            "proposal": _public(p),
            "installed_as": p.installed_as,
            "installed": True,
            "enabled": False,
            "project": (live or {}).get("grants"),
            "next_step": (
                f"Agreed. The contract is at {p.installed_as}. "
                + (f"{len(pending)} need(s) still wait for their own review in this queue. "
                   if pending else "")
                + "Nothing was switched on."
            ),
        }

    reload_result = None
    reg = getattr(request.app.state, "dynamic_registry", None)
    registry = getattr(request.app.state, "tool_registry", None)

    # Seed BEFORE the reload: replace_dynamic decides a new tool's state as
    # it adds it, so marking the names afterwards would leave a window where
    # an approved-but-not-yet-enabled tool was callable.
    if registry is not None:
        registry.seed_disabled(set(p.tool_names))

    if reg is not None:
        snap = await reg.reload()
        reload_result = {
            "added": snap.added,
            "removed": snap.removed,
            "replaced": snap.replaced,
        }

    return {
        "ok": True,
        "proposal": _public(p),
        "installed_as": p.installed_as,
        "reload": reload_result,
        "installed": True,
        "enabled": False,
        "persisted": bool(getattr(registry, "persisted", False)),
        "next_step": (
            f"Installed and registered, currently DISABLED. Enable "
            f"{', '.join(p.tool_names)} from the Tool State tab when you want "
            f"it callable."
            if reload_result else
            "Installed, but the live reloader isn't available — it will appear "
            "(disabled) after a restart."
        ),
    }


@router.get("/projects")
async def list_projects(request: Request):
    """Every agreed project, with its grants. The list a harness reads to
    know where work may happen and what it may fetch."""
    store = _store(request)
    items = store.projects()
    return {"count": len(items),
            # where projects live: each one's `drawer` is a folder in here
            "cabinet": store.cabinet(),
            "projects": [{**x["grants"], "state": x["state"],
                          "journal_count": len(x["journal"]),
                          "last_entry": (x["journal"][-1] if x["journal"] else None),
                          "files_touched": x["files_touched"]} for x in items]}


@router.post("/projects/{name}/done")
async def confirm_project_done(request: Request, name: str):
    """Operator only. The model may ASK for done (project_log with status
    done); the person who checked the result says it is."""
    body = await request.json() if int(request.headers.get("content-length") or 0) else {}
    try:
        state = _store(request).confirm_done(name, str((body or {}).get("note") or ""))
    except ProposalError as ex:
        return JSONResponse({"ok": False, "error": str(ex)}, status_code=409)
    return {"ok": True, "state": state}


@router.post("/projects/{name}/status")
async def set_project_status(request: Request, name: str):
    body = await request.json()
    try:
        state = _store(request).set_status(name, str(body.get("status") or ""),
                                           str(body.get("note") or ""))
    except ProposalError as ex:
        return JSONResponse({"ok": False, "error": str(ex)}, status_code=409)
    return {"ok": True, "state": state}


@router.get("/projects/{name}")
async def get_project(request: Request, name: str):
    p = _store(request).project(name)
    if p is None:
        raise HTTPException(status_code=404, detail=f"no project '{name}'")
    return p


@router.get("/projects/{name}/grants")
async def get_project_grants(request: Request, name: str):
    p = _store(request).project(name)
    if p is None:
        raise HTTPException(status_code=404, detail=f"no project '{name}'")
    return p["grants"]


async def _plug_in(request: Request, store, p) -> dict:
    """An approved plugin proposal: read the file that was just installed
    (the bytes that were reviewed), plug it into the running hub and ask it
    for its tools. They arrive SWITCHED OFF - the file says start_disabled,
    and that is the second gate, the same one an approved tool goes through:
    approving the address and letting a tool run are two decisions."""
    from ..config import PluginConfig

    hub = getattr(request.app.state, "upstreams", None)
    path = store.plugin_file(p)
    name = (p.plugin or {}).get("name")
    row = None
    plugged = False
    if hub is not None and path is not None:
        plugs = [x for x in PluginConfig.many_from_dir(str(path.parent)) if x.name == name]
        if plugs and hub.add_plugin(plugs[0]):
            plugged = True
            row = next((r for r in await hub.refresh(only=name) if r["component"] == name), None)
    registry = getattr(request.app.state, "tool_registry", None)
    count = (row or {}).get("tool_count", 0)
    if not plugged:
        next_step = ("Installed. The component hub isn't running, so it will be plugged in "
                     "(every tool off) at the next start.")
    elif row and row.get("available"):
        next_step = (f"Plugged in: {count} tool(s) listed, all switched OFF. Read each one and "
                     f"enable the ones that may run from the Tool State tab.")
    else:
        next_step = (f"Plugged in, and it did not answer ({(row or {}).get('error') or 'no reason given'}). "
                     "It is asked again every minute; whatever it offers when it does answer arrives "
                     "switched OFF.")
    return {
        "ok": True,
        "proposal": _public(p),
        "installed_as": p.installed_as,
        "installed": True,
        "plugged_in": plugged,
        "component": row,
        "enabled": False,
        "persisted": bool(getattr(registry, "persisted", False)),
        "next_step": next_step,
    }


@router.post("/proposals/{pid}/reject")
async def reject_proposal(request: Request, pid: str):
    """Refuse a proposal, with a critique.

    Body: {"critique": "...", "final": false} — the critique is required,
    and the store enforces it. A bare refusal gives the proposer nothing to
    revise against, which turns a review loop into a wall. This mirrors the
    consolidator's reject-with-critique exactly.

    `final` is "not at all" rather than "not like this": the chain cannot
    be superseded, and the names it asked for are refused at propose time
    with this critique attached.
    """
    body = await request.json()
    critique = str(body.get("critique") or "")
    final = bool(body.get("final", False))
    try:
        p = _store(request).reject(pid, critique, final=final)
    except ProposalError as ex:
        return JSONResponse({"ok": False, "error": str(ex)}, status_code=409)
    return {"ok": True, "proposal": _public(p)}
