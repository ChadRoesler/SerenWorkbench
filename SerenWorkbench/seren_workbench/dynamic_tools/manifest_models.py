# ════════════════════════════════════════════════════════════════════════
#  ManifestModels - dataclasses for the plug-and-play tool manifest YAML.
#
#  Why these have Optional everywhere:
#    Lenient parse. Missing fields land as None and the loader/dispatcher
#    decides what's actually required for THIS tool to be usable.
# ════════════════════════════════════════════════════════════════════════

from __future__ import annotations

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


@dataclass
class ManifestAuthor:
    name: Optional[str] = None
    contact: Optional[str] = None


@dataclass
class ManifestConfiguration:
    """Tool-set-wide defaults; tools can override per-invoke."""
    cwd: Optional[str] = None
    base_url: Optional[str] = None


@dataclass
class ToolEntry:
    """One tool entry. Either INLINE or REMOTE-IMPORT (from:)."""
    # -- Inline fields --
    name: Optional[str] = None
    description: Optional[str] = None
    test: Optional[str] = None
    invoke: Optional["ToolInvoke"] = None
    parameters: Optional[List["ToolParameter"]] = None

    # -- Remote-import fields --
    from_: Optional[str] = None  # YAML key: from
    overrides: Optional[List["ToolOverrideEntry"]] = None

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
    """One parameter the tool takes."""
    name: Optional[str] = None
    type: Optional[str] = None           # "string" | "integer" | "number" | "boolean"
    required: Optional[bool] = None
    description: Optional[str] = None
    default: Optional[object] = None
    min: Optional[float] = None
    max: Optional[float] = None


@dataclass
class ToolOverrideEntry:
    """One entry in the overrides list for a remote-import."""
    name: Optional[str] = None
    description: Optional[str] = None
    parameters: Optional[List[ToolParameter]] = None
