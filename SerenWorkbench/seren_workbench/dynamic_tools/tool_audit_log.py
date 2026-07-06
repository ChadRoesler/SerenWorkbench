# ════════════════════════════════════════════════════════════════════════
#  ToolAuditLog - thread-safe ring buffer of tool invocations.
#
#  CONTENT-BLIND BY DESIGN - only metadata, never arg values or result text.
#
#  v1: only YamlDispatchedTool (dynamic / plug-and-play tools). Compile-time
#  tools don't go through our code path yet.
#
#  CAPACITY: 500 entries fixed. LinkedList for O(1) prepend + tail-trim.
# ════════════════════════════════════════════════════════════════════════

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from typing import List, Optional


ERROR_MESSAGE_MAX_CHARS = 200


@dataclass
class AuditEntry:
    """One row in the audit log. Content-blind: no arg values, no result text."""
    timestamp: float = 0.0  # unix seconds (utc)
    tool: str = ""
    kind: str = ""  # "process" | "web" | "builtin"
    source_file: Optional[str] = None
    duration_ms: int = 0
    success: bool = False
    error_message: Optional[str] = None
    arg_count: int = 0


class ToolAuditLog:
    MAX_ENTRIES = 500

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: deque[AuditEntry] = deque()

    def record(self, entry: AuditEntry) -> None:
        with self._lock:
            self._entries.appendleft(entry)
            while len(self._entries) > self.MAX_ENTRIES:
                self._entries.pop()

    def snapshot(self, limit: int = 100, tool_filter: Optional[str] = None) -> List[AuditEntry]:
        with self._lock:
            q = list(self._entries)
        if tool_filter:
            q = [e for e in q if e.tool == tool_filter]
        return q[:limit]

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._entries)
