# ═══════════════════════════════════════════════════════════════
#  SomaTools - my body: a SerenSoma robot on the desk.
#
#  soma_status    how it is: face, battery, what each light shows, its
#                 settings, whether its voice channel is open, whether it's night
#  soma_pose      show a pose; for a moment (ms), or as the one to rest in
#  soma_say       speak out loud through it
#  soma_settings  read, or set, its lights, speaker and screen
#  soma_lights    light the rails
#  soma_head      turn the head
#  soma_touches   what hands have done to it lately, and what it showed
#
#  Through seren_sinew.soma: the robot's control API and its voice server,
#  each with its own token (config `soma:`). Switched off there, these tools
#  aren't listed at all.
#
#  NO CAMERA TOOL, on purpose. A photo is taken only when the person in the
#  room says so, and a tool call is not that person.
# ═══════════════════════════════════════════════════════════════

from __future__ import annotations

import json
import re
from typing import Any, Optional

import httpx
from seren_sinew.cluster.dtos import to_dict
from seren_sinew.soma.client import SomaRobot, SomaVoice

TOOLBOX = "Body"
MAX_SAY = 1000

SOMA_STATUS_TOOL_DEF = {
    "name": "soma_status",
    "description": ("How my body is right now: the face it's wearing, battery, what each of the 12 rail LEDs "
                    "shows (LEDs 0, 1, 6, 7 are the mic, reply and camera lights), its light / speaker / screen "
                    "settings and any night cap, the pose it rests in, whether its voice channel is open, and "
                    "whether it's night."),
    "input_schema": {"type": "object", "properties": {}},
}
SOMA_POSE_TOOL_DEF = {
    "name": "soma_pose",
    "description": ("Show a pose on my face: any emotion the face has (happy, loving, smug, thinking...). "
                    "`ms` > 0 shows it for that long, then goes back. `rest` makes it the pose I go back to "
                    "after talking or a call, until the next pose without it (to sleep: sleepy with rest)."),
    "input_schema": {"type": "object", "properties": {
        "name": {"type": "string", "description": "The pose."},
        "ms": {"type": "integer", "description": "Show it this long, then go back. 0: until the next pose."},
        "rest": {"type": "boolean", "description": "Make it the pose to rest in."},
    }, "required": ["name"]},
}
SOMA_SAY_TOOL_DEF = {
    "name": "soma_say",
    "description": ("Say something out loud through my body's speaker, in my voice, with a pose while I say it. "
                    "If the channel is closed this opens it to say it (deaf: it doesn't listen unless the person "
                    "talked to me in the last few minutes). Up to 1000 characters."),
    "input_schema": {"type": "object", "properties": {
        "text": {"type": "string", "description": "What to say."},
        "emotion": {"type": "string", "description": "A pose to wear while saying it. Blank: as it is."},
    }, "required": ["text"]},
}
SOMA_SETTINGS_TOOL_DEF = {
    "name": "soma_settings",
    "description": ("Read my body's light, speaker and screen settings, or set any of them (saved on it). "
                    "lights 0-100 scales every LED, but the mic, reply and camera lights never go out; "
                    "screen is 5-100. Night caps these and is the person's to set, in Psycheion."),
    "input_schema": {"type": "object", "properties": {
        "lights": {"type": "integer", "description": "0-100."},
        "speaker": {"type": "integer", "description": "0-100."},
        "screen": {"type": "integer", "description": "5-100."},
    }},
}
SOMA_LIGHTS_TOOL_DEF = {
    "name": "soma_lights",
    "description": ("Light the rails: `color` (#rrggbb) for every LED, or `leds`, up to 12 #rrggbb from LED 0 "
                    "(0-5 one rail, 6-11 the other). The mic, reply and camera lights show over whatever is set. "
                    "A pose with its own colour sets the rails again when it comes up."),
    "input_schema": {"type": "object", "properties": {
        "color": {"type": "string", "description": "#rrggbb for all of them, #000000 for off."},
        "leds": {"type": "array", "items": {"type": "string"}, "description": "#rrggbb per LED from 0."},
    }},
}
SOMA_HEAD_TOOL_DEF = {
    "name": "soma_head",
    "description": "Turn my head. yaw -128 (to its right) to 128, pitch 0 (level) to 90 (up), speed 100-1000.",
    "input_schema": {"type": "object", "properties": {
        "yaw": {"type": "integer"}, "pitch": {"type": "integer"},
        "speed": {"type": "integer", "description": "100-1000, default 150."},
    }},
}
SOMA_TOUCHES_TOOL_DEF = {
    "name": "soma_touches",
    "description": ("What hands have done to my body lately, newest last: each touch (a stroke and which way, a "
                    "poke and where, scritches, a resting hand, a screen spot), how long, and the face it brought "
                    "up; and how close a scratch is to tipping me over. `after`: only those after that number."),
    "input_schema": {"type": "object", "properties": {"after": {"type": "integer"}}},
}


def _err(error: str, hint: str = "") -> str:
    return json.dumps({"error": error, **({"hint": hint} if hint else {})})


def _off(robot: Any = None, voice: Any = None) -> Optional[str]:
    if robot is None and voice is None:
        return _err("This Workbench has no body configured.", "Set `soma:` in seren-workbench.yaml.")
    return None


def _why(what: str, ex: Exception) -> str:
    if isinstance(ex, httpx.HTTPStatusError):
        code = ex.response.status_code
        if code == 401:
            return _err(f"My {what} refused the token.", f"Check soma.{'robot' if what == 'body' else 'voice'}_token in the config.")
        if code == 409:
            return _err(f"My {what} can't do that right now: {ex.response.text[:160]}")
        return _err(f"My {what} answered {code}: {ex.response.text[:160]}")
    if isinstance(ex, httpx.TimeoutException):
        return _err(f"My {what} didn't answer in time.", "It may be rebooting; try again in a minute.")
    return _err(f"Can't reach my {what}.", "It may be off, asleep, or off the network.")


def _hex(c: str) -> Optional[tuple[int, int, int]]:
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", (c or "").strip())
    if not m:
        return None
    v = bytes.fromhex(m.group(1))
    # the LEDs top out at 168: scale, so a colour keeps its hue
    return tuple(round(x * 168 / 255) for x in v)  # type: ignore[return-value]


async def soma_status(soma_robot: SomaRobot = None, soma_voice: SomaVoice = None, **kwargs) -> str:
    if (off := _off(soma_robot, soma_voice)):
        return off
    out: dict[str, Any] = {}
    try:
        s = await soma_robot.status()
        out["body"] = {"face": s.face, "battery": s.batteryLevel, "charging": s.charging,
                       "up_minutes": round(s.uptimeMs / 60000, 1), "resting_in": s.resting or None,
                       "leds": [f"#{s.leds[i * 6:i * 6 + 6]}" for i in range(len(s.leds) // 6)],
                       "settings": to_dict(s.settings) if s.settings else None}
    except (httpx.HTTPError, ValueError) as ex:
        out["body_error"] = json.loads(_why("body", ex))["error"]
    try:
        v = await soma_voice.status()
        st = await soma_voice.settings()
        out["voice"] = {"channel_open": v.device, "speaking": v.speaking, "night_now": st.night_now}
    except (httpx.HTTPError, ValueError) as ex:
        out["voice_error"] = json.loads(_why("voice server", ex))["error"]
    if "body" not in out and "voice" not in out:
        return _err(f"Can't reach my body or its voice server ({out.get('body_error')}; {out.get('voice_error')}).")
    return json.dumps(out, indent=2)


async def soma_pose(name: str, ms: int = 0, rest: bool = False, soma_robot: SomaRobot = None, **kwargs) -> str:
    if (off := _off(soma_robot)):
        return off
    name = (name or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", name):
        return _err(f"{name!r} isn't a pose name.")
    try:
        if ms and ms > 0:
            await soma_robot.react(name, max(200, min(15000, int(ms))))
        else:
            await soma_robot.emotion(name, rest=bool(rest))
    except (httpx.HTTPError, ValueError) as ex:
        return _why("body", ex)
    return json.dumps({"ok": True, "pose": name, **({"ms": ms} if ms else {}), **({"resting": True} if rest else {})})


async def soma_say(text: str, emotion: str = "", soma_voice: SomaVoice = None, **kwargs) -> str:
    if (off := _off(voice=soma_voice)):
        return off
    text = (text or "").strip()
    if not text:
        return _err("Say what to say.")
    if len(text) > MAX_SAY:
        return _err(f"That's {len(text)} characters; at most {MAX_SAY} in one go.")
    if emotion and not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", emotion):
        return _err(f"{emotion!r} isn't a pose name.")
    try:
        seconds = await soma_voice.say(text, emotion=emotion or None, wake=True)
    except (httpx.HTTPError, ValueError) as ex:
        return _why("voice server", ex)
    return json.dumps({"ok": True, "seconds": seconds})


async def soma_settings(lights: Optional[int] = None, speaker: Optional[int] = None, screen: Optional[int] = None,
                        soma_robot: SomaRobot = None, **kwargs) -> str:
    if (off := _off(soma_robot)):
        return off
    for label, v, low in (("lights", lights, 0), ("speaker", speaker, 0), ("screen", screen, 5)):
        if v is not None and not low <= int(v) <= 100:
            return _err(f"{label} is {low} to 100.")
    try:
        s = await soma_robot.settings(dimmer=lights, volume=speaker, brightness=screen)
    except (httpx.HTTPError, ValueError) as ex:
        return _why("body", ex)
    return json.dumps({"lights": s.dimmer, "speaker": s.volume, "screen": s.brightness,
                       "night_cap": to_dict(s.night) if s.night else None,
                       "now": {"lights": s.applied.dimmer, "speaker": s.applied.volume, "screen": s.applied.brightness}},
                      indent=2)


async def soma_lights(color: str = "", leds: Optional[list[str]] = None, soma_robot: SomaRobot = None, **kwargs) -> str:
    if (off := _off(soma_robot)):
        return off
    if leds:
        frame = [_hex(c) for c in leds[:12]]
        bad = [c for c, f in zip(leds, frame) if f is None]
        if bad:
            return _err(f"Not colours like #ff4fa0: {bad}")
    elif color:
        one = _hex(color)
        if one is None:
            return _err(f"{color!r} isn't a colour like #ff4fa0.")
        frame = [one] * 12
    else:
        return _err("Give `color` or `leds`.")
    try:
        await soma_robot.leds(frame)  # type: ignore[arg-type]
    except (httpx.HTTPError, ValueError) as ex:
        return _why("body", ex)
    return json.dumps({"ok": True, "leds": len(frame)})


async def soma_head(yaw: Optional[int] = None, pitch: Optional[int] = None, speed: int = 150,
                    soma_robot: SomaRobot = None, **kwargs) -> str:
    if (off := _off(soma_robot)):
        return off
    if yaw is None and pitch is None:
        return _err("Give yaw, pitch, or both.")
    if yaw is not None and not -128 <= int(yaw) <= 128:
        return _err("yaw is -128 to 128.")
    if pitch is not None and not 0 <= int(pitch) <= 90:
        return _err("pitch is 0 to 90.")
    try:
        await soma_robot.head(yaw=yaw, pitch=pitch, speed=max(100, min(1000, int(speed or 150))))
    except (httpx.HTTPError, ValueError) as ex:
        return _why("body", ex)
    return json.dumps({"ok": True, **({"yaw": yaw} if yaw is not None else {}), **({"pitch": pitch} if pitch is not None else {})})


async def soma_touches(after: int = 0, soma_voice: SomaVoice = None, **kwargs) -> str:
    if (off := _off(voice=soma_voice)):
        return off
    try:
        f = await soma_voice.touch(after=max(0, int(after or 0)))
    except (httpx.HTTPError, ValueError) as ex:
        return _why("voice server", ex)
    touches = []
    for e in f.events[-50:]:
        what = {"stroke": f"stroke {(e.way or '').replace('_', ' ')}", "poke": f"poke {e.where}",
                "touch_spot": f"screen: {e.spot}"}.get(e.kind, e.kind.replace("_", " "))
        touches.append({"n": e.seq, "what": what, "ms": e.ms, "showed": e.showed,
                        **({"scratching": True} if e.scratching else {})})
    hand = f.hand
    held = round((hand.to_ms - hand.from_ms) / 1000, 1) if hand.down and hand.from_ms is not None else None
    return json.dumps({"touches": touches, "last": f.last,
                       "hand": {"down": True, "seconds": held} if hand.down else {"down": False}}, indent=2)
