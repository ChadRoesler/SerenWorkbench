"""
seren_workbench — the MCP server for the Seren stack.

The tool surface that LLMs see. Builtin tools (memory, web, time, cluster, etc.)
are defined in models/tools/; dynamic tools are loaded from YAML manifests in a
tools/ directory. This package wires everything into a FastAPI app that serves
the MCP transport AND an operator dashboard.

Integrates seren_meninges (config/auth/viewer baseplate) and seren_sinew
(request logging) following the same pattern as seren_loci, seren_memory,
seren_corpus_callosum, and seren_probe.
"""
from __future__ import annotations

# Version flows from the git tag via setuptools-scm (written to _version.py at
# build time, read here). Fallback only fires in a bare source checkout that was
# never built. Mirrors the family so every seren_* exposes __version__ alike.
try:
    from ._version import version as __version__
except Exception:  # noqa: BLE001 - source checkout without a build
    __version__ = "0.0.0+unknown"

from .config import WorkbenchConfig, load_config  # noqa: F401,E402
from .tool_registry import ToolRegistry, build_registry  # noqa: F401,E402

__all__ = [
    "__version__",
    "WorkbenchConfig",
    "load_config",
    "ToolRegistry",
    "build_registry",
]
