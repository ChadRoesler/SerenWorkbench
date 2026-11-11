"""The kitchen's builtins (kitchen_tools): each against a fake kitchen, that everything they leave is signed
with one name, the `kitchen:` config, and that switching it off takes the tools away rather than leaving
them to fail."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from seren_workbench.config import KitchenConfig, load_config
from seren_workbench.models.tools import kitchen_tools as kt
from seren_workbench.models.tools.kitchen_tools import Kitchen

SHEET = {"id": "ab12cd34ef56", "fridge": "seren-memory", "kind": "now", "title": "the honey-do", "author": "alex",
         "body": "two things, one of them a choice", "place": "door", "x": 10, "y": 20, "rot": -3, "pin": "#d0393e",
         "lines": [{"text": "bump the version", "done": True}, {"text": "colour ? red | blue", "done": False, "options": ["red", "blue"], "pick": None}],
         "art": [], "made": "2026-01-02T03:04:05", "done_at": None, "waiting": True, "sour": False, "replies": [
             {"id": "ffffffffffff", "fridge": "seren-memory", "kind": "other", "title": "red, obviously", "author": "ada", "body": "red, obviously",
              "lines": [], "art": [], "made": "2026-01-02T04:00:00", "reply_to": "ab12cd34ef56", "reply_line": 1, "replies": []}]}
WALL = {"house": {"person": "Alex", "partner": "Ada", "fridge": "seren-project"},
        "fridges": [{"name": "seren-memory", "display": "Memory", "facts": {"release": "6.1.0", "build": "ok"}, "held_by": ["ada"],
                     "counts": {"door": 1, "shelf": 0, "crisper": 0, "leftovers": 4}, "sour": 0},
                    {"name": "seren-project", "display": "The House", "facts": {}, "held_by": [],
                     "counts": {"door": 0, "shelf": 0, "crisper": 0, "leftovers": 0}, "sour": 0}],
        "magnets": [{"partner": "ada", "fridge": "seren-memory"}],
        "doors": [SHEET]}


class FakeKitchen:
    def __init__(self, token="kt"):
        self.token = token
        self.calls: list[tuple[str, str, object]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path + (("?" + request.url.query.decode()) if request.url.query else ""), body))
        if self.token and request.headers.get("Authorization") != f"Bearer {self.token}":
            return httpx.Response(401, json={"detail": "bad or missing bearer"})
        p, m = request.url.path, request.method
        if p == "/api/kitchen":
            return httpx.Response(200, json=WALL)
        if p == "/api/sheets":
            return httpx.Response(200, json=[dict(SHEET, sour=True)])
        if p == "/api/fridges/seren-memory":
            return httpx.Response(200, json={**WALL["fridges"][0], "magnets": WALL["magnets"], "door": [SHEET], "shelf": [], "crisper": [],
                                             "leftovers": [dict(SHEET, id="0123456789ab", title="old", done_at="2025-12-31T10:00:00")]})
        if p.startswith("/api/fridges/") and m == "GET":
            return httpx.Response(404, json={"detail": f"no fridge called {p.rsplit('/', 1)[1]!r}"})
        if p == "/api/sheets/ab12cd34ef56" and m == "GET":
            return httpx.Response(200, json=SHEET)
        if p == "/api/fridges/seren-memory/sheets" and m == "POST":
            return httpx.Response(200, json={**body, "id": "123456789abc", "fridge": "seren-memory", "place": "door" if body["kind"] != "list" else "shelf"})
        if p == "/api/sheets/ab12cd34ef56/reply" and m == "POST":
            return httpx.Response(200, json={**body, "id": "cba987654321", "reply_to": "ab12cd34ef56"})
        if p == "/api/sheets/ab12cd34ef56" and m == "PATCH":
            s = json.loads(json.dumps(SHEET))
            if "pick_line" in body:
                if body["pick_line"]["option"] not in s["lines"][1]["options"]:
                    return httpx.Response(400, json={"detail": "the pick is one of: red, blue"})
                s["lines"][1]["pick"] = body["pick_line"]["option"]; s["lines"][1]["done"] = True
            for i in body.get("tick", []):
                s["lines"][i]["done"] = True
            if body.get("done"):
                s["done_at"] = "2026-01-03T09:00:00"; s["place"] = "leftovers"
            return httpx.Response(200, json=s)
        if p == "/api/sessions" and m == "POST":
            hand = body.get("hand") or "memory"
            return httpx.Response(200, json={"ok": True, "label": f"{body['partner']}·{hand}", "hand": hand, "on": None, "state": "live",
                                             "starter": f"cd {body.get('cwd', '')}\nclaude --resume {body['session']}"})
        if p == "/api/magnets" and m == "POST":
            self.magnet = {**getattr(self, "magnet", {"partner": body["partner"], "starter": "", "picture": "", "fridge": None}), **body}
            return httpx.Response(200, json=self.magnet)
        if p.startswith("/api/magnets/") and p.endswith("/stick"):
            who = p.split("/")[3]
            return httpx.Response(200, json={"partner": who, "hand": body.get("hand", ""), "label": f"{who}·{body['hand']}" if body.get("hand") else who,
                                             "fridge": body["fridge"], "last": "seren-loci"})
        return httpx.Response(404, json={"detail": f"no {p}"})


def run(coro):
    return json.loads(asyncio.run(coro))


@pytest.fixture
def fake():
    return FakeKitchen()


@pytest.fixture
def kitchen(fake):
    return Kitchen("http://kitchen:7431", "kt", author="ada", transport=httpx.MockTransport(fake))


def test_wall_is_the_glance(kitchen):
    r = run(kt.kitchen_wall(kitchen=kitchen))
    assert r["house"] == {"person": "Alex", "partner": "Ada"} and r["signed_as"] == "ada"
    mem = r["fridges"][0]
    assert mem["fridge"] == "seren-memory" and mem["release"] == "6.1.0" and mem["build"] == "ok" and mem["held_by"] == ["ada"]
    assert mem["sheets"] == 1 and mem["leftovers"] == 4 and mem["door"][0]["title"] == "the honey-do" and mem["door"][0]["waiting"] is True
    assert "sheets" not in r["fridges"][1]                      # an empty fridge says nothing about counts
    assert r["waiting_on_a_pick"] == [{"id": "ab12cd34ef56", "fridge": "seren-memory", "title": "the honey-do", "by": "alex", "sour": True}]


def test_author_comes_from_the_house_when_the_config_is_blank(fake):
    k = Kitchen("http://kitchen:7431", "kt", transport=httpx.MockTransport(fake))
    assert asyncio.run(k.author()) == "Ada"
    assert asyncio.run(k.author()) == "Ada" and sum(1 for c in fake.calls if c[1] == "/api/kitchen") == 1   # asked once


def test_fridge_opened(kitchen):
    r = run(kt.kitchen_fridge("seren-memory", kitchen=kitchen))
    assert r["called"] == "Memory" and r["held_by"] == ["ada"] and r["leftovers"] == 1
    door = r["door"][0]
    assert door["lines"][1] == {"text": "colour ? red | blue", "done": False, "choices": ["red", "blue"], "pick": None}
    assert door["left"] == 1 and door["stuck_on_it"] == 1 and "x" not in door and "pin" not in door
    assert run(kt.kitchen_fridge("seren-memory", leftovers=True, kitchen=kitchen))["leftovers"][0]["title"] == "old"
    assert "no fridge called" in run(kt.kitchen_fridge("seren-nope", kitchen=kitchen))["error"]
    assert "isn't a fridge slug" in run(kt.kitchen_fridge("../etc", kitchen=kitchen))["error"]


def test_sheet_in_full_with_what_is_stuck_on_it(kitchen):
    r = run(kt.kitchen_sheet("ab12cd34ef56", kitchen=kitchen))
    assert r["note"] == "two things, one of them a choice" and r["place"] == "door" and r["waiting"] is True
    assert r["stuck_on_it"][0]["by"] == "ada" and r["stuck_on_it"][0]["on_line"] == 1
    assert "isn't a sheet id" in run(kt.kitchen_sheet("nope", kitchen=kitchen))["error"]


def test_leave_is_signed_as_me_whatever_is_asked(kitchen, fake):
    r = run(kt.kitchen_leave("seren-memory", "a list", kind="list", lines=["one", "two ? a | b"], kitchen=kitchen))
    assert r == {"ok": True, "sheet": "123456789abc", "fridge": "seren-memory", "place": "shelf", "signed": "ada"}
    sent = fake.calls[-1][2]
    assert sent == {"kind": "list", "title": "a list", "body": "", "lines": ["one", "two ? a | b"], "author": "ada"}
    assert "needs a title" in run(kt.kitchen_leave("seren-memory", "", kitchen=kitchen))["error"]
    assert "is one of" in run(kt.kitchen_leave("seren-memory", "x", kind="chad", kitchen=kitchen))["error"]
    assert "at most" in run(kt.kitchen_leave("seren-memory", "x", lines=["l"] * 41, kitchen=kitchen))["error"]


def test_reply_under_a_line(kitchen, fake):
    r = run(kt.kitchen_reply("ab12cd34ef56", "blue, and here is why\nbecause.", line=1, kitchen=kitchen))
    assert r == {"ok": True, "sheet": "cba987654321", "on": "ab12cd34ef56", "line": 1, "signed": "ada"}
    assert fake.calls[-1][2] == {"kind": "other", "title": "blue, and here is why", "body": "blue, and here is why\nbecause.", "author": "ada", "line": 1}
    assert "Say what" in run(kt.kitchen_reply("ab12cd34ef56", "  ", kitchen=kitchen))["error"]


def test_tick_pick_and_done(kitchen, fake):
    r = run(kt.kitchen_tick("ab12cd34ef56", pick_line=1, pick="blue", kitchen=kitchen))
    assert fake.calls[-1][2] == {"pick_line": {"line": 1, "option": "blue"}} and r["left"] == 0 and "done" not in r   # picking is not finishing
    assert "one of: red, blue" in run(kt.kitchen_tick("ab12cd34ef56", pick_line=1, pick="green", kitchen=kitchen))["error"]
    assert "both" in run(kt.kitchen_tick("ab12cd34ef56", pick="blue", kitchen=kitchen))["error"]
    r = run(kt.kitchen_tick("ab12cd34ef56", done=True, kitchen=kitchen))
    assert r["place"] == "leftovers" and r["done"] == "2026-01-03 09:00"
    assert "Give something" in run(kt.kitchen_tick("ab12cd34ef56", kitchen=kitchen))["error"]


def test_stick_makes_my_magnet_then_sticks_it(kitchen, fake):
    r = run(kt.kitchen_stick("seren-memory", kitchen=kitchen))
    assert r == {"ok": True, "magnet": "ada", "on": "seren-memory", "was_on": "seren-loci"}
    assert fake.calls[-2] == ("POST", "/api/magnets", {"partner": "ada"})
    assert fake.calls[-1] == ("POST", "/api/magnets/ada/stick", {"fridge": "seren-memory"})
    assert run(kt.kitchen_stick(kitchen=kitchen))["on"] == "the counter" and fake.calls[-1][2] == {"fridge": None}


def test_a_second_hand_of_me_is_its_own_magnet_and_signs_with_its_name(kitchen, fake):
    r = run(kt.kitchen_stick("seren-memory", hand="carabiner", kitchen=kitchen))
    assert r["magnet"] == "ada·carabiner" and fake.calls[-2][2] == {"partner": "ada", "hand": "carabiner"}
    assert fake.calls[-1] == ("POST", "/api/magnets/ada/stick", {"fridge": "seren-memory", "hand": "carabiner"})
    r = run(kt.kitchen_leave("seren-memory", "from the carabiner me", hand="carabiner", kitchen=kitchen))
    assert fake.calls[-1][2]["hand"] == "carabiner" and fake.calls[-1][2]["author"] == "ada" and r["signed"] == "ada · carabiner"
    run(kt.kitchen_reply("ab12cd34ef56", "me too", hand="carabiner", kitchen=kitchen))
    assert fake.calls[-1][2]["hand"] == "carabiner"
    r = run(kt.kitchen_magnet(starter="start the carabiner ada", hand="carabiner", kitchen=kitchen))
    assert fake.calls[-1][2] == {"partner": "ada", "hand": "carabiner", "starter": "start the carabiner ada"}
    assert "isn't a hand name" in run(kt.kitchen_leave("seren-memory", "x", hand="../x", kitchen=kitchen))["error"]
    assert "hand" in kt.KITCHEN_LEAVE_TOOL_DEF["input_schema"]["properties"]


PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00" + b"\x90wS\xde"
       + b"\x00\x00\x00\x00IEND\xaeB`\x82")


def test_magnet_is_mine_with_a_starter_and_a_face(kitchen, fake, tmp_path):
    r = run(kt.kitchen_magnet(starter="cd ~/house\nstart the partner", kitchen=kitchen))
    assert r == {"ok": True, "magnet": "ada", "starter": "cd ~/house\nstart the partner", "face": False, "on": "the counter"}
    assert fake.calls[-1] == ("POST", "/api/magnets", {"partner": "ada", "starter": "cd ~/house\nstart the partner"})
    face = tmp_path / "face.png"
    face.write_bytes(PNG)
    out = asyncio.run(kt.kitchen_magnet(picture=str(face), kitchen=kitchen))
    r = json.loads(out)
    assert r["face"] is True and r["starter"] == "cd ~/house\nstart the partner"           # the starter was left as it was
    sent = fake.calls[-1][2]
    assert "starter" not in sent and sent["picture"].startswith("data:image/png;base64,")
    assert out.images == [(PNG, "png")]                                                    # I get to see my own face
    assert "Give a starter" in run(kt.kitchen_magnet(kitchen=kitchen))["error"]
    assert "Can't read" in run(kt.kitchen_magnet(picture=str(tmp_path / "nope.png"), kitchen=kitchen))["error"]
    (tmp_path / "words.txt").write_text("not a picture", encoding="utf-8")
    assert "isn't a PNG" in run(kt.kitchen_magnet(picture=str(tmp_path / "words.txt"), kitchen=kitchen))["error"]
    (tmp_path / "big.png").write_bytes(PNG + b"\x00" * (kt.MAX_FACE_BYTES + 1))
    assert "under 190 KB" in run(kt.kitchen_magnet(picture=str(tmp_path / "big.png"), kitchen=kitchen))["error"]


def test_a_session_takes_over_the_magnet_and_tells_me_who_i_am(kitchen, fake, tmp_path):
    r = run(kt.kitchen_magnet(session="6e58d97d-b67a", cwd="D:\\git\\SerenMemory", kitchen=kitchen))
    assert fake.calls[-1] == ("POST", "/api/sessions", {"event": "start", "partner": "ada", "harness": "claude", "session": "6e58d97d-b67a", "cwd": "D:\\git\\SerenMemory"})
    assert r["magnet"] == "ada·memory" and r["starter"].endswith("claude --resume 6e58d97d-b67a") and r["sign_as"] == "pass hand=memory on the kitchen tools"
    face = tmp_path / "f.png"; face.write_bytes(PNG)
    out = asyncio.run(kt.kitchen_magnet(session="6e58d97d-b67a", cwd="D:\\git\\SerenMemory", picture=str(face), kitchen=kitchen))
    assert fake.calls[-1][1] == "/api/magnets" and fake.calls[-1][2]["hand"] == "memory" and out.images       # the face goes on the hand the session got
    assert "isn't a session id" in run(kt.kitchen_magnet(session="../x", kitchen=kitchen))["error"]


def test_a_refused_token_says_so(fake):
    k = Kitchen("http://kitchen:7431", "wrong", author="ada", transport=httpx.MockTransport(fake))
    assert "refused the token" in run(kt.kitchen_wall(kitchen=k))["error"]


def test_no_kitchen_configured_is_an_error_not_a_crash():
    assert "no kitchen configured" in run(kt.kitchen_wall())["error"]
    assert "no kitchen configured" in run(kt.kitchen_leave("seren-memory", "x"))["error"]


def test_the_tools_and_nothing_that_reads_a_store():
    names = {v["name"] for k, v in vars(kt).items() if k.endswith("_TOOL_DEF")}
    assert names == {"kitchen_wall", "kitchen_fridge", "kitchen_sheet", "kitchen_leave", "kitchen_reply", "kitchen_tick", "kitchen_magnet",
                     "kitchen_stick"}
    # no tool takes an author: what they leave is signed as me, whatever is asked
    assert all("author" not in v["input_schema"]["properties"] for k, v in vars(kt).items() if k.endswith("_TOOL_DEF"))


def test_config_on_by_default_off_by_yaml_or_env(tmp_path, monkeypatch):
    assert KitchenConfig().enabled and not KitchenConfig().active
    tok = tmp_path / "token"
    tok.write_text("from-file\n", encoding="utf-8")
    y = tmp_path / "wb.yaml"
    y.write_text(f"kitchen:\n  url: http://10.0.0.9:7431\n  author: ada\n  bearer_token_file: {str(tok).replace(chr(92), '/')}\n",
                 encoding="utf-8")
    cfg = load_config(str(y))
    assert cfg.kitchen.active and cfg.kitchen.url == "http://10.0.0.9:7431" and cfg.kitchen.author == "ada"
    assert cfg.kitchen.token() == "from-file"
    monkeypatch.setenv("SEREN_WORKBENCH_KITCHEN_ENABLED", "false")
    assert not load_config(str(y)).kitchen.active
    monkeypatch.delenv("SEREN_WORKBENCH_KITCHEN_ENABLED")
    monkeypatch.setenv("SEREN_WORKBENCH_KITCHEN_URL", "http://10.0.0.10:7431")
    assert load_config(str(y)).kitchen.url == "http://10.0.0.10:7431"


def _tool_names(client) -> set[str]:
    return {t.name for t in client.app.state.tool_registry.all_tools()}


def test_with_no_kitchen_set_the_tools_are_absent(make_client, tmp_path):
    cfg = load_config(str(tmp_path / "none.yaml"))
    cfg.dashboard.tools_dir = str(tmp_path / "tools")
    assert cfg.kitchen.enabled and not cfg.kitchen.active
    c = make_client(cfg)
    assert not any(n.startswith("kitchen_") for n in _tool_names(c))
    assert "kitchen" not in c.app.state.di_registry


def test_switched_on_they_are_listed_and_wired(make_client, tmp_path):
    cfg = load_config(str(tmp_path / "none.yaml"))
    cfg.dashboard.tools_dir = str(tmp_path / "tools")
    cfg.kitchen.url = "http://10.0.0.9:7431"
    c = make_client(cfg)
    assert {"kitchen_wall", "kitchen_leave", "kitchen_stick"} <= _tool_names(c)
    assert isinstance(c.app.state.di_registry["kitchen"], Kitchen)


def test_the_mcp_mount_survives_the_kitchen(make_client, tmp_path):
    """The client is injected by parameter name and must be listed as a DI type in mcp/server.py:
    the first build forgot, pydantic could not make a schema of it, and the WHOLE MCP mount failed on
    the NUC, taking every tool with it. The mount is a /mcp Mount on the app once the lifespan ran."""
    pytest.importorskip("mcp")
    cfg = load_config(str(tmp_path / "none.yaml"))
    cfg.dashboard.tools_dir = str(tmp_path / "tools")
    cfg.kitchen.url = "http://10.0.0.9:7431"
    c = make_client(cfg)
    assert any(getattr(r, "path", None) == "/mcp" for r in c.app.routes), "the MCP mount failed with the kitchen active"
    from seren_workbench.mcp.server import _DI_TYPES
    assert Kitchen in _DI_TYPES
