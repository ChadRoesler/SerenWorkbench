"""
Gallery routes - GET /gallery, /gallery/{day}, /gallery/{day}/{name}.

What make_image kept, served at full size. The gallery is a folder of days
(tools.make_image.gallery, else the instance's stores/workbench/gallery), each
day a folder of pictures with a .json note beside each (what was asked, why,
the model, the seed). A picture over MCP is capped at what a model API will
take; this is where the whole file is, for a Symposium chat to show it at
full size, for Lodestar to hand it to another box, for a store on that box
to keep it (seren_sinew.blobs: a keepsake, an eidetic picture, a figure).

Behind the Workbench's bearer like everything else. A name is served only
from inside the gallery: a day is YYYY-MM-DD and a name has no path in it.
15 Oct 2026.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

router = APIRouter(prefix="/gallery", tags=["gallery"])

_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_PICTURE = (".png", ".jpg", ".jpeg", ".webp", ".gif")
_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
         ".gif": "image/gif", ".json": "application/json"}


def _gallery(request: Request) -> Path:
    from ..models.tools.make_image_tool import _settings, gallery_dir
    return gallery_dir(_settings(getattr(request.app.state, "mcp_config", None)))


def _note(p: Path) -> dict:
    side = p.with_suffix(".json")
    try:
        note = json.loads(side.read_text(encoding="utf-8")) if side.is_file() else {}
    except (OSError, ValueError):
        note = {}
    return {k: note.get(k) for k in ("asked", "why", "drew", "model", "base", "loras", "seed", "width", "height",
                                     "node", "seconds", "at") if k in note}


def _day_dir(request: Request, day: str) -> Path:
    if not _DAY.match(day):
        raise HTTPException(400, "a day is YYYY-MM-DD")
    d = _gallery(request) / day
    if not d.is_dir():
        raise HTTPException(404, f"no pictures on {day}")
    return d


@router.get("")
async def days(request: Request):
    """The days that have pictures, newest first, with how many."""
    g = _gallery(request)
    out = []
    if g.is_dir():
        for d in sorted((p for p in g.iterdir() if p.is_dir() and _DAY.match(p.name)), reverse=True):
            n = sum(1 for p in d.iterdir() if p.is_file() and p.suffix.lower() in _PICTURE)
            if n:
                out.append({"day": d.name, "pictures": n})
    return {"gallery": str(g), "days": out, "count": sum(d["pictures"] for d in out)}


@router.get("/{day}")
async def day(request: Request, day: str):
    """One day's pictures, newest first, each with its note."""
    d = _day_dir(request, day)
    pics = sorted((p for p in d.iterdir() if p.is_file() and p.suffix.lower() in _PICTURE), reverse=True)
    return {"day": day, "count": len(pics),
            "pictures": [{"file": p.name, "bytes": p.stat().st_size, "mime": _MIME.get(p.suffix.lower()),
                          "note": _note(p)} for p in pics]}


@router.get("/{day}/{name}")
async def picture(request: Request, day: str, name: str):
    """The file itself - a picture at full size, or its .json note - served
    as what it is and nothing more (nosniff, a sandbox CSP)."""
    d = _day_dir(request, day)
    if not _NAME.match(name) or ".." in name:
        raise HTTPException(400, "a name has no path in it")
    p = d / name
    if not p.is_file() or p.suffix.lower() not in _MIME:
        raise HTTPException(404, f"no {name} on {day}")
    if p.suffix.lower() == ".json":
        try:
            return JSONResponse(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            raise HTTPException(404, f"the note beside {name} cannot be read")
    return FileResponse(str(p), media_type=_MIME[p.suffix.lower()], headers={
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "sandbox; default-src 'none'; img-src data:",
        "Content-Disposition": f'inline; filename="{p.name}"',
        "Cache-Control": "private, max-age=86400"})
