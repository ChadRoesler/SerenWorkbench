"""
seren_workbench.tool_config — tool-level configuration overrides.

Every tool ships with Nano-floor defaults at its call site. This module is
purely the override layer: operator-tunable knobs loaded from YAML.
"""
from __future__ import annotations

from .mcp_config import McpConfig
from .tool_section import ToolSection

__all__ = ["McpConfig", "ToolSection"]
