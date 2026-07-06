"""
Auth tests — bearer token enforcement.

Same pattern as SerenMemory/tests/test_auth.py and SerenLoci/tests/test_auth.py.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from seren_workbench.app import create_app
from seren_meninges import ServerConfig, TlsConfig
from seren_workbench.config import WorkbenchConfig, DashboardConfig


@pytest.fixture
def auth_client():
    """TestClient with a bearer token configured."""
    cfg = WorkbenchConfig(
        server=ServerConfig(bearer_token="sekret"),
        tls=TlsConfig(),
        dashboard=DashboardConfig(),
    )
    app = create_app(cfg)
    with TestClient(app) as c:
        yield c


# ── Public routes (no auth required) ────────────────────────────────────

def test_health_is_public(auth_client):
    """Health endpoint should be accessible without a token."""
    r = auth_client.get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


# ── Protected routes ────────────────────────────────────────────────────

def test_root_is_public(auth_client):
    """Root endpoint is intentionally public (DEFAULT_PUBLIC_PATHS)."""
    r = auth_client.get("/")
    assert r.status_code == 200


def test_viewer_is_public(auth_client):
    """Viewer endpoint is intentionally public (DEFAULT_PUBLIC_PATHS)."""
    r = auth_client.get("/viewer")
    assert r.status_code == 200


def test_config_requires_auth(auth_client):
    r = auth_client.get("/config")
    assert r.status_code == 401


def test_logs_requires_auth(auth_client):
    r = auth_client.get("/logs")
    assert r.status_code == 401


# ── Valid token access ──────────────────────────────────────────────────

def test_root_with_valid_token(auth_client):
    r = auth_client.get("/", headers={"Authorization": "Bearer sekret"})
    assert r.status_code == 200
    assert r.json()["service"] == "SerenWorkbench"


def test_tools_with_valid_token(auth_client):
    r = auth_client.get("/tools", headers={"Authorization": "Bearer sekret"})
    assert r.status_code == 200


def test_viewer_with_valid_token(auth_client):
    r = auth_client.get("/viewer", headers={"Authorization": "Bearer sekret"})
    assert r.status_code == 200


def test_toggle_with_valid_token(auth_client):
    r = auth_client.post("/tools/state",
                         json={"tool": "remember", "enabled": False},
                         headers={"Authorization": "Bearer sekret"})
    assert r.status_code == 200
    assert r.json()["enabled"] is False


def test_config_with_valid_token(auth_client):
    r = auth_client.get("/config", headers={"Authorization": "Bearer sekret"})
    assert r.status_code == 200


def test_logs_with_valid_token(auth_client):
    r = auth_client.get("/logs", headers={"Authorization": "Bearer sekret"})
    assert r.status_code == 200
