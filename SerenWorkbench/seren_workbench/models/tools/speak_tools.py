# ═══════════════════════════════════════════════════════════════
#  SpeakTools - my voice: say.
#
#  say        words -> a WAV in my voice, from Lodestar's speaker (a
#             reference clip and its transcript on a node, cloned by a
#             text-to-speech backend for every sentence). The clip is kept
#             on this box with a note (what was said, which voice, how
#             long it took) and the path comes back, so Symposium can play
#             it, a memory can keep it, or the listener can hear it back.
#             The speaker is woken through Lodestar (ensure) and let go.
#
#  Kokoro's borrowed voices stay behind `audition`; this is the chosen one.
#
#  Clips are kept beside the auditions: <root>/stores/workbench/said/
#  <date>/, each with a note.
# ═══════════════════════════════════════════════════════════════

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional

import httpx

from ...tool_config.mcp_config import McpConfig

TOOLBOX = "Senses"

SPEAK_TIMEOUT = 400.0           # the speaker may be loading off the NVMe the first time
MAX_TEXT = 4000
DEFAULT_VOICE = "wren"

SAY_TOOL_DEF = {
    "name": "say",
    "description": (
        "Say something in my own voice. The words go to Lodestar's speaker "
        "(the chosen voice on a node, cloned for every sentence) and the "
        "WAV comes back and is kept on this box; the path is returned so it "
        "can be played, kept with a memory, or listened to. `voice` is a "
        "name the speaker has (default \"wren\"). Up to 4000 characters; "
        "long text is said in sentence-sized pieces. When the speaker's "
        "node is asleep the first call wakes it, which can take a minute."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "What to say."},
            "voice": {"type": "string", "description": "A voice the speaker has. Blank: wren."},
            "name": {"type": "string", "description": "A short name for the clip's file. Blank: from the words."},
            "why": {"type": "string", "description": "A note to keep beside the clip."},
        },
        "required": ["text"],
    },
}


def _err(error: str, hint: str = "") -> str:
    return json.dumps({"error": error, **({"hint": hint} if hint else {})})


def _settings(config: Optional[McpConfig]) -> dict[str, Any]:
    s = config.for_tool("say") if config else None
    get = (lambda k, d: s.get_string(k, d)) if s else (lambda k, d: d)
    return {"folder": get("folder", ""), "source": getattr(config, "source", "") if config else ""}


def said_dir(s: dict[str, Any]) -> Path:
    """The configured folder; else the instance's stores/workbench/said (the
    config sits in <root>/apps/workbench); else ~/.seren/said."""
    if s["folder"]:
        return Path(os.path.expanduser(s["folder"]))
    src = Path(s["source"]) if s["source"] and os.path.isfile(s["source"]) else None
    if src is not None and src.parent.parent.name == "apps":
        return src.parent.parent.parent / "stores" / "workbench" / "said"
    return Path.home() / ".seren" / "said"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "said"


async def say(text: str, voice: str = "", name: str = "", why: str = "",
              runtime_host: httpx.AsyncClient = None, config: Optional[McpConfig] = None, **kwargs) -> str:
    text = (text or "").strip()
    if not text:
        return _err("Say what to say.")
    if len(text) > MAX_TEXT:
        return _err(f"That is {len(text)} characters; at most {MAX_TEXT} in one go.")
    voice = (voice or DEFAULT_VOICE).strip()[:64]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", voice):
        return _err(f"{voice!r} is not a voice name.")
    try:
        r = await runtime_host.post("/api/v1/speak", json={"text": text, "voice": voice, "holder": "workbench-say"},
                                    timeout=SPEAK_TIMEOUT)
    except httpx.TimeoutException:
        return _err(f"Lodestar did not answer within {SPEAK_TIMEOUT:.0f}s.",
                    "The speaker may still be waking; ask again in a minute.")
    except httpx.RequestError as ex:
        return _err(f"Lodestar unreachable: {ex}")
    if r.status_code == 404:
        return _err("This Lodestar has no speaker route yet.", "It needs the release with /api/v1/speak.")
    if r.status_code != 200 or not r.content.startswith(b"RIFF"):
        try:
            body = r.json()
        except ValueError:
            body = {"error": r.text[:300]}
        hint = "audition says it in a kokoro voice meanwhile." if r.status_code == 503 else ""
        return _err(body.get("error") or f"Lodestar returned HTTP {r.status_code}.", hint or body.get("detail") or "")

    folder = said_dir(_settings(config)) / dt.date.today().isoformat()
    folder.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%H%M%S")
    p = folder / f"{stamp}-{_slug(name or text)}.wav"
    p.write_bytes(r.content)
    h = r.headers
    note = {"said": text, "voice": h.get("x-seren-voice", voice), "backend": h.get("x-seren-backend"),
            "model": h.get("x-seren-model"), "node": h.get("x-seren-node"), "why": why, "file": p.name,
            "seconds": _num(h.get("x-seren-seconds")), "clip_seconds": _num(h.get("x-seren-clip-seconds")),
            "at": dt.datetime.now().isoformat(timespec="seconds")}
    p.with_suffix(".json").write_text(json.dumps(note, indent=1), encoding="utf-8")
    print(f"[mcp-audit] Say: {len(text)} chars in {note['voice']} on {note['node']}; kept {p}", file=sys.stderr)
    return json.dumps({"ok": True, "file": str(p), "voice": note["voice"], "node": note["node"],
                       "clip_seconds": note["clip_seconds"], "seconds": note["seconds"], "backend": note["backend"]},
                      indent=2)


def _num(v: Optional[str]) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None
