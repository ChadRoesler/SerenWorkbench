"""
make_image / image_options: pictures through Lodestar's image route, kept in
a gallery with a note each, under a daily budget the config sets and the
model cannot argue with.
"""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import json
from pathlib import Path

import httpx

from seren_workbench.models.tools import make_image_tool as mi
from seren_workbench.tool_config.mcp_config import McpConfig


class FakeLodestar:
    def __init__(self, status: int = 200, body: dict | None = None):
        self.status, self.body, self.seen = status, body, []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.seen.append((request.method, request.url.path, json.loads(request.content) if request.content else None,
                          dict(request.url.params)))
        if request.url.path == "/api/v1/image":
            n = (self.seen[-1][2] or {}).get("count", 1)
            body = self.body or {"ok": True, "node": "xavier32gb", "model": "checkpoints/krea2Turbo.safetensors",
                                 "prompt": "storybook folk art illustration, a wren", "negative": "", "seed": 42,
                                 "loras": [{"name": "Storybook_Folk_Art_Krea2.safetensors", "weight": 0.7, "fit": "good"}],
                                 "width": 1024, "height": 1024, "seconds": 31.5,
                                 "images": [{"filename": f"wren/42_{i}.png", "mime": "image/png",
                                             "data": base64.b64encode(b"PNG%d" % i).decode()} for i in range(n)]}
            return httpx.Response(self.status, json=body)
        if request.url.path == "/api/v1/comfy/catalog":
            return httpx.Response(200, json={"node": "spark128gb", "nodes": [
                {"node": "spark128gb", "families": [], "summary": {"lora": {"Krea 2": 17}}},
                {"node": "xavier32gb", "families": ["sdxl", "sd15"], "summary": {"lora": {"Krea 2": 17}}}], "files": {
                "checkpoints/krea2Turbo.safetensors": {"role": "checkpoint", "name": "krea2Turbo.safetensors", "arch": "krea2",
                                                       "kind": "image", "drawable_on": ["spark128gb"]},
                "diffusion_models/wan22.safetensors": {"role": "diffusion", "name": "wan22.safetensors", "arch": "wan",
                                                       "kind": "video", "drawable_on": []},
                "loras/x.safetensors": {"role": "lora", "name": "x.safetensors", "arch": "krea2"}}})
        if request.url.path == "/api/v1/comfy/compatible":
            return httpx.Response(200, json={"model": "checkpoints/krea2Turbo.safetensors", "loras": [{"name": "x.safetensors", "fit": "good"}]})
        return httpx.Response(404, json={"detail": "Not Found"})

    def client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler), base_url="http://lodestar")


def config(gallery: Path, **kv) -> McpConfig:
    return McpConfig({"make_image": {"gallery": str(gallery), **{k: str(v) for k, v in kv.items()}}}, "(test)")


def run(coro):
    return asyncio.run(coro)


def test_a_picture_is_drawn_and_kept_with_its_note(tmp_path):
    fake = FakeLodestar()
    out = json.loads(run(mi.make_image("a wren on a lantern-lit chair", why="the morning message",
                                       loras=[{"name": "Storybook_Folk_Art_Krea2.safetensors", "weight": 0.7}],
                                       runtime_host=fake.client(), config=config(tmp_path))))
    assert out["ok"] and out["seed"] == 42 and out["today"] == "1 of 24"
    (saved,) = map(Path, out["saved"])
    assert saved.parent == tmp_path / dt.date.today().isoformat() and saved.read_bytes() == b"PNG0"
    assert "morning-message" in saved.name
    note = json.loads(saved.with_suffix(".json").read_text())
    assert note["why"] == "the morning message" and note["asked"] == "a wren on a lantern-lit chair"
    assert note["seed"] == 42 and note["loras"][0]["weight"] == 0.7 and note["file"] == saved.name
    method, path, body, _ = fake.seen[0]
    assert (method, path) == ("POST", "/api/v1/image")
    assert body["loras"] == [{"name": "Storybook_Folk_Art_Krea2.safetensors", "weight": 0.7}]
    assert body["holder"] == "workbench-make_image" and "model" not in body, "blank means the node's default"


def test_the_day_has_a_limit_the_model_cannot_argue_with(tmp_path):
    fake = FakeLodestar()
    cfg = config(tmp_path, daily_limit=3)
    assert json.loads(run(mi.make_image("one", count=2, runtime_host=fake.client(), config=cfg)))["today"] == "2 of 3"
    out = json.loads(run(mi.make_image("two more", count=2, runtime_host=fake.client(), config=cfg)))
    assert "limit is 3" in out["error"] and len(fake.seen) == 1, "refused before anything is drawn"
    assert json.loads(run(mi.make_image("one more", runtime_host=fake.client(), config=cfg)))["ok"]


def test_off_is_off_and_too_big_is_too_big(tmp_path):
    fake = FakeLodestar()
    assert "turned off" in json.loads(run(mi.make_image("x", runtime_host=fake.client(),
                                                        config=config(tmp_path, enabled="false"))))["error"]
    assert "bigger than" in json.loads(run(mi.make_image("x", width=4096, runtime_host=fake.client(),
                                                         config=config(tmp_path))))["error"]
    assert not fake.seen


def test_lodestar_saying_no_is_said_back(tmp_path):
    fake = FakeLodestar(400, {"error": "x.safetensors is a sdxl LoRA; it will not load on krea2"})
    out = json.loads(run(mi.make_image("x", runtime_host=fake.client(), config=config(tmp_path))))
    assert "will not load" in out["error"] and not list(tmp_path.rglob("*.png"))
    old = FakeLodestar(404, {"detail": "Not Found"})
    assert "no image route" in json.loads(run(mi.make_image("x", runtime_host=old.client(), config=config(tmp_path))))["error"]


def test_the_gallery_sits_in_the_stores_beside_the_apps(tmp_path):
    root = tmp_path / "seren" / "wren"
    (root / "apps" / "workbench").mkdir(parents=True)
    yml = root / "apps" / "workbench" / "seren-workbench.yaml"
    yml.write_text("x: 1\n")
    assert mi.gallery_dir(mi._settings(McpConfig({}, str(yml)))) == root / "stores" / "workbench" / "gallery"


def test_the_options_list_models_and_what_fits(tmp_path):
    fake = FakeLodestar()
    out = json.loads(run(mi.image_options(runtime_host=fake.client())))
    assert [m["name"] for m in out["models"]] == ["krea2Turbo.safetensors", "wan22.safetensors"], "drawable first"
    assert out["models"][0]["drawable_on"] == ["spark128gb"] and out["nodes"][1]["families"] == ["sdxl", "sd15"]
    fits = json.loads(run(mi.image_options(model="krea2Turbo.safetensors", runtime_host=fake.client())))
    assert fits["loras"] == [{"name": "x.safetensors", "fit": "good"}]
    assert fake.seen[-1][3] == {"model": "krea2Turbo.safetensors"}


def test_the_tools_are_registered(make_client, tmp_path):
    from seren_workbench.config import WorkbenchConfig
    cfg = WorkbenchConfig()
    cfg.dashboard.tools_dir = str(tmp_path / "tools")
    tools = {t["name"]: t for t in make_client(cfg).get("/tools").json()["tools"]}
    assert tools["make_image"]["toolbox"] == "Creation" and "image_options" in tools
    assert [p["name"] for p in tools["make_image"]["parameters"]][:2] == ["prompt", "why"]
