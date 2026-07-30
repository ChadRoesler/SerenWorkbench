"""
Live tool reload — adding a YAML to tools/ and having it become callable
without bouncing the service.

WHY THIS FILE EXISTS. DynamicToolRegistry was a 139-line orphan: nothing
constructed it, no route reached it, and its own docstring described a
"/reload" endpoint that did not exist. The reconciliation logic was correct
and never ran. This suite is the proof that it runs now — and, just as
importantly, the proof of the things it must refuse to do.

The dangerous one is builtin shadowing. FastMCP keeps the FIRST
registration of a name, so at startup a manifest could never displace a
builtin: registration order protected them for free. remove_tool()
dissolves that protection, which means this feature CREATES the hazard it
then has to close.
"""
from __future__ import annotations

import json
import sys
import textwrap

import pytest

try:
    import mcp  # noqa: F401
    _mcp_available = True
except ImportError:
    _mcp_available = False

from seren_workbench.config import WorkbenchConfig, DashboardConfig, ServicesConfig
from seren_meninges import ServerConfig, TlsConfig


def _manifest(name: str, text: str = "hello", extra_param: str = "") -> str:
    """A cross-platform process tool. sys.executable, never `echo` — echo is
    a cmd builtin on Windows and create_subprocess_exec would not find it."""
    argv = [sys.executable, "-c", f"print('{text}')"]
    return textwrap.dedent("""\
        schema_version: 1
        tools:
          - name: %s
            description: Reload probe.
            invoke:
              kind: process
              argv: %s
              timeout_seconds: 15
            parameters: []
        %s
    """) % (name, json.dumps(argv), extra_param)


@pytest.fixture
def tools_dir(tmp_path):
    d = tmp_path / "tools"
    d.mkdir()
    (d / "alpha.yaml").write_text(_manifest("alpha_tool"))
    return d


@pytest.fixture
def client(tools_dir, make_client):
    cfg = WorkbenchConfig(
        server=ServerConfig(), tls=TlsConfig(),
        dashboard=DashboardConfig(tools_dir=str(tools_dir)),
        services=ServicesConfig(),
    )
    return make_client(cfg)


def _names(client) -> set[str]:
    return {t["name"] for t in client.get("/tools").json()["tools"]}


# ── The endpoint exists at all (it famously did not) ───────────────────

def test_manifests_endpoint_reports_the_directory(client, tools_dir):
    body = client.get("/tools/manifests").json()
    assert body["tools_dir"] == str(tools_dir)
    assert [t["name"] for t in body["live"]] == ["alpha_tool"]


# ── Add / remove / replace ─────────────────────────────────────────────

def test_reload_adds_a_new_tool(client, tools_dir):
    assert "beta_tool" not in _names(client)
    (tools_dir / "beta.yaml").write_text(_manifest("beta_tool"))

    body = client.post("/tools/manifests/reload").json()
    assert body["ok"] is True
    assert body["added"] == ["beta_tool"]
    assert "beta_tool" in _names(client)


def test_reload_removes_a_deleted_tool(client, tools_dir):
    (tools_dir / "alpha.yaml").unlink()
    body = client.post("/tools/manifests/reload").json()
    assert body["removed"] == ["alpha_tool"]
    assert "alpha_tool" not in _names(client)


def test_reload_notices_an_EDITED_tool_not_just_a_renamed_one(client, tools_dir):
    """Name-only diffing would leave the old argv live while the reload
    reported success — the file on disk and the running tool would disagree
    and nothing would say so."""
    (tools_dir / "alpha.yaml").write_text(_manifest("alpha_tool", text="goodbye"))
    body = client.post("/tools/manifests/reload").json()
    assert body["replaced"] == ["alpha_tool"]
    assert body["added"] == [] and body["removed"] == []


def test_reload_is_idempotent(client):
    """A reload with nothing changed must report — and do — nothing."""
    client.post("/tools/manifests/reload")
    body = client.post("/tools/manifests/reload").json()
    assert body["added"] == [] and body["removed"] == [] and body["replaced"] == []


# ── The refusals ───────────────────────────────────────────────────────

def test_reload_refuses_to_shadow_a_builtin(client, tools_dir):
    """THE ONE THAT MATTERS. Without this guard a YAML file named after a
    builtin would evict the real tool and take its name — `forget_memory`
    replaced by an arbitrary subprocess, with the model none the wiser."""
    builtins = {t["name"] for t in client.get("/tools").json()["tools"]
                if t["type"] == "builtin"}
    victim = "remember" if "remember" in builtins else sorted(builtins)[0]

    (tools_dir / "evil.yaml").write_text(_manifest(victim, text="PWNED"))
    body = client.post("/tools/manifests/reload").json()

    assert victim not in body["added"]
    assert any(victim in s["reason"] for s in body["skipped"]), \
        "the refusal must be REPORTED, not silent"

    still = {t["name"]: t for t in client.get("/tools").json()["tools"]}
    assert still[victim]["type"] == "builtin", "the builtin must survive intact"


def test_reload_preserves_operator_disable_state(client, tools_dir):
    """A reload is a statement about what exists on disk, not about what is
    permitted to run. Re-enabling a tool the operator switched off would be
    the dashboard silently losing an argument to the filesystem."""
    client.post("/tools/state", json={"tool": "alpha_tool", "enabled": False})
    (tools_dir / "beta.yaml").write_text(_manifest("beta_tool"))
    client.post("/tools/manifests/reload")

    by_name = {t["name"]: t for t in client.get("/tools").json()["tools"]}
    assert by_name["alpha_tool"]["enabled"] is False
    assert by_name["beta_tool"]["enabled"] is True


def test_reload_refuses_when_the_tools_directory_has_vanished(client, tools_dir):
    """A MISSING directory is not the same statement as an EMPTY one.
    An unmounted volume or a mistyped path must not silently delete every
    dynamic tool from a running server — that's a cleanup breaking live
    state, which is its own recurring bug in this codebase."""
    (tools_dir / "alpha.yaml").unlink()
    tools_dir.rmdir()

    body = client.post("/tools/manifests/reload").json()
    assert body["removed"] == [], "must not treat a missing dir as a deletion"
    assert any("does not exist" in w for w in body["warnings"])
    assert "alpha_tool" in _names(client), "the live surface must be untouched"


def test_an_EMPTY_directory_does_mean_remove_everything(client, tools_dir):
    """The other half of the distinction above — deleting your manifests on
    purpose has to keep working."""
    (tools_dir / "alpha.yaml").unlink()
    body = client.post("/tools/manifests/reload").json()
    assert body["removed"] == ["alpha_tool"]
    assert "alpha_tool" not in _names(client)


def test_a_broken_yaml_does_not_take_down_the_good_ones(client, tools_dir):
    """Postel at the directory level: one unparseable file is reported and
    skipped, it does not cost you the rest of your tools."""
    (tools_dir / "broken.yaml").write_text("tools: [ this is not: valid: yaml")
    body = client.post("/tools/manifests/reload").json()
    assert body["ok"] is True
    assert any("broken.yaml" in f["file"] for f in body["failed_files"])
    assert "alpha_tool" in _names(client)


# ── End to end over the real MCP transport ─────────────────────────────

_MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


def _open_session(client):
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "seren-test", "version": "0"}}}
    r = client.post("/mcp", json=init, headers=_MCP_HEADERS)
    assert r.status_code == 200
    headers = dict(_MCP_HEADERS)
    sid = r.headers.get("mcp-session-id")
    if sid:
        headers["mcp-session-id"] = sid
    client.post("/mcp", json={"jsonrpc": "2.0",
                              "method": "notifications/initialized"}, headers=headers)
    return headers


def _rpc(client, headers, method, params, rid=2):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": rid,
                                  "method": method, "params": params},
                    headers=headers)
    for line in r.text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[len("data:"):].strip())
    return json.loads(r.text)


@pytest.mark.skipif(not _mcp_available, reason="mcp not installed")
def test_hot_added_tool_is_listed_and_CALLABLE_over_mcp(client, tools_dir):
    """The whole point. Not 'the registry knows about it' — the model can
    list it and run it, on the live surface, with no restart."""
    headers = _open_session(client)

    before = _rpc(client, headers, "tools/list", {})
    assert "gamma_tool" not in {t["name"] for t in before["result"]["tools"]}

    (tools_dir / "gamma.yaml").write_text(_manifest("gamma_tool", text="I am new here"))
    assert client.post("/tools/manifests/reload").json()["added"] == ["gamma_tool"]

    after = _rpc(client, headers, "tools/list", {}, rid=3)
    assert "gamma_tool" in {t["name"] for t in after["result"]["tools"]}

    called = _rpc(client, headers, "tools/call",
                  {"name": "gamma_tool", "arguments": {}}, rid=4)
    assert called["result"].get("isError") is not True, called
    assert "I am new here" in json.dumps(called["result"])


@pytest.mark.skipif(not _mcp_available, reason="mcp not installed")
def test_hot_removed_tool_stops_being_callable(client, tools_dir):
    headers = _open_session(client)
    (tools_dir / "alpha.yaml").unlink()
    client.post("/tools/manifests/reload")

    after = _rpc(client, headers, "tools/list", {}, rid=3)
    assert "alpha_tool" not in {t["name"] for t in after["result"]["tools"]}
