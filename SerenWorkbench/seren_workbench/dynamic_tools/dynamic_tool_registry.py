# ════════════════════════════════════════════════════════════════════════
#  DynamicToolRegistry - re-reads tools/ and applies the difference to the
#  LIVE MCP surface, without restarting the service.
#
#  HISTORY, because the shape of this file is a lesson. It used to be a
#  139-line orphan: nothing constructed it, no route reached it, and its
#  own docstring described a "/reload" endpoint that did not exist. Anyone
#  reading this directory to learn the reload semantics learned a story
#  about the system that wasn't true. The reconciliation logic was correct
#  and simply never called.
#
#  WHAT A RELOAD MAY AND MAY NOT TOUCH
#
#  It may add, replace and remove DYNAMIC tools. It may not touch a builtin,
#  ever. FastMCP keeps the FIRST registration of a name, so at startup a
#  manifest could never shadow a builtin - registration order protected
#  them for free. remove_tool() dissolves that protection, so the guard that
#  used to be implicit has to be written down here: builtin names are
#  refused, and the refusal is reported rather than silently dropped.
#
#  THE LOAD RUNS OFF-LOOP. load_directory() walks the disk and may fetch
#  remote `from:` manifests - three attempts, two seconds apart, ten second
#  timeout each. At startup, blocking on that is fine and documented. At
#  runtime it would freeze every in-flight request for up to half a minute,
#  so the reload hands it to a thread.
# ════════════════════════════════════════════════════════════════════════

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

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
    skipped: List[SkipInfo] = field(default_factory=list)
    failed_files: List[FileFailureInfo] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    # What the last reload actually changed on the live surface.
    added: List[str] = field(default_factory=list)
    removed: List[str] = field(default_factory=list)
    replaced: List[str] = field(default_factory=list)


def _fingerprint(entry) -> str:
    """Stable hash-ish of a ToolEntry, so an EDITED tool re-registers.

    Name-only diffing would leave a tool whose argv changed still running
    the old argv - the file on disk and the live surface would disagree
    while the reload reported success.
    """
    try:
        return json.dumps(asdict(entry), sort_keys=True, default=str)
    except Exception:
        return repr(entry)


class DynamicToolRegistry:
    """Owns the live-vs-disk reconciliation for YAML manifest tools."""

    def __init__(
        self,
        tools_dir: str,
        initial_load: LoadResult,
        tool_registry,
        mcp_server=None,
        register: Optional[Callable] = None,
    ) -> None:
        self._tools_dir = tools_dir
        self._loader = ManifestLoader()
        self._current = initial_load
        self._registry = tool_registry
        self._mcp = mcp_server
        # Injected so this module never imports the mcp layer: the caller
        # passes a callable(mcp, tool_info) that performs one registration.
        self._register = register
        self._lock = asyncio.Lock()
        self._fingerprints: Dict[str, str] = {
            (e.name or ""): _fingerprint(e)
            for e, _, _ in initial_load.resolved_inline_tools
            if e.name
        }
        self._last_change: Dict[str, List[str]] = {
            "added": [], "removed": [], "replaced": []
        }
        self._log_startup_summary(initial_load)

    # -- The live reload --

    async def reload(self) -> RegistrySnapshot:
        """Re-read tools/ and apply the difference to the live MCP surface."""
        async with self._lock:
            # A MISSING directory is not the same statement as an EMPTY one.
            # load_directory() treats both as "no tools", which at startup is
            # correct and harmless. Here it would mean an unmounted volume or
            # a mistyped path silently deletes every dynamic tool from a
            # running server. Refuse, keep what's live, and say why.
            if not os.path.isdir(self._tools_dir):
                snap = self._build_snapshot()
                snap.warnings = list(snap.warnings) + [
                    f"tools directory {self._tools_dir} does not exist; reload "
                    "refused and the live tool surface left untouched. Create "
                    "the directory (an EMPTY one does mean remove-everything)."
                ]
                snap.added = snap.removed = snap.replaced = []
                print(
                    f"[mcp-registry] reload refused: {self._tools_dir} is missing",
                    file=sys.stderr,
                )
                return snap

            result = await asyncio.to_thread(
                self._loader.load_directory, self._tools_dir
            )

            builtins = self._registry.builtin_names()
            kept, shadowed = [], []
            for triple in result.resolved_inline_tools:
                entry = triple[0]
                if (entry.name or "") in builtins:
                    shadowed.append((
                        entry.name or "",
                        f"tool '{entry.name}' shadows a builtin tool of the same "
                        "name and was not loaded; rename it in the manifest",
                    ))
                else:
                    kept.append(triple)
            result.resolved_inline_tools = kept
            result.skipped_tools.extend(shadowed)

            from ..tool_registry import tool_info_from_load_result
            new_infos = tool_info_from_load_result(result)

            new_fps = {
                (e.name or ""): _fingerprint(e)
                for e, _, _ in result.resolved_inline_tools if e.name
            }
            old_fps = self._fingerprints

            added = sorted(set(new_fps) - set(old_fps))
            removed = sorted(set(old_fps) - set(new_fps))
            replaced = sorted(
                n for n in set(new_fps) & set(old_fps)
                if new_fps[n] != old_fps[n]
            )

            # Registry first: the call-time enable gate reads from it, so it
            # must know about a tool before that tool can be reached.
            self._registry.replace_dynamic(new_infos)
            self._apply_to_mcp(new_infos, added, removed, replaced, builtins)

            self._current = result
            self._fingerprints = new_fps
            self._last_change = {
                "added": added, "removed": removed, "replaced": replaced
            }

            print(
                f"[mcp-registry] reload: +{len(added)} -{len(removed)} "
                f"~{len(replaced)} (skipped={len(result.skipped_tools)}, "
                f"failed_files={len(result.failed_files)})",
                file=sys.stderr,
            )
            return self._build_snapshot()

    def _apply_to_mcp(self, new_infos, added, removed, replaced, builtins) -> None:
        """Mutate the FastMCP tool table. No-op when there's no server."""
        if self._mcp is None or self._register is None:
            return

        by_name = {t.name: t for t in new_infos}

        for name in removed + replaced:
            if name in builtins:        # belt AND braces - see header
                continue
            try:
                self._mcp.remove_tool(name)
            except Exception as ex:
                print(f"[mcp-registry] remove '{name}' failed: {ex}", file=sys.stderr)

        for name in added + replaced:
            info = by_name.get(name)
            if info is None:
                continue
            try:
                self._register(self._mcp, info)
            except Exception as ex:
                print(f"[mcp-registry] register '{name}' failed: {ex}", file=sys.stderr)

    # -- Read-only views --

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
        for _name, reason in result.skipped_tools:
            print(f"[mcp-registry]   skipped: {reason}", file=sys.stderr)
        for path, err in result.failed_files:
            print(f"[mcp-registry]   failed:  {Path(path).name}: {err}", file=sys.stderr)
        for warning in result.warnings:
            print(f"[mcp-registry]   warning: {warning}", file=sys.stderr)

    def _build_snapshot(self) -> RegistrySnapshot:
        return RegistrySnapshot(
            tools_dir=self._tools_dir,
            live=sorted(
                [
                    LoadedToolInfo(name=e.name or "(unnamed)", source=Path(s).name)
                    for e, _, s in self._current.resolved_inline_tools
                ],
                key=lambda x: x.name,
            ),
            skipped=[SkipInfo(name=n, reason=r)
                     for n, r in self._current.skipped_tools],
            failed_files=[FileFailureInfo(file=Path(p).name, error=e)
                          for p, e in self._current.failed_files],
            warnings=list(self._current.warnings),
            added=list(self._last_change["added"]),
            removed=list(self._last_change["removed"]),
            replaced=list(self._last_change["replaced"]),
        )
