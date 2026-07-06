"""
Route tests — the HTTP surface via TestClient.

Covers the info endpoints, tools, state toggles, config, logs, and the viewer.
Auth tests live in test_auth.py.
"""
from __future__ import annotations

import pytest


# ── Info ────────────────────────────────────────────────────────────────

def test_health(client):
    assert client.get("/health").status_code == 200
    assert client.get("/health").json()["ok"] is True


def test_root_reports_service_and_counts(client):
    body = client.get("/").json()
    assert body["service"] == "SerenWorkbench"
    assert body["version"]
    assert body["tools_count"] >= 16
    assert body["builtin_count"] >= 16
    assert body["dynamic_count"] == 0  # no YAML manifests in CI


# ── Tools ───────────────────────────────────────────────────────────────

def test_list_tools_returns_schemas(client):
    body = client.get("/tools").json()
    assert body["count"] >= 16
    for t in body["tools"]:
        assert "name" in t
        assert "description" in t
        assert "type" in t
        assert "enabled" in t
        assert isinstance(t["parameters"], list)
        # Each param should have at least a name and type
        for p in t["parameters"]:
            assert "name" in p
            assert "type" in p


# ── Tool State ──────────────────────────────────────────────────────────

def test_get_state_snapshot(client):
    snap = client.get("/tools/state").json()
    assert "tools" in snap
    for t in snap["tools"]:
        assert "name" in t
        assert "enabled" in t
        assert isinstance(t["enabled"], bool)


def test_disable_tool(client):
    r = client.post("/tools/state", json={"tool": "fetch_url", "enabled": False})
    assert r.status_code == 200
    assert r.json()["enabled"] is False

    # Verify
    snap = client.get("/tools/state").json()
    for t in snap["tools"]:
        if t["name"] == "fetch_url":
            assert t["enabled"] is False
            break


def test_enable_tool(client):
    # First disable, then re-enable
    client.post("/tools/state", json={"tool": "recall", "enabled": False})
    r = client.post("/tools/state", json={"tool": "recall", "enabled": True})
    assert r.status_code == 200
    assert r.json()["enabled"] is True

    snap = client.get("/tools/state").json()
    for t in snap["tools"]:
        if t["name"] == "recall":
            assert t["enabled"] is True
            break


def test_toggle_nonexistent_tool(client):
    r = client.post("/tools/state", json={"tool": "does_not_exist", "enabled": False})
    assert r.status_code == 404


# ── Config ──────────────────────────────────────────────────────────────

def test_config_endpoint(client):
    r = client.get("/config")
    assert r.status_code == 200
    data = r.json()
    assert "server" in data
    assert "tls" in data
    assert "dashboard" in data
    assert "tool_overrides" in data
    # Server should have port/host
    assert "port" in data["server"]
    assert "host" in data["server"]


# ── Logs ────────────────────────────────────────────────────────────────

def test_logs_endpoint(client):
    r = client.get("/logs")
    assert r.status_code == 200
    data = r.json()
    assert "entries" in data
    assert "count" in data
    # Log starts empty — count should be 0
    assert data["count"] == 0
    assert data["entries"] == []


def test_logs_with_limit(client):
    r = client.get("/logs?limit=10")
    assert r.status_code == 200


# ── Viewer ──────────────────────────────────────────────────────────────

def test_viewer_status(client):
    r = client.get("/viewer")
    assert r.status_code == 200
    assert r.headers["content-type"] == "text/html; charset=utf-8"


def test_viewer_contains_brand(client):
    html = client.get("/viewer").text
    assert "Seren" in html
    assert "Workbench" in html


def test_viewer_contains_accent(client):
    html = client.get("/viewer").text
    # The accent is embedded as a CSS variable or inline style
    assert "#8e9aaf" in html or "8e9aaf" in html


def test_viewer_contains_tool_list_area(client):
    html = client.get("/viewer").text
    # The body.html should have a section for the tool list
    assert "tools" in html.lower() or "tool-list" in html
