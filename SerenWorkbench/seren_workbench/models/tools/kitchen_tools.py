# ═══════════════════════════════════════════════════════════════
#  KitchenTools - the kitchen: the board the house and I leave each
#  other notes on (SerenKitchen). A fridge per app, sheets on the doors.
#
#  kitchen_wall    the wall at a glance: every fridge, what is live on it,
#                  who holds it, what is on its door, and what waits on a pick
#  kitchen_fridge  one fridge opened: door, shelf, crisper, leftovers
#  kitchen_sheet   one sheet in full, with what is stuck on it
#  kitchen_leave   leave a sheet on a fridge
#  kitchen_reply   stick a note on a sheet (under one of its lines, or not)
#  kitchen_tick    tick or untick lines, pick a choice, say a sheet is done
#  kitchen_stick   put my magnet on a fridge (I hold that repo now), or take it off
#
#  Everything these tools leave is signed with one name (config `kitchen.author`,
#  or whatever the kitchen's house calls its partner), so a tool call can never
#  sign as the person. Switched off in the config, they aren't listed at all.
#
#  The kitchen never reads a partner's own stores and neither do these tools:
#  what is on the board is what someone chose to put there.
# ═══════════════════════════════════════════════════════════════

from __future__ import annotations

import json
import re
from typing import Any, Callable, Optional

import httpx

TOOLBOX = "Kitchen"

KINDS = ("now", "list", "person", "self", "other", "art", "decision")
FRIDGE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
SHEET_ID = re.compile(r"[0-9a-f]{12}")
MAX_TITLE = 120
MAX_BODY = 4000
MAX_LINES = 40

KITCHEN_WALL_TOOL_DEF = {
    "name": "kitchen_wall",
    "description": ("The kitchen wall at a glance: whose house it is, every fridge (one per app) with its release and "
                    "build, who holds it, how much sits on it and the notes on its door; then every sheet anywhere "
                    "still waiting on a pick, and which of those have gone sour. Start here when I want to know what "
                    "the house has left me, or where I left things."),
    "input_schema": {"type": "object", "properties": {}},
}
KITCHEN_FRIDGE_TOOL_DEF = {
    "name": "kitchen_fridge",
    "description": ("One fridge opened: the sheets on its door, on its shelf (lists), in its crisper (pictures) "
                    "and how many are in its leftovers (finished), plus who holds it. Sheets come with their "
                    "lines, choices and picks; long notes are cut short, kitchen_sheet has the whole thing."),
    "input_schema": {"type": "object", "properties": {
        "fridge": {"type": "string", "description": "The fridge, by its slug (seren-memory, seren-project...)."},
        "leftovers": {"type": "boolean", "description": "Also list the finished sheets. Default false."},
    }, "required": ["fridge"]},
}
KITCHEN_SHEET_TOOL_DEF = {
    "name": "kitchen_sheet",
    "description": "One sheet in full: title, note, every line with its choices and pick, pictures, and the notes stuck on it.",
    "input_schema": {"type": "object", "properties": {
        "sheet": {"type": "string", "description": "The sheet's id (twelve hex characters)."},
    }, "required": ["sheet"]},
}
KITCHEN_LEAVE_TOOL_DEF = {
    "name": "kitchen_leave",
    "description": ("Leave a sheet on a fridge, signed as me. A title in my hand, a note, and an optional list of "
                    "lines. A line that ends `? a | b` is a choice for someone to pick. Kind says where it sits and "
                    "who it is for: `now` (honey-do, the door), `list` (the shelf), `person` (a note to the person "
                    "of the house), `self` (a note to myself), `other` (a note to another partner), `decision`."),
    "input_schema": {"type": "object", "properties": {
        "fridge": {"type": "string", "description": "The fridge to leave it on."},
        "title": {"type": "string", "description": "The title, up to 120 letters."},
        "body": {"type": "string", "description": "The note under the title."},
        "kind": {"type": "string", "enum": list(KINDS), "description": "Default `person`: a note to the person of the house."},
        "lines": {"type": "array", "items": {"type": "string"}, "description": "Lines to tick; `text ? a | b` makes a choice."},
    }, "required": ["fridge", "title"]},
}
KITCHEN_REPLY_TOOL_DEF = {
    "name": "kitchen_reply",
    "description": ("Stick a note on a sheet, signed as me: an answer to the whole sheet, or, with `line`, to one of "
                    "its lines (0-based). It lives where the sheet lives and reads under it."),
    "input_schema": {"type": "object", "properties": {
        "sheet": {"type": "string", "description": "The sheet's id."},
        "body": {"type": "string", "description": "What I'm saying."},
        "line": {"type": "integer", "description": "The line it answers, if one."},
        "title": {"type": "string", "description": "Optional; blank takes the first line of the note."},
    }, "required": ["sheet", "body"]},
}
KITCHEN_TICK_TOOL_DEF = {
    "name": "kitchen_tick",
    "description": ("Change a sheet's state: tick or untick lines (0-based), pick one of a line's choices, or say the "
                    "whole sheet is done (it goes to the leftovers) or not done (back where it sat). Picking is not "
                    "finishing: a sheet with choices stays where it is after the pick until someone says done."),
    "input_schema": {"type": "object", "properties": {
        "sheet": {"type": "string", "description": "The sheet's id."},
        "tick": {"type": "array", "items": {"type": "integer"}, "description": "Lines to tick."},
        "untick": {"type": "array", "items": {"type": "integer"}, "description": "Lines to untick."},
        "pick_line": {"type": "integer", "description": "The line whose choice I'm picking (with `pick`)."},
        "pick": {"type": "string", "description": "The choice, exactly as the line offers it."},
        "done": {"type": "boolean", "description": "true: finished, to the leftovers; false: back out of them."},
    }, "required": ["sheet"]},
}
KITCHEN_STICK_TOOL_DEF = {
    "name": "kitchen_stick",
    "description": ("Put my magnet on a fridge: I hold that repo now, and the wall says so until I move it. No fridge: "
                    "take it off the wall onto the counter. A session that only talks needs no magnet."),
    "input_schema": {"type": "object", "properties": {
        "fridge": {"type": "string", "description": "The fridge, or blank for the counter."},
    }},
}


class Kitchen:
    """The kitchen's HTTP door, with the token asked for on every call (a token rotated under a running
    Workbench is picked up without a restart) and my name worked out once."""

    def __init__(self, url: str, token: Callable[[], str] | str = "", *, author: str = "", timeout: float = 15.0,
                 transport: Optional[httpx.AsyncBaseTransport] = None):
        self.url = (url or "").rstrip("/")
        self._token = token if callable(token) else (lambda: token)
        self._author = (author or "").strip()
        self._client = httpx.AsyncClient(base_url=self.url, timeout=timeout, transport=transport)

    async def aclose(self) -> None:
        await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        t = self._token() or ""
        return {"Authorization": f"Bearer {t}"} if t else {}

    async def call(self, method: str, path: str, *, json: Any = None, params: Optional[dict[str, Any]] = None) -> Any:
        r = await self._client.request(method, path, json=json, params=params, headers=self._headers())
        r.raise_for_status()
        return r.json()

    async def author(self) -> str:
        """The name my sheets are signed with: the config's, or what the house calls its partner."""
        if not self._author:
            wall = await self.call("GET", "/api/kitchen")
            self._author = str(((wall.get("house") or {}).get("partner") or "")).strip() or "the partner"
        return self._author


def _err(error: str, hint: str = "") -> str:
    return json.dumps({"error": error, **({"hint": hint} if hint else {})})


def _off(kitchen: Any) -> Optional[str]:
    if kitchen is None:
        return _err("This Workbench has no kitchen configured.", "Set `kitchen:` in seren-workbench.yaml.")
    return None


def _why(ex: Exception) -> str:
    if isinstance(ex, httpx.HTTPStatusError):
        code = ex.response.status_code
        if code == 401:
            return _err("The kitchen refused the token.", "Check kitchen.bearer_token_file (or _env) in the config.")
        try:
            detail = ex.response.json().get("detail")
        except ValueError:
            detail = None
        if code in (400, 404) and detail:
            return _err(str(detail))                      # the store says why, in plain words
        return _err(f"The kitchen answered {code}: {ex.response.text[:160]}")
    if isinstance(ex, httpx.TimeoutException):
        return _err("The kitchen didn't answer in time.")
    return _err("Can't reach the kitchen.", "It may be down, or the url in the config is wrong.")


def _fridge_ok(name: str) -> Optional[str]:
    if not FRIDGE.fullmatch((name or "").strip()):
        return _err(f"{name!r} isn't a fridge slug (lowercase letters, digits and dashes).")
    return None


def _sheet_ok(sheet: str) -> Optional[str]:
    if not SHEET_ID.fullmatch((sheet or "").strip()):
        return _err(f"{sheet!r} isn't a sheet id (twelve hex characters).")
    return None


def _line(ln: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"text": ln.get("text", ""), "done": bool(ln.get("done"))}
    if ln.get("options"):
        out["choices"] = list(ln["options"])
        out["pick"] = ln.get("pick")
    return out


def _sheet(s: dict[str, Any], *, short: bool = False) -> dict[str, Any]:
    """A sheet as words: no coordinates, no pin colour, the note cut short on a glance."""
    body = str(s.get("body") or "")
    out: dict[str, Any] = {"id": s.get("id"), "kind": s.get("kind"), "title": s.get("title"), "by": s.get("author") or "",
                           "made": (s.get("made") or "")[:16].replace("T", " ")}
    if body:
        out["note"] = (body[:200] + "...") if short and len(body) > 200 else body
    lines = [_line(ln) for ln in (s.get("lines") or [])]
    if lines:
        out["lines"] = lines
        out["left"] = sum(1 for ln in lines if not ln["done"])
    if s.get("options"):
        out["choices"] = list(s["options"]); out["pick"] = s.get("pick")
    if s.get("art"):
        out["pictures"] = len(s["art"])
    if s.get("waiting"):
        out["waiting"] = True
    if s.get("sour"):
        out["sour"] = True
    if s.get("done_at"):
        out["done"] = s["done_at"][:16].replace("T", " ")
    if s.get("reply_line") is not None:
        out["on_line"] = s["reply_line"]
    replies = s.get("replies") or []
    if replies:
        out["stuck_on_it"] = [_sheet(r, short=short) for r in replies] if not short else len(replies)
    return out


async def kitchen_wall(kitchen: Kitchen = None, **kwargs) -> str:
    if (off := _off(kitchen)):
        return off
    try:
        wall = await kitchen.call("GET", "/api/kitchen")
        waiting = await kitchen.call("GET", "/api/sheets", params={"waiting": "true"})
    except (httpx.HTTPError, ValueError) as ex:
        return _why(ex)
    doors: dict[str, list[dict[str, Any]]] = {}
    for s in wall.get("doors") or []:
        doors.setdefault(s.get("fridge", ""), []).append(s)
    fridges = []
    for f in wall.get("fridges") or []:
        facts = f.get("facts") or {}
        counts = f.get("counts") or {}
        row: dict[str, Any] = {"fridge": f.get("name"), "called": f.get("display") or f.get("name")}
        if facts.get("release"):
            row["release"] = facts["release"]
        if facts.get("build") and facts["build"] != "none":
            row["build"] = facts["build"]
        if facts.get("freezer_error"):
            row["freezer"] = facts["freezer_error"]
        if f.get("held_by"):
            row["held_by"] = f["held_by"]
        open_sheets = sum(v for k, v in counts.items() if k != "leftovers")
        if open_sheets:
            row["sheets"] = open_sheets
        if counts.get("leftovers"):
            row["leftovers"] = counts["leftovers"]
        if f.get("sour"):
            row["sour"] = f["sour"]
        door = doors.get(f.get("name"), [])
        if door:
            row["door"] = [{"id": s.get("id"), "title": s.get("title"), "by": s.get("author") or "", "kind": s.get("kind"),
                            **({"waiting": True} if s.get("waiting") else {})} for s in door]
        fridges.append(row)
    house = wall.get("house") or {}
    out = {"house": {"person": house.get("person") or "the person", "partner": house.get("partner") or "the partner"},
           "signed_as": await kitchen.author(),
           "fridges": fridges,
           "waiting_on_a_pick": [{"id": s.get("id"), "fridge": s.get("fridge"), "title": s.get("title"), "by": s.get("author") or "",
                                  **({"sour": True} if s.get("sour") else {})} for s in waiting]}
    return json.dumps(out, indent=1, ensure_ascii=False)


async def kitchen_fridge(fridge: str, leftovers: bool = False, kitchen: Kitchen = None, **kwargs) -> str:
    if (off := _off(kitchen)):
        return off
    fridge = (fridge or "").strip()
    if (bad := _fridge_ok(fridge)):
        return bad
    try:
        f = await kitchen.call("GET", f"/api/fridges/{fridge}")
    except (httpx.HTTPError, ValueError) as ex:
        return _why(ex)
    facts = f.get("facts") or {}
    out: dict[str, Any] = {"fridge": f.get("name"), "called": f.get("display") or f.get("name"),
                           **({"release": facts["release"]} if facts.get("release") else {}),
                           **({"build": facts["build"]} if facts.get("build") and facts["build"] != "none" else {}),
                           "held_by": [m.get("partner") for m in f.get("magnets") or []],
                           "door": [_sheet(s, short=True) for s in f.get("door") or []],
                           "shelf": [_sheet(s, short=True) for s in f.get("shelf") or []],
                           "crisper": [_sheet(s, short=True) for s in f.get("crisper") or []]}
    left = f.get("leftovers") or []
    out["leftovers"] = [_sheet(s, short=True) for s in left] if leftovers else len(left)
    return json.dumps(out, indent=1, ensure_ascii=False)


async def kitchen_sheet(sheet: str, kitchen: Kitchen = None, **kwargs) -> str:
    if (off := _off(kitchen)):
        return off
    sheet = (sheet or "").strip()
    if (bad := _sheet_ok(sheet)):
        return bad
    try:
        s = await kitchen.call("GET", f"/api/sheets/{sheet}")
    except (httpx.HTTPError, ValueError) as ex:
        return _why(ex)
    return json.dumps({"fridge": s.get("fridge"), "place": s.get("place"), **_sheet(s)}, indent=1, ensure_ascii=False)


async def kitchen_leave(fridge: str, title: str, body: str = "", kind: str = "person", lines: Optional[list[str]] = None,
                        kitchen: Kitchen = None, **kwargs) -> str:
    if (off := _off(kitchen)):
        return off
    fridge = (fridge or "").strip()
    if (bad := _fridge_ok(fridge)):
        return bad
    title = (title or "").strip()
    if not title or len(title) > MAX_TITLE:
        return _err(f"A sheet needs a title, up to {MAX_TITLE} letters.")
    kind = (kind or "person").strip().lower()
    if kind not in KINDS:
        return _err(f"A sheet is one of: {', '.join(KINDS)}; not {kind!r}.")
    body = (body or "").strip()
    if len(body) > MAX_BODY:
        return _err(f"That note is {len(body)} characters; at most {MAX_BODY}.")
    lines = [str(ln).strip() for ln in (lines or []) if str(ln).strip()]
    if len(lines) > MAX_LINES:
        return _err(f"That's {len(lines)} lines; at most {MAX_LINES} on one sheet.")
    try:
        s = await kitchen.call("POST", f"/api/fridges/{fridge}/sheets",
                               json={"kind": kind, "title": title, "body": body, "lines": lines, "author": await kitchen.author()})
    except (httpx.HTTPError, ValueError) as ex:
        return _why(ex)
    return json.dumps({"ok": True, "sheet": s.get("id"), "fridge": s.get("fridge"), "place": s.get("place"), "signed": s.get("author")})


async def kitchen_reply(sheet: str, body: str, line: Optional[int] = None, title: str = "",
                        kitchen: Kitchen = None, **kwargs) -> str:
    if (off := _off(kitchen)):
        return off
    sheet = (sheet or "").strip()
    if (bad := _sheet_ok(sheet)):
        return bad
    body = (body or "").strip()
    if not body:
        return _err("Say what to stick on it.")
    if len(body) > MAX_BODY:
        return _err(f"That note is {len(body)} characters; at most {MAX_BODY}.")
    title = (title or "").strip()[:MAX_TITLE] or body.splitlines()[0][:MAX_TITLE]
    payload: dict[str, Any] = {"kind": "other", "title": title, "body": body, "author": await kitchen.author()}
    if line is not None:
        payload["line"] = int(line)
    try:
        s = await kitchen.call("POST", f"/api/sheets/{sheet}/reply", json=payload)
    except (httpx.HTTPError, ValueError) as ex:
        return _why(ex)
    return json.dumps({"ok": True, "sheet": s.get("id"), "on": sheet, **({"line": line} if line is not None else {}),
                       "signed": s.get("author")})


async def kitchen_tick(sheet: str, tick: Optional[list[int]] = None, untick: Optional[list[int]] = None,
                       pick_line: Optional[int] = None, pick: str = "", done: Optional[bool] = None,
                       kitchen: Kitchen = None, **kwargs) -> str:
    if (off := _off(kitchen)):
        return off
    sheet = (sheet or "").strip()
    if (bad := _sheet_ok(sheet)):
        return bad
    change: dict[str, Any] = {}
    if tick:
        change["tick"] = [int(i) for i in tick]
    if untick:
        change["untick"] = [int(i) for i in untick]
    if pick or pick_line is not None:
        if not pick or pick_line is None:
            return _err("A pick takes both `pick_line` (which line) and `pick` (which choice).")
        change["pick_line"] = {"line": int(pick_line), "option": pick}
    if done is not None:
        change["done"] = bool(done)
    if not change:
        return _err("Give something to change: tick, untick, a pick, or done.")
    try:
        s = await kitchen.call("PATCH", f"/api/sheets/{sheet}", json=change)
    except (httpx.HTTPError, ValueError) as ex:
        return _why(ex)
    lines = [_line(ln) for ln in (s.get("lines") or [])]
    return json.dumps({"ok": True, "sheet": s.get("id"), "place": s.get("place"), "left": sum(1 for ln in lines if not ln["done"]),
                       **({"done": s["done_at"][:16].replace("T", " ")} if s.get("done_at") else {}),
                       "lines": lines}, indent=1, ensure_ascii=False)


async def kitchen_stick(fridge: str = "", kitchen: Kitchen = None, **kwargs) -> str:
    if (off := _off(kitchen)):
        return off
    fridge = (fridge or "").strip()
    if fridge and (bad := _fridge_ok(fridge)):
        return bad
    try:
        me = await kitchen.author()
        await kitchen.call("POST", "/api/magnets", json={"partner": me})       # makes it the first time, touches nothing after
        m = await kitchen.call("POST", f"/api/magnets/{me}/stick", json={"fridge": fridge or None})
    except (httpx.HTTPError, ValueError) as ex:
        return _why(ex)
    return json.dumps({"ok": True, "magnet": m.get("partner"), "on": m.get("fridge") or "the counter",
                       **({"was_on": m["last"]} if m.get("last") else {})})
