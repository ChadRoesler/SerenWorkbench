"""
seren_workbench.holds
════════════════════════════════════════════════════════════════════════

The services this Workbench is holding up, and letting go of them.

`ensure_service_running` asks Lodestar for a service and Lodestar keeps a
LEASE in the Workbench's name (seren_sinew.orchestration): the service stays
up while anyone holds it and is stopped when the last holder lets go, if an
ensure is what started it. `release_service` is the letting go.

A lease has no clock of its own. A session that asks for llama and ends -
out of context, closed, woken for one job and done - never releases it, and
one GPU then carries a model nobody is using while the next thing that needs
the memory cannot have it. So the Workbench remembers what it holds and lets
go on the model's behalf:

  - after tools.ensure_service_running.hold_minutes (default 120) with no
    new ensure for that service. Asking again renews it; 0 = never.
  - when the Workbench shuts down. Its memory of the holds goes with it, and
    a lease nobody remembers is one nobody will ever release.

In memory, like Lodestar's own book: a Workbench that is killed outright
leaves its leases behind, and Lodestar restarting forgets them all.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

log = logging.getLogger(__name__)

DEFAULT_HOLDER = "seren-workbench"
DEFAULT_HOLD_MINUTES = 120.0
CHECK_SECONDS = 60.0
RELEASE_TIMEOUT = 30.0


@dataclass
class Hold:
    service: str
    node: str = ""
    since: float = 0.0          # the first ensure
    renewed: float = 0.0        # the latest one


class Holds:
    def __init__(self, holder: str = DEFAULT_HOLDER, hold_minutes: float = DEFAULT_HOLD_MINUTES) -> None:
        self.holder = (holder or DEFAULT_HOLDER).strip()[:120]
        self.hold_minutes = max(0.0, float(hold_minutes))
        self._held: dict[str, Hold] = {}

    # ── the book ──────────────────────────────────────────────────────────
    def note(self, service: str, node: str = "", now: Optional[float] = None) -> Hold:
        """An ensure came back ready: held from now (or held a while longer)."""
        now = time.time() if now is None else now
        h = self._held.get(service)
        if h is None:
            h = self._held[service] = Hold(service=service, node=node, since=now, renewed=now)
        else:
            h.renewed, h.node = now, node or h.node
        return h

    def drop(self, service: str) -> Optional[Hold]:
        return self._held.pop(service, None)

    def overdue(self, now: Optional[float] = None) -> list[str]:
        if self.hold_minutes <= 0:
            return []
        now = time.time() if now is None else now
        return [s for s, h in self._held.items() if now - h.renewed >= self.hold_minutes * 60.0]

    def snapshot(self, now: Optional[float] = None) -> list[dict[str, Any]]:
        now = time.time() if now is None else now
        out = []
        for h in self._held.values():
            row: dict[str, Any] = {"service": h.service, "node": h.node, "held_as": self.holder,
                                   "held_minutes": round((now - h.since) / 60.0, 1)}
            if self.hold_minutes > 0:
                row["let_go_in_minutes"] = round(max(0.0, self.hold_minutes - (now - h.renewed) / 60.0), 1)
            out.append(row)
        return out

    # ── letting go ────────────────────────────────────────────────────────
    async def release(self, lodestar: Any, service: str, reason: str = "") -> dict[str, Any]:
        """Tell Lodestar this Workbench is done with *service*. Returns the
        answer as a dict with at least ok / stopped / holders / error;
        "leases" is False when the Lodestar is too old to keep any. Never
        raises: the hold is dropped whatever Lodestar says, because a hold
        the Workbench keeps trying to release forever helps nobody."""
        from seren_sinew.orchestration import ReleaseRequest
        h = self.drop(service)
        req = ReleaseRequest(holder=self.holder, reason=reason[:200], node=h.node if h else "")
        try:
            resp = await lodestar.post(f"/api/v1/service/{service}/release", json=req.to_dict(),
                                       timeout=RELEASE_TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "service": service, "stopped": False, "holders": [], "leases": True,
                    "error": f"Lodestar could not be reached: {type(exc).__name__}: {exc}"}
        if resp.status_code in (404, 405):
            return {"ok": True, "service": service, "stopped": False, "holders": [], "leases": False, "error": ""}
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            body = {}
        if not resp.is_success or not isinstance(body, dict):
            return {"ok": False, "service": service, "stopped": False, "holders": [], "leases": True,
                    "error": f"Lodestar answered HTTP {resp.status_code}"}
        return {"ok": bool(body.get("ok")), "service": service, "node": str(body.get("node") or ""),
                "stopped": bool(body.get("stopped")), "holders": list(body.get("holders") or []),
                "leases": True, "error": str(body.get("error") or "")}

    async def release_overdue(self, lodestar: Any) -> list[dict[str, Any]]:
        out = []
        for service in self.overdue():
            got = await self.release(lodestar, service, f"held {self.hold_minutes:g} minutes with no new ensure")
            log.info("[seren-workbench] let go of '%s' after %g minutes unasked-for: %s", service, self.hold_minutes,
                     "stopped" if got.get("stopped") else (got.get("error") or "left running (others hold it, "
                                                           "or it was already up)"))
            out.append(got)
        return out

    async def release_all(self, lodestar: Any, reason: str) -> list[dict[str, Any]]:
        return [await self.release(lodestar, s, reason) for s in list(self._held)]

    async def run(self, lodestar: Any, check_seconds: float = CHECK_SECONDS) -> None:
        """The Workbench's lifetime, as a background task."""
        while True:
            await asyncio.sleep(check_seconds)
            try:
                await self.release_overdue(lodestar)
            except Exception as exc:  # noqa: BLE001 - the loop outlives one bad pass
                log.info("[seren-workbench] letting go of overdue services failed: %r", exc)
