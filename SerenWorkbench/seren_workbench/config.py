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
from seren_meninges.credentials import resolve_token

log = logging.getLogger(__name__)

# Port 7425 — the family map, canonical and verified by running --describe
# on all eight installers:
#   lodestar 6361 · memory 7420 · margin 7421 · loci 7422 ·
#   corpus-callosum 7423 · workbench 7425 · probe 7430 · observatory 7777
#
# The installer used to say 7444 while this said 7425, so an installed node
# answered somewhere the docs didn't. Both are 7425 now. If you move this,
# move services/{bash,powershell}/seren-workbench-setup.sh with it — those
# are checked against each other by --describe, not by hope.
DEFAULT_PORT = 7425


# Where the plug-and-play manifests live when the operator says nothing.
# This matches what the Starwright installer writes. It used to be
# /opt/seren/tools, which needs root to create and which the installer never
# wrote, so an installed Workbench and its own default disagreed.
DEFAULT_TOOLS_DIR = "~/seren-workbench/tools"
STATE_FILE_NAME = ".tool-state.json"


def _expand(path: str) -> str:
    """`~` means the home directory, on every OS, in every place a path is
    read. The installer writes `~/seren-workbench/tools`; a config that stored
    that raw loaded zero tools, refused every reload ("directory does not
    exist") and staged proposals under a literal directory named `~`."""
    return os.path.expanduser(str(path or ""))


@dataclass
class DashboardConfig:
    """Operator dashboard knobs.

    tools_enabled / tools_disabled seed the registry's enable state at
    startup:
      - tools_disabled: these tools start DISABLED.
      - tools_enabled:  if non-empty, it is an ALLOWLIST - every tool NOT
        named here starts disabled. Empty list = everything enabled.

    state_file is where the dashboard's own toggles are REMEMBERED, so a
    tool you switched off (or a proposal you approved but left off) is still
    off after the box reboots. Blank means <tools_dir>/.tool-state.json.
    Precedence when they disagree: a tool named in the yaml lists above is
    the operator's written word and wins; everything else is whatever the
    dashboard last said.

    proposals_dir is the STAGING area for tools the model has proposed. It
    defaults to a subdirectory of tools_dir because that is where an
    operator will look for it - and it is safe there because the loader
    globs "*.yaml" NON-recursively, so a subdirectory is invisible to it.
    That safety is load-bearing rather than incidental, so there is a test
    asserting a manifest in here never reaches the live surface.

    proposals_enabled gates the propose_tool tool itself. Default TRUE is
    defensible only because a proposal cannot run: it is a text file in a
    directory nothing loads until a human moves it. Set false to remove the
    tool entirely - "don't install" as a config line.
    """
    enabled: bool = True
    tools_dir: str = DEFAULT_TOOLS_DIR
    tools_enabled: list[str] = field(default_factory=lambda: [])
    tools_disabled: list[str] = field(default_factory=lambda: [])
    proposals_dir: str = ""          # "" => <tools_dir>/proposed
    proposals_enabled: bool = True
    state_file: str = ""             # "" => <tools_dir>/.tool-state.json

    def __post_init__(self) -> None:
        self.tools_dir = _expand(self.tools_dir)
        self.proposals_dir = _expand(self.proposals_dir)
        self.state_file = _expand(self.state_file)

    def resolve_proposals_dir(self) -> str:
        return self.proposals_dir or os.path.join(self.tools_dir, "proposed")

    def resolve_state_file(self) -> str:
        return self.state_file or os.path.join(self.tools_dir, STATE_FILE_NAME)

    @classmethod
    def from_dict(cls, d: Optional[dict[str, Any]]) -> "DashboardConfig":
        d = d or {}
        return cls(
            enabled=bool(d.get("enabled", True)),
            tools_dir=str(d.get("tools_dir") or DEFAULT_TOOLS_DIR),
            tools_enabled=list(d.get("tools_enabled") or []),
            tools_disabled=list(d.get("tools_disabled") or []),
            proposals_dir=str(d.get("proposals_dir", "") or ""),
            proposals_enabled=bool(d.get("proposals_enabled", True)),
            state_file=str(d.get("state_file", "") or ""),
        )


# The Seren services the builtin tools talk to, by the DI parameter name the
# tool impls use. SearXNG is not here: it is not a Seren service and speaks
# no bearer.
SEREN_SERVICES = ("memory", "runtime_host", "scheduler")


@dataclass
class ServicesConfig:
    """Base URLs and credentials for the Seren services the builtin tools
    reach through.

    These are the DI targets: each builtin tool takes an httpx.AsyncClient
    named after a service (memory, runtime_host, searxng, scheduler); the
    app builds one client per URL here and injects it by parameter name.

    Defaults are localhost + the family port convention, so a zero-config
    run on the cluster head Just Works. Point them across the LAN in yaml
    for a split deploy.

    TOKENS. A stack installed with --gen-token has a bearer on Memory and on
    Lodestar, and until this block existed the builtins had no way to send
    one - every remember/recall/start_service came back 401 with no config
    key to fix it. The three family pointers (inline / env-var name /
    keyring ref, same precedence as seren_meninges.credentials) exist here
    twice over: a shared set that every Seren service gets, and a
    per-service set that wins for that one service. One cluster token goes
    in the shared slot; a split deploy with different tokens per box uses
    the per-service ones.
    """
    memory_url: str = "http://127.0.0.1:7420"        # SerenMemory
    runtime_host_url: str = "http://127.0.0.1:6361"  # SerenLodestar (cluster head)
    searxng_url: str = "http://127.0.0.1:8080"       # SearXNG metasearch
    scheduler_url: str = "http://127.0.0.1:6361"     # scheduler surface (Lodestar)
    timeout_seconds: float = 15.0                    # per-request client timeout

    # Shared credential for every Seren service (never SearXNG).
    bearer_token: str = field(default="", repr=False)
    bearer_token_env: str = ""
    bearer_token_keyring: str = ""
    # Per-service credentials; each wins over the shared one for its service.
    memory_bearer_token: str = field(default="", repr=False)
    memory_bearer_token_env: str = ""
    memory_bearer_token_keyring: str = ""
    runtime_host_bearer_token: str = field(default="", repr=False)
    runtime_host_bearer_token_env: str = ""
    runtime_host_bearer_token_keyring: str = ""
    scheduler_bearer_token: str = field(default="", repr=False)
    scheduler_bearer_token_env: str = ""
    scheduler_bearer_token_keyring: str = ""

    def resolve_bearer(self, service: str) -> str:
        """The token to PRESENT to *service*, or "" for none.

        Per-service pointers first; if none of the three is set, the shared
        pointers. Resolution is seren_meninges.credentials.resolve_token,
        the same call every leaf uses inbound, so "config holds a pointer,
        not the secret" is true in this direction too.
        """
        if service not in SEREN_SERVICES:
            return ""
        own = (getattr(self, f"{service}_bearer_token"),
               getattr(self, f"{service}_bearer_token_keyring"),
               getattr(self, f"{service}_bearer_token_env"))
        if any(own):
            inline, keyring_ref, env_var = own
        else:
            inline, keyring_ref, env_var = (self.bearer_token,
                                            self.bearer_token_keyring,
                                            self.bearer_token_env)
        return resolve_token(inline=inline or None, keyring_ref=keyring_ref or None,
                             env_var=env_var or None)

    @classmethod
    def from_dict(cls, d: Optional[dict[str, Any]]) -> "ServicesConfig":
        d = d or {}
        out = cls()
        out.memory_url = str(d.get("memory_url", out.memory_url))
        # `lodestar_url` is the family name for the cluster head; the DI
        # parameter the tools take is still `runtime_host`, so both keys land
        # in the same place and the older one keeps working.
        out.runtime_host_url = str(d.get("lodestar_url") or d.get("runtime_host_url")
                                   or out.runtime_host_url)
        out.searxng_url = str(d.get("searxng_url", out.searxng_url))
        out.scheduler_url = str(d.get("scheduler_url", out.scheduler_url))
        try:
            out.timeout_seconds = float(d.get("timeout_seconds", out.timeout_seconds))
        except (TypeError, ValueError):
            pass  # lenient: unparseable timeout keeps the default
        for key in _TOKEN_KEYS:
            if key in d:
                setattr(out, key, str(d.get(key) or ""))
        return out


def _token_keys() -> tuple[str, ...]:
    suffixes = ("bearer_token", "bearer_token_env", "bearer_token_keyring")
    keys = list(suffixes)
    for svc in SEREN_SERVICES:
        keys += [f"{svc}_{suffix}" for suffix in suffixes]
    return tuple(keys)


_TOKEN_KEYS = _token_keys()


@dataclass
class UpdatesConfig:
    """"Is there a newer seren-workbench" checking. Cosmetic, opt-outable.

    Needs seren-meninges[updates]. Without it the check reports
    status="unavailable" rather than silently reading as "you're current" -
    see seren_meninges/updates.py for why that distinction is load-bearing.
    """
    enabled: bool = True
    check_interval_hours: float = 6.0
    index_url: str = "https://pypi.org/pypi/{distribution}/json"
    allow_prerelease: bool = False

    @classmethod
    def from_dict(cls, d: Optional[dict[str, Any]]) -> "UpdatesConfig":
        d = d or {}
        default = cls()
        interval = d.get("check_interval_hours")
        try:
            hours = float(interval) if interval is not None else default.check_interval_hours
        except (TypeError, ValueError):
            log.warning("unparseable updates.check_interval_hours %r — using %s",
                        interval, default.check_interval_hours)
            hours = default.check_interval_hours
        return cls(
            enabled=bool(d.get("enabled", True)),
            check_interval_hours=hours if hours > 0 else default.check_interval_hours,
            index_url=str(d.get("index_url", "") or default.index_url),
            allow_prerelease=bool(d.get("allow_prerelease", False)),
        )


@dataclass
class WorkbenchConfig:
    """The top-level config, composed from shared blocks + service blocks."""
    server: ServerConfig = field(default_factory=lambda: ServerConfig(port=DEFAULT_PORT))
    tls: TlsConfig = field(default_factory=TlsConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    services: ServicesConfig = field(default_factory=ServicesConfig)
    updates: "UpdatesConfig" = field(default_factory=lambda: UpdatesConfig())
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
        cfg.dashboard.tools_dir = _expand(v)
    if v := env.get("SEREN_WORKBENCH_STATE_FILE"):
        cfg.dashboard.state_file = _expand(v)
    if v := env.get("SEREN_WORKBENCH_MEMORY_URL"):
        cfg.services.memory_url = v
    if v := env.get("SEREN_WORKBENCH_LODESTAR_URL") or env.get("SEREN_WORKBENCH_RUNTIME_HOST_URL"):
        cfg.services.runtime_host_url = v
    if v := env.get("SEREN_WORKBENCH_SEARXNG_URL"):
        cfg.services.searxng_url = v
    if v := env.get("SEREN_WORKBENCH_SCHEDULER_URL"):
        cfg.services.scheduler_url = v
    if v := env.get("SEREN_WORKBENCH_UPDATES_ENABLED"):
        cfg.updates.enabled = v.lower() in ("1", "true", "yes", "on")
    # Outbound credentials: SEREN_WORKBENCH_SERVICES_BEARER_TOKEN[_ENV|_KEYRING]
    # for the shared one, SEREN_WORKBENCH_MEMORY_BEARER_TOKEN[...] and so on
    # per service. The plain SEREN_WORKBENCH_BEARER_TOKEN above is what THIS
    # service requires of its callers - a different thing, kept apart.
    for key in _TOKEN_KEYS:
        env_name = "SEREN_WORKBENCH_" + (key if key.startswith(SEREN_SERVICES) else "services_" + key).upper()
        if v := env.get(env_name):
            setattr(cfg.services, key, v)
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
            # encoding= IS NOT OPTIONAL. Without it Python uses the LOCALE
            # codec - cp1252 on Windows - and seren-workbench.yaml.sample opens
            # with a `# ═══` banner (U+2550 -> E2 95 90), so byte 0x90 raises
            # UnicodeDecodeError at position 4. The bare except below would
            # then swallow it and hand back {}, meaning a Windows operator's
            # ENTIRE config is silently ignored while they stare at the values
            # they just set. Leniency is right for a MALFORMED file; it must
            # not be what hides a file we simply failed to read.
            with open(cfg_path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            source_path = str(cfg_path)
        except Exception as ex:  # noqa: BLE001
            log.warning("could not read %s: %s — using defaults + env", cfg_path, ex)
            data = {}

    server = ServerConfig.from_dict(data.get("server"), default_port=DEFAULT_PORT)
    tls = TlsConfig.from_dict(data.get("tls"))
    dashboard = DashboardConfig.from_dict(data.get("dashboard"))
    services = ServicesConfig.from_dict(data.get("services"))
    updates = UpdatesConfig.from_dict(data.get("updates"))

    cfg = WorkbenchConfig(server=server, tls=tls, dashboard=dashboard,
                          services=services, updates=updates,
                          source_path=source_path)
    return _apply_env_overrides(cfg)
