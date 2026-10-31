# ════════════════════════════════════════════════════════════════════════
#  ManifestModels - dataclasses for the plug-and-play tool manifest YAML.
#
#  Why these have Optional everywhere:
#    Lenient parse. Missing fields land as None and the loader/dispatcher
#    decides what's actually required for THIS tool to be usable.
# ════════════════════════════════════════════════════════════════════════

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class ManifestFile:
    """Top-level shape of one *.yaml file in tools/."""
    schema_version: Optional[int] = None
    metadata: Optional["ManifestMetadata"] = None
    configuration: Optional["ManifestConfiguration"] = None
    tools: List["ToolEntry"] = field(default_factory=list)


@dataclass
class ManifestMetadata:
    version: Optional[str] = None
    license: Optional[str] = None
    authors: Optional[List["ManifestAuthor"]] = None
    site: Optional[str] = None
    other: Optional[Dict[str, object]] = None
    # Dashboard grouping for every tool in this file. Purely presentational
    # — it changes nothing about how a tool runs. Omit it and the file name
    # is used, so dropping in `hotdog-math.yaml` gets you a "Hotdog Math"
    # box without learning a new key.
    toolbox: Optional[str] = None


@dataclass
class ManifestAuthor:
    name: Optional[str] = None
    contact: Optional[str] = None


# A local_config name is a file stem, never a path: letters, digits, dot,
# dash, underscore, not starting with a dot. Anything with a separator in it
# is refused at dispatch so a manifest cannot name a file outside the dir.
LOCAL_CONFIG_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")

# The keys a <tool_config_dir>/<name>.yaml may set.
LOCAL_CONFIG_KEYS = ("base_url", "bearer_token", "bearer_token_env", "bearer_token_keyring")


@dataclass
class ManifestConfiguration:
    """Tool-set-wide defaults; tools can override per-invoke.

    The three bearer pointers are the family's usual trio (inline literal /
    env-var NAME / keyring ref, see seren_meninges.credentials). When set,
    every kind=web tool in the file that does not carry its own
    Authorization header sends `Bearer <resolved>` - resolved at call time,
    so the secret is never stored in the parsed manifest. This is how a
    tool reaches a service that wants a token WITHOUT the token being a
    parameter the model has to hold and hand over on every call.

    local_config is the fourth way, and the one for a tool that is reviewed
    in one place and deployed in another: a NAME, resolved at call time to
    <tool_config_dir>/<name>.yaml on the box the Workbench runs on. That
    file may set base_url and any of the three bearer pointers, and what it
    sets wins over the manifest. The manifest then carries no address and
    no pointer to this box's environment, and the operator changes either
    by editing a file, with no restart and no reviewer involved.
    """
    cwd: Optional[str] = None
    base_url: Optional[str] = None
    bearer_token: Optional[str] = None
    bearer_token_env: Optional[str] = None
    bearer_token_keyring: Optional[str] = None
    local_config: Optional[str] = None

    @property
    def has_bearer(self) -> bool:
        return bool(self.bearer_token or self.bearer_token_env or self.bearer_token_keyring)

    def credential_label(self) -> str:
        """Which pointer is set, for a reviewer. Never the value."""
        if self.local_config:
            return f"local:{self.local_config}" + (" (and a pointer in the manifest)" if self.has_bearer else "")
        if self.bearer_token:
            return "inline literal"
        if self.bearer_token_keyring:
            return f"keyring:{self.bearer_token_keyring}"
        if self.bearer_token_env:
            return f"env:{self.bearer_token_env}"
        return ""


@dataclass
class ToolEntry:
    """One tool entry. Either INLINE or REMOTE-IMPORT (from:)."""
    # -- Inline fields --
    name: Optional[str] = None
    description: Optional[str] = None
    test: Optional[str] = None
    invoke: Optional["ToolInvoke"] = None
    parameters: Optional[List["ToolParameter"]] = None

    # -- Presentation only; neither affects dispatch --
    # `name` stays the identifier the model calls. display_name is what a
    # person reads on the dashboard (derived Title Case if absent), and
    # toolbox overrides the file's metadata.toolbox for this one tool.
    display_name: Optional[str] = None
    toolbox: Optional[str] = None

    # -- Remote-import fields --
    from_: Optional[str] = None  # YAML key: from
    overrides: Optional[List["ToolOverrideEntry"]] = None
    # Credential for the imported tools, set on the LOCAL stub that points
    # at the remote manifest (the remote file is never trusted to name a
    # secret on this box). Same trio as ManifestConfiguration; applied to
    # every web tool the import yields.
    bearer_token: Optional[str] = None
    bearer_token_env: Optional[str] = None
    bearer_token_keyring: Optional[str] = None

    @property
    def is_remote(self) -> bool:
        return bool(self.from_)


@dataclass
class ToolInvoke:
    """HOW the tool actually runs: kind=process | web."""
    kind: Optional[str] = None

    # -- kind=process fields --
    argv: Optional[List[str]] = None
    cwd: Optional[str] = None
    timeout_seconds: Optional[int] = None

    # -- kind=web fields --
    base_url: Optional[str] = None
    method: Optional[str] = None
    path: Optional[str] = None
    body_template: Optional[str] = None
    headers: Optional[Dict[str, str]] = None


@dataclass
class ToolParameter:
    """One parameter the tool takes.

    CONSTRAINTS, and why strings needed their own:
      min/max are NUMERIC bounds — they say nothing about a string. But
      strings are exactly what lands in an argv slot or a URL path, so for
      a long time the only parameter type that could reach a subprocess was
      the only type with no constraint surface at all. `pattern` and `enum`
      close that.

      Both are also EMITTED INTO THE JSON SCHEMA. A constraint the model
      can't see is a constraint it violates blind and then gets scolded for;
      publishing them turns a rejection into a contract.
    """
    name: Optional[str] = None
    type: Optional[str] = None           # "string" | "integer" | "number" | "boolean"
    required: Optional[bool] = None
    description: Optional[str] = None
    default: Optional[object] = None
    min: Optional[float] = None
    max: Optional[float] = None

    # -- String constraints --
    pattern: Optional[str] = None        # full-match regex (re.fullmatch)
    enum: Optional[List[object]] = None  # closed set of permitted values

    # -- Argument-injection opt-in (kind=process only) --
    #
    # argv is a list and we never touch a shell, so CWE-78 is structurally
    # closed. What is NOT closed by that is CWE-88: a VALUE that becomes a
    # FLAG. `argv: [curl, "{url}"]` with url="-o/home/you/.ssh/authorized_keys"
    # is a file write, and every part of it behaved exactly as designed.
    #
    # So a leading "-" in a process argv substitution is refused by default.
    # Set this when the parameter is genuinely meant to carry flags — and
    # prefer putting a literal "--" in argv ahead of the slot instead.
    allow_leading_dash: Optional[bool] = None


@dataclass
class ToolOverrideEntry:
    """One entry in the overrides list for a remote-import."""
    name: Optional[str] = None
    description: Optional[str] = None
    parameters: Optional[List[ToolParameter]] = None
