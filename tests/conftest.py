"""Shared pytest fixtures for the webglass-cli test suite.

Two things live here:

* the local fixture-site HTTP server (see ``tests/fixtures/server.py``): a
  stdlib-only ``http.server`` instance bound to 127.0.0.1 on an OS-assigned
  ephemeral port. It serves the deterministic and hostile pages under
  ``tests/fixtures/pages/`` -- clean, throwing, keydown-logging,
  agent-state-node, redirect chains (including a redirect-to-private-target),
  spoofed-console, and boilerplate-heavy -- so the default suite never
  depends on a live public website (issue #1 section 18; CLAUDE.md "Test
  ownership");
* an autouse guard that keeps every test's *session* state, and its browser
  posture, inside the test run (see :func:`isolated_session_state`).
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from tests.fixtures.server import FixtureSite
from webglass.adapters.session_store import ALLOW_UNSANDBOXED_ENV, STATE_DIR_ENV
from webglass.cli._factory import BROWSER_BACKEND_ENV


@pytest.fixture(autouse=True)
def isolated_session_state(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Give every test its own session state directory and no browser.

    ``webglass.adapters.session_store`` resolves its state root from
    ``$WEBGLASS_STATE_DIR``, else ``$XDG_STATE_HOME/webglass``, else
    ``~/.local/state/webglass``. Without this fixture, any test that runs
    ``session create`` through the real CLI factory would write records into
    the developer's own home directory and read back leftovers from previous
    runs -- and cross-test session leakage is the exact failure this store
    exists to make impossible.

    The browser-selection and sandbox-opt-in variables are cleared for the
    same reason in the other direction: a developer who exported
    ``WEBGLASS_BROWSER_BACKEND=playwright`` in their shell must not thereby
    turn the default suite into one that launches Chromium. Tests that want a
    browser set these explicitly, in the subprocess environment they build.
    """
    monkeypatch.setenv(STATE_DIR_ENV, str(tmp_path_factory.mktemp("webglass-state")))
    monkeypatch.delenv(BROWSER_BACKEND_ENV, raising=False)
    monkeypatch.delenv(ALLOW_UNSANDBOXED_ENV, raising=False)
    assert os.environ[STATE_DIR_ENV]  # nosec B101 - guards the fixture itself


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
