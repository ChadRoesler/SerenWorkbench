"""
seren_workbench.__main__
════════════════════════════════════════════════════════════════════════

Entry point for ``python -m seren_workbench`` — starts the uvicorn server.

Usage::

    python -m seren_workbench [--config CONFIG] [--port PORT] [--host HOST]

Config is loaded from ./seren-workbench.yaml by default; override with
--config or the SEREN_WORKBENCH_CONFIG env var.
"""
from __future__ import annotations

import argparse
import os
import sys


def main():
    parser = argparse.ArgumentParser(
        description="SerenWorkbench — MCP server for the Seren stack")
    parser.add_argument("--config", "-c", default=None,
                        help="Path to seren-workbench.yaml (default: "
                             "./seren-workbench.yaml or $SEREN_WORKBENCH_CONFIG)")
    parser.add_argument("--port", type=int, default=0,
                        help="Override the configured port")
    parser.add_argument("--host", type=str, default=None,
                        help="Override the configured host")
    args = parser.parse_args()

    from .app import create_app
    from .config import load_config

    cfg = load_config(args.config)
    if args.port:
        cfg.server.port = args.port
    if args.host:
        cfg.server.host = args.host

    app = create_app(cfg)

    import uvicorn
    uvicorn.run(
        app,
        host=cfg.server.host,
        port=cfg.server.port,
        log_level=os.environ.get("SEREN_WORKBENCH_LOG_LEVEL", "info").lower(),
    )


if __name__ == "__main__":
    main()
