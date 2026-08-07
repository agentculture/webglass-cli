"""Shared pytest fixtures for the webglass-cli test suite.

Currently this only wires up the local fixture-site HTTP server (see
``tests/fixtures/server.py``): a stdlib-only ``http.server`` instance bound
to 127.0.0.1 on an OS-assigned ephemeral port. It serves the deterministic
and hostile pages under ``tests/fixtures/pages/`` -- clean, throwing,
keydown-logging, agent-state-node, redirect chains (including a
redirect-to-private-target), spoofed-console, and boilerplate-heavy -- so the
default suite never depends on a live public website (issue #1 section 18;
CLAUDE.md "Test ownership").
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.fixtures.server import FixtureSite


@pytest.fixture(scope="session")
def fixture_site() -> Iterator[str]:
    """Start the fixture HTTP server for the test session; yield its base URL.

    Session-scoped: the server is stateless per request (any client-side
    state, e.g. the keydown log, lives only in a browser that later loads
    these pages -- M2 territory, not this server), so one instance can be
    shared across every test that needs it.
    """
    site = FixtureSite()
    base_url = site.start()
    try:
        yield base_url
    finally:
        site.stop()
