"""Shared, reusable test helpers for the webglass-cli test suite.

Distinct from ``tests/fixtures`` (the deterministic/hostile HTTP fixture
pages served by ``tests/fixtures/server.py``): this package holds *Python
helpers*, not served content — starting with
:mod:`tests.helpers.session_seed`, the session-store seeding harness build
plan task t1 introduces so later tasks (t7's owner token, t8's navigated
hosts, and the session lifecycle/reporting tasks that read them) all seed
their "before state" the same way instead of hand-rolling
:class:`~webglass.adapters.session_store.FileSessionRecord` construction in
every test module.
"""

from __future__ import annotations
