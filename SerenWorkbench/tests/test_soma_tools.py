"""The body's builtins (soma_tools): each against a fake robot and voice server, the `soma:` config, and that
switching it off takes the tools away rather than leaving them to fail."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from seren_sinew.soma.client import SomaRobot, SomaVoice

from seren_workbench.config import SomaConfig, load_config
from seren_workbench.models.tools import soma_tools as st

STATUS = {"ok": True, "uptimeMs": 120000, "leds": "000000" * 2 + "431530" * 4 + "000000" * 2 + "431530" * 4,
          "resting": "", "settings": {"dimmer": 80, "volume": 50, "brightness": 40, "night": None,
                                      "applied": {"dimmer": 80, "volume": 50, "brightness": 40}},
          "batteryLevel": 88, "charging": True, "face": "studio"}


class Body:
    def __init__(self):
        self.calls = []

    def robot(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.url.path, body))
        if request.headers.get("X-StackChan-Token") != "rt":
            return httpx.Response(401, json={"error": "bad or missing token"})
        if request.url.path == "/api/status":
            return httpx.Response(200, json=STATUS)
        if request.url.path == "/api/settings":
            s = dict(STATUS["settings"], **(body or {}))
            return httpx.Response(200, json={"ok": True, "settings": s})
        return httpx.Response(200, json={"ok": True})

    def voice(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.url.path, json.loads(request.content) if request.content else None))
        if request.url.path == "/status":
            return httpx.Response(200, json={"ok": True, "device": False, "speaking": False})
        if request.url.path == "/settings":
            return httpx.Response(200, json={"night": {}, "work_hours": {}, "channel": {}, "night_now": True})
        if request.url.path == "/say":
            return httpx.Response(200, json={"ok": True, "seconds": 2.5})
        if request.url.path == "/touch":
            return httpx.Response(200, json={"events": [
                {"seq": 4, "t": 1.0, "kind": "stroke", "way": "front_to_back", "t_ms": 1, "ms": 450, "showed": "loving"},
                {"seq": 5, "t": 2.0, "kind": "touch_spot", "spot": "boop", "index": 0, "t_ms": 2, "ms": 0, "showed": None},
                {"seq": 6, "t": 3.0, "kind": "scritch", "t_ms": 3, "ms": 120, "showed": "laughing", "scratching": True}],
                "last": 6, "hand": {"down": True, "from_ms": 1000, "to_ms": 4200}})
        return httpx.Response(404)


def run(coro):
    return json.loads(asyncio.run(coro))


@pytest.fixture
def body():
    b = Body()
    robot = SomaRobot("http://bot:8080", "rt", transport=httpx.MockTransport(b.robot))
    voice = SomaVoice("http://voice:7480", "vt", transport=httpx.MockTransport(b.voice))
    return b, robot, voice


def test_status_reads_both_sides(body):
    b, robot, voice = body
    r = run(st.soma_status(soma_robot=robot, soma_voice=voice))
    assert r["body"]["face"] == "studio" and r["body"]["battery"] == 88 and len(r["body"]["leds"]) == 12
    assert r["body"]["leds"][2] == "#431530" and r["voice"]["night_now"] is True


def test_status_says_which_side_is_missing():
    b = Body()
    robot = SomaRobot("http://bot:8080", "wrong", transport=httpx.MockTransport(b.robot))
    voice = SomaVoice("http://voice:7480", "vt", transport=httpx.MockTransport(b.voice))
    r = run(st.soma_status(soma_robot=robot, soma_voice=voice))
    assert "refused the token" in r["body_error"] and r["voice"]["channel_open"] is False


def test_pose_rest_and_react(body):
    b, robot, _ = body
    assert run(st.soma_pose("sleepy", rest=True, soma_robot=robot)) == {"ok": True, "pose": "sleepy", "resting": True}
    run(st.soma_pose("ohbrother", ms=2500, soma_robot=robot))
    assert b.calls[-2] == ("/api/emotion", {"name": "sleepy", "rest": True})
    assert b.calls[-1] == ("/api/react", {"name": "ohbrother", "ms": 2500})
    assert "error" in run(st.soma_pose("../../etc", soma_robot=robot))


def test_say_wakes_the_channel_with_a_pose(body):
    b, _, voice = body
    assert run(st.soma_say("Look at me, I'm Chad.", emotion="funny", soma_voice=voice))["seconds"] == 2.5
    assert b.calls[-1] == ("/say", {"text": "Look at me, I'm Chad.", "emotion": "funny", "wake": True})
    assert "error" in run(st.soma_say("x" * 1001, soma_voice=voice))


def test_settings_read_and_set(body):
    b, robot, _ = body
    r = run(st.soma_settings(lights=30, soma_robot=robot))
    assert r["lights"] == 30 and b.calls[-1] == ("/api/settings", {"dimmer": 30})
    assert "error" in run(st.soma_settings(screen=2, soma_robot=robot))
    run(st.soma_settings(soma_robot=robot))
    assert b.calls[-1] == ("/api/settings", {})


def test_lights_scale_to_the_leds(body):
    b, robot, _ = body
    run(st.soma_lights(color="#ff0000", soma_robot=robot))
    assert b.calls[-1] == ("/api/leds", {"frame": [[168, 0, 0]] * 12})
    run(st.soma_lights(leds=["#000000", "#00ff00"], soma_robot=robot))
    assert b.calls[-1] == ("/api/leds", {"frame": [[0, 0, 0], [0, 168, 0]]})
    assert "error" in run(st.soma_lights(color="pink", soma_robot=robot))
    assert "error" in run(st.soma_lights(soma_robot=robot))


def test_head_checks_its_ranges(body):
    b, robot, _ = body
    run(st.soma_head(yaw=40, soma_robot=robot))
    assert b.calls[-1] == ("/api/head", {"speed": 150, "yaw": 40})
    assert "error" in run(st.soma_head(pitch=120, soma_robot=robot))
    assert "error" in run(st.soma_head(soma_robot=robot))


def test_touches_in_words(body):
    _, _, voice = body
    r = run(st.soma_touches(soma_voice=voice))
    assert r["touches"][0] == {"n": 4, "what": "stroke front to back", "ms": 450, "showed": "loving"}
    assert r["touches"][1]["what"] == "screen: boop" and r["last"] == 6
    assert r["touches"][2] == {"n": 6, "what": "scritch", "ms": 120, "showed": "laughing", "scratching": True}
    assert r["hand"] == {"down": True, "seconds": 3.2}


def test_no_body_configured_is_an_error_not_a_crash():
    assert "no body configured" in run(st.soma_status())["error"]
    assert "no body configured" in run(st.soma_say("hi"))["error"]


def test_no_camera_tool():
    names = {v["name"] for k, v in vars(st).items() if k.endswith("_TOOL_DEF")}
    assert names == {"soma_status", "soma_pose", "soma_say", "soma_settings", "soma_lights", "soma_head", "soma_touches"}
    assert not any("photo" in n or "camera" in n for n in names)


def test_config_on_by_default_off_by_yaml_or_env(tmp_path, monkeypatch):
    assert SomaConfig().enabled
    tok = tmp_path / "control_token"
    tok.write_text("from-file\n", encoding="utf-8")
    y = tmp_path / "wb.yaml"
    y.write_text(f"soma:\n  enabled: false\n  robot_url: http://10.0.0.9:8080\n  robot_token_file: {str(tok).replace(chr(92), '/')}\n",
                 encoding="utf-8")
    cfg = load_config(str(y))
    assert not cfg.soma.enabled and cfg.soma.robot_url == "http://10.0.0.9:8080"
    assert cfg.soma.token_for("robot") == "from-file" and cfg.soma.token_for("voice") == ""
    monkeypatch.setenv("SEREN_WORKBENCH_SOMA_ENABLED", "true")
    assert load_config(str(y)).soma.enabled


def _tool_names(client) -> set[str]:
    return {t.name for t in client.app.state.tool_registry.all_tools()}


def test_switched_off_the_tools_are_absent(make_client, tmp_path):
    cfg = load_config(str(tmp_path / "none.yaml"))
    cfg.dashboard.tools_dir = str(tmp_path / "tools")
    cfg.soma.enabled = False
    c = make_client(cfg)
    assert not any(n.startswith("soma_") for n in _tool_names(c))
    assert "soma_robot" not in c.app.state.di_registry


def test_with_no_robot_set_the_tools_are_absent(make_client, tmp_path):
    cfg = load_config(str(tmp_path / "none.yaml"))
    cfg.dashboard.tools_dir = str(tmp_path / "tools")
    assert cfg.soma.enabled and cfg.soma.robot_url == "" and not cfg.soma.active
    c = make_client(cfg)
    assert not any(n.startswith("soma_") for n in _tool_names(c))


def test_switched_on_they_are_listed_and_wired(make_client, tmp_path):
    cfg = load_config(str(tmp_path / "none.yaml"))
    cfg.dashboard.tools_dir = str(tmp_path / "tools")
    cfg.soma.robot_url = "http://10.0.0.9:8080"
    c = make_client(cfg)
    assert {"soma_status", "soma_say", "soma_pose"} <= _tool_names(c)
    assert isinstance(c.app.state.di_registry["soma_robot"], SomaRobot)
    assert isinstance(c.app.state.di_registry["soma_voice"], SomaVoice)
