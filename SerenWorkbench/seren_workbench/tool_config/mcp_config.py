# ════════════════════════════════════════════════════════════════════════
#  McpConfig - operator-tunable tool configuration for the Workbench MCP server.
#
#  Every tool ships with defaults sane for the FLOOR of the cluster: an
#  Orin Nano Super dev kit ($250, 8GB shared, modest ctx). Those defaults
#  live at the CALL SITE inside each tool - so a fresh checkout with NO
#  config file present runs correctly on the cheapest viable hardware, no
#  questions asked. This file is purely the OVERRIDE layer: it exists only
#  so someone on beefier iron (Xavier 32GB, a workstation) can crank a knob
#  up. Absent config == Nano defaults.
#
#  YAML, not JSON - matches RuntimeHost + agent.
#
#  LENIENT BY DESIGN (Postel)
# ════════════════════════════════════════════════════════════════════════

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Dict

import yaml

from .tool_section import ToolSection


class McpConfig:
    """Loaded, queryable tool configuration. Registered as a DI singleton."""

    def __init__(
        self,
        tools: Dict[str, Dict[str, str]],
        source: str,
    ) -> None:
        self._tools = tools
        self.source = source

    def for_tool(self, tool_name: str) -> ToolSection:
        section = self._tools.get(tool_name)
        if section is not None:
            return ToolSection(section)
        return ToolSection.empty()

    def snapshot(self) -> Dict[str, Dict[str, str]]:
        return {k: dict(v) for k, v in self._tools.items()}

    @staticmethod
    def load() -> "McpConfig":
        path = os.environ.get("SEREN_WORKBENCH_CONFIG") or os.environ.get("MCP_CONFIG_PATH")
        if not path:
            path = str(Path(sys.argv[0]).parent / "seren-workbench.yaml")

        if not os.path.isfile(path):
            return McpConfig({}, f"(none - built-in defaults; looked at {path})")

        try:
            with open(path, "r") as f:
                raw = yaml.safe_load(f)

            if not isinstance(raw, dict):
                return McpConfig({}, path)

            tools_raw = raw.get("tools", {})
            if not isinstance(tools_raw, dict):
                tools_raw = {}

            tools: Dict[str, Dict[str, str]] = {}
            for tool_name, section in tools_raw.items():
                if not tool_name or not isinstance(section, dict):
                    continue
                flat: Dict[str, str] = {}
                for k, v in section.items():
                    flat[str(k)] = str(v) if v is not None else ""
                tools[tool_name] = flat

            return McpConfig(tools, path)

        except Exception as ex:
            print(
                f"[seren-workbench] WARNING: failed to parse {path}: {ex}. "
                "Falling back to built-in tool defaults.",
                file=sys.stderr,
            )
            return McpConfig({}, f"(parse failed - built-in defaults; {path})")
