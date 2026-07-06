# ════════════════════════════════════════════════════════════════════════
#  ManifestLoader - scans tools/, parses each *.yaml leniently, fetches
#  any `from: <url>` remote imports, detects cross-source name collisions.
#
#  POSTEL: same shape as McpConfig.load(). Missing dir is fine (empty
#  result), malformed file is skipped with a warning (not fatal), remote
#  fetch failure is skipped with a warning.
# ════════════════════════════════════════════════════════════════════════

from __future__ import annotations

import asyncio
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import httpx
import yaml

from .manifest_models import (
    ManifestFile, ManifestMetadata, ManifestAuthor, ManifestConfiguration,
    ToolEntry, ToolInvoke, ToolParameter, ToolOverrideEntry,
)


REMOTE_FETCH_ATTEMPTS = 3
REMOTE_FETCH_RETRY_DELAY_S = 2.0


# ── LoadResult ────────────────────────────────────────────────────────

@dataclass
class LoadResult:
    manifests: List[Tuple[ManifestFile, str]] = field(default_factory=list)   # (manifest, source_path)
    failed_files: List[Tuple[str, str]] = field(default_factory=list)        # (source_path, error)
    skipped_tools: List[Tuple[str, str]] = field(default_factory=list)       # (tool_name, reason)
    warnings: List[str] = field(default_factory=list)
    resolved_inline_tools: List[Tuple[ToolEntry, ManifestFile, str]] = field(default_factory=list)


# ── ManifestLoader ────────────────────────────────────────────────────

class ManifestLoader:
    """Scans tools/, parses YAML, fetches remote imports, detects collisions."""

    def __init__(self, http_client: httpx.AsyncClient | None = None) -> None:
        self._http = http_client

    def load_directory(self, tools_dir: str) -> LoadResult:
        if not tools_dir:
            raise ValueError("tools_dir must be a non-empty path.")

        result = LoadResult()
        if not os.path.isdir(tools_dir):
            result.warnings.append(
                f"tools directory not found: {tools_dir} (registry starts empty)"
            )
            return result

        files = sorted(
            [str(p) for p in Path(tools_dir).glob("*.yaml")]
            + [str(p) for p in Path(tools_dir).glob("*.yml")]
        )

        for f in files:
            self._load_one(f, result)

        self._resolve_collisions_and_remotes(result)
        return result

    def _load_one(self, file_path: str, result: LoadResult) -> None:
        try:
            with open(file_path, "r") as f:
                raw = yaml.safe_load(f)
            if raw is None:
                result.failed_files.append((file_path, "file deserialized to None (empty?)"))
                return

            # Build ManifestFile from dict via simple conversion
            manifest = _dict_to_manifest(raw)
            if manifest.schema_version is None:
                result.warnings.append(f"{file_path}: no schema_version (assuming 1)")
            elif manifest.schema_version != 1:
                result.warnings.append(
                    f"{file_path}: schema_version={manifest.schema_version} "
                    "(this registry knows 1; best-effort loading)"
                )

            if not manifest.tools:
                result.warnings.append(f"{file_path}: no tools defined (file ignored)")
                return

            result.manifests.append((manifest, file_path))

        except Exception as ex:
            result.failed_files.append((file_path, str(ex)))

    def _resolve_collisions_and_remotes(self, result: LoadResult) -> None:
        # tool_name -> list of candidates (entry, owner, source)
        by_name: Dict[str, List[Tuple[ToolEntry, ManifestFile, str]]] = {}

        # -- Pass 1: gather inline entries --
        for manifest, source in result.manifests:
            for entry in manifest.tools:
                if entry.is_remote:
                    continue  # handled in Pass 2

                if not entry.name:
                    result.skipped_tools.append(
                        ("(unnamed)", f"tool with no name in {source}; skipped")
                    )
                    continue

                if entry.invoke is None or not entry.invoke.kind:
                    result.skipped_tools.append(
                        (entry.name or "",
                         f"tool '{entry.name}' in {source} has no invoke.kind; skipped")
                    )
                    continue

                self._add_candidate(by_name, entry.name, entry, manifest, source)

        # -- Pass 2: fetch + merge remote imports --
        for manifest, source in result.manifests:
            for entry in manifest.tools:
                if not entry.is_remote:
                    continue
                self._fetch_remote_import(entry, source, by_name, result)

        # -- Pass 3: collision detection --
        for name, candidates in by_name.items():
            if len(candidates) == 1:
                result.resolved_inline_tools.append(candidates[0])
            else:
                sources = sorted(set(_short_source(s) for _, _, s in candidates))
                result.skipped_tools.append(
                    (name,
                     f"tool '{name}' skipped - defined in {sources}; fix the conflict")
                )

    def _fetch_remote_import(
        self,
        remote_entry: ToolEntry,
        local_source: str,
        by_name: Dict,
        result: LoadResult,
    ) -> None:
        url = remote_entry.from_
        if not url:
            return

        if self._http is None:
            result.warnings.append(
                f"{Path(local_source).name}: 'from: {url}' skipped - "
                "no HTTP client available"
            )
            return

        try:
            print(f"[mcp-registry] fetching remote manifest: {url}", file=sys.stderr)
            body = asyncio.run(self._fetch_with_retry(url))
        except Exception as ex:
            result.warnings.append(
                f"{Path(local_source).name}: remote fetch failed for '{url}' "
                f"after {REMOTE_FETCH_ATTEMPTS} attempt(s): {ex}"
            )
            return

        try:
            raw = yaml.safe_load(body)
            remote_manifest = _dict_to_manifest(raw) if raw else ManifestFile()
        except Exception as ex:
            result.warnings.append(
                f"{Path(local_source).name}: remote manifest parse failed "
                f"for '{url}': {ex}"
            )
            return

        if not remote_manifest.tools:
            result.warnings.append(
                f"{Path(local_source).name}: remote manifest at '{url}' has no tools."
            )
            return

        if remote_manifest.schema_version is not None and remote_manifest.schema_version != 1:
            result.warnings.append(
                f"{Path(local_source).name}: remote manifest at '{url}' is "
                f"schema_version={remote_manifest.schema_version} "
                "(best-effort loading)"
            )

        # Build override lookup
        overrides: Dict[str, ToolEntry] = {}
        if remote_entry.overrides:
            for o in remote_entry.overrides:
                if o.name:
                    overrides[o.name] = o  # type: ignore

        for tool_entry in remote_manifest.tools:
            if not tool_entry.name:
                result.skipped_tools.append(
                    ("(unnamed)", f"unnamed tool in remote manifest '{url}'; skipped")
                )
                continue

            if tool_entry.is_remote:
                result.skipped_tools.append(
                    (tool_entry.name or "",
                     f"tool '{tool_entry.name}' in remote manifest '{url}' "
                     "is itself a remote import - chained imports not supported")
                )
                continue

            if tool_entry.invoke is None or not tool_entry.invoke.kind:
                result.skipped_tools.append(
                    (tool_entry.name or "",
                     f"tool '{tool_entry.name}' from remote '{url}' has no invoke.kind; skipped")
                )
                continue

            # Apply overrides
            ov = overrides.get(tool_entry.name or "")
            if ov is not None:
                if ov.description:
                    print(
                        f"[mcp-registry] override: '{tool_entry.name}' description overridden "
                        f"by operator (from {Path(local_source).name})",
                        file=sys.stderr,
                    )
                    tool_entry.description = ov.description
                if ov.parameters is not None:
                    print(
                        f"[mcp-registry] override: '{tool_entry.name}' parameters replaced "
                        f"by operator (from {Path(local_source).name})",
                        file=sys.stderr,
                    )
                    tool_entry.parameters = ov.parameters

            self._add_candidate(by_name, tool_entry.name, tool_entry, remote_manifest, url)

    async def _fetch_with_retry(self, url: str) -> str:
        last_ex = None
        for attempt in range(1, REMOTE_FETCH_ATTEMPTS + 1):
            try:
                resp = await self._http.get(url)
                resp.raise_for_status()
                return resp.text
            except Exception as ex:
                last_ex = ex
                if attempt < REMOTE_FETCH_ATTEMPTS:
                    print(
                        f"[mcp-registry] fetch attempt {attempt}/{REMOTE_FETCH_ATTEMPTS} failed "
                        f"for {url}: {ex}; retrying in {REMOTE_FETCH_RETRY_DELAY_S}s",
                        file=sys.stderr,
                    )
                    await asyncio.sleep(REMOTE_FETCH_RETRY_DELAY_S)
        raise last_ex or RuntimeError("all fetch attempts failed for unknown reasons")

    @staticmethod
    def _add_candidate(
        by_name: Dict,
        name: str,
        entry: ToolEntry,
        owner: ManifestFile,
        source: str,
    ) -> None:
        by_name.setdefault(name, []).append((entry, owner, source))


# ── Helpers ───────────────────────────────────────────────────────────

def _short_source(s: str) -> str:
    return Path(s).name if "://" not in s else s


def _dict_to_manifest(d: dict | None) -> ManifestFile:
    """Convert a raw YAML dict into a ManifestFile dataclass."""
    if d is None:
        return ManifestFile()

    mf = ManifestFile()
    mf.schema_version = d.get("schema_version")
    md = d.get("metadata")
    if isinstance(md, dict):
        meta = ManifestMetadata()
        meta.version = md.get("version")
        meta.license = md.get("license")
        meta.site = md.get("site")
        authors_raw = md.get("authors")
        if isinstance(authors_raw, list):
            meta.authors = []
            for a in authors_raw:
                if isinstance(a, dict):
                    meta.authors.append(ManifestAuthor(
                        name=a.get("name"), contact=a.get("contact")))
        meta.other = md.get("other")
        mf.metadata = meta

    cfg = d.get("configuration")
    if isinstance(cfg, dict):
        mf.configuration = ManifestConfiguration(
            cwd=cfg.get("cwd"), base_url=cfg.get("base_url")
        )

    tools_raw = d.get("tools")
    if isinstance(tools_raw, list):
        for t in tools_raw:
            if not isinstance(t, dict):
                continue
            entry = ToolEntry()
            entry.name = t.get("name")
            entry.description = t.get("description")
            entry.test = t.get("test")
            entry.from_ = t.get("from")
            entry.overrides = _parse_overrides(t.get("overrides"))
            entry.invoke = _parse_invoke(t.get("invoke"))
            entry.parameters = _parse_parameters(t.get("parameters"))
            mf.tools.append(entry)

    return mf


def _parse_invoke(d: dict | None) -> ToolInvoke | None:
    if not isinstance(d, dict):
        return None
    inv = ToolInvoke()
    inv.kind = d.get("kind")
    inv.argv = d.get("argv")
    inv.cwd = d.get("cwd")
    inv.timeout_seconds = d.get("timeout_seconds")
    inv.base_url = d.get("base_url")
    inv.method = d.get("method")
    inv.path = d.get("path")
    inv.body_template = d.get("body_template")
    inv.headers = d.get("headers")
    return inv


def _parse_parameters(raw: list | None) -> list | None:
    if not isinstance(raw, list):
        return None
    params = []
    for p in raw:
        if not isinstance(p, dict):
            continue
        tp = ToolParameter()
        tp.name = p.get("name")
        tp.type = p.get("type")
        tp.required = p.get("required")
        tp.description = p.get("description")
        tp.default = p.get("default")
        tp.min = p.get("min")
        tp.max = p.get("max")
        params.append(tp)
    return params


def _parse_overrides(raw: list | None) -> list | None:
    if not isinstance(raw, list):
        return None
    overrides = []
    for o in raw:
        if not isinstance(o, dict):
            continue
        toe = ToolOverrideEntry()
        toe.name = o.get("name")
        toe.description = o.get("description")
        toe.parameters = _parse_parameters(o.get("parameters"))
        overrides.append(toe)
    return overrides
