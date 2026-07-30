"""
seren_workbench.config
════════════════════════════════════════════════════════════════════════

Service-specific config for the Workbench MCP server. Uses seren_meninges
shared blocks (ServerConfig, TlsConfig) plus its own server-specific sections:
tools, dashboard, services, and dynamic_tools.

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
    """Operator dashboard knobs.

    tools_enabled / tools_disabled seed the registry's enable state at
    startup (so an operator's disables survive a restart):
      - tools_disabled: these tools start DISABLED.
      - tools_enabled:  if non-empty, it is an ALLOWLIST — every tool NOT
        named here starts disabled. Empty list = everything enabled.

    proposals_dir is the STAGING area for tools the model has proposed. It
    defaults to a subdirectory of tools_dir because that is where an
    operator will look for it — and it is safe there because the loader
    globs "*.yaml" NON-recursively, so a subdirectory is invisible to it.
    That safety is load-bearing rather than incidental, so there is a test
    asserting a manifest in here never reaches the live surface.

    proposals_enabled gates the propose_tool tool itself. Default TRUE is
    defensible only because a proposal cannot run: it is a text file in a
    directory nothing loads until a human moves it. Set false to remove the
    tool entirely — "don't install" as a config line.
    """
    enabled: bool = True
    tools_dir: str = "/opt/seren/tools"
    tools_enabled: list[str] = field(default_factory=lambda: [])
    tools_disabled: list[str] = field(default_factory=lambda: [])
    proposals_dir: str = ""          # "" => <tools_dir>/proposed
    proposals_enabled: bool = True

    def resolve_proposals_dir(self) -> str:
        import os
        return self.proposals_dir or os.path.join(self.tools_dir, "proposed")

    @classmethod
    def from_dict(cls, d: Optional[dict[str, Any]]) -> "DashboardConfig":
        d = d or {}
        return cls(
            enabled=bool(d.get("enabled", True)),
            tools_dir=str(d.get("tools_dir", "/opt/seren/tools")),
            tools_enabled=list(d.get("tools_enabled", [])),
            tools_disabled=list(d.get("tools_disabled", [])),
            proposals_dir=str(d.get("proposals_dir", "") or ""),
            proposals_enabled=bool(d.get("proposals_enabled", True)),
        )


@dataclass
class ServicesConfig:
    """Base URLs for the Seren services the builtin tools reach through.

    These are the DI targets: each builtin tool takes an httpx.AsyncClient
    named after a service (memory, runtime_host, searxng, scheduler); the
    app builds one client per URL here and injects it by parameter name.

    Defaults are localhost + the family port convention, so a zero-config
    run on the cluster head Just Works. Point them across the LAN in yaml
    for a split deploy.
    """
    memory_url: str = "http://127.0.0.1:7420"        # SerenMemory
    runtime_host_url: str = "http://127.0.0.1:6361"  # SerenRuntimeHost (cluster head)
    searxng_url: str = "http://127.0.0.1:8080"       # SearXNG metasearch
    scheduler_url: str = "http://127.0.0.1:6361"     # scheduler surface (RuntimeHost today)
    timeout_seconds: float = 15.0                    # per-request client timeout

    @classmethod
    def from_dict(cls, d: Optional[dict[str, Any]]) -> "ServicesConfig":
        d = d or {}
        out = cls()
        out.memory_url = str(d.get("memory_url", out.memory_url))
        out.runtime_host_url = str(d.get("runtime_host_url", out.runtime_host_url))
        out.searxng_url = str(d.get("searxng_url", out.searxng_url))
        out.scheduler_url = str(d.get("scheduler_url", out.scheduler_url))
        try:
            out.timeout_seconds = float(d.get("timeout_seconds", out.timeout_seconds))
        except (TypeError, ValueError):
            pass  # lenient: unparseable timeout keeps the default
        return out


@dataclass
class WorkbenchConfig:
    """The top-level config, composed from shared blocks + service blocks."""
    server: ServerConfig = field(default_factory=lambda: ServerConfig(port=DEFAULT_PORT))
    tls: TlsConfig = field(default_factory=TlsConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    services: ServicesConfig = field(default_factory=ServicesConfig)
    # The yaml file this config was loaded from (None = defaults/env only).
    # Threaded into McpConfig.load() so the server block and the tools block
    # always come from the SAME file — no CWD-vs-argv[0] split brain.
    source_path: Optional[str] = None


def _apply_env_overrides(cfg: WorkbenchConfig) -> WorkbenchConfig:
    """SEREN_WORKBENCH_* env wins last."""
    env = os.environ
    if v := env.get("SEREN_WORKBENCH_HOST"):
        cfg.server.host = v
    if v := env.get("SEREN_WORKBENCH_PORT"):
        try:
            cfg.server.port = int(v)
        except ValueError:
            log.warning("SEREN_WORKBENCH_PORT=%r is not an int; keeping %s", v, cfg.server.port)
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
    if v := env.get("SEREN_WORKBENCH_MEMORY_URL"):
        cfg.services.memory_url = v
    if v := env.get("SEREN_WORKBENCH_RUNTIME_HOST_URL"):
        cfg.services.runtime_host_url = v
    if v := env.get("SEREN_WORKBENCH_SEARXNG_URL"):
        cfg.services.searxng_url = v
    if v := env.get("SEREN_WORKBENCH_SCHEDULER_URL"):
        cfg.services.scheduler_url = v
    return cfg


def load_config(path: Optional[str] = None) -> WorkbenchConfig:
    """Defaults -> yaml -> env (later wins). A missing file is fine — defaults
    + env is a valid zero-config run."""
    data: dict[str, Any] = {}
    candidate = path or os.environ.get("SEREN_WORKBENCH_CONFIG") or "seren-workbench.yaml"
    cfg_path = Path(os.path.expanduser(candidate))
    source_path: Optional[str] = None
    if cfg_path.is_file():
        try:
            with open(cfg_path) as f:
                data = yaml.safe_load(f) or {}
            source_path = str(cfg_path)
        except Exception:  # noqa: BLE001
            data = {}

    server = ServerConfig.from_dict(data.get("server"), default_port=DEFAULT_PORT)
    tls = TlsConfig.from_dict(data.get("tls"))
    dashboard = DashboardConfig.from_dict(data.get("dashboard"))
    services = ServicesConfig.from_dict(data.get("services"))

    cfg = WorkbenchConfig(server=server, tls=tls, dashboard=dashboard,
                          services=services, source_path=source_path)
    return _apply_env_overrides(cfg)
