# ════════════════════════════════════════════════════════════════════════
#  TimeTool - cure for LLM time-blindness.
#
#  LLMs lie about time. This is the cheapest possible fix. The LLM can
#  call this any time it needs to know what time it is, and gets a real
#  answer. Cost: one syscall.
# ════════════════════════════════════════════════════════════════════════

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta


TOOL_DEFINITION = {
    "name": "get_current_time",
    "description": (
        "Returns the current date and time. Use this whenever the user asks "
        "about time, day, date, or anything time-relative ('how long ago "
        "was that', 'is it morning'). Do NOT guess - your training cutoff "
        "means you don't actually know what time it is. Returns JSON with "
        "iso_utc, iso_local, day_of_week, timezone, and unix_timestamp."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}


async def get_current_time(**kwargs) -> str:
    now_utc = datetime.now(timezone.utc)
    now_local = datetime.now()

    result = {
        "iso_utc": now_utc.isoformat(),
        "iso_local": now_local.isoformat(),
        "day_of_week": now_local.strftime("%A"),
        "timezone": str(now_local.tzinfo) if now_local.tzinfo else "local",
        "timezone_offset_hours": now_local.utcoffset().total_seconds() / 3600 if now_local.tzinfo else 0,
        "unix_timestamp": int(now_utc.timestamp()),
        "human_readable": now_local.strftime("%A, %B %d, %Y at %I:%M %p"),
    }

    return json.dumps(result, indent=2)
