# ═══════════════════════════════════════════════════════════════
#  WithImages - a tool's answer that is text AND pictures.
#
#  A builtin answers with a JSON string; every caller (the MCP wrapper, the
#  tests, anything that json.loads it) reads it as one. A tool that also has
#  a picture to show returns this instead: still that string - nothing that
#  reads the text changes - with the pictures riding along as an attribute.
#  The MCP wrapper (mcp/server.py) sends them as image content beside the
#  text, so the client SEES them: the model looks at what it drew, and a
#  harness like Symposium keeps them in the chat - where a path on the
#  Workbench's box would mean nothing (15 Oct 2026: every picture make_image
#  drew stayed on the NUC).
# ═══════════════════════════════════════════════════════════════

from __future__ import annotations

from typing import Optional

# Per answer. One image block is capped by the model APIs at about 5 MB
# base64 (3.75 MB of bytes); the total keeps one tool result from carrying
# a whole batch at full size.
MAX_IMAGE_BYTES = 3_500_000
MAX_TOTAL_BYTES = 8_000_000


class WithImages(str):
    """A str (the tool's text) with `.images`: [(bytes, format)], format
    "png" / "jpeg" / "webp" / "gif". Pictures over the caps are left out and
    counted in `.skipped`."""

    images: list[tuple[bytes, str]]
    skipped: int

    def __new__(cls, text: str, images: Optional[list[tuple[bytes, str]]] = None) -> "WithImages":
        self = super().__new__(cls, text)
        kept, total, skipped = [], 0, 0
        for data, fmt in images or []:
            if not data or len(data) > MAX_IMAGE_BYTES or total + len(data) > MAX_TOTAL_BYTES:
                skipped += 1
                continue
            kept.append((data, fmt))
            total += len(data)
        self.images, self.skipped = kept, skipped
        return self


def image_format(data: bytes) -> Optional[str]:
    """The format by its first bytes; None for what is not a picture."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return None
