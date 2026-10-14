# ═══════════════════════════════════════════════════════════════
#  MakeImageTool - pictures, whenever the model wants one.
#
#  make_image / image_options.
#
#  Through Lodestar's image route (seren_lodestar.routes.images): a request
#  in words - a prompt, maybe a model and LoRAs - becomes a workflow from the
#  image node's catalog, ComfyUI is woken and held for the drawing and let
#  go after, and the pictures come back. This tool keeps them: a gallery
#  folder, one subfolder a day, each picture with a note beside it (what was
#  asked, why, the model, the LoRAs, the seed - enough to draw it again).
#
#  FREE, WITH A BUDGET (14 Oct 2026). Chad's word was "make any and all art
#  you want to whenever you want to" - so no asking. A box with one image
#  model on a shared GPU still has limits a model in the mood could walk
#  past, so the config sets them and the model cannot talk its way around:
#
#    tools:
#      make_image:
#        enabled: "true"        # "false" turns pictures off
#        daily_limit: "24"      # pictures a day; 0 = no limit
#        max_side: "1536"       # the largest width or height
#        gallery: "..."         # where they are kept (default: the stores' workbench/gallery)
# ═══════════════════════════════════════════════════════════════

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional

import httpx

from ...tool_config.mcp_config import McpConfig

TOOLBOX = "Creation"

DEFAULT_DAILY_LIMIT = 24
DEFAULT_MAX_SIDE = 1536
MAX_COUNT = 4
DRAW_TIMEOUT = 960          # Lodestar waits up to 900s on the node, plus the hops

MAKE_IMAGE_TOOL_DEF = {
    "name": "make_image",
    "description": (
        "Draw a picture. Yours to use whenever you want one - a morning note, "
        "something you were thinking about, an answer that is better shown. "
        "Describe it in words; the picture is drawn on the cluster's image node "
        "(ComfyUI, through Lodestar) and kept in the gallery with a note of what "
        "you asked and why.\n"
        "\n"
        "With no model it is drawn with Krea 2 Turbo, which is good at painterly "
        "and illustrated scenes and follows long, plain descriptions well - write "
        "the prompt as sentences, not tag lists. Every model wants its own way of "
        "being prompted (booru tags for Pony and Illustrious, short phrases for "
        "SD 1.5...): to pick another model, or LoRAs (styles, characters, details) "
        "to wear, look at image_options(model=...) first - it says the model's "
        "prompt style, which LoRAs fit it (one made for another family will not "
        "load), and its profile (the LoRAs it wears when you name none). A LoRA's "
        "trigger words and a model's quality tags are added to the prompt for you.\n"
        "\n"
        "Returns where each picture was saved, the seed (to draw it again or "
        "vary it) and what was used."),
    "input_schema": {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "What to draw, in words."},
            "why": {"type": "string", "description": "What it is for (kept in its note): 'the morning message', 'for Chad'..."},
            "model": {"type": "string", "description": "A model's file name from image_options. Blank: Krea 2 Turbo."},
            "loras": {"type": "array", "description": "LoRAs to wear: [{name, weight}] - weight 0.3-1.0 usually.",
                      "items": {"type": "object", "properties": {"name": {"type": "string"}, "weight": {"type": "number"}},
                                "required": ["name"]}},
            "negative": {"type": "string", "description": "What to keep out (SDXL/SD1.5; Krea 2 ignores it)."},
            "width": {"type": "integer", "description": "Default 1024."},
            "height": {"type": "integer", "description": "Default 1024. Portrait: 832x1216; landscape: 1216x832."},
            "count": {"type": "integer", "description": f"How many to draw at once, 1-{MAX_COUNT}. Default 1."},
            "seed": {"type": "integer", "description": "Repeat or vary an earlier picture."},
        },
        "required": ["prompt"],
    },
}

IMAGE_OPTIONS_TOOL_DEF = {
    "name": "image_options",
    "description": (
        "What make_image can draw with: the models on the image node, by family "
        "(Krea 2, SDXL - Pony / Illustrious -, SD 1.5; video models are listed "
        "but not drawn with yet), and with a model named: how it wants to be "
        "prompted (its style and a guide), its profile, and the LoRAs that fit "
        "it - 'good' (made for it) or 'may work' (same family, another base) - "
        "with their trigger words."),
    "input_schema": {
        "type": "object",
        "properties": {"model": {"type": "string", "description": "A model's file name: list what goes with it."}},
    },
}


def _err(error: str, hint: str = "") -> str:
    return json.dumps({"error": error, **({"hint": hint} if hint else {})})


def _settings(config: Optional[McpConfig]) -> dict[str, Any]:
    s = config.for_tool("make_image") if config else None
    get = (lambda k, d: s.get_string(k, d)) if s else (lambda k, d: d)
    return {"enabled": str(get("enabled", "true")).lower() not in ("false", "0", "no", "off"),
            "daily_limit": int(get("daily_limit", str(DEFAULT_DAILY_LIMIT)) or 0),
            "max_side": int(get("max_side", str(DEFAULT_MAX_SIDE)) or DEFAULT_MAX_SIDE),
            "gallery": get("gallery", ""),
            "source": getattr(config, "source", "") if config else ""}


def gallery_dir(s: dict[str, Any]) -> Path:
    """The configured folder; else the instance's stores/workbench/gallery
    (the config sits in <root>/apps/workbench); else ~/.seren/gallery."""
    if s["gallery"]:
        return Path(os.path.expanduser(s["gallery"]))
    src = Path(s["source"]) if s["source"] and os.path.isfile(s["source"]) else None
    if src is not None and src.parent.parent.name == "apps":
        return src.parent.parent.parent / "stores" / "workbench" / "gallery"
    return Path.home() / ".seren" / "gallery"


def drawn_today(gallery: Path, today: dt.date) -> int:
    d = gallery / today.isoformat()
    return sum(1 for p in d.glob("*.json")) if d.is_dir() else 0


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "picture"


async def make_image(
    prompt: str,
    why: str = "",
    model: str = "",
    loras: Optional[list] = None,
    negative: Optional[str] = None,
    width: int = 0,
    height: int = 0,
    count: int = 1,
    seed: Optional[int] = None,
    runtime_host: httpx.AsyncClient = None,
    config: Optional[McpConfig] = None,
    **kwargs,
) -> str:
    s = _settings(config)
    if not s["enabled"]:
        return _err("Pictures are turned off here.", "tools.make_image.enabled in the Workbench's config.")
    if not (prompt or "").strip():
        return _err("Say what to draw.")
    for side in (width, height):
        if side and int(side) > s["max_side"]:
            return _err(f"{side} is bigger than this box draws ({s['max_side']}).",
                        "tools.make_image.max_side raises it.")
    try:
        count = max(1, min(int(count or 1), MAX_COUNT))
    except (TypeError, ValueError):
        count = 1
    gallery = gallery_dir(s)
    today = dt.date.today()
    done = drawn_today(gallery, today)
    if s["daily_limit"] and done + count > s["daily_limit"]:
        return _err(f"That would make {done + count} pictures today; the day's limit is {s['daily_limit']}.",
                    "Tomorrow, or fewer at once - tools.make_image.daily_limit sets it.")

    body: dict[str, Any] = {"prompt": prompt, "count": count, "holder": "workbench-make_image", "save_as": "wren"}
    for k, v in (("model", model), ("loras", loras), ("negative", negative), ("width", width),
                 ("height", height), ("seed", seed)):
        if v not in (None, "", 0, []):
            body[k] = v
    try:
        resp = await runtime_host.post("/api/v1/image", json=body, timeout=DRAW_TIMEOUT)
    except httpx.TimeoutException:
        return _err(f"Lodestar did not answer within {DRAW_TIMEOUT}s.", "The image node may be busy; try again later.")
    except httpx.RequestError as ex:
        return _err(f"Lodestar unreachable: {ex}", "Check Lodestar is running.")
    try:
        out = resp.json()
    except ValueError:
        return _err(f"Lodestar returned HTTP {resp.status_code} and no JSON.", resp.text[:300])
    if resp.status_code == 404:
        return _err("This Lodestar has no image route yet.", "It needs the release with /api/v1/image.")
    if not resp.is_success or not out.get("ok"):
        return _err(out.get("error") or f"Lodestar returned HTTP {resp.status_code}.",
                    str(out.get("detail") or out.get("node_errors") or "")[:500])

    folder = gallery / today.isoformat()
    folder.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%H%M%S")
    note = {"asked": prompt, "why": why, "drew": out.get("prompt"), "negative": out.get("negative"),
            "model": out.get("model"), "base": out.get("base"), "loras": out.get("loras"), "seed": out.get("seed"),
            "width": out.get("width"), "height": out.get("height"), "node": out.get("node"),
            "seconds": out.get("seconds"), "at": dt.datetime.now().isoformat(timespec="seconds")}
    saved = []
    for i, im in enumerate(out.get("images") or []):
        ext = ".jpg" if "jpeg" in (im.get("mime") or "") else ".png"
        path = folder / f"{stamp}-{out.get('seed')}-{_slug(why or prompt)}{f'-{i + 1}' if count > 1 else ''}{ext}"
        path.write_bytes(base64.b64decode(im.get("data") or ""))
        path.with_suffix(".json").write_text(json.dumps({**note, "file": path.name}, indent=1), encoding="utf-8")
        saved.append(str(path))
    if not saved:
        return _err("The node finished but sent no picture back.", "get_recent_logs comfy shows what ComfyUI said.")
    print(f"[mcp-audit] MakeImage: {len(saved)} picture(s), seed {out.get('seed')}, {out.get('model')}, "
          f"{out.get('seconds')}s", file=sys.stderr)
    return json.dumps({"ok": True, "saved": saved, "seed": out.get("seed"), "model": out.get("model"),
                       "loras": out.get("loras"), "prompt": out.get("prompt"), "notes": out.get("notes") or [],
                       "seconds": out.get("seconds"),
                       "today": f"{done + len(saved)}" + (f" of {s['daily_limit']}" if s["daily_limit"] else "")})


async def image_options(model: str = "", runtime_host: httpx.AsyncClient = None, **kwargs) -> str:
    try:
        if model:
            resp = await runtime_host.get("/api/v1/comfy/compatible", params={"model": model}, timeout=150)
        else:
            resp = await runtime_host.get("/api/v1/comfy/catalog", timeout=150)
        out = resp.json()
    except httpx.RequestError as ex:
        return _err(f"Lodestar unreachable: {ex}", "Check Lodestar is running.")
    except ValueError:
        return _err(f"Lodestar returned HTTP {resp.status_code} and no JSON.")
    if not resp.is_success:
        return _err(out.get("error") or f"Lodestar returned HTTP {resp.status_code}.", str(out.get("detail") or ""))
    if model:
        return json.dumps(out)
    models = [{"name": f["name"], "family": f.get("arch"), "base": f.get("base"), "kind": f.get("kind"),
               "drawable_on": f.get("drawable_on", [])}
              for f in (out.get("files") or {}).values()
              if f.get("role") in ("checkpoint", "diffusion") and not f.get("error")]
    models.sort(key=lambda m: (not m["drawable_on"], m["kind"] != "image", m["family"] or "", m["name"]))
    return json.dumps({"nodes": [{"node": n.get("node"), "families": n.get("families") or "all",
                                  "summary": n.get("summary")} for n in out.get("nodes") or []],
                       "models": models,
                       "hint": "drawable_on is empty for a model no image node can run yet. "
                               "image_options(model=...) lists the LoRAs that fit one."})
