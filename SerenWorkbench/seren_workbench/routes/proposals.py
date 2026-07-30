"""
Proposal routes — the operator half of the tool-proposal gate.

GET  /proposals                 list, newest first
GET  /proposals/{id}            one, including the full manifest text
POST /proposals/{id}/approve    install it and reload the live surface
POST /proposals/{id}/reject     refuse it, with a critique the proposer reads

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
    p = _store(request).get(pid)
    if p is None:
        raise HTTPException(status_code=404, detail=f"no proposal '{pid}'")
    return _public(p, include_manifest=True)


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
        "next_step": (
            f"Installed and registered, currently DISABLED. Enable "
            f"{', '.join(p.tool_names)} from the Tool State tab when you want "
            f"it callable."
            if reload_result else
            "Installed, but the live reloader isn't available — it will appear "
            "(disabled) after a restart."
        ),
    }


@router.post("/proposals/{pid}/reject")
async def reject_proposal(request: Request, pid: str):
    """Refuse a proposal, with a critique.

    Body: {"critique": "..."} — required, and the store enforces it. A bare
    refusal gives the proposer nothing to revise against, which turns a
    review loop into a wall. This mirrors the consolidator's
    reject-with-critique exactly.
    """
    body = await request.json()
    critique = str(body.get("critique") or "")
    try:
        p = _store(request).reject(pid, critique)
    except ProposalError as ex:
        return JSONResponse({"ok": False, "error": str(ex)}, status_code=409)
    return {"ok": True, "proposal": _public(p)}
