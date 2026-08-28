"""Shared pytest fixtures for the webglass-cli test suite.

Three things live here:

* the local fixture-site HTTP server (see ``tests/fixtures/server.py``): a
  stdlib-only ``http.server`` instance bound to 127.0.0.1 on an OS-assigned
  ephemeral port. It serves the deterministic and hostile pages under
  ``tests/fixtures/pages/`` -- clean, throwing, keydown-logging,
  agent-state-node, redirect chains (including a redirect-to-private-target),
  spoofed-console, and boilerplate-heavy -- so the default suite never
  depends on a live public website (issue #1 section 18; CLAUDE.md "Test
  ownership");
* an autouse guard that keeps every test's *session* state, and its browser
  posture, inside the test run (see :func:`isolated_session_state`);
* a session-store seeding fixture (:func:`session_store` /
  :func:`seed_session_records`, backed by ``tests/helpers/session_seed.py``,
  build plan task t1) that later session-lifecycle tasks' tests build their
  "before state" from, instead of hand-rolling
  :class:`~webglass.adapters.session_store.FileSessionRecord` construction
  per test module.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator

import pytest

from tests.fixtures.server import FixtureSite
from tests.helpers.session_seed import seed_records
from webglass.adapters.brave import WEBGLASS_BRAVE_API_KEY_ENV
from webglass.adapters.session_store import (
    ALLOW_UNSANDBOXED_ENV,
    STATE_DIR_ENV,
    FileSessionRecord,
    FileSessionStore,
    default_sessions_dir,
)
from webglass.cli._factory import BROWSER_BACKEND_ENV, POLICY_PROFILE_ENV

#: What :func:`seed_session_records` hands back to a test: call it with the
#: same keyword arguments as :func:`tests.helpers.session_seed.seed_records`
#: (minus ``store``, which is bound already) any number of times against the
#: one store the fixture returned.
SeedSessionRecords = Callable[..., list[FileSessionRecord]]


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

    The browser posture is pinned for the same reason in the other direction.
    Since build plan t13 the *shipped* default is
    ``WEBGLASS_BROWSER_BACKEND=playwright`` — ``webglass page open URL`` drives
    a real Chromium — so the default suite has to opt out **explicitly** rather
    than by unsetting a variable. ``none`` is a real, documented posture (the
    page/action verbs then report a structured ``backend_unavailable``), and
    pinning it here is what keeps the browser-free suite browser-free on any
    developer's machine. Tests that want a browser set these variables
    themselves, in the subprocess environment they build.

    The two remaining variables are cleared so an exported shell value cannot
    change what the suite tests: ``WEBGLASS_BRAVE_API_KEY`` would wire a real
    search provider (and could send a query to a live API), and
    ``WEBGLASS_POLICY_PROFILE`` would silently replace the deny-by-default
    policy every policy assertion here is written against.
    """
    monkeypatch.setenv(STATE_DIR_ENV, str(tmp_path_factory.mktemp("webglass-state")))
    monkeypatch.setenv(BROWSER_BACKEND_ENV, "none")
    monkeypatch.delenv(ALLOW_UNSANDBOXED_ENV, raising=False)
    monkeypatch.delenv(WEBGLASS_BRAVE_API_KEY_ENV, raising=False)
    monkeypatch.delenv(POLICY_PROFILE_ENV, raising=False)
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


@pytest.fixture()
def session_store() -> FileSessionStore:
    """A :class:`FileSessionStore` rooted at this test's isolated state dir.

    Deliberately built on top of :func:`isolated_session_state` (autouse,
    runs first) rather than duplicating its ``tmp_path`` handling: this
    fixture just resolves :func:`default_sessions_dir`, which reads
    ``$WEBGLASS_STATE_DIR`` -- already pinned to a fresh per-test directory
    by the time this fixture body runs. Any CLI invocation a test makes
    under the same environment (in-process or via ``subprocess`` with the
    environment copied forward) sees the exact same on-disk records this
    fixture seeds.
    """
    return FileSessionStore(default_sessions_dir())


@pytest.fixture()
def seed_session_records(session_store: FileSessionStore) -> SeedSessionRecords:
    """A callable that seeds ``session_store`` with records of a chosen shape.

    Thin binding over :func:`tests.helpers.session_seed.seed_records` --
    see that module for the full contract (status/expiry/owner/host-set
    selection, the forward-compatible ``owner_token``/``hosts`` seams for
    build plan tasks t7/t8, and why records are written directly rather than
    through :meth:`~webglass.adapters.session_store.FileSessionStore.create`).
    A test calls this fixture once per distinct record shape it needs, e.g.
    once for a batch of ``closed`` records and again for a batch of
    ``active`` records with a dead pid, all landing in the one
    ``session_store``.
    """

    def _seed(**kwargs: object) -> list[FileSessionRecord]:
        return seed_records(session_store, **kwargs)  # type: ignore[arg-type]

    return _seed
