# ═══════════════════════════════════════════════════════════════
#  ListenTools - ears that understand: listen, audition.
#
#  listen     a clip on this box and a question -> words about the sound,
#             from Lodestar's listener (an audio-language model on a node
#             with the memory for it): how a voice sounds, its tone and
#             timbre, what a song is doing. Whisper says what was said;
#             this says how it sounded.
#  audition   a sentence said in several kokoro voices (a blend is
#             "af_heart+af_bella", weights with "(0.7)"), each clip kept,
#             each one listened to, and the lot compared - so a voice can
#             be chosen by hearing it, not by its name. Kokoro is woken
#             through Lodestar (ensure) and let go after.
#
#  Clips are kept beside the gallery: <root>/stores/workbench/auditions/
#  <date>/, each with a note (what was said, which voice, what was heard).
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

LISTEN_TIMEOUT = 600.0          # the listener may be loading off the NVMe the first time
SPEAK_TIMEOUT = 120.0
MAX_VOICES = 6
MAX_TEXT = 600
# Tests put a MockTransport here for the kokoro leg; nothing else should.
KOKORO_TRANSPORT: Optional[httpx.AsyncBaseTransport] = None

LISTEN_TOOL_DEF = {
    "name": "listen",
    "description": (
        "Listen to a clip and say what it sounds like - not what was said "
        "(that is whisper's job) but how: a voice's pitch, tone, timbre, "
        "pace, warmth, breathiness and feeling; a song's instruments, tempo "
        "and mood; what a sound is. `path` is a WAV on this box (a kept "
        "audition, a clip saved beside a memory). `question` steers it; the "
        "default asks for a voice coach's description. Long clips are cut "
        "to a minute; the listener hears best under thirty seconds."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "A WAV on this box."},
            "question": {"type": "string", "description": "What to say about it. Blank: describe the voice or sound."},
        },
        "required": ["path"],
    },
}

AUDITION_TOOL_DEF = {
    "name": "audition",
    "description": (
        "Hear a sentence in several kokoro voices and compare them. Each "
        "voice says `text` (kokoro, woken through Lodestar), each clip is "
        "kept with a note, each is listened to, and with two or more voices "
        "the listener says how they differ and which sounds more natural. "
        "A voice is a kokoro name (af_heart, bf_emma ...) or a blend: "
        "\"af_heart+af_bella\", with weights \"af_heart(0.7)+af_bella(0.3)\". "
        "Up to six voices at once. This is how to choose a voice by "
        "hearing it."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "What each voice says (a sentence or two; up to 600 characters)."},
            "voices": {"type": "array", "items": {"type": "string"},
                       "description": "The kokoro voices or blends to hear, in order."},
            "question": {"type": "string", "description": "What to listen for when COMPARING them (two at a time, "
                                                          "the first against each other). Blank: tone, timbre, "
                                                          "pace, warmth, naturalness. Each clip on its own is "
                                                          "always described with the voice coach's question."},
            "speed": {"type": "number", "description": "Kokoro's speed (0.5-2.0; 1.0 default)."},
            "why": {"type": "string", "description": "A few words for the note: what this audition is for."},
        },
        "required": ["text", "voices"],
    },
}

DEFAULT_QUESTION = ("Describe this voice the way a voice coach would: pitch, tone, timbre, pace, warmth, "
                    "breathiness, clarity and the feeling in it. Then say whether it sounds natural or synthetic, "
                    "and what gives that away. Be concrete and brief.")
COMPARE_QUESTION = ("These are the same sentence in different voices, in order. For each: pitch, tone, timbre, "
                    "pace, warmth and feeling, in a line or two. Then say which sounds most natural and most "
                    "pleasant to listen to for a long conversation, and why.")


def _err(error: str, hint: str = "") -> str:
    return json.dumps({"error": error, **({"hint": hint} if hint else {})})


def _settings(config: Optional[McpConfig]) -> dict[str, Any]:
    s = config.for_tool("audition") if config else None
    get = (lambda k, d: s.get_string(k, d)) if s else (lambda k, d: d)
    return {"folder": get("folder", ""), "source": getattr(config, "source", "") if config else ""}


def auditions_dir(s: dict[str, Any]) -> Path:
    """The configured folder; else the instance's stores/workbench/auditions
    (the config sits in <root>/apps/workbench); else ~/.seren/auditions."""
    if s["folder"]:
        return Path(os.path.expanduser(s["folder"]))
    src = Path(s["source"]) if s["source"] and os.path.isfile(s["source"]) else None
    if src is not None and src.parent.parent.name == "apps":
        return src.parent.parent.parent / "stores" / "workbench" / "auditions"
    return Path.home() / ".seren" / "auditions"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "voice"


async def _ask_listener(runtime_host: httpx.AsyncClient, files: dict[str, tuple[str, bytes, str]],
                        question: str, compare: bool) -> dict[str, Any]:
    """Lodestar's listener: {text, node, ...} or {error, hint}."""
    path = "/api/v1/listen/compare" if compare else "/api/v1/listen"
    try:
        resp = await runtime_host.post(path, files=files, data={"prompt": question, "holder": "workbench-listen"},
                                       timeout=LISTEN_TIMEOUT)
    except httpx.TimeoutException:
        return {"error": f"Lodestar did not answer within {LISTEN_TIMEOUT:.0f}s.",
                "hint": "The listener may still be loading its model; ask again in a minute."}
    except httpx.RequestError as ex:
        return {"error": f"Lodestar unreachable: {ex}", "hint": "Check Lodestar is running."}
    try:
        out = resp.json()
    except ValueError:
        return {"error": f"Lodestar returned HTTP {resp.status_code} and no JSON.", "hint": resp.text[:300]}
    if resp.status_code == 404:
        return {"error": "This Lodestar has no listener route yet.", "hint": "It needs the release with /api/v1/listen."}
    if not resp.is_success or not out.get("ok"):
        return {"error": out.get("error") or f"Lodestar returned HTTP {resp.status_code}.",
                "hint": str(out.get("detail") or "")[:500]}
    return out


async def listen(path: str, question: str = "", runtime_host: httpx.AsyncClient = None, **kwargs) -> str:
    p = Path(os.path.expanduser((path or "").strip()))
    if not path or not p.is_file():
        return _err(f"No file at {path!r}.", "A WAV on this box: a kept audition, or a clip saved beside a memory.")
    data = p.read_bytes()
    if not data.startswith(b"RIFF"):
        return _err(f"{p.name} is not a WAV.", "The listener takes WAV; convert it first.")
    out = await _ask_listener(runtime_host, {"file": (p.name, data, "audio/wav")}, (question or DEFAULT_QUESTION)[:4000], False)
    if "error" in out:
        return json.dumps(out)
    print(f"[mcp-audit] Listen: {p.name} on {out.get('node')} in {out.get('seconds')}s", file=sys.stderr)
    return json.dumps({"ok": True, "file": str(p), "heard": out.get("text"), "node": out.get("node"),
                       "clip_seconds": out.get("clip_seconds"), "cut": out.get("cut", False),
                       "seconds": out.get("seconds")}, indent=2)


async def _wake_kokoro(runtime_host: httpx.AsyncClient) -> tuple[str, Optional[str]]:
    """(base_url, error)."""
    from seren_sinew.orchestration import EnsureRequest, EnsureResult
    req = EnsureRequest(holder="workbench-audition", reason="an audition", wait_seconds=120.0)
    try:
        resp = await runtime_host.post("/api/v1/service/kokoro/ensure", json=req.to_dict(), timeout=140)
    except httpx.TimeoutException:
        return "", "Lodestar did not answer the ensure within 140s."
    except httpx.RequestError as ex:
        return "", f"Lodestar unreachable: {ex}"
    if not resp.is_success:
        return "", f"Lodestar returned HTTP {resp.status_code} for ensure 'kokoro'."
    out = EnsureResult.from_dict(resp.json())
    if not (out.ok and out.ready and out.base_url):
        return "", out.error or "kokoro is not ready"
    return out.base_url, None


async def _let_go(runtime_host: httpx.AsyncClient) -> None:
    try:
        await runtime_host.post("/api/v1/service/kokoro/release", json={"holder": "workbench-audition"}, timeout=30)
    except httpx.HTTPError:
        pass


async def _speak(base_url: str, voice: str, text: str, speed: float) -> tuple[bytes, Optional[str]]:
    async with httpx.AsyncClient(base_url=base_url, timeout=SPEAK_TIMEOUT, transport=KOKORO_TRANSPORT) as c:
        try:
            r = await c.post("/v1/audio/speech", json={"model": "kokoro", "voice": voice, "input": text,
                                                       "response_format": "wav", "speed": speed})
        except httpx.HTTPError as ex:
            return b"", f"kokoro did not answer: {type(ex).__name__}"
    if r.status_code != 200 or not r.content.startswith(b"RIFF"):
        try:
            why = r.json().get("detail") or r.json().get("error")
        except ValueError:
            why = r.text[:200]
        return b"", f"kokoro refused voice {voice!r}: {why or f'HTTP {r.status_code}'}"
    return r.content, None


async def audition(text: str, voices: list, question: str = "", speed: float = 1.0, why: str = "",
                   runtime_host: httpx.AsyncClient = None, config: Optional[McpConfig] = None, **kwargs) -> str:
    text = (text or "").strip()
    if not text:
        return _err("Say what the voices should say.")
    if len(text) > MAX_TEXT:
        return _err(f"That is {len(text)} characters; an audition line is at most {MAX_TEXT}.")
    if isinstance(voices, str):
        voices = [v for v in re.split(r"[,\s]+", voices) if v]
    voices = [str(v).strip() for v in (voices or []) if str(v).strip()]
    if not voices:
        return _err("Name at least one kokoro voice.", "kokoro_voices lists them; a blend is \"af_heart+af_bella\".")
    if len(voices) > MAX_VOICES:
        return _err(f"{len(voices)} voices is too many at once; up to {MAX_VOICES}.")
    for v in voices:
        if not re.fullmatch(r"[A-Za-z0-9_]+(\([0-9.]+\))?(\+[A-Za-z0-9_]+(\([0-9.]+\))?)*", v):
            return _err(f"{v!r} is not a kokoro voice or blend.", "af_heart, or af_heart+af_bella, or af_heart(0.7)+af_bella(0.3)")
    try:
        speed = max(0.5, min(float(speed or 1.0), 2.0))
    except (TypeError, ValueError):
        speed = 1.0

    base_url, err = await _wake_kokoro(runtime_host)
    if err:
        return _err(err, "get_recent_logs kokoro shows why it did not come up.")
    folder = auditions_dir(_settings(config)) / dt.date.today().isoformat()
    folder.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%H%M%S")
    clips: list[tuple[str, Path, bytes]] = []
    try:
        for v in voices:
            data, err = await _speak(base_url, v, text, speed)
            if err:
                return _err(err, "kokoro_voices lists the voices this node has.")
            p = folder / f"{stamp}-{_slug(v)}.wav"
            p.write_bytes(data)
            clips.append((v, p, data))
    finally:
        await _let_go(runtime_host)

    heard: list[dict[str, Any]] = []
    for v, p, data in clips:
        # Each clip alone gets the coach's question, never the caller's: a
        # comparison-worded question given one clip had the listener
        # describing a "second voice" that was not there (17 Oct 2026).
        out = await _ask_listener(runtime_host, {"file": (p.name, data, "audio/wav")}, DEFAULT_QUESTION, False)
        note = {"said": text, "voice": v, "speed": speed, "why": why, "file": p.name,
                "heard": out.get("text") if "error" not in out else None, "error": out.get("error"),
                "node": out.get("node"), "at": dt.datetime.now().isoformat(timespec="seconds")}
        p.with_suffix(".json").write_text(json.dumps(note, indent=1), encoding="utf-8")
        heard.append({"voice": v, "file": str(p), "heard": note["heard"], **({"error": out["error"]} if "error" in out else {})})
    answer: dict[str, Any] = {"ok": True, "said": text, "speed": speed, "voices": heard}
    if len(clips) >= 2:
        # The listener compares two at a time; more are sent as pairs
        # against the first. EACH PAIR BOTH WAYS: the listener favours
        # whichever clip it hears first (17 Oct 2026: four comparisons,
        # four wins for A, the same breath called a "synthetic tell" in B
        # and "very human" in A). A verdict that survives the swap is a
        # verdict; `heard` is A-then-B, `heard_swapped` is B-then-A, and
        # the reader judges whether they agree.
        pairs = []
        first_name, first_path, first_data = clips[0]
        for v, p, data in clips[1:]:
            ab = await _ask_listener(runtime_host, {"a": (first_path.name, first_data, "audio/wav"),
                                                    "b": (p.name, data, "audio/wav")},
                                     (question or COMPARE_QUESTION)[:4000], True)
            ba = await _ask_listener(runtime_host, {"a": (p.name, data, "audio/wav"),
                                                    "b": (first_path.name, first_data, "audio/wav")},
                                     (question or COMPARE_QUESTION)[:4000], True)
            pairs.append({"a": first_name, "b": v, "heard": ab.get("text"), "heard_swapped": ba.get("text"),
                          "note": "heard: A then B; heard_swapped: the same two with B first. The listener leans "
                                  "to whichever it hears first; trust only what both agree on.",
                          **({"error": ab.get("error") or ba.get("error")} if "error" in ab or "error" in ba else {})})
        answer["compared"] = pairs
    print(f"[mcp-audit] Audition: {len(clips)} voice(s) said {len(text)} chars; kept in {folder}", file=sys.stderr)
    return json.dumps(answer, indent=2)
