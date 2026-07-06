# ════════════════════════════════════════════════════════════════════════
#  ToolSection - one tool's config section in Python.
#
#  Typed getters are LENIENT: a missing key OR a value that won't parse
#  returns the caller's fallback - which IS the Nano-floor default,
#  living at the call site. Never raises.
# ════════════════════════════════════════════════════════════════════════

from __future__ import annotations

from typing import Dict


class ToolSection:
    """One tool's config overrides. Lenient typed getters."""

    _empty_instance: ToolSection | None = None

    @classmethod
    def empty(cls) -> ToolSection:
        if cls._empty_instance is None:
            cls._empty_instance = ToolSection({})
        return cls._empty_instance

    def __init__(self, kv: Dict[str, str]) -> None:
        self._kv = kv

    def get_string(self, key: str, fallback: str) -> str:
        v = self._kv.get(key)
        if v is not None and v.strip():
            return v
        return fallback

    def get_int(self, key: str, fallback: int) -> int:
        v = self._kv.get(key)
        if v is not None:
            try:
                return int(v)
            except (ValueError, TypeError):
                pass
        return fallback

    def get_long(self, key: str, fallback: int) -> int:
        return self.get_int(key, fallback)

    def get_double(self, key: str, fallback: float) -> float:
        v = self._kv.get(key)
        if v is not None:
            try:
                return float(v)
            except (ValueError, TypeError):
                pass
        return fallback

    def get_bool(self, key: str, fallback: bool) -> bool:
        v = self._kv.get(key)
        if v is None:
            return fallback
        lower = v.strip().lower()
        if lower in ("true", "yes", "on", "1"):
            return True
        if lower in ("false", "no", "off", "0"):
            return False
        return fallback

    @property
    def has_any(self) -> bool:
        return len(self._kv) > 0
