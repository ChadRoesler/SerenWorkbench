"""
Tool routes — GET /tools (list tools), GET /tools/state (snapshot),
POST /tools/state (enable/disable toggle).

The LLM calls GET /tools for tool discovery. The viewer calls the state
endpoints for enable/disable toggles.
"""
from __future__ import annotations

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
                "description": t.description,
                "type": t.type,
                "source": t.source,
                "enabled": t.enabled,
                "parameters": t.parameters,
            }
            for t in tools
        ],
    }


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
    tool_name = body.get("tool", "").strip()
    action_name = body.get("action", "").strip()
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
