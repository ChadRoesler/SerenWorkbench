"""
seren_workbench.config
════════════════════════════════════════════════════════════════════════

Service-specific config for the Workbench MCP server. Uses seren_meninges
shared blocks (ServerConfig, TlsConfig) plus its own server-specific sections:
tools, dashboard, and dynamic_tools.

Follows the same pattern as seren_loci.config, seren_memory.config, and
seren_corpus_callosum.config — the family's lenient-load discipline.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from seren_meninges import ServerConfig, TlsConfig

log = logging.getLogger(__name__)

# Port 7425 — family convention: memory 7420, loci-v 7421, loci-nv 7422,
# scc-nv 7423, scc-v 7424, workbench 7425, probe 7430
DEFAULT_PORT = 7425


@dataclass
class DashboardConfig:
    """Operator dashboard knobs."""
    enabled: bool = True
    tools_dir: str = "/opt/seren/tools"
    tools_enabled: list[str] = field(default_factory=lambda: [])
    tools_disabled: list[str] = field(default_factory=lambda: [])

    @classmethod
    def from_dict(cls, d: Optional[dict[str, Any]]) -> "DashboardConfig":
        d = d or {}
        return cls(
            enabled=bool(d.get("enabled", True)),
            tools_dir=str(d.get("tools_dir", "/opt/seren/tools")),
            tools_enabled=list(d.get("tools_enabled", [])),
            tools_disabled=list(d.get("tools_disabled", [])),
        )


@dataclass
class WorkbenchConfig:
    """The top-level config, composed from shared blocks + service blocks."""
    server: ServerConfig = field(default_factory=lambda: ServerConfig(port=DEFAULT_PORT))
    tls: TlsConfig = field(default_factory=TlsConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)


def _apply_env_overrides(cfg: WorkbenchConfig) -> WorkbenchConfig:
    """SEREN_WORKBENCH_* env wins last."""
    env = os.environ
    if v := env.get("SEREN_WORKBENCH_HOST"):
        cfg.server.host = v
    if v := env.get("SEREN_WORKBENCH_PORT"):
        cfg.server.port = int(v)
    if v := env.get("SEREN_WORKBENCH_BEARER_TOKEN"):
        cfg.server.bearer_token = v
    if v := env.get("SEREN_WORKBENCH_BEARER_TOKEN_ENV"):
        cfg.server.bearer_token_env = v
    if v := env.get("SEREN_WORKBENCH_BEARER_TOKEN_KEYRING"):
        cfg.server.bearer_token_keyring = v
    if v := env.get("SEREN_WORKBENCH_TRUST_SYSTEM_STORE"):
        cfg.tls.trust_system_store = v.lower() in ("1", "true", "yes", "on")
    if v := env.get("SEREN_WORKBENCH_TOOLS_DIR"):
        cfg.dashboard.tools_dir = v
    return cfg


def load_config(path: Optional[str] = None) -> WorkbenchConfig:
    """Defaults -> yaml -> env (later wins). A missing file is fine — defaults
    + env is a valid zero-config run."""
    data: dict[str, Any] = {}
    candidate = path or os.environ.get("SEREN_WORKBENCH_CONFIG") or "seren-workbench.yaml"
    cfg_path = Path(os.path.expanduser(candidate))
    if cfg_path.is_file():
        try:
            with open(cfg_path) as f:
                data = yaml.safe_load(f) or {}
        except Exception:  # noqa: BLE001
            data = {}

    server = ServerConfig.from_dict(data.get("server"), default_port=DEFAULT_PORT)
    tls = TlsConfig.from_dict(data.get("tls"))
    dashboard = DashboardConfig.from_dict(data.get("dashboard"))

    cfg = WorkbenchConfig(server=server, tls=tls, dashboard=dashboard)
    return _apply_env_overrides(cfg)
