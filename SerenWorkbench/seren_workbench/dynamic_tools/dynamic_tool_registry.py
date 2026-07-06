# ════════════════════════════════════════════════════════════════════════
#  DynamicToolRegistry - state tracker for plug-and-play YAML tools.
#
#  ARCHITECTURAL NOTE (the v1 reality)
#
#  In v1, the actual live tool surface is FIXED at app startup. This
#  registry holds the LoadResult captured at startup and provides
#  RescanDiskAsync() that re-reads tools/ and updates the "current disk
#  state" snapshot. /reload calls this - it does NOT change the live
#  tool surface; that needs a restart.
# ════════════════════════════════════════════════════════════════════════

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from .manifest_loader import LoadResult, ManifestLoader


@dataclass
class LoadedToolInfo:
    name: str = ""
    source: str = ""


@dataclass
class SkipInfo:
    name: str = ""
    reason: str = ""


@dataclass
class FileFailureInfo:
    file: str = ""
    error: str = ""


@dataclass
class RegistrySnapshot:
    tools_dir: str = ""
    live: List[LoadedToolInfo] = field(default_factory=list)
    on_disk: List[LoadedToolInfo] = field(default_factory=list)
    skipped: List[SkipInfo] = field(default_factory=list)
    failed_files: List[FileFailureInfo] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    restart_pending: bool = False
    would_add_on_restart: List[str] = field(default_factory=list)
    would_remove_on_restart: List[str] = field(default_factory=list)


class DynamicToolRegistry:
    def __init__(self, tools_dir: str, initial_load: LoadResult) -> None:
        self._tools_dir = tools_dir
        self._loader = ManifestLoader()
        self._initial_load = initial_load
        self._current_disk_state = initial_load
        self._lock = asyncio.Lock()
        self._log_startup_summary(initial_load)

    async def rescan_disk_async(self) -> RegistrySnapshot:
        async with self._lock:
            self._current_disk_state = self._loader.load_directory(self._tools_dir)
            print(
                f"[mcp-registry] disk rescan: "
                f"parsed={len(self._current_disk_state.resolved_inline_tools)} "
                f"skipped={len(self._current_disk_state.skipped_tools)} "
                f"failed_files={len(self._current_disk_state.failed_files)}",
                file=sys.stderr,
            )
            return self._build_snapshot()

    def current_snapshot(self) -> RegistrySnapshot:
        return self._build_snapshot()

    # -- Helpers --

    def _log_startup_summary(self, result: LoadResult) -> None:
        print(
            f"[mcp-registry] startup: loaded={len(result.resolved_inline_tools)} "
            f"from {self._tools_dir} "
            f"(skipped={len(result.skipped_tools)}, "
            f"failed_files={len(result.failed_files)})",
            file=sys.stderr,
        )
        for name, reason in result.skipped_tools:
            print(f"[mcp-registry]   skipped: {reason}", file=sys.stderr)
        for path, err in result.failed_files:
            print(f"[mcp-registry]   failed:  {Path(path).name}: {err}", file=sys.stderr)
        for warning in result.warnings:
            print(f"[mcp-registry]   warning: {warning}", file=sys.stderr)

    def _build_snapshot(self) -> RegistrySnapshot:
        live_names = set(
            t.name or "(unnamed)"
            for t, _, _ in self._initial_load.resolved_inline_tools
        )
        disk_names = set(
            t.name or "(unnamed)"
            for t, _, _ in self._current_disk_state.resolved_inline_tools
        )

        would_add = sorted(disk_names - live_names)
        would_remove = sorted(live_names - disk_names)

        def to_info(t: tuple) -> LoadedToolInfo:
            entry, _, source = t
            return LoadedToolInfo(
                name=entry.name or "(unnamed)",
                source=Path(source).name,
            )

        return RegistrySnapshot(
            tools_dir=self._tools_dir,
            live=sorted(
                [to_info(t) for t in self._initial_load.resolved_inline_tools],
                key=lambda x: x.name,
            ),
            on_disk=sorted(
                [to_info(t) for t in self._current_disk_state.resolved_inline_tools],
                key=lambda x: x.name,
            ),
            skipped=[
                SkipInfo(name=n, reason=r)
                for n, r in self._current_disk_state.skipped_tools
            ],
            failed_files=[
                FileFailureInfo(file=Path(p).name, error=e)
                for p, e in self._current_disk_state.failed_files
            ],
            warnings=list(self._current_disk_state.warnings),
            restart_pending=bool(would_add or would_remove),
            would_add_on_restart=would_add,
            would_remove_on_restart=would_remove,
        )
