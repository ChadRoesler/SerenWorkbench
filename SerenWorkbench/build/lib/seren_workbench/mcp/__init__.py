"""
seren_workbench.mcp — FastMCP server wiring for SerenWorkbench.

The MCP tools here wrap the ToolRegistry and expose tools to the connected LLM
via the MCP protocol. Tools are conditionally enabled/disabled based on the
registry state — the operator dashboard's toggles feed into this.
"""
from __future__ import annotations
