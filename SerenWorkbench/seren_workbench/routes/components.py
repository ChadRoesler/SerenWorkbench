"""
The standard components this Workbench passes through (seren_workbench.upstream).

    GET  /components            each component: on or off, reachable or why not, its tools
    POST /components/refresh    ask them again now   {"component": "memory"} for one
    POST /components/state      switch one on or off {"component": "loci", "enabled": false}

A switch flipped here holds until restart; `components:` in the yaml is what a
restart starts from.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, Request

router = APIRouter(tags=["components"])


def _hub(request: Request):
    hub = getattr(request.app.state, "upstreams", None)
    if hub is None:
        raise HTTPException(503, "the component hub is not running (no MCP surface)")
    return hub


@router.get("/components")
async def components(request: Request):
    out = {"ok": True, "components": _hub(request).snapshot()}
    # Beside the components, because it is the other half of "what is this
    # Workbench offering right now": who has been told about changes to the
    # list, and what it is holding up on the cluster.
    watch = getattr(request.app.state, "tool_watch", None)
    if watch is not None:
        out["tool_list"] = watch.snapshot()
    holds = getattr(request.app.state, "holds", None)
    if holds is not None:
        out["holding"] = holds.snapshot()
    return out


@router.post("/components/refresh")
async def refresh(request: Request, body: dict = Body(default={})):
    hub = _hub(request)
    only = (body or {}).get("component") or None
    if only and only not in hub.states:
        raise HTTPException(404, f"no component named '{only}'")
    return {"ok": True, "components": await hub.refresh(only=only)}


@router.post("/components/state")
async def set_state(request: Request, body: dict = Body(...)):
    hub = _hub(request)
    key = str((body or {}).get("component") or "")
    if "enabled" not in (body or {}):
        raise HTTPException(400, "say {\"component\": ..., \"enabled\": true|false}")
    if not hub.set_enabled(key, bool(body["enabled"])):
        raise HTTPException(404, f"no component named '{key}'")
    if body["enabled"]:
        await hub.refresh(only=key)
    return {"ok": True, "components": hub.snapshot()}
