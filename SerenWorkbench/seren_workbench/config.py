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

    def resolve_plugins_dir(self) -> str:
        """Where an APPROVED plugin proposal is installed: one small yaml per
        plugin. A subdirectory of tools_dir for the reason proposals_dir is -
        the manifest loader does not look in subdirectories, so nothing in
        here is ever mistaken for a tool manifest - and so that it is part of
        the same snapshot as the tools."""
        return os.path.join(self.tools_dir, "plugins")

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
SEREN_SERVICES = ("memory", "loci", "callosum", "hippocampus", "runtime_host", "scheduler")


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
    # The rest of the standard system (6 Oct 2026). The Workbench had a
    # connection for Memory and none for these, so there was no way to reach
    # a fact, a cross-store search or the sleep cycle through it. Their tools
    # are passed through from each service's own MCP endpoint: see upstream.py.
    loci_url: str = "http://127.0.0.1:7422"          # SerenLoci
    callosum_url: str = "http://127.0.0.1:7423"      # SerenCorpusCallosum
    hippocampus_url: str = "http://127.0.0.1:7424"   # SerenHippocampus
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
    loci_bearer_token: str = field(default="", repr=False)
    loci_bearer_token_env: str = ""
    loci_bearer_token_keyring: str = ""
    callosum_bearer_token: str = field(default="", repr=False)
    callosum_bearer_token_env: str = ""
    callosum_bearer_token_keyring: str = ""
    hippocampus_bearer_token: str = field(default="", repr=False)
    hippocampus_bearer_token_env: str = ""
    hippocampus_bearer_token_keyring: str = ""
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
        out.loci_url = str(d.get("loci_url", out.loci_url))
        out.callosum_url = str(d.get("callosum_url") or d.get("corpus_callosum_url") or out.callosum_url)
        out.hippocampus_url = str(d.get("hippocampus_url", out.hippocampus_url))
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
class ComponentsConfig:
    """Which of the standard components this Workbench passes through
    (seren_workbench.upstream). All on by default: the standard system is
    there from the start, and a component that is not running simply offers
    nothing until it is. A component switched off here contributes no tools
    at all. Plugins (Probe, Theatre, Margin...) are not listed here - they
    are manifests in dashboard.tools_dir."""
    memory: bool = True
    loci: bool = True
    corpus_callosum: bool = True
    hippocampus: bool = True
    lodestar: bool = True

    def as_dict(self) -> dict[str, bool]:
        return {"memory": self.memory, "loci": self.loci, "corpus_callosum": self.corpus_callosum,
                "hippocampus": self.hippocampus, "lodestar": self.lodestar}

    @classmethod
    def from_dict(cls, d: Optional[dict[str, Any]]) -> "ComponentsConfig":
        d = d if isinstance(d, dict) else {}
        out = cls()
        aliases = {"callosum": "corpus_callosum", "corpuscallosum": "corpus_callosum", "runtime_host": "lodestar"}
        for key, value in d.items():
            name = aliases.get(str(key).lower().replace("-", "_"), str(key).lower().replace("-", "_"))
            if hasattr(out, name) and isinstance(getattr(out, name), bool):
                setattr(out, name, bool(value) if not isinstance(value, str)
                        else value.strip().lower() in ("1", "true", "yes", "on"))
            else:
                log.warning("components.%s is not a component this Workbench knows; ignoring", key)
        return out


@dataclass
class PluginConfig:
    """One MCP plugin: a service that is not part of the standard system and
    speaks MCP - Margin, Probe, anything of yours. Its own tools are passed
    through exactly like a component's (seren_workbench.upstream).

    WHY MARGIN IS ONE OF THESE and not a manifest import. Margin publishes a
    manifest the Workbench can import (`from:` in a tools file), and that path
    works by calling Margin's HTTP routes - which means turning on Margin's
    server.http_reads, the switch that keeps a diary from being read over
    plain HTTP. Over MCP nothing has to be opened: the reads stay where
    Margin's owner left them, and the diary still comes along."""
    name: str = ""
    url: str = ""
    display: str = ""
    enabled: bool = True
    # start_disabled: every tool this plugin offers arrives SWITCHED OFF, and
    # a person turns on the ones they want from the dashboard. For a server
    # you are still getting to know, or one that carries something
    # destructive (a delete, a deploy): plug it in, look at what it has, test
    # the safe parts, and the rest cannot run until someone says so. A tool
    # someone has switched on stays on; only tools seen for the first time
    # arrive off.
    start_disabled: bool = False
    bearer_token: str = field(default="", repr=False)
    bearer_token_env: str = ""
    bearer_token_keyring: str = ""

    def resolve_bearer(self) -> str:
        return resolve_token(inline=self.bearer_token or None, keyring_ref=self.bearer_token_keyring or None,
                             env_var=self.bearer_token_env or None) or ""

    @classmethod
    def many_from_dir(cls, path: str, taken: Optional[set[str]] = None) -> "list[PluginConfig]":
        """The plugins an approved proposal installed: every *.yaml in *path*
        (dashboard.resolve_plugins_dir()), each a `plugins:` mapping like the
        config's. A name the config already has (*taken*) is the operator's
        written word and wins. Lenient: a file that does not read is skipped
        with a line in the log, never a failed start."""
        out: list[PluginConfig] = []
        seen = set(taken or ())
        folder = Path(_expand(path))
        if not folder.is_dir():
            return out
        for f in sorted(folder.glob("*.yaml")):
            try:
                raw = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            except Exception as ex:  # noqa: BLE001
                log.warning("could not read plugin file %s: %s; skipping", f, ex)
                continue
            for plug in cls.many_from_dict(raw.get("plugins") if isinstance(raw, dict) else None):
                if plug.name in seen:
                    log.warning("plugin '%s' in %s is already configured; the config's wins", plug.name, f.name)
                    continue
                seen.add(plug.name)
                out.append(plug)
        return out

    @classmethod
    def many_from_dict(cls, d: Optional[dict[str, Any]]) -> "list[PluginConfig]":
        """`plugins:` is a mapping of name -> {url, bearer_token..., display,
        enabled}. Lenient: an entry with no url, or a name a standard
        component already has, is skipped with a line in the log."""
        out: list[PluginConfig] = []
        if not isinstance(d, dict):
            return out
        reserved = {"memory", "loci", "corpus_callosum", "callosum", "hippocampus", "lodestar"}
        for raw_name, body in d.items():
            name = str(raw_name).strip().lower().replace("-", "_").replace(" ", "_")
            if not isinstance(body, dict) or not str(body.get("url") or "").strip():
                log.warning("plugins.%s has no url; ignoring", raw_name)
                continue
            if name in reserved:
                log.warning("plugins.%s is the name of a standard component (see components: / services:); ignoring", raw_name)
                continue
            enabled = body.get("enabled", True)
            gated = body.get("start_disabled", False)
            out.append(cls(
                name=name, url=str(body["url"]).strip(),
                display=str(body.get("display") or "") or name.replace("_", " ").title(),
                enabled=enabled.strip().lower() in ("1", "true", "yes", "on") if isinstance(enabled, str) else bool(enabled),
                start_disabled=gated.strip().lower() in ("1", "true", "yes", "on") if isinstance(gated, str) else bool(gated),
                bearer_token=str(body.get("bearer_token") or ""),
                bearer_token_env=str(body.get("bearer_token_env") or ""),
                bearer_token_keyring=str(body.get("bearer_token_keyring") or "")))
        return out


@dataclass
class BackupConfig:
    """Snapshots of what the Workbench keeps (seren_workbench.keeping): the
    tool manifests, the approved plugins, the proposals and the switches.

    restore_from / restore_reason are THE RESTORE: a snapshot folder (or its
    .tar.gz) to put back at startup, and why. Only into an EMPTY tools
    folder - one that holds anything is never overwritten, and the key is
    then passed by with a line in the log. There is no route and no tool for
    this, on purpose: it is these two keys and a restart."""
    enabled: bool = True
    dir: str = ""                 # blank = `backups` beside the tools folder
    every_hours: float = 24.0     # 0 = never on its own
    keep_daily: int = 14
    keep_weekly: int = 8
    restore_from: str = ""
    restore_reason: str = ""

    @classmethod
    def from_dict(cls, d: Optional[dict[str, Any]]) -> "BackupConfig":
        d = d if isinstance(d, dict) else {}
        out = cls()
        enabled = d.get("enabled", True)
        out.enabled = (enabled.strip().lower() in ("1", "true", "yes", "on") if isinstance(enabled, str)
                       else bool(enabled))
        out.dir = _expand(d.get("dir") or "")
        for key, cast in (("every_hours", float), ("keep_daily", int), ("keep_weekly", int)):
            if d.get(key) is None:
                continue
            try:
                setattr(out, key, max(cast(0), cast(d[key])))
            except (TypeError, ValueError):
                log.warning("unparseable backup.%s %r - using %s", key, d[key], getattr(out, key))
        out.restore_from = _expand(d.get("restore_from") or "")
        out.restore_reason = str(d.get("restore_reason") or "")
        for key in d:
            if key not in cls.__dataclass_fields__:
                log.warning("backup.%s is not a key this Workbench knows; ignoring", key)
        return out


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
    components: "ComponentsConfig" = field(default_factory=lambda: ComponentsConfig())
    plugins: "list[PluginConfig]" = field(default_factory=list)
    backup: "BackupConfig" = field(default_factory=lambda: BackupConfig())
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
    if v := env.get("SEREN_WORKBENCH_LOCI_URL"):
        cfg.services.loci_url = v
    if v := env.get("SEREN_WORKBENCH_CALLOSUM_URL"):
        cfg.services.callosum_url = v
    if v := env.get("SEREN_WORKBENCH_HIPPOCAMPUS_URL"):
        cfg.services.hippocampus_url = v
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
    components = ComponentsConfig.from_dict(data.get("components"))
    plugins = PluginConfig.many_from_dict(data.get("plugins"))
    updates = UpdatesConfig.from_dict(data.get("updates"))
    backup = BackupConfig.from_dict(data.get("backup"))

    cfg = WorkbenchConfig(server=server, tls=tls, dashboard=dashboard,
                          services=services, components=components, plugins=plugins, updates=updates,
                          backup=backup,
                          source_path=source_path)
    return _apply_env_overrides(cfg)
