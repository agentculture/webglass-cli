"""Backend adapter protocols and fake implementations (build plan task t6).

CLAUDE.md "Target architecture" section 8 ("Policy, persistence, and the
replaceable adapter") is the load-bearing constraint for this package:
Playwright/Chromium is the *first backend*, never the operation model, and
must stay behind protocols. This package defines those protocol seams —
:class:`~webglass.adapters.search.SearchProvider`,
:class:`~webglass.adapters.fetch.FetchBackend`,
:class:`~webglass.adapters.browser.BrowserBackend`,
:class:`~webglass.adapters.artifacts.ArtifactStore` — plus the
:class:`~webglass.adapters.clock.Clock` / :class:`~webglass.adapters.clock.IdProvider`
determinism seams, and deterministic fake implementations of every one of
them so the M1 operation core (t9) and CLI (t10) can be built and
characterization-tested before any real backend exists.

Two seams are deliberately **not** redefined here, only consumed or
re-exported:

- **Browser session storage** already has a home:
  :class:`webglass.sessions.SessionStore`. It is re-exported below as
  ``BrowserSessionStore`` so callers of this package never need to know it
  actually lives in a sibling module — but the protocol itself is defined
  exactly once, in ``sessions.py``. Its on-disk *implementation* does live
  here (:mod:`webglass.adapters.session_store`), because that is what an
  adapter is: the part that touches the outside world — files, permissions,
  locks, and browser processes.
- **Web policy** already has a home too: :class:`webglass.policy.WebPolicyEvaluator`.
  ``fetch.py`` and ``browser.py`` *consume* an evaluator (an optional
  constructor argument that re-checks every redirect hop) — this package
  never re-implements policy evaluation.

Every protocol here is ``@runtime_checkable`` and every method is
synchronous — the CLI is a one-shot process; async, if a real backend ever
needs it, is an adapter-internal implementation detail invisible at this
seam.

No import in this package, or anywhere else in ``webglass``, names
``playwright`` — see ``tests/test_import_boundaries.py``. t11 is the first
task allowed to add one, and only inside its own dedicated adapter module.
"""

from __future__ import annotations

from webglass.sessions import SessionStore as BrowserSessionStore

from .artifacts import ArtifactRef, ArtifactStore, FakeArtifactStore
from .browser import (
    BrowserBackend,
    BrowserOpenResult,
    ConsoleMessage,
    FakeBrowserBackend,
    FakeBrowserRoute,
    PageError,
    PressResult,
    encode_solid_png,
    is_decodable_png,
    open_page_snapshot,
)
from .clock import Clock, FixedClock, IdProvider, SequentialIds
from .fetch import FakeFetchBackend, FakeFetchRoute, FetchBackend, FetchResult
from .search import FakeSearchProvider, SearchProvider, SearchResult, SearchResultSet
from .session_store import FileSessionRecord, FileSessionStore

__all__ = [
    # Re-exported, not redefined.
    "BrowserSessionStore",
    # Sessions on disk (the protocol's cross-invocation implementation).
    "FileSessionRecord",
    "FileSessionStore",
    # Determinism seams.
    "Clock",
    "FixedClock",
    "IdProvider",
    "SequentialIds",
    # Search.
    "SearchProvider",
    "SearchResult",
    "SearchResultSet",
    "FakeSearchProvider",
    # Fetch.
    "FetchBackend",
    "FetchResult",
    "FakeFetchRoute",
    "FakeFetchBackend",
    # Browser.
    "BrowserBackend",
    "BrowserOpenResult",
    "ConsoleMessage",
    "PageError",
    "PressResult",
    "FakeBrowserRoute",
    "FakeBrowserBackend",
    "open_page_snapshot",
    "encode_solid_png",
    "is_decodable_png",
    # Artifacts.
    "ArtifactRef",
    "ArtifactStore",
    "FakeArtifactStore",
]
