"""
Shared test fixtures for SerenWorkbench.

Follows the same pattern as SerenMemory/tests/conftest.py and SerenLoci/tests/conftest.py:

    - ``make_client`` factory fixture — creates a TestClient backed by a
      fresh ``WorkbenchConfig`` with a random port (not used by TestClient).
      Tears down cleanly after the test.

    - ``client`` fixture — convenience fixture that calls ``make_client``
      with a default config.

What's NOT here:
    - No embedder — the MCP server doesn't use embeddings.
    - No store — the MCP server is a tool surface, not a data service.
      Tools call external services via HTTP; those calls are mocked in
      the tool-level tests.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from seren_workbench.app import create_app
from seren_workbench.config import WorkbenchConfig, load_config


@pytest.fixture(autouse=True)
def offline_update_checks(monkeypatch):
    """No test may talk to pypi.org.

    ``GET /`` carries the update status and update checking is ON by default,
    so without this every test that touches the root route would make a real
    network call - slow, flaky offline, and rude to someone else's server.

    Patching the CLASS method rather than an env var is deliberate: some tests
    build a WorkbenchConfig directly instead of going through load_config, so
    an env override wouldn't reach them. The checker still runs and still
    returns a well-formed status - just status="error" instead of a real
    answer, which is exactly what a box with no internet would see.
    """
    try:
        from seren_meninges.updates import UpdateChecker
    except ImportError:
        return  # meninges < 2.0.0, nothing to muzzle

    async def _no_network(self, distribution):
        raise ConnectionError("network disabled in tests")

    # Must be patched BEFORE any UpdateChecker is constructed - __init__ binds
    # self._fetch = fetcher or self._fetch_from_index. autouse + function scope
    # puts it in place before the app lifespan runs.
    monkeypatch.setattr(UpdateChecker, "_fetch_from_index", _no_network)


@pytest.fixture
def make_client():
    """Factory fixture. Call it with an WorkbenchConfig to get a fully wired
    TestClient that tears down cleanly after the test.

    Usage in a per-file client fixture::

        @pytest.fixture
        def client(make_client):
            return make_client(WorkbenchConfig(...))

    ``raise_server_exceptions`` is forwarded as a kwarg when needed.
    """
    _clients: list[TestClient] = []

    def _factory(cfg: WorkbenchConfig | None = None,
                 raise_server_exceptions: bool = False) -> TestClient:
        cfg = cfg or load_config()
        app = create_app(cfg)
        tc = TestClient(app, raise_server_exceptions=raise_server_exceptions)
        tc.__enter__()
        _clients.append(tc)
        return tc

    yield _factory

    for tc in _clients:
        try:
            tc.__exit__(None, None, None)
        except Exception:  # noqa: BLE001
            pass


@pytest.fixture
def client(make_client):
    """Convenience fixture: a default TestClient."""
    return make_client()
