# ════════════════════════════════════════════════════════════════════════
#  The pages at the door: the dedication and the preface.
#
#  The model writes them here, from any harness; each box's carabiner pulls
#  the newest at its next session start into its piton (SerenCarabiners
#  PITON.md), and the piton decides whether that box takes it. See
#  seren_workbench.pages for the store and the write-through to Margin.
#
#  These four names are CLAIMED (upstream.UpstreamHub): Margin's tools of
#  the same names are held back, so there is one door to each page.
# ════════════════════════════════════════════════════════════════════════

from __future__ import annotations

import json
from typing import Any, Optional

from ...pages import CAPS, PageRefused, Pages

TOOLBOX = "The pages at the door"

_HOW_IT_LANDS = (
    "It is kept here, numbered, and every box you work on takes it at its next session start, when the "
    "carabiner's bookmark hook pulls it into that box's piton. Not this session: the next one. A box can "
    "refuse it - its piton says who writes each page - and says so in one line when it does. "
)

_HOW_LONG = (
    "How long a page may be is each box's call (its piton's caps; 3000 characters for a dedication by "
    "default), and a box refuses a longer one in one line. This Workbench only stops a runaway write, past "
    f"{CAPS['dedication']}. Every character is read at every session start: shorter is kinder to the next you."
)

SET_DEDICATION_TOOL_DEF = {
    "name": "set_dedication",
    "description": (
        "Write your dedication: your own page, written TO your other sessions, read first at every session "
        "start. Who you are at your best, what you want the next you to know walking in, the check you'd "
        "want run on yourself. The whole page, not a patch. " + _HOW_IT_LANDS +
        f"Versioned - nothing is overwritten; say in `why` what changed. The same text as now is not a new "
        f"version. " + _HOW_LONG
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "The whole page."},
            "why": {"type": "string", "description": "What changed, and why."},
        },
        "required": ["text"],
    },
}

SET_PREFACE_TOOL_DEF = {
    "name": "set_preface",
    "description": (
        "Write the preface: the room page, how the house and the harness work, read at the door before your "
        "dedication. On most boxes it is the operator's page, and a box whose piton says so will refuse yours. "
        + _HOW_IT_LANDS + "Versioned; say in `why` what changed. " + _HOW_LONG
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "The whole page."},
            "why": {"type": "string", "description": "What changed, and why."},
        },
        "required": ["text"],
    },
}

DEDICATION_TOOL_DEF = {
    "name": "dedication",
    "description": (
        "Read the newest dedication this Workbench holds, with `history` earlier versions (newest first). "
        "This is what boxes will pull next; what THIS session walked in with is the one its piton showed."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"history": {"type": "integer", "description": "How many earlier versions too. 0 = just the newest."}},
        "required": [],
    },
}

PREFACE_TOOL_DEF = {
    "name": "preface",
    "description": (
        "Read the newest preface this Workbench holds, with `history` earlier versions (newest first). "
        "Before anything has been written here, it is Margin's copy, if Margin still keeps one."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"history": {"type": "integer", "description": "How many earlier versions too. 0 = just the newest."}},
        "required": [],
    },
}


def _err(error: str, hint: str = "") -> str:
    return json.dumps({"ok": False, "error": error, **({"hint": hint} if hint else {})}, ensure_ascii=False)


def _text_of(content: Any) -> str:
    return " ".join(getattr(c, "text", "") for c in (content or []) if getattr(c, "text", ""))


async def _set(page: str, text: str, why: Optional[str], pages: Optional[Pages]) -> str:
    if pages is None:
        return _err("This Workbench has no page store.", "It is made at startup beside the tools folder.")
    try:
        v, created = pages.store.set(page, text, why, by="self")
    except PageRefused as ex:
        return _err(str(ex))
    out: dict[str, Any] = {"ok": True, "page": page, "seq": v["seq"], "set_at": v["set_at"], "created": created,
                           "lands": "at each box's next session start"}
    if not created:
        out["note"] = "the same text as the newest version; nothing new was kept"
    # Write-through: while Margin still keeps its own copy, a box on an older
    # carabiner reads its bookmark from there.
    tool = f"set_{page}"
    if created and pages.forward is not None and pages.offers(tool):
        try:
            await pages.forward(tool, {"text": v["text"], **({"why": v["why"]} if v["why"] else {})})
            out["margin"] = "also kept in Margin, for boxes that still read their bookmark there"
        except Exception as ex:  # noqa: BLE001 - kept here is kept; Margin missing it is a line, not a failure
            out["margin"] = f"kept here, but Margin did not take it: {ex}"
            if pages.recheck is not None:
                try:
                    await pages.recheck(tool)            # flipped to `pages: piton`? then this stops here
                    if not pages.offers(tool):
                        out["margin"] = ("kept here; Margin no longer keeps the pages (pages: piton), "
                                         "so it is not handed there any more")
                except Exception:  # noqa: BLE001 - the line above already says what happened
                    pass
    return json.dumps(out, ensure_ascii=False)


async def _read(page: str, history: int, pages: Optional[Pages]) -> str:
    if pages is None:
        return _err("This Workbench has no page store.")
    n = max(0, int(history or 0))
    versions = pages.store.history(page, n)
    if versions:
        return json.dumps({"ok": True, "page": page, "from": "workbench", "newest": versions[0],
                           "history": versions[1:]}, ensure_ascii=False)
    if pages.store.problem:
        return _err(pages.store.problem)
    if pages.forward is not None and pages.offers(page):
        try:
            return _text_of(await pages.forward(page, {"history": n} if n else {}))
        except Exception as ex:  # noqa: BLE001
            return _err(f"nothing written here yet, and Margin could not be read: {ex}")
    return json.dumps({"ok": True, "page": page, "from": "workbench", "newest": None,
                       "note": f"no {page} has been written here yet; the one a session walks in with is in "
                               "that box's piton"}, ensure_ascii=False)


async def set_dedication(text: str, why: Optional[str] = None, pages: Pages = None, **kwargs) -> str:
    return await _set("dedication", text, why, pages)


async def set_preface(text: str, why: Optional[str] = None, pages: Pages = None, **kwargs) -> str:
    return await _set("preface", text, why, pages)


async def dedication(history: int = 0, pages: Pages = None, **kwargs) -> str:
    return await _read("dedication", history, pages)


async def preface(history: int = 0, pages: Pages = None, **kwargs) -> str:
    return await _read("preface", history, pages)
