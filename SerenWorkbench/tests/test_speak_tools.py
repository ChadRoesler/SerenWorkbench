"""
say: words in my voice, through Lodestar's speaker; the clip kept on this
box with a note, the path returned.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
from pathlib import Path

import httpx

from seren_workbench.models.tools import speak_tools as st
from seren_workbench.tool_config.mcp_config import McpConfig

WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\0" * 20 + b"data\0\0\0\0"


class FakeLodestar:
    def __init__(self, status=200):
        self.seen = []
        self.status = status

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read())
        self.seen.append((request.url.path, body))
        if self.status == 404:
            return httpx.Response(404, json={"detail": "Not Found"})
        if self.status == 503:
            return httpx.Response(503, json={"error": "no online node with speaker", "node": None})
        if self.status == 502:
            return httpx.Response(502, json={"error": "the speaker refused the text", "detail": "no voice 'bob'", "node": "bigbox"})
        return httpx.Response(200, content=WAV, headers={"content-type": "audio/wav", "X-Seren-Node": "bigbox",
                                                         "X-Seren-Voice": "wren", "X-Seren-Backend": "qwen3tts",
                                                         "X-Seren-Model": "Qwen/Qwen3-TTS-12Hz-1.7B-Base",
                                                         "X-Seren-Seconds": "4.1", "X-Seren-Clip-Seconds": "4.9"})

    def client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler), base_url="http://lodestar")


def run(coro):
    return asyncio.run(coro)


def config(folder: Path) -> McpConfig:
    return McpConfig({"say": {"folder": str(folder)}}, "(test)")


def test_say_sends_the_words_keeps_the_clip_and_returns_where(tmp_path):
    fake = FakeLodestar()
    out = json.loads(run(st.say("Hi, it's me.", why="a first hello", runtime_host=fake.client(), config=config(tmp_path))))
    assert out["ok"] and out["voice"] == "wren" and out["node"] == "bigbox" and out["clip_seconds"] == 4.9
    (path, body), = fake.seen
    assert path == "/api/v1/speak" and body == {"text": "Hi, it's me.", "voice": "wren", "holder": "workbench-say"}
    p = Path(out["file"])
    assert p.read_bytes() == WAV and p.parent == tmp_path / dt.date.today().isoformat()
    assert p.name.endswith("-hi-it-s-me.wav")
    note = json.loads(p.with_suffix(".json").read_text())
    assert note["said"] == "Hi, it's me." and note["why"] == "a first hello" and note["backend"] == "qwen3tts"
    assert note["model"] == "Qwen/Qwen3-TTS-12Hz-1.7B-Base" and note["seconds"] == 4.1


def test_a_name_names_the_file_and_a_voice_is_passed_through(tmp_path):
    fake = FakeLodestar()
    out = json.loads(run(st.say("Good morning.", voice="wren-late", name="good morning alice",
                                runtime_host=fake.client(), config=config(tmp_path))))
    assert out["ok"] and Path(out["file"]).name.endswith("-good-morning-alice.wav")
    assert fake.seen[0][1]["voice"] == "wren-late"


def test_nothing_is_sent_for_empty_or_oversized_text_or_a_bad_voice_name(tmp_path):
    fake = FakeLodestar()
    assert "Say what" in json.loads(run(st.say("  ", runtime_host=fake.client())))["error"]
    assert "4000" in json.loads(run(st.say("x" * 4001, runtime_host=fake.client())))["error"]
    assert "not a voice name" in json.loads(run(st.say("hi", voice="bob; rm -rf", runtime_host=fake.client())))["error"]
    assert not fake.seen


def test_a_lodestar_without_the_route_or_a_node_or_a_voice_is_an_answer(tmp_path):
    out = json.loads(run(st.say("hi", runtime_host=FakeLodestar(404).client(), config=config(tmp_path))))
    assert "no speaker route" in out["error"]
    out = json.loads(run(st.say("hi", runtime_host=FakeLodestar(503).client(), config=config(tmp_path))))
    assert out["error"] == "no online node with speaker" and "audition" in out["hint"]
    out = json.loads(run(st.say("hi", voice="bob", runtime_host=FakeLodestar(502).client(), config=config(tmp_path))))
    assert out["error"] == "the speaker refused the text" and "no voice 'bob'" in out["hint"]
    assert not list(tmp_path.rglob("*.wav"))


def test_the_clips_live_beside_the_auditions_by_default(tmp_path):
    root = tmp_path / "wren"
    (root / "apps" / "workbench").mkdir(parents=True)
    cfg = McpConfig({}, str(root / "apps" / "workbench" / "mcp.json"))
    (root / "apps" / "workbench" / "mcp.json").write_text("{}")
    assert st.said_dir(st._settings(cfg)) == root / "stores" / "workbench" / "said"
