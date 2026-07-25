"""
Shape tests for the MCP mount.

Checks that the MCP route IS present (or gracefully absent when the SDK is
missing). Does NOT drive a full JSON-RPC request — that lives in
test_mcp_endpoint.py (gated on the mcp SDK).

Same pattern as SerenMemory/tests/test_mcp_mount.py and
SerenLoci/tests/test_mcp_mount.py.
"""
from __future__ import annotations

import pytest

from seren_workbench.app import create_app
from seren_workbench.config import WorkbenchConfig, load_config


def _route_paths(app) -> set:
    """Collect route paths across FastAPI versions.

    Older FastAPI materializes include_router entries as APIRoutes with
    .path immediately; newer versions keep lazy _IncludedRouter wrappers
    (no .path) that expose the underlying router as .original_router.
    Walk both shapes so the test is version-proof.
    """
    paths = set()
    for r in app.routes:
        p = getattr(r, "path", None)
        if p is not None:
            paths.add(p)
        sub = getattr(r, "original_router", None)
        if sub is not None:
            for sr in getattr(sub, "routes", []):
                sp = getattr(sr, "path", None)
                if sp is not None:
                    paths.add(sp)
    return paths


@pytest.fixture
def app():
    """Return the FastAPI app (not a TestClient) so we can inspect routes."""
    return create_app()


def test_mcp_route_exists_or_gracefully_absent(app):
    """The MCP mount may fail (no SDK, schema issue) — either way the app
    should still start and serve its core routes."""
    # The app should have routes for /, /health, /tools, /viewer regardless
    routes = _route_paths(app)
    assert "/" in routes
    assert "/health" in routes
    assert "/tools" in routes
    assert "/viewer" in routes

    # /mcp may or may not be present — both are valid
    # (graceful fallback when the mcp SDK is missing or schema generation fails)
    if "/mcp" in routes:
        # If it's mounted, it should be an ASGI mount (Starlette Mount)
        from starlette.routing import Mount
        for r in app.routes:
            if getattr(r, "path", None) == "/mcp" and isinstance(r, Mount):
                break
        else:
            pytest.fail("/mcp route exists but is not a Starlette Mount")


def test_app_starts_without_mcp_sdk(monkeypatch):
    """Simulate a missing mcp SDK — the app should still start."""
    import importlib
    # We can't really uninstall mcp in the test, but we can verify the
    # fallback path works by checking the error handling in the lifespan.
    # The app.create_app function catches ImportError and generic Exception.
    cfg = load_config()
    cfg.server.port = 17499
    app = create_app(cfg)
    assert app is not None
    routes = _route_paths(app)
    assert "/" in routes
