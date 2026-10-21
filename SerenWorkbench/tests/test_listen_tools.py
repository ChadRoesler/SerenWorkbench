"""
listen / audition: words about a sound, through Lodestar's listener; a
sentence in several kokoro voices, kept and compared.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
from pathlib import Path

import httpx

from seren_workbench.models.tools import listen_tools as lt
from seren_workbench.tool_config.mcp_config import McpConfig

WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\0" * 20 + b"data\0\0\0\0"


class FakeLodestar:
    def __init__(self):
        self.seen = []
        self.listener_status = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = request.read()
        self.seen.append((request.url.path, body))
        if request.url.path == "/api/v1/service/kokoro/ensure":
            return httpx.Response(200, json={"ok": True, "service": "kokoro", "node": "jetson", "ready": True,
                                             "started": True, "base_url": "http://kokoro:8880", "port": 8880})
        if request.url.path == "/api/v1/service/kokoro/release":
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/api/v1/listen":
            if self.listener_status != 200:
                return httpx.Response(self.listener_status, json={"error": "the listener did not come up"})
            return httpx.Response(200, json={"ok": True, "text": "A warm alto, unhurried, a little breath.",
                                             "node": "bigbox", "seconds": 2.0, "clip_seconds": 3.0, "cut": False})
        if request.url.path == "/api/v1/listen/compare":
            # which clip came first is in the multipart order: say so, so the test can see both orders ran
            first_file = body.split(b'filename="', 1)[1].split(b'"', 1)[0]
            first = "the blend" if b"af-bella" in first_file else "af_heart"
            return httpx.Response(200, json={"ok": True, "text": f"{first} first: A sounds more natural.",
                                             "node": "bigbox", "seconds": 3.0})
        return httpx.Response(404, json={"detail": "Not Found"})

    def client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler), base_url="http://lodestar")


class FakeKokoro:
    def __init__(self):
        self.asked = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read())
        self.asked.append(body)
        if body["voice"] == "zz_nope":
            return httpx.Response(400, json={"detail": "voice not found"})
        return httpx.Response(200, content=WAV + body["voice"].encode(), headers={"content-type": "audio/wav"})


def run(coro):
    return asyncio.run(coro)


def config(folder: Path) -> McpConfig:
    return McpConfig({"audition": {"folder": str(folder)}}, "(test)")


def test_listen_sends_the_clip_and_the_question_and_says_what_was_heard(tmp_path):
    clip = tmp_path / "me.wav"
    clip.write_bytes(WAV)
    fake = FakeLodestar()
    out = json.loads(run(lt.listen(str(clip), question="how warm is it?", runtime_host=fake.client())))
    assert out["ok"] and out["heard"].startswith("A warm alto") and out["node"] == "bigbox"
    (path, body), = fake.seen
    assert path == "/api/v1/listen" and WAV in body and b'name="prompt"\r\n\r\nhow warm is it?' in body
    assert b'name="holder"\r\n\r\nworkbench-listen' in body


def test_listen_refuses_what_is_not_a_wav_on_this_box(tmp_path):
    fake = FakeLodestar()
    assert "No file" in json.loads(run(lt.listen(str(tmp_path / "nope.wav"), runtime_host=fake.client())))["error"]
    (tmp_path / "x.wav").write_bytes(b"not a wav")
    assert "not a WAV" in json.loads(run(lt.listen(str(tmp_path / "x.wav"), runtime_host=fake.client())))["error"]
    assert not fake.seen
    (tmp_path / "ok.wav").write_bytes(WAV)
    fake.listener_status = 503
    out = json.loads(run(lt.listen(str(tmp_path / "ok.wav"), runtime_host=fake.client())))
    assert out["error"] == "the listener did not come up"


def test_an_audition_speaks_each_voice_keeps_the_clips_and_compares_them(tmp_path, monkeypatch):
    fake, kokoro = FakeLodestar(), FakeKokoro()
    monkeypatch.setattr(lt, "KOKORO_TRANSPORT", httpx.MockTransport(kokoro.handler))
    out = json.loads(run(lt.audition("Hi, I am here.", ["af_heart", "af_heart(0.7)+af_bella(0.3)"], why="picking a voice",
                                     question="which is warmer?", runtime_host=fake.client(), config=config(tmp_path))))
    assert out["ok"] and out["said"] == "Hi, I am here."
    assert [a["voice"] for a in kokoro.asked] == ["af_heart", "af_heart(0.7)+af_bella(0.3)"]
    assert kokoro.asked[0] == {"model": "kokoro", "voice": "af_heart", "input": "Hi, I am here.",
                               "response_format": "wav", "speed": 1.0}
    paths = [p for p, _ in fake.seen]
    assert paths[0] == "/api/v1/service/kokoro/ensure" and paths[1] == "/api/v1/service/kokoro/release", "woken, let go before the listening"
    assert paths[2:] == ["/api/v1/listen", "/api/v1/listen", "/api/v1/listen/compare", "/api/v1/listen/compare"], "each pair both ways"
    # the caller's question steers the comparison only; each clip alone gets the coach's question
    singles = [b for p, b in fake.seen if p == "/api/v1/listen"]
    assert all(b"voice coach" in b and b"which is warmer?" not in b for b in singles)
    assert b"which is warmer?" in [b for p, b in fake.seen if p == "/api/v1/listen/compare"][0]
    assert [v["heard"] for v in out["voices"]] == ["A warm alto, unhurried, a little breath."] * 2
    (pair,) = out["compared"]
    assert (pair["a"], pair["b"]) == ("af_heart", "af_heart(0.7)+af_bella(0.3)")
    assert pair["heard"] == "af_heart first: A sounds more natural." and pair["heard_swapped"] == "the blend first: A sounds more natural."
    assert "whichever it hears first" in pair["note"]
    day = tmp_path / dt.date.today().isoformat()
    first, second = (Path(v["file"]) for v in out["voices"])
    assert first.parent == day and len(list(day.glob("*.wav"))) == 2
    assert first.read_bytes() == WAV + b"af_heart" and "af-heart-0-7-af-bella" in second.name
    note = json.loads(first.with_suffix(".json").read_text())
    assert note["voice"] == "af_heart" and note["why"] == "picking a voice" and note["heard"].startswith("A warm alto")


def test_an_audition_refuses_bad_input_before_waking_anything(tmp_path, monkeypatch):
    fake, kokoro = FakeLodestar(), FakeKokoro()
    monkeypatch.setattr(lt, "KOKORO_TRANSPORT", httpx.MockTransport(kokoro.handler))
    assert "Say what" in json.loads(run(lt.audition("", ["af_heart"], runtime_host=fake.client(), config=config(tmp_path))))["error"]
    assert "at least one" in json.loads(run(lt.audition("hi", [], runtime_host=fake.client(), config=config(tmp_path))))["error"]
    assert "not a kokoro voice" in json.loads(run(lt.audition("hi", ["af heart!"], runtime_host=fake.client(), config=config(tmp_path))))["error"]
    assert "too many" in json.loads(run(lt.audition("hi", [f"v{i}" for i in range(7)], runtime_host=fake.client(), config=config(tmp_path))))["error"]
    assert not fake.seen and not kokoro.asked
    # a voice kokoro does not have: said plainly, kokoro let go, nothing kept
    out = json.loads(run(lt.audition("hi", ["zz_nope"], runtime_host=fake.client(), config=config(tmp_path))))
    assert "refused voice 'zz_nope'" in out["error"]
    assert [p for p, _ in fake.seen] == ["/api/v1/service/kokoro/ensure", "/api/v1/service/kokoro/release"]
    assert not list(tmp_path.rglob("*.wav"))


def test_the_tools_are_discovered(tmp_path):
    from seren_workbench.models.tools import listen_tools
    assert listen_tools.LISTEN_TOOL_DEF["name"] == "listen" and listen_tools.AUDITION_TOOL_DEF["name"] == "audition"
    assert listen_tools.TOOLBOX == "Senses"
