"""
Tool routes — GET /tools (list tools), GET /tools/state (snapshot),
POST /tools/state (enable/disable toggle), GET+POST /tools/manifests
(inspect and live-reload the YAML manifest directory).

The LLM calls GET /tools for tool discovery. The viewer calls the state
endpoints for enable/disable toggles.

ROUTE ORDER: the literal paths here must stay ahead of any future
/tools/{name}, or FastAPI will match "state" and "manifests" as a name.
Same specific-before-generic rule that bit SerenMargin's /notes/stats.
"""
from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

router = APIRouter(tags=["tools"])


@router.get("/tools")
async def list_tools(request: Request):
    """Return the full tool list with schemas — the MCP client calls this
    for tool discovery, the dashboard reads it for display."""
    reg = request.app.state.tool_registry
    tools = reg.all_tools()
    return {
        "count": len(tools),
        "tools": [
            {
                "name": t.name,
                "display_name": t.display_name or t.name,
                "toolbox": t.toolbox or "Other",
                "description": t.description,
                "type": t.type,
                "source": t.source,
                "enabled": t.enabled,
                "parameters": t.parameters,
            }
            for t in tools
        ],
    }


@router.get("/tools/manifests")
async def get_manifests(request: Request):
    """What the YAML manifest directory looked like at the last load —
    live tools, skipped tools and the reason, files that failed to parse."""
    reg = getattr(request.app.state, "dynamic_registry", None)
    if reg is None:
        raise HTTPException(
            status_code=503,
            detail="dynamic tool registry unavailable (MCP surface not mounted)",
        )
    return asdict(reg.current_snapshot())


@router.post("/tools/manifests/reload")
async def reload_manifests(request: Request):
    """Re-read the manifest directory and apply the difference LIVE.

    Adds, replaces and removes dynamic tools on the running MCP surface.
    Builtin tools are never touched, operator enable/disable state survives,
    and the response says exactly what changed.

    A note on what this deliberately is NOT: it reads what is already on
    disk. It cannot author a tool. Registration stays a thing that happens
    because a human put a file somewhere — hands on the surface.
    """
    reg = getattr(request.app.state, "dynamic_registry", None)
    if reg is None:
        raise HTTPException(
            status_code=503,
            detail="dynamic tool registry unavailable (MCP surface not mounted)",
        )
    snapshot = await reg.reload()
    return {"ok": True, **asdict(snapshot)}


@router.get("/tools/state")
async def get_tool_state(request: Request):
    """Snapshot of which tools and actions are enabled/disabled."""
    reg = request.app.state.tool_registry
    return reg.snapshot()


@router.post("/tools/state")
async def set_tool_state(request: Request):
    """Toggle enable/disable for a tool or an action.

    Body::
        {
            "tool": "<tool_name>",
            "action": "<action_name>",   # optional — if set, toggles a sub-action
            "enabled": true|false
        }
    """
    body = await request.json()
    tool_name = str(body.get("tool") or "").strip()
    action_name = str(body.get("action") or "").strip()
    enabled = body.get("enabled", True)
    reg = request.app.state.tool_registry

    if action_name:
        ok = (reg.enable_action(tool_name, action_name)
              if enabled else reg.disable_action(tool_name, action_name))
        if not ok:
            return JSONResponse(
                {"ok": False, "error": f"no action '{tool_name}.{action_name}'"},
                status_code=404)
        return {"ok": True, "tool": tool_name, "action": action_name,
                "enabled": enabled}
    else:
        ok = (reg.enable_tool(tool_name)
              if enabled else reg.disable_tool(tool_name))
        if not ok:
            return JSONResponse(
                {"ok": False, "error": f"no tool '{tool_name}'"},
                status_code=404)
        return {"ok": True, "tool": tool_name, "enabled": enabled}
