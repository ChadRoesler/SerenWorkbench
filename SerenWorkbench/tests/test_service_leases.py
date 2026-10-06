"""
ensure_service_running / release_service through Lodestar's leases
(seren_sinew.orchestration), and the Workbench letting go of what a session
forgot (seren_workbench.holds).

The Lodestar here is a stand-in that speaks the real messages and keeps the
REAL book (seren_sinew.orchestration.Leases), so "who holds what, and is it
stopped" is decided by the code that decides it in the cluster.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from seren_sinew.orchestration import EnsureRequest, EnsureResult, Leases, ReleaseRequest

from seren_workbench.holds import Holds
from seren_workbench.models.tools.ensure_service_running_tool import ensure_service_running, release_service


class FakeLodestar:
    """ensure / release with the real lease book; `old=True` is a Lodestar
    from before either route existed."""

    def __init__(self, old: bool = False, running: bool = False, fail: str = ""):
        self.old, self.fail = old, fail
        self.running = running
        self.book = Leases()
        self.seen: list[tuple[str, str, dict]] = []
        self.stopped = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content or b"{}") if request.content else {}
        self.seen.append((request.method, path, body))
        if path.endswith("/ensure") and not self.old:
            req = EnsureRequest.from_dict(body)
            if self.fail:
                return httpx.Response(200, json=EnsureResult.failed("llama", self.fail, "node-a").to_dict())
            started = not self.running
            self.running = True
            out = EnsureResult(ok=True, service="llama", node="node-a", ready=True, started=started,
                               already_running=not started, base_url="http://192.0.2.101:8080", port=8080,
                               waited_seconds=41.5 if started else 0.0)
            out.holders = self.book.acquire("node-a", "llama", req.holder or "anonymous", started=started)
            return httpx.Response(200, json=out.to_dict())
        if path.endswith("/release") and not self.old:
            req = ReleaseRequest.from_dict(body)
            node = req.node or self.book.node_of("llama", req.holder)
            if not node:
                return httpx.Response(200, json={"ok": True, "service": "llama"})
            left, should_stop = self.book.release(node, "llama", req.holder)
            if should_stop:
                self.running = False
                self.stopped += 1
            return httpx.Response(200, json={"ok": True, "service": "llama", "node": node, "holders": left,
                                             "stopped": should_stop})
        if path.endswith("/status"):
            return httpx.Response(200, json={"node": "node-a", "status": {"running": self.running}})
        if path.endswith("/start"):
            self.running = True
            return httpx.Response(200, json={"node": "node-a"})
        return httpx.Response(404, json={"detail": "Not Found"})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler), base_url="http://lodestar")


def _run(coro):
    return asyncio.run(coro)


def test_ensure_takes_a_lease_and_release_gives_it_back():
    lode, holds = FakeLodestar(), Holds()

    async def go():
        async with lode.client() as c:
            out = json.loads(await ensure_service_running("llama", reason="a sleep", runtime_host=c, holds=holds))
            assert out["action"] == "started" and out["node"] == "node-a" and out["running"] is True
            assert out["base_url"] == "http://192.0.2.101:8080", "where to send requests, without asking again"
            assert out["leased"] is True and out["held_as"] == "seren-workbench"
            assert out["holders"] == ["seren-workbench"] and out["let_go_after_minutes"] == 120.0
            method, path, body = lode.seen[0]
            assert (method, path) == ("POST", "/api/v1/service/llama/ensure")
            assert body["holder"] == "seren-workbench" and body["reason"] == "a sleep" and body["wait_seconds"] == 120.0
            assert [h["service"] for h in holds.snapshot()] == ["llama"]

            # the hippocampus takes it too, and lets go: ours keeps it up
            lode.book.acquire("node-a", "llama", "seren-hippocampus", started=False)
            lode.book.release("node-a", "llama", "seren-hippocampus")
            assert lode.running is True

            done = json.loads(await release_service("llama", runtime_host=c, holds=holds))
            assert done == {"service": "llama", "released": True, "stopped": True, "node": "node-a",
                            "still_held_by": []}
            assert lode.running is False and holds.snapshot() == []
            # letting go of nothing is harmless
            again = json.loads(await release_service("llama", runtime_host=c, holds=holds))
            assert again["released"] is True and again["stopped"] is False
    _run(go())


def test_a_service_that_was_already_up_is_never_stopped_by_a_release():
    lode, holds = FakeLodestar(running=True), Holds()

    async def go():
        async with lode.client() as c:
            out = json.loads(await ensure_service_running("llama", runtime_host=c, holds=holds))
            assert out["action"] == "already_running"
            done = json.loads(await release_service("llama", runtime_host=c, holds=holds))
            assert done["released"] is True and done["stopped"] is False and lode.running is True
    _run(go())


def test_a_service_that_did_not_come_up_is_an_error_and_nothing_is_held():
    lode, holds = FakeLodestar(fail="llama on node-a did not answer in time (240s)"), Holds()

    async def go():
        async with lode.client() as c:
            out = json.loads(await ensure_service_running("llama", runtime_host=c, holds=holds))
            assert set(out) == {"error", "hint"} and "did not answer in time" in out["error"]
            assert holds.snapshot() == []
            bad = json.loads(await ensure_service_running("rm -rf", runtime_host=c, holds=holds))
            assert "not in the allowed-services list" in bad["error"]
            assert len(lode.seen) == 1, "a service that is not allowed is never asked for"
    _run(go())


def test_a_lodestar_from_before_leases_is_driven_the_old_way():
    lode, holds = FakeLodestar(old=True), Holds()

    async def go():
        async with lode.client() as c:
            out = json.loads(await ensure_service_running("llama", timeout=5, runtime_host=c, holds=holds))
            assert out["action"] == "started" and out["leased"] is False and out["running"] is True
            assert [p for _, p, _ in lode.seen][:3] == ["/api/v1/service/llama/ensure", "/api/v1/service/llama/status",
                                                        "/api/v1/service/llama/start"]
            assert holds.snapshot() == [], "nothing is held on a Lodestar that keeps no leases"
            done = json.loads(await release_service("llama", runtime_host=c, holds=holds))
            assert done["released"] is False and "keeps no leases" in done["note"] and lode.running is True
    _run(go())


def test_the_timeout_is_capped_by_the_config():
    from seren_workbench.tool_config.mcp_config import McpConfig
    lode, holds = FakeLodestar(), Holds()
    cfg = McpConfig({"ensure_service_running": {"timeout": "45"}}, "test")

    async def go():
        async with lode.client() as c:
            await ensure_service_running("llama", timeout=9999, runtime_host=c, config=cfg, holds=holds)
            assert lode.seen[0][2]["wait_seconds"] == 45.0
    _run(go())


def test_a_hold_a_session_forgot_is_let_go_and_asking_again_renews_it():
    lode, holds = FakeLodestar(), Holds(hold_minutes=60)

    async def go():
        async with lode.client() as c:
            await ensure_service_running("llama", runtime_host=c, holds=holds)
            t0 = holds._held["llama"].renewed
            assert holds.overdue(now=t0 + 59 * 60) == []
            assert holds.overdue(now=t0 + 61 * 60) == ["llama"]
            # asked for again at minute 50: the clock starts over
            holds.note("llama", "node-a", now=t0 + 50 * 60)
            assert holds.overdue(now=t0 + 61 * 60) == []
            assert holds.overdue(now=t0 + 111 * 60) == ["llama"]
            # the session never comes back
            holds._held["llama"].renewed = t0 - 2 * 3600
            got = await holds.release_overdue(c)
            assert [g["stopped"] for g in got] == [True] and lode.running is False and holds.snapshot() == []
            assert lode.seen[-1][2]["reason"].startswith("held 60 minutes")
    _run(go())
    assert Holds(hold_minutes=0).overdue(now=10**12) == [], "0 = never let go on its own"


def test_shutting_down_lets_go_of_everything_and_a_dead_lodestar_does_not_raise():
    lode, holds = FakeLodestar(), Holds()

    async def go():
        async with lode.client() as c:
            await ensure_service_running("llama", runtime_host=c, holds=holds)
            got = await holds.release_all(c, "the Workbench is shutting down")
            assert got[0]["stopped"] is True and lode.running is False

        def boom(request):
            raise httpx.ConnectError("refused")
        holds.note("llama", "node-a")
        async with httpx.AsyncClient(transport=httpx.MockTransport(boom), base_url="http://lodestar") as dead:
            got = await holds.release(dead, "llama")
            assert got["ok"] is False and "could not be reached" in got["error"]
            assert holds.snapshot() == [], "dropped all the same: it is not retried forever"
    _run(go())


def test_the_tools_are_registered_and_holds_are_shown(make_client, tmp_path):
    from seren_workbench.config import WorkbenchConfig
    cfg = WorkbenchConfig()
    cfg.dashboard.tools_dir = str(tmp_path / "tools")
    client = make_client(cfg)
    tools = {t["name"]: t for t in client.get("/tools").json()["tools"]}
    assert {"ensure_service_running", "release_service"} <= set(tools)
    assert tools["release_service"]["toolbox"] == "Services"
    assert [p["name"] for p in tools["ensure_service_running"]["parameters"]] == ["service", "timeout", "reason"], \
        "the injected clients and the hold book are not parameters a model sees"
    client.app.state.holds.note("llama", "node-a")
    held = client.get("/components").json()["holding"]
    assert held[0]["service"] == "llama" and held[0]["held_as"] == "seren-workbench"
    client.app.state.holds.drop("llama")
