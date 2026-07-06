"""
Smoke test for SerenWorkbench.

Boots the app and exercises the full loop: root -> health -> tools -> viewer.
Does NOT need any external services — the MCP server is a tool surface that
dispatches to other Seren services; those calls are tested in the tool-level
tests.

Run:  pytest tests/test_smoke.py -v
"""
from __future__ import annotations

import pytest


def test_root_and_health(client):
    assert client.get("/health").json()["ok"] is True
    root = client.get("/").json()
    assert root["service"] == "SerenWorkbench"
    assert "version" in root
    assert root["tools_count"] >= 16
    assert root["builtin_count"] >= 16
    assert root["dynamic_count"] == 0  # no YAML manifests on disk


def test_tools_endpoint(client):
    r = client.get("/tools")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] >= 16
    tools = data["tools"]
    # Spot-check a few well-known tools
    names = {t["name"] for t in tools}
    assert "remember" in names
    assert "recall" in names
    assert "forget" in names
    assert "fetch_url" in names
    assert "search_the_web" in names

    # Each tool should have a name, description, type, and parameters
    for t in tools:
        assert t["name"]
        assert t["description"]
        assert t["type"] in ("builtin", "dynamic")
        assert isinstance(t["parameters"], list)


def test_tool_state_toggle(client):
    """Enable/disable cycle for a tool."""
    # Disable 'remember'
    r = client.post("/tools/state", json={"tool": "remember", "enabled": False})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["enabled"] is False

    # Snapshot should reflect the change
    snap = client.get("/tools/state").json()
    for t in snap["tools"]:
        if t["name"] == "remember":
            assert t["enabled"] is False
            break
    else:
        pytest.fail("remember not in snapshot")

    # Re-enable
    r = client.post("/tools/state", json={"tool": "remember", "enabled": True})
    assert r.status_code == 200
    assert r.json()["enabled"] is True

    # Verify the snapshot updated
    snap = client.get("/tools/state").json()
    for t in snap["tools"]:
        if t["name"] == "remember":
            assert t["enabled"] is True
            break


def test_tool_state_404(client):
    """Toggling a non-existent tool returns 404."""
    r = client.post("/tools/state", json={"tool": "nonexistent_tool", "enabled": False})
    assert r.status_code == 404
    assert r.json()["ok"] is False


def test_viewer_returns_html(client):
    r = client.get("/viewer")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "Seren" in r.text
    assert "8e9aaf" in r.text or "cool-grey" in r.text or "#8e9aaf" in r.text
    assert "mcp" in r.text.lower()
