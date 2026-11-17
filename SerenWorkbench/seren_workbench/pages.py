"""
The pages at the door, as the Workbench holds them (SerenCarabiners PITON.md).

WHAT THIS IS. A dedication and a preface live in a piton: plain files on the
box a harness runs on, read at every session start. The model writes them
from wherever it is - and the Workbench is usually not on that box. So the
Workbench never writes a piton. It KEEPS the newest version it was handed,
numbered, and a carabiner's bookmark hook asks for it at the next session
start: GET /pages/<page>?after=<the number it last pulled>. A dedication is
written for the next session anyway; arriving at the next session start
costs nothing.

WHY A NUMBER AND NOT A TIME. `after` compares the Workbench's own count, so
a box whose clock runs fast never skips a version that really is newer. The
time is carried for people, and for the one comparison that is made on a
single box: a hand edit on that box against a pulled version.

WHO MAY WRITE WHICH PAGE is not decided here. The piton on each box holds
that (`self | operator | off`) and refuses a pull it should not take. This
only checks what can be checked without the box: not empty, not over the
default cap.

WRITE-THROUGH. While Margin still offers set_dedication / set_preface
(`pages: margin`, its default), a write here is also handed to Margin, so a
box on an older carabiner - one with no piton, reading the bookmark from
Margin - sees it too. When Margin stops offering them (`pages: piton`), the
write-through stops by itself. Nothing has to be flipped in this Workbench.

THE FILE. One JSON file, beside the switches in the tools folder, so a
Workbench backup carries it. A file that will not read is never written over:
the writes are refused with the reason, and the history in it is still there
for a person to look at.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

log = logging.getLogger(__name__)

PAGES_FILE_NAME = ".pages.json"
PAGES = ("dedication", "preface")
# The piton's defaults (SerenCarabiners kbh/piton.py), which are Margin's.
# A box with its own caps checks again when it pulls.
CAPS = {"dedication": 2000, "preface": 8000}
SCHEMA = 1


class PageRefused(ValueError):
    """A write that was not kept: empty, over the cap, or the file is unreadable."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class PageStore:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self.problem: Optional[str] = None

    # -- the file --
    def _load(self) -> dict[str, list[dict[str, Any]]]:
        self.problem = None
        if not self.path.exists():
            return {p: [] for p in PAGES}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("not a mapping")
            out = {}
            for p in PAGES:
                versions = raw.get(p) or []
                if not isinstance(versions, list):
                    raise ValueError(f"{p} is not a list")
                out[p] = [v for v in versions if isinstance(v, dict) and isinstance(v.get("seq"), int)]
            return out
        except Exception as ex:  # noqa: BLE001
            self.problem = (f"{self.path} could not be read ({ex}); nothing will be written over it. "
                            "Fix or move it and the pages start again from what it holds.")
            log.warning("[pages] %s", self.problem)
            return {p: [] for p in PAGES}

    def _save(self, data: dict[str, list[dict[str, Any]]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps({"schema": SCHEMA, **data}, ensure_ascii=False, indent=1)
        fd, tmp = tempfile.mkstemp(prefix=".pages-", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(body)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # -- reads --
    def history(self, page: str, n: int = 0) -> list[dict[str, Any]]:
        """Newest first; n=0 is just the newest (or nothing)."""
        _check(page)
        with self._lock:
            versions = self._load()[page]
        versions = sorted(versions, key=lambda v: v["seq"], reverse=True)
        return versions[: max(1, n + 1)] if versions else []

    def newest(self, page: str) -> Optional[dict[str, Any]]:
        h = self.history(page)
        return h[0] if h else None

    def after(self, page: str, seq: int) -> Optional[dict[str, Any]]:
        """The newest version if it is past *seq* (what a box last pulled), else None."""
        v = self.newest(page)
        return v if v is not None and v["seq"] > seq else None

    # -- the one write --
    def set(self, page: str, text: str, why: Optional[str] = None, by: str = "self") -> tuple[dict[str, Any], bool]:
        """Keep *text* as the newest version. Returns (version, created):
        the same text as now is not a new version."""
        _check(page)
        text = (text or "").strip()
        if not text:
            raise PageRefused(f"the {page} is empty; a page that says nothing is not kept")
        if len(text) > CAPS[page]:
            raise PageRefused(f"the {page} is {len(text)} characters; the cap is {CAPS[page]}")
        with self._lock:
            data = self._load()
            if self.problem:
                raise PageRefused(self.problem)
            versions = data[page]
            top = max(versions, key=lambda v: v["seq"]) if versions else None
            if top is not None and top.get("text") == text:
                return top, False
            v = {"seq": (top["seq"] + 1) if top else 1, "text": text,
                 "why": (why or "").strip() or None, "set_at": _now(), "by": by}
            versions.append(v)
            self._save(data)
            return v, True

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for p in PAGES:
            v = self.newest(p)
            out[p] = None if v is None else {"seq": v["seq"], "set_at": v["set_at"], "by": v["by"],
                                             "chars": len(v["text"]), "cap": CAPS[p]}
        if self.problem:
            out["problem"] = self.problem
        return out


def _check(page: str) -> None:
    if page not in PAGES:
        raise KeyError(f"there is no page called {page!r}; the pages are {', '.join(PAGES)}")


class Pages:
    """What the page tools are handed: the store, and a way to reach Margin's
    own page tools while it still offers them (seren_workbench.upstream holds
    them back from the surface; see UpstreamHub's `claimed`)."""

    def __init__(self, store: PageStore,
                 offers: Optional[Callable[[str], bool]] = None,
                 forward: Optional[Callable[[str, dict], Awaitable[Any]]] = None) -> None:
        self.store = store
        self.offers = offers or (lambda name: False)
        self.forward = forward
