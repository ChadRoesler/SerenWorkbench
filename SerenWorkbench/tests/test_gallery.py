"""
The gallery routes: what make_image kept, at full size (15 Oct 2026).

Pinned here:
- the days with pictures, newest first, with counts; a day's pictures with
  the note beside each; the file itself, nosniff, inline
- only from inside the gallery: a day is YYYY-MM-DD, a name has no path, and
  a file that is not a picture or its note is not served
"""
from __future__ import annotations

import json
from pathlib import Path

from seren_workbench.tool_config.mcp_config import McpConfig

PNG = bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 40


def _gallery(tmp_path: Path) -> Path:
    g = tmp_path / "gallery"
    for day, names in (("2026-10-14", ["010101-1-old.png"]), ("2026-10-15", ["030021-7-for-the-margins.png", "031500-8-a-houseboat.jpg"])):
        (g / day).mkdir(parents=True)
        for n in names:
            (g / day / n).write_bytes(PNG)
            (g / day / n).with_suffix(".json").write_text(json.dumps(
                {"asked": "a thing", "why": "because", "model": "sdxl", "seed": 7, "file": n, "secret_key": "no"}), encoding="utf-8")
    (g / "2026-10-15" / "notes.txt").write_text("not a picture")
    (g / "scratch").mkdir()
    return g


def test_days_pictures_and_the_file_itself(client, tmp_path):
    g = _gallery(tmp_path)
    client.app.state.mcp_config = McpConfig({"make_image": {"gallery": str(g)}}, "(test)")
    days = client.get("/gallery").json()
    assert days["count"] == 3 and [d["day"] for d in days["days"]] == ["2026-10-15", "2026-10-14"]
    day = client.get("/gallery/2026-10-15").json()
    assert day["count"] == 2 and [p["file"] for p in day["pictures"]] == ["031500-8-a-houseboat.jpg", "030021-7-for-the-margins.png"]
    note = day["pictures"][1]["note"]
    assert note["asked"] == "a thing" and note["seed"] == 7 and "secret_key" not in note, "the note's known fields only"
    r = client.get("/gallery/2026-10-15/030021-7-for-the-margins.png")
    assert r.status_code == 200 and r.content == PNG and r.headers["content-type"] == "image/png"
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["content-security-policy"].startswith("sandbox")
    assert client.get("/gallery/2026-10-15/030021-7-for-the-margins.json").json()["why"] == "because"


def test_only_from_inside_the_gallery(client, tmp_path):
    g = _gallery(tmp_path)
    (tmp_path / "outside.png").write_bytes(PNG)
    client.app.state.mcp_config = McpConfig({"make_image": {"gallery": str(g)}}, "(test)")
    assert client.get("/gallery/scratch").status_code == 400
    assert client.get("/gallery/2026-10-16").status_code == 404
    assert client.get("/gallery/2026-10-15/notes.txt").status_code == 404, "not a picture"
    assert client.get("/gallery/2026-10-15/nope.png").status_code == 404
    assert client.get("/gallery/2026-10-15/..%2F..%2Foutside.png").status_code in (400, 404)
    assert client.get("/gallery/2026-10-15/.hidden.png").status_code in (400, 404)


def test_an_empty_gallery_is_an_empty_list(client, tmp_path):
    client.app.state.mcp_config = McpConfig({"make_image": {"gallery": str(tmp_path / "nowhere")}}, "(test)")
    assert client.get("/gallery").json() == {"gallery": str(tmp_path / "nowhere"), "days": [], "count": 0}
