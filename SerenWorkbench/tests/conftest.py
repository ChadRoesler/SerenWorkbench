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
