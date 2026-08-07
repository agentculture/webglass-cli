"""The one operation service: a single execution path for library and CLI callers.

Issue #1 sections 1 and 14 (CLAUDE.md "Target architecture" section 1) fix the
shape of this module: WebGlass is architected around **one operation
lifecycle**, not around CLI handlers or direct backend calls. A caller builds a
:class:`~webglass.operations.WebOperation`, hands it plus a
:class:`~webglass.context.WebContext` to :meth:`WebGlassService.execute`, and
receives a :class:`~webglass.results.WebOperationResult`. The CLI (t10) calls
exactly this method and *renders* the result; it never re-implements operation
logic, so the library and the CLI cannot drift apart semantically.

Every backend is injected (issue #1 section 8): search provider, fetch backend,
browser backend, session store, policy evaluator, artifact store, clock, and id
provider. Nothing here imports Playwright, and nothing constructs a default
backend behind the caller's back — a missing adapter is a *structured* result
(``backend_unavailable``), never an ``AttributeError`` and never a silent
substitution.

What this module guarantees
---------------------------

**No raw exception ever escapes.** :meth:`WebGlassService.execute` is the single
exception boundary of the operation core: policy denials, budget exhaustion,
timeouts, cancellation, stale references, unknown sessions, and even an outright
backend crash all leave through the same door — a ``WebOperationResult`` with a
lifecycle state and, on every non-success path, a stable machine-readable
:class:`~webglass.results.OperationError` code drawn from :data:`ERROR_CODES`.

**Budgets are charged per dimension, and exhaustion is a result.** See
:class:`BudgetLedger` for the five dimensions and the reserve/charge split.

**Policy is re-evaluated by the service, not delegated to the adapter.** An
adapter *may* hold an evaluator (the fakes do), but the service evaluates the
requested URL and then every hop of the returned navigation chain itself. A
backend that forgot to check, or one that cannot, still cannot smuggle a
private-network hop past policy.

**Trust zones are structural.** Each result's payload is split across
:class:`~webglass.results.TrustZones` by one rule, applied uniformly:

    Any structure containing page- or provider-authored text goes in
    ``untrusted`` — even when it also carries WebGlass-generated refs.
    ``trusted`` holds only WebGlass-authored control metadata and
    caller-supplied arguments. ``derived`` holds WebGlass-computed measurements
    and projections that contain no source text. ``sensitive`` stays empty at
    M1.

That rule is what stops a hostile page from getting its own text rendered where
a reader expects a WebGlass diagnostic (issue #1 section 7).

Reconciling the two ``PolicyVerdict`` shapes
--------------------------------------------

``results.py`` (t4) carries a deliberately minimal local ``PolicyVerdict``
(``decision`` + ``matched_rule_ids``) documented as "t9 wires in the concrete
``policy.py`` type". This module is that wiring — **without editing
``results.py``**, which t9 does not own. :func:`to_result_verdict` converts a
:class:`webglass.policy.PolicyVerdict` into the results-module shape at the
boundary, and because that shape has nowhere to put ``url``, ``reason``, or
``hop_index``, the *complete* verdict (and every per-hop verdict) is preserved
verbatim under ``content.trusted["policy"]``. Nothing is dropped silently: the
typed field carries the decisive decision, the trusted bucket carries the whole
evaluation record.

M1 scope and declared seams
---------------------------

- **No cache layer exists.** Every result reports ``cache.hit = False`` with the
  requested mode echoed back. ``prefer-cache`` / ``refresh`` are served live
  with a WebGlass-authored warning; ``cache-only`` is **blocked**, because
  serving it live would be exactly the silent stale/live confusion CLAUDE.md
  section 8 forbids.
- **No evidence store exists** (M3). ``evidence_refs`` stays empty rather than
  minting ids no store can resolve; a screenshot is reported as
  content-addressed :class:`~webglass.adapters.artifacts.ArtifactRef` data under
  ``content.trusted``.
- **Effect-class overrides are a seam, not a policy.** ``action.press``
  classifies as ``remote-action`` and therefore previews. The spec's
  test-profile reclassification (t13) plugs in through
  :data:`EffectClassResolver` — see :func:`default_effect_class`.
- **Lens operations do not re-fetch.** ``page.read``/``inspect``/``extract``/
  ``links`` resolve ``target.page_ref`` against an in-process
  :class:`SnapshotRegistry`. Supplying a ``target.url`` instead is an explicit
  open-then-lens request, charged as such; supplying only a ``session_id`` is
  an explicit *live re-read* of that session's current page, which navigates
  nothing (see :meth:`WebGlassService._live_entry`). There is no hidden
  re-fetch behind a snapshot reference, and no hidden navigation behind a live
  read.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from webglass.adapters.artifacts import ArtifactStore
from webglass.adapters.browser import BrowserBackend, BrowserOpenResult, ConsoleMessage, PageError
from webglass.adapters.clock import Clock, IdProvider
from webglass.adapters.fetch import FetchBackend
from webglass.adapters.search import SearchProvider
from webglass.context import WebContext
from webglass.effects import EffectClass, OperationKind
from webglass.extraction import SelectorSyntaxError, extract_page, extract_selector
from webglass.operations import ApplyState, CacheMode, WebOperation
from webglass.pages import TOKEN_ESTIMATE_METHOD, Block, PageSnapshot, ReadBudget, estimate_tokens
from webglass.policy import PolicyDecision
from webglass.policy import PolicyVerdict as WebPolicyVerdict
from webglass.policy import WebPolicyEvaluator, WebPolicyProfile
from webglass.references import (
    ReferenceSyntaxError,
    RefKind,
    SnapshotRef,
    SnapshotReferenceError,
    StaleReferenceError,
    UnknownReferenceError,
    parse_qualified_ref,
    parse_ref,
)
from webglass.results import (
    CacheFreshness,
    Completeness,
    LifecycleState,
    NavigationHop,
    OperationError,
)
from webglass.results import PolicyVerdict as ResultPolicyVerdict
from webglass.results import (
    Timings,
    TrustZones,
    WebOperationResult,
)
from webglass.sessions import (
    DEFAULT_LEASE_TTL_SECONDS,
    LeaseRefusal,
    SessionRecord,
    SessionStatus,
    SessionStore,
)

__all__ = [
    "DEFAULT_SESSION_TTL_SECONDS",
    "DEFAULT_SNAPSHOT_RETENTION",
    "ERROR_CODES",
    "BudgetDimension",
    "BudgetLedger",
    "BudgetLimits",
    "BudgetSpend",
    "CancellationToken",
    "EffectClassResolver",
    "SnapshotEntry",
    "SnapshotRegistry",
    "WebGlassService",
    "default_effect_class",
    "to_result_verdict",
]


# ---------------------------------------------------------------------------
# Stable machine-readable error codes
# ---------------------------------------------------------------------------

#: A caller supplied an argument this operation kind cannot use.
ERROR_INVALID_ARGUMENT = "invalid_argument"
#: The operation kind is not implemented at this milestone (or is unknown).
ERROR_UNSUPPORTED_KIND = "unsupported_operation_kind"
#: The adapter this kind needs was not injected into the service.
ERROR_BACKEND_UNAVAILABLE = "backend_unavailable"
#: An injected adapter raised. The service never lets that escape as-is.
ERROR_BACKEND_FAILURE = "backend_failure"
#: Policy refused the target (policy working as designed).
ERROR_POLICY_DENIED = "policy_denied"
#: Policy could not be evaluated at all — malformed policy, failing closed.
ERROR_POLICY_ERROR = "policy_error"
#: A budget dimension was exhausted; partial evidence is preserved.
ERROR_BUDGET_EXHAUSTED = "budget_exhausted"
#: The operation's wall-clock deadline passed.
ERROR_TIMED_OUT = "operation_timed_out"
#: The caller's cancellation handle was set.
ERROR_CANCELLED = "operation_cancelled"
#: ``cache-only`` was requested and no cache layer exists yet (M1).
ERROR_CACHE_UNAVAILABLE = "cache_unavailable"
#: A reference from a different snapshot generation was applied.
ERROR_STALE_REFERENCE = "stale_reference"
#: A correctly scoped reference names nothing in the snapshot.
ERROR_UNKNOWN_REFERENCE = "unknown_reference"
#: A reference (or snapshot id) is not well formed.
ERROR_INVALID_REFERENCE = "invalid_reference"
#: No snapshot with that id is retained by this service instance.
ERROR_UNKNOWN_SNAPSHOT = "unknown_snapshot"
#: No session record with that id exists in the injected store.
ERROR_UNKNOWN_SESSION = "unknown_session"
#: The session belongs to a different caller.
ERROR_SESSION_NOT_OWNED = "session_not_owned"
#: Another holder currently leases the session.
ERROR_SESSION_LEASE_HELD = "session_lease_held"
#: The session exists but is closed or expired.
ERROR_SESSION_NOT_ACTIVE = "session_not_active"
#: A remote-action kind was submitted with ``apply``; prepare/commit/verify is M5.
ERROR_APPLY_UNAVAILABLE = "remote_action_apply_unavailable"
#: The payload exceeded ``ResourceLimits.max_response_bytes``.
ERROR_RESPONSE_TOO_LARGE = "response_too_large"
#: The navigation chain exceeded ``ResourceLimits.max_redirects``.
ERROR_REDIRECT_LIMIT = "redirect_limit_exceeded"
#: The backend reached the target but the navigation itself never completed —
#: connection refused, DNS failure, TLS failure, or a navigation timeout. A
#: *transport* failure, distinct from a policy denial (the target was allowed)
#: and from a backend bug (the adapter behaved correctly). WebGlass never
#: starts or supervises an app under test, so this is the caller's server to
#: check (spec honesty h33).
ERROR_NAVIGATION_FAILED = "navigation_failed"
#: ``page.screenshot --out`` could not write the PNG at the caller's path.
ERROR_ARTIFACT_WRITE_FAILED = "artifact_write_failed"

#: Every code this service can put on a result. Callers (and the CLI's own error
#: mapping at t10) may switch on these; a code absent from this set is a bug,
#: and ``tests/test_service.py`` asserts the module emits nothing else.
ERROR_CODES: frozenset[str] = frozenset(
    {
        ERROR_INVALID_ARGUMENT,
        ERROR_UNSUPPORTED_KIND,
        ERROR_BACKEND_UNAVAILABLE,
        ERROR_BACKEND_FAILURE,
        ERROR_POLICY_DENIED,
        ERROR_POLICY_ERROR,
        ERROR_BUDGET_EXHAUSTED,
        ERROR_TIMED_OUT,
        ERROR_CANCELLED,
        ERROR_CACHE_UNAVAILABLE,
        ERROR_STALE_REFERENCE,
        ERROR_UNKNOWN_REFERENCE,
        ERROR_INVALID_REFERENCE,
        ERROR_UNKNOWN_SNAPSHOT,
        ERROR_UNKNOWN_SESSION,
        ERROR_SESSION_NOT_OWNED,
        ERROR_SESSION_LEASE_HELD,
        ERROR_SESSION_NOT_ACTIVE,
        ERROR_APPLY_UNAVAILABLE,
        ERROR_RESPONSE_TOO_LARGE,
        ERROR_REDIRECT_LIMIT,
        ERROR_NAVIGATION_FAILED,
        ERROR_ARTIFACT_WRITE_FAILED,
    }
)

#: Default lifetime for a session created without an explicit ``ttl_seconds``.
DEFAULT_SESSION_TTL_SECONDS = 300.0

#: How many snapshots one service instance retains for lens operations. Bounded
#: so a long-lived process cannot grow without limit; the oldest entry is
#: evicted first, and a lens against an evicted id fails with
#: :data:`ERROR_UNKNOWN_SNAPSHOT` rather than silently re-fetching.
DEFAULT_SNAPSHOT_RETENTION = 32

#: The lenses ``page.inspect`` projects. Each is a *view of one snapshot*: none
#: re-fetches, and all preserve the snapshot's block/link/field references.
INSPECT_LENSES: frozenset[str] = frozenset(
    {"outline", "controls", "metadata", "console", "structure"}
)

_TEXT_LIMIT = 200


def _sanitize(text: object, limit: int = _TEXT_LIMIT) -> str:
    """Make backend/exception text safe to embed in WebGlass-authored prose.

    Same rationale as :func:`webglass.policy._sanitize`: an error message is
    *trusted control metadata*, rendered next to WebGlass's own diagnostics, so
    a newline or control character in a backend's exception text must not be
    able to forge a second diagnostic line.
    """
    cleaned = "".join(ch for ch in str(text) if ch.isprintable())
    return cleaned[:limit] + "..." if len(cleaned) > limit else cleaned


def _iso(epoch_seconds: float) -> str:
    """Render epoch seconds as a UTC ISO-8601 instant (``...Z``).

    Reads the injected clock's value, never the wall clock — with a
    :class:`~webglass.adapters.clock.FixedClock` this is fully deterministic.
    """
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------


class BudgetDimension(StrEnum):
    """The five dimensions an operation charges (issue #1 section 1).

    ``ESTIMATED_TOKENS`` is a **labeled heuristic**, never a tokenizer's answer:
    it is :func:`webglass.pages.estimate_tokens`, and every report of it carries
    :data:`webglass.pages.TOKEN_ESTIMATE_METHOD` alongside (spec h31).
    """

    REQUESTS = "requests"
    TRANSFERRED_BYTES = "transferred_bytes"
    BROWSER_SECONDS = "browser_seconds"
    ARTIFACT_BYTES = "artifact_bytes"
    ESTIMATED_TOKENS = "estimated_tokens"


@dataclass(frozen=True)
class BudgetLimits:
    """Per-dimension ceilings. ``None`` means "unbounded on that dimension".

    Frozen and hashable so it can take part in the ledger key — two contexts
    with the same scope but different ceilings get different ledgers, rather
    than one silently inheriting the other's limits.
    """

    requests: int | None = None
    transferred_bytes: int | None = None
    browser_seconds: float | None = None
    artifact_bytes: int | None = None
    estimated_tokens: int | None = None

    @classmethod
    def from_context(
        cls, context: WebContext, *, artifact_bytes: int | None = None
    ) -> BudgetLimits:
        """Read the ceilings a :class:`~webglass.context.WebContext` carries.

        ``WebContext`` (t8) carries four of the five dimensions —
        ``request_budget``, ``byte_budget``, ``time_budget_seconds``,
        ``token_budget``. It has no artifact-bytes field, and ``context.py`` is
        not this task's to change, so the fifth ceiling is supplied here by the
        caller (the service passes its own
        :attr:`WebGlassService.default_artifact_byte_budget`). Adding the field
        to ``WebContext`` is the cleaner long-term home; until then this is the
        single documented place the gap is bridged.
        """
        return cls(
            requests=context.request_budget,
            transferred_bytes=context.byte_budget,
            browser_seconds=context.time_budget_seconds,
            artifact_bytes=artifact_bytes,
            estimated_tokens=context.token_budget,
        )

    def limit_for(self, dimension: BudgetDimension) -> float | None:
        return getattr(self, dimension.value)

    def to_dict(self) -> dict[str, Any]:
        return {dimension.value: self.limit_for(dimension) for dimension in BudgetDimension}


@dataclass
class BudgetSpend:
    """Mutable running total per dimension."""

    requests: int = 0
    transferred_bytes: int = 0
    browser_seconds: float = 0.0
    artifact_bytes: int = 0
    estimated_tokens: int = 0

    def spent(self, dimension: BudgetDimension) -> float:
        return getattr(self, dimension.value)

    def add(self, dimension: BudgetDimension, amount: float) -> None:
        setattr(self, dimension.value, self.spent(dimension) + amount)

    def to_dict(self) -> dict[str, Any]:
        return {dimension.value: self.spent(dimension) for dimension in BudgetDimension}


class BudgetLedger:
    """Tracks spend across *every operation executed in one context scope*.

    Two verbs, and the difference between them is the whole design:

    ``reserve``
        Asked **before** work that can be declined — issuing a network request.
        A reservation that would cross the ceiling spends nothing and reports
        the dimension, so an operation with no request budget left never
        touches a backend at all.

    ``charge``
        Applied **after** work whose size is only knowable once done —
        transferred bytes, browser seconds, stored artifact bytes, the
        agent-visible token estimate. The spend is recorded even when it crosses
        the ceiling (the resources really were consumed), and the crossing is
        reported so the operation terminates as ``blocked`` with its partial
        evidence intact.

    Either way the caller gets a structured result: exhaustion is never an
    exception, and never a silently truncated success.
    """

    def __init__(self, limits: BudgetLimits) -> None:
        self.limits = limits
        self.spend = BudgetSpend()
        self._lock = threading.RLock()

    def reserve(self, dimension: BudgetDimension, amount: float = 1) -> BudgetDimension | None:
        """Spend ``amount`` only if it fits; return the dimension if it does not."""
        with self._lock:
            limit = self.limits.limit_for(dimension)
            if limit is not None and self.spend.spent(dimension) + amount > limit:
                return dimension
            self.spend.add(dimension, amount)
            return None

    def charge(self, dimension: BudgetDimension, amount: float) -> BudgetDimension | None:
        """Record ``amount`` unconditionally; return the dimension if it now exceeds."""
        with self._lock:
            self.spend.add(dimension, amount)
            limit = self.limits.limit_for(dimension)
            if limit is not None and self.spend.spent(dimension) > limit:
                return dimension
            return None

    def exhausted(self) -> tuple[BudgetDimension, ...]:
        """Every dimension currently at or past its ceiling."""
        with self._lock:
            return tuple(
                dimension
                for dimension in BudgetDimension
                if (limit := self.limits.limit_for(dimension)) is not None
                and self.spend.spent(dimension) >= limit
            )

    def remaining(self, dimension: BudgetDimension) -> float | None:
        with self._lock:
            limit = self.limits.limit_for(dimension)
            if limit is None:
                return None
            return max(0.0, limit - self.spend.spent(dimension))

    def to_dict(self) -> dict[str, Any]:
        """The budget block every result carries under ``content.trusted``."""
        with self._lock:
            return {
                "limits": self.limits.to_dict(),
                "spent": self.spend.to_dict(),
                "remaining": {
                    dimension.value: self.remaining(dimension) for dimension in BudgetDimension
                },
                "exhausted": [dimension.value for dimension in self.exhausted()],
                "token_estimate_method": TOKEN_ESTIMATE_METHOD,
            }


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


class CancellationToken:
    """A cooperative cancellation handle.

    Deliberately thin: it wraps a :class:`threading.Event`, so a caller can
    either pass one of these (and call :meth:`cancel`) or pass a bare
    ``threading.Event`` it already owns — :meth:`WebGlassService.execute`
    accepts both, since both answer ``is_set()``.

    Cooperative, not preemptive: the service checks the handle at every step
    boundary (before dispatch, and after each backend interaction). A backend
    already blocked inside a syscall is not interrupted — it finishes, and the
    operation then terminates as ``cancelled`` with whatever evidence had
    accumulated by then.
    """

    def __init__(self, event: threading.Event | None = None) -> None:
        self._event = event if event is not None else threading.Event()

    def cancel(self) -> None:
        """Request cancellation. Idempotent."""
        self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()


# ---------------------------------------------------------------------------
# Snapshot retention (the lens substrate)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SnapshotEntry:
    """One retained observation: the snapshot plus the context it came from.

    Console messages and page errors ride along because they are part of the
    same observation (issue #9 item 2), and a lens such as ``page inspect
    --lens console`` must be able to report them without re-opening the page.
    Both are *untrusted source material* and are only ever routed into
    ``content.untrusted``.

    ``html`` is the raw document the observation was extracted from, retained
    for exactly one reason: selector-scoped extraction
    (:func:`webglass.extraction.extract_selector`) reads elements the readable
    pipeline deliberately drops — a ``<script type="application/json">`` state
    node is the motivating case (issue #9 item 3). It is *untrusted source
    material*, never rendered wholesale into a result; only the selector's own
    matches are. Retention stays bounded by
    :data:`DEFAULT_SNAPSHOT_RETENTION` entries, each already capped by the
    operation's ``limits.max_response_bytes``.
    """

    snapshot: PageSnapshot
    session_id: str
    backend: str
    console_messages: tuple[ConsoleMessage, ...] = ()
    page_errors: tuple[PageError, ...] = ()
    navigation_history: tuple[NavigationHop, ...] = ()
    html: str = ""


class SnapshotRegistry:
    """Bounded, process-local map of snapshot id -> :class:`SnapshotEntry`.

    This is the documented M1 answer to "how does ``page read`` reach the page
    ``page open`` produced": the service keeps the snapshot in memory and the
    lens projects it. It deliberately does **not** re-fetch behind a snapshot
    reference — a reference whose entry was never created, or has been evicted,
    fails with :data:`ERROR_UNKNOWN_SNAPSHOT`, because silently re-opening the
    URL would hand back a *different* observation under the same reference
    (exactly the stale-reference hazard issue #1 section 5 forbids).

    Durable, cross-process snapshot retention is an M3 persistence concern.
    """

    def __init__(self, max_entries: int = DEFAULT_SNAPSHOT_RETENTION) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        self.max_entries = max_entries
        self._entries: dict[str, SnapshotEntry] = {}
        self._lock = threading.RLock()

    def put(self, entry: SnapshotEntry) -> None:
        with self._lock:
            snapshot_id = entry.snapshot.snapshot_id
            self._entries.pop(snapshot_id, None)
            self._entries[snapshot_id] = entry
            while len(self._entries) > self.max_entries:
                del self._entries[next(iter(self._entries))]

    def get(self, snapshot_id: str) -> SnapshotEntry | None:
        with self._lock:
            return self._entries.get(snapshot_id)

    def ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._entries)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


# ---------------------------------------------------------------------------
# Effect-class resolution (the test-profile seam)
# ---------------------------------------------------------------------------

#: How the service decides an operation's effect class: given the operation and
#: the *effective* policy profile, return the class to enforce.
EffectClassResolver = Callable[[WebOperation, WebPolicyProfile], EffectClass]


def default_effect_class(operation: WebOperation, profile: WebPolicyProfile) -> EffectClass:
    """M1 resolver: the kind's own classification, with no profile override.

    ``operation.effect_class`` is computed from the kind by
    :func:`webglass.effects.classify`, which classifies upward — so
    ``action.press`` is ``remote-action`` and previews by default.

    The spec's 2026-08-07 decision (resolving q4) lets a *declared test profile*
    treat ``press`` as ``observe`` for the app under test. That is a
    profile-scoped authorization, not a fact about the kind, so it belongs in a
    resolver rather than in :mod:`webglass.effects`. Task t13 supplies one and
    passes it as ``WebGlassService(effect_class_resolver=...)``; ``profile`` is
    in the signature precisely so that resolver can read the declared targets it
    is scoped to. This default ignores it.
    """
    return operation.effect_class


# ---------------------------------------------------------------------------
# Policy verdict conversion (the results.py reconciliation)
# ---------------------------------------------------------------------------


def to_result_verdict(verdict: WebPolicyVerdict | None) -> ResultPolicyVerdict:
    """Convert a :mod:`webglass.policy` verdict into the ``results.py`` shape.

    ``results.PolicyVerdict`` carries only ``decision`` + ``matched_rule_ids``;
    the policy module's verdict also carries ``url``, ``reason``, and
    ``hop_index``. Rather than edit ``results.py`` (t4's module, not t9's), the
    service converts here and preserves the discarded fields verbatim under
    ``content.trusted["policy"]`` — see this module's docstring.

    ``None`` (an operation that evaluated no URL, e.g. ``session list``)
    converts to the empty verdict, which renders as ``decision: null``: "no
    policy question was asked", never "allowed".
    """
    if verdict is None:
        return ResultPolicyVerdict()
    return ResultPolicyVerdict(
        decision=str(verdict.decision),
        matched_rule_ids=tuple(verdict.matched_rule_ids),
    )


# ---------------------------------------------------------------------------
# Internal execution scratch space
# ---------------------------------------------------------------------------


class _Halt(Exception):
    """Internal control flow: stop this operation with a terminal state.

    Never escapes :meth:`WebGlassService.execute` — it is caught there and
    turned into the corresponding :class:`WebOperationResult`, which is why a
    halt can be raised from deep inside a handler without any handler needing to
    thread a status back up by hand.
    """

    def __init__(self, lifecycle: LifecycleState, error: OperationError | None = None) -> None:
        super().__init__(error.message if error is not None else lifecycle.value)
        self.lifecycle = lifecycle
        self.error = error


@dataclass
class _Run:
    """Everything accumulated while one operation executes.

    Kept separate from the result so a run that halts part-way (cancelled, timed
    out, budget-exhausted, policy-denied on hop 3 of 4) still renders the
    evidence it *did* gather — "partial evidence preserved" is a property of
    building the result from this accumulator unconditionally.
    """

    operation: WebOperation
    context: WebContext
    started_at: float
    trusted: dict[str, Any] = field(default_factory=dict)
    untrusted: dict[str, Any] = field(default_factory=dict)
    sensitive: dict[str, Any] = field(default_factory=dict)
    derived: dict[str, Any] = field(default_factory=dict)
    navigation: list[NavigationHop] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    effects: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    verdict: WebPolicyVerdict | None = None
    hop_verdicts: list[WebPolicyVerdict] = field(default_factory=list)
    completeness: Completeness = field(default_factory=Completeness)
    entry: SnapshotEntry | None = None
    degraded: bool = False
    backend: str | None = None

    def warn(self, message: str) -> None:
        """Record a WebGlass-authored diagnostic (never page-sourced text)."""
        if message not in self.warnings:
            self.warnings.append(message)

    def effect(self, label: str) -> None:
        if label not in self.effects:
            self.effects.append(label)

    def has_evidence(self) -> bool:
        """Whether anything observable was gathered before a halt."""
        return bool(self.untrusted or self.navigation or self.derived)


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------

#: One operation handler's signature (bound methods on :class:`WebGlassService`).
_Handler = Callable[
    [_Run, BudgetLedger, CancellationToken | threading.Event | None, float | None], None
]


class WebGlassService:
    """Executes :class:`~webglass.operations.WebOperation` objects.

    One instance owns its injected adapters, its snapshot registry, and its
    budget ledgers. Construct it once per process (the CLI does this in its
    dispatch path); ``execute`` is safe to call from multiple threads — the
    ledger, the registry, and
    :class:`~webglass.sessions.InMemorySessionStore` all guard their own state.

    Every adapter is optional at construction because different callers need
    different subsets (a search-only integration needs no browser). A kind whose
    adapter is missing returns :data:`ERROR_BACKEND_UNAVAILABLE`. In particular
    the fetch backend is **never** used as a stand-in for the browser backend:
    "never silently fall back from a browser operation to a semantically
    different fetch operation" (CLAUDE.md section 8), so ``page.open`` without a
    browser backend fails even when a fetch backend is present.
    """

    def __init__(
        self,
        *,
        clock: Clock,
        ids: IdProvider,
        search: SearchProvider | None = None,
        fetch: FetchBackend | None = None,
        browser: BrowserBackend | None = None,
        sessions: SessionStore | None = None,
        policy: WebPolicyEvaluator | None = None,
        artifacts: ArtifactStore | None = None,
        effect_class_resolver: EffectClassResolver = default_effect_class,
        default_artifact_byte_budget: int | None = None,
        snapshot_retention: int = DEFAULT_SNAPSHOT_RETENTION,
    ) -> None:
        self.clock = clock
        self.ids = ids
        self.search = search
        #: Declared and held, but no M1 operation kind routes to it — see the
        #: class docstring on why a fetch backend never substitutes for a
        #: browser backend. A fetch-backed page read would be a *different*,
        #: explicitly declared operation kind, never an implicit fallback.
        self.fetch = fetch
        self.browser = browser
        self.sessions = sessions
        #: Absent policy is a normal posture (the built-in deny-by-default
        #: profile), distinct from malformed policy — see :mod:`webglass.policy`.
        self.policy = policy if policy is not None else WebPolicyEvaluator()
        self.artifacts = artifacts
        self.effect_class_resolver = effect_class_resolver
        self.default_artifact_byte_budget = default_artifact_byte_budget
        self.snapshots = SnapshotRegistry(max_entries=snapshot_retention)
        self._ledgers: dict[tuple[str, str, str, BudgetLimits], BudgetLedger] = {}
        self._ledger_lock = threading.RLock()
        self._handlers: dict[OperationKind, _Handler] = {
            OperationKind.SEARCH: self._op_search,
            OperationKind.PAGE_OPEN: self._op_page_open,
            OperationKind.PAGE_READ: self._op_page_read,
            OperationKind.PAGE_INSPECT: self._op_page_inspect,
            OperationKind.PAGE_EXTRACT: self._op_page_extract,
            OperationKind.PAGE_LINKS: self._op_page_links,
            OperationKind.PAGE_SCREENSHOT: self._op_page_screenshot,
            OperationKind.ACTION_FOLLOW: self._op_action_follow,
            OperationKind.ACTION_PRESS: self._op_action_press,
            OperationKind.SESSION_CREATE: self._op_session_create,
            OperationKind.SESSION_LIST: self._op_session_list,
            OperationKind.SESSION_SHOW: self._op_session_show,
            OperationKind.SESSION_CLOSE: self._op_session_close,
            OperationKind.SESSION_CLEAN: self._op_session_clean,
        }

    @property
    def supported_kinds(self) -> frozenset[OperationKind]:
        """Exactly the kinds this service dispatches (t10's CLI reads this)."""
        return frozenset(self._handlers)

    # -- budget ledgers -----------------------------------------------------

    def ledger_for(self, context: WebContext) -> BudgetLedger:
        """The ledger charged by every operation run in ``context``'s scope.

        Keyed by ``(caller, task, workspace)`` plus the context's own limits.
        The scope triple is what "one context" means for budget purposes: a
        child context derived with
        :meth:`~webglass.context.WebContext.with_reduced` that keeps the same
        triple *and* the same ceilings deliberately shares the parent's spend,
        so a child cannot reset a budget by re-deriving the context — while a
        child given different (narrower) ceilings gets its own ledger, because a
        different ceiling is a different budget.
        """
        limits = BudgetLimits.from_context(
            context, artifact_bytes=self.default_artifact_byte_budget
        )
        key = (context.caller, context.task, context.workspace, limits)
        with self._ledger_lock:
            ledger = self._ledgers.get(key)
            if ledger is None:
                ledger = BudgetLedger(limits)
                self._ledgers[key] = ledger
            return ledger

    def reset_budgets(self) -> None:
        """Drop every ledger (test helper / long-lived-process reset)."""
        with self._ledger_lock:
            self._ledgers.clear()

    # -- the one entry point ------------------------------------------------

    def execute(
        self,
        operation: WebOperation,
        context: WebContext,
        *,
        cancel: CancellationToken | threading.Event | None = None,
    ) -> WebOperationResult:
        """Execute one operation and return its structured result.

        This is the single method the CLI, a library caller, and (at M4) the
        Colleague tool adapter all go through, and the single exception boundary
        of the operation core: no failure mode leaves here as an exception,
        including a backend that raises something unexpected.
        """
        run = _Run(operation=operation, context=context, started_at=self.clock.now())
        ledger = self.ledger_for(context)
        deadline = self._deadline(operation, run.started_at)
        try:
            self._preflight(run, ledger, cancel, deadline)
            self._handlers[OperationKind(operation.kind)](run, ledger, cancel, deadline)
            lifecycle: LifecycleState = LifecycleState.SUCCEEDED
            error: OperationError | None = None
        except _Halt as halt:
            lifecycle, error = halt.lifecycle, halt.error
        except Exception as exc:  # the operation core's one exception boundary
            lifecycle = LifecycleState.FAILED
            error = OperationError(
                code=ERROR_BACKEND_FAILURE,
                message=f"backend raised {type(exc).__name__}: {_sanitize(exc)}",
                remediation=(
                    "this is a bug in the backend adapter or in WebGlass; re-run the same "
                    "operation to reproduce, and report it with this operation id"
                ),
            )
        return self._build_result(run, ledger, lifecycle, error)

    # -- lifecycle scaffolding ---------------------------------------------

    def _deadline(self, operation: WebOperation, started_at: float) -> float | None:
        timeout = operation.limits.timeout_seconds
        return None if timeout is None else started_at + timeout

    def _step(
        self,
        run: _Run,
        cancel: CancellationToken | threading.Event | None,
        deadline: float | None,
    ) -> None:
        """One step boundary: honor cancellation, then the wall-clock deadline.

        Called before dispatch and after every backend interaction — never after
        a handler has finished, because discarding work that is already complete
        would lose evidence the caller has already paid for.

        Cancellation is checked first: a caller who cancelled wants
        ``cancelled``, not a ``timed_out`` that happens to expire in the same
        instant. A deadline reached exactly is a timeout — the time was spent.
        """
        if cancel is not None and cancel.is_set():
            run.degraded = run.has_evidence()
            raise _Halt(
                LifecycleState.CANCELLED,
                OperationError(
                    code=ERROR_CANCELLED,
                    message="operation cancelled by the caller",
                    remediation=(
                        "the partial observation gathered before cancellation is on this "
                        "result; re-run the operation to complete it"
                    ),
                ),
            )
        if deadline is not None and self.clock.now() >= deadline:
            run.degraded = run.has_evidence()
            timeout = run.operation.limits.timeout_seconds
            raise _Halt(
                LifecycleState.TIMED_OUT,
                OperationError(
                    code=ERROR_TIMED_OUT,
                    message=f"operation exceeded its {timeout}s timeout",
                    remediation=(
                        "raise limits.timeout_seconds, narrow the operation, or split it "
                        "into smaller steps; the partial observation is on this result"
                    ),
                ),
            )

    def _preflight(
        self,
        run: _Run,
        ledger: BudgetLedger,
        cancel: CancellationToken | threading.Event | None,
        deadline: float | None,
    ) -> None:
        """Everything checked before a handler runs, in the order that matters."""
        kind = _resolve_kind(run.operation.kind)
        if kind is None or kind not in self._handlers:
            # An unknown kind never executes, so "classify upward" still holds;
            # reporting it as unsupported is more useful to the caller than
            # previewing an operation nobody can describe.
            raise _Halt(
                LifecycleState.FAILED,
                OperationError(
                    code=ERROR_UNSUPPORTED_KIND,
                    message=(
                        f"operation kind {_sanitize(run.operation.kind, 60)!r} is not "
                        "implemented at this milestone"
                    ),
                    remediation="use one of: " + ", ".join(sorted(k.value for k in self._handlers)),
                ),
            )
        self._step(run, cancel, deadline)
        self._check_cache_mode(run)
        self._check_effect_class(run)

    def _check_cache_mode(self, run: _Run) -> None:
        """M1 has no cache layer; say so rather than quietly serving live.

        ``cache-only`` is the one mode that cannot be honored by serving live
        content — doing so would answer "give me only what you already have"
        with a fresh network read. It blocks.
        """
        mode = run.operation.cache_mode
        if mode is CacheMode.CACHE_ONLY:
            raise _Halt(
                LifecycleState.BLOCKED,
                OperationError(
                    code=ERROR_CACHE_UNAVAILABLE,
                    message="cache-only was requested but WebGlass has no cache layer yet",
                    remediation="re-run with cache_mode=live to fetch, or wait for the M3 cache",
                ),
            )
        if mode in (CacheMode.PREFER_CACHE, CacheMode.REFRESH):
            run.warn(
                f"cache mode {mode.value!r} requested; no cache layer exists yet — served "
                "live, and nothing was read from or written to a cache"
            )

    def _check_effect_class(self, run: _Run) -> None:
        """Gate on the effect class the resolver reports (issue #1 section 3)."""
        effect_class = self.effect_class_resolver(run.operation, self.policy.profile)
        run.trusted["effect_class"] = effect_class.value
        if effect_class is not EffectClass.REMOTE_ACTION:
            return
        if run.operation.apply_state is ApplyState.APPLY:
            raise _Halt(
                LifecycleState.DENIED,
                OperationError(
                    code=ERROR_APPLY_UNAVAILABLE,
                    message=(
                        "remote-action operations cannot be applied yet: the "
                        "prepare -> commit -> verify protocol lands at M5"
                    ),
                    remediation=(
                        "run the operation with apply_state=preview to see what it would "
                        "do, or authorize it through a declared effect-class override"
                    ),
                ),
            )
        run.trusted["preview"] = {
            "kind": _kind_value(run.operation.kind),
            "effect_class": effect_class.value,
            "normalized_args": dict(run.operation.normalized_args),
            "target": run.operation.target.to_dict(),
            "authorization_required": "apply",
            "reason": (
                "this kind's effect cannot be proven navigational, so it classifies upward "
                "to remote-action and previews by default"
            ),
        }
        raise _Halt(LifecycleState.PREVIEWED)

    # -- budget helpers -----------------------------------------------------

    def _reserve(
        self, run: _Run, ledger: BudgetLedger, dimension: BudgetDimension, amount: float = 1
    ) -> None:
        if ledger.reserve(dimension, amount) is not None:
            self._halt_budget(run, ledger, dimension, reserved=True)

    def _charge(
        self, run: _Run, ledger: BudgetLedger, dimension: BudgetDimension, amount: float
    ) -> None:
        if amount and ledger.charge(dimension, amount) is not None:
            self._halt_budget(run, ledger, dimension, reserved=False)

    def _halt_budget(
        self, run: _Run, ledger: BudgetLedger, dimension: BudgetDimension, *, reserved: bool
    ) -> None:
        limit = ledger.limits.limit_for(dimension)
        spent = ledger.spend.spent(dimension)
        run.degraded = run.has_evidence()
        detail = (
            "the request was not issued"
            if reserved
            else "the work completed and its cost is recorded; partial evidence is preserved"
        )
        raise _Halt(
            LifecycleState.BLOCKED,
            OperationError(
                code=ERROR_BUDGET_EXHAUSTED,
                message=(
                    f"budget dimension {dimension.value!r} exhausted "
                    f"(limit {limit}, spent {spent}) — {detail}"
                ),
                remediation=(
                    "raise the corresponding WebContext budget, start a new task scope, or "
                    "narrow the operation; token figures are heuristic estimates "
                    f"({TOKEN_ESTIMATE_METHOD})"
                ),
            ),
        )

    def _charge_observation(
        self,
        run: _Run,
        ledger: BudgetLedger,
        *,
        payload: str,
        transferred_bytes: int | None = None,
    ) -> None:
        """Charge the bytes and the agent-visible token estimate for a payload.

        ``transferred_bytes`` defaults to the UTF-8 size of the agent-visible
        payload. At M1 that is measured from what the backend handed back, not
        from the socket — a real backend reports wire bytes at M2, and until then
        this under-reports rather than over-reports.
        """
        size = len(payload.encode("utf-8")) if transferred_bytes is None else transferred_bytes
        self._charge(run, ledger, BudgetDimension.TRANSFERRED_BYTES, size)
        self._charge(run, ledger, BudgetDimension.ESTIMATED_TOKENS, estimate_tokens(payload))

    # -- policy helpers -----------------------------------------------------

    def _evaluate(self, run: _Run, url: str, hop_index: int | None = None) -> WebPolicyVerdict:
        """Evaluate one URL and record the verdict on the run."""
        verdict = self.policy.evaluate(url, hop_index=hop_index)
        run.verdict = verdict
        run.hop_verdicts.append(verdict)
        return verdict

    def _require_allowed(self, run: _Run, verdict: WebPolicyVerdict) -> None:
        if verdict.decision is PolicyDecision.ALLOWED:
            return
        run.degraded = run.has_evidence()
        if verdict.decision is PolicyDecision.ERROR:
            raise _Halt(
                LifecycleState.FAILED,
                OperationError(
                    code=ERROR_POLICY_ERROR,
                    message=f"policy could not be evaluated: {_sanitize(verdict.reason)}",
                    remediation=(
                        "fix the effective policy profile; malformed policy fails closed and "
                        "is never treated as an allow"
                    ),
                ),
            )
        raise _Halt(
            LifecycleState.DENIED,
            OperationError(
                code=ERROR_POLICY_DENIED,
                message=f"policy denied this target: {_sanitize(verdict.reason)}",
                remediation=(
                    "rule(s): "
                    + ", ".join(verdict.matched_rule_ids)
                    + " — declare the origin in the effective policy profile if it is a "
                    "legitimate app under test, or choose a different target"
                ),
            ),
        )

    def _check_navigation(
        self, run: _Run, requested_url: str, hops: Sequence[NavigationHop]
    ) -> None:
        """Re-evaluate every hop the backend actually navigated.

        The service does this itself even when the adapter holds an evaluator: an
        adapter that forgot to check, or one that cannot, must not be able to
        smuggle a private-network or metadata hop past policy (CLAUDE.md
        section 7, "revalidate every redirect hop").
        """
        chain = _chain_urls(requested_url, hops)
        limit = run.operation.limits.max_redirects
        if limit is not None and len(chain) - 1 > limit:
            run.degraded = run.has_evidence()
            raise _Halt(
                LifecycleState.BLOCKED,
                OperationError(
                    code=ERROR_REDIRECT_LIMIT,
                    message=(
                        f"navigation took {len(chain) - 1} redirect hop(s), over the "
                        f"operation's limit of {limit}"
                    ),
                    remediation="raise limits.max_redirects, or target the final URL directly",
                ),
            )
        for verdict in self.policy.evaluate_redirect_chain(chain):
            run.verdict = verdict
            run.hop_verdicts.append(verdict)
            self._require_allowed(run, verdict)

    # -- adapter helpers ----------------------------------------------------

    def _require(self, adapter: Any, name: str, attribute: str) -> Any:
        if adapter is None:
            raise _Halt(
                LifecycleState.FAILED,
                OperationError(
                    code=ERROR_BACKEND_UNAVAILABLE,
                    message=f"this operation needs a {name}, and none was injected",
                    remediation=(
                        f"construct WebGlassService({attribute}=...) with a {name}; WebGlass "
                        "never substitutes a different backend for a missing one"
                    ),
                ),
            )
        return adapter

    def _backend_label(self, adapter: Any) -> str:
        provider_id = getattr(adapter, "provider_id", None)
        name = type(adapter).__name__
        return f"{name}:{provider_id}" if provider_id else name

    # -- session helpers ----------------------------------------------------

    def _lease_holder(self, context: WebContext) -> str:
        """Leases are per caller *and* task: one caller's two concurrent tasks
        must not drive the same live browser session at once."""
        return f"{context.caller}:{context.task}"

    def _require_session(self, run: _Run, session_id: str, *, lease: bool) -> SessionRecord:
        store: SessionStore = self._require(self.sessions, "session store", "sessions")
        record = store.get(session_id)
        if record is None:
            raise _Halt(
                LifecycleState.FAILED,
                OperationError(
                    code=ERROR_UNKNOWN_SESSION,
                    message=f"no session {_sanitize(session_id, 80)!r} in this store",
                    remediation="run 'session list' to see live sessions, or create one",
                ),
            )
        if record.caller != run.context.caller:
            # Never disclose whose it is: that would leak another caller's
            # identity to someone with no business knowing it.
            raise _Halt(
                LifecycleState.DENIED,
                OperationError(
                    code=ERROR_SESSION_NOT_OWNED,
                    message="that session belongs to a different caller",
                    remediation="create your own session; sessions are isolated per caller",
                ),
            )
        if record.status is not SessionStatus.ACTIVE:
            raise _Halt(
                LifecycleState.FAILED,
                OperationError(
                    code=ERROR_SESSION_NOT_ACTIVE,
                    message=f"session {_sanitize(session_id, 80)!r} is {record.status.value}",
                    remediation="create a new session",
                ),
            )
        if lease:
            outcome = store.acquire_lease(
                session_id,
                self._lease_holder(run.context),
                self.clock.now(),
                DEFAULT_LEASE_TTL_SECONDS,
            )
            if isinstance(outcome, LeaseRefusal):
                raise _Halt(
                    LifecycleState.DENIED,
                    OperationError(
                        code=ERROR_SESSION_LEASE_HELD,
                        message=(
                            f"session {_sanitize(session_id, 80)!r} is leased by another "
                            f"holder ({_sanitize(outcome.reason, 60)})"
                        ),
                        remediation=(
                            "wait for the lease to expire, or use a separate session for this "
                            "task; concurrent tasks never share one live session"
                        ),
                    ),
                )
        return record

    def _session_for_navigation(self, run: _Run) -> tuple[str, dict[str, Any]]:
        """Resolve the browser session a navigation runs in.

        With no ``session_id`` on the operation this mints an **ephemeral** id
        that is deliberately not stored: an anonymous, unshared context is the
        safe default (issue #1 section 2, "isolated per caller/task"), and
        persisting one would create session state no caller asked for.
        """
        session_id = run.operation.session_id
        if session_id is None:
            ephemeral = self.ids.new_id("session")
            return ephemeral, {"session_id": ephemeral, "ephemeral": True}
        self._require_session(run, session_id, lease=True)
        return session_id, {"session_id": session_id, "ephemeral": False}

    # -- reference helpers --------------------------------------------------

    def _entry(self, run: _Run, snapshot_id: str) -> SnapshotEntry:
        entry = self.snapshots.get(snapshot_id)
        if entry is None:
            raise _Halt(
                LifecycleState.FAILED,
                OperationError(
                    code=ERROR_UNKNOWN_SNAPSHOT,
                    message=f"no retained snapshot {_sanitize(snapshot_id, 80)!r}",
                    remediation=(
                        "re-open the page: this service retains the most recent "
                        f"{self.snapshots.max_entries} snapshots in memory and never "
                        "re-fetches behind a snapshot reference"
                    ),
                ),
            )
        return entry

    def _resolve_ref(self, entry: SnapshotEntry, text: str, kind: RefKind) -> SnapshotRef:
        """Parse a short (``link:3``) or qualified (``snap-1@0/link:3``) reference.

        A qualified reference carries its own scope and is checked against this
        snapshot's; a short one is interpreted *in* this snapshot's scope, never
        re-scoped silently.
        """
        snapshot = entry.snapshot
        try:
            if "/" in text:
                ref = parse_qualified_ref(text)
            else:
                ref = parse_ref(
                    text, snapshot_id=snapshot.snapshot_id, generation=snapshot.generation
                )
            ref.require_scope(snapshot.snapshot_id, snapshot.generation)
        except SnapshotReferenceError as exc:
            raise _reference_halt(exc) from exc
        if ref.kind is not kind:
            raise _Halt(
                LifecycleState.FAILED,
                OperationError(
                    code=ERROR_INVALID_REFERENCE,
                    message=f"expected a {kind.value} reference, got {ref.short!r}",
                    remediation=f"pass a '{kind.value}:<index>' reference from this snapshot",
                ),
            )
        return ref

    # -- argument helpers ---------------------------------------------------

    def _arg(self, run: _Run, name: str, *, required: bool = False, default: Any = None) -> Any:
        value = run.operation.normalized_args.get(name, default)
        if required and (value is None or value == ""):
            raise _Halt(
                LifecycleState.FAILED,
                OperationError(
                    code=ERROR_INVALID_ARGUMENT,
                    message=f"this operation kind requires a {name!r} argument",
                    remediation=f"set normalized_args[{name!r}] on the operation",
                ),
            )
        return value

    def _target_url(self, run: _Run) -> str:
        url = run.operation.target.url
        if not url:
            raise _Halt(
                LifecycleState.FAILED,
                OperationError(
                    code=ERROR_INVALID_ARGUMENT,
                    message="this operation kind requires a target URL",
                    remediation="set operation.target.url",
                ),
            )
        return url

    def _require_page_ref(self, run: _Run) -> str:
        page_ref = run.operation.target.page_ref
        if not page_ref:
            raise _Halt(
                LifecycleState.FAILED,
                OperationError(
                    code=ERROR_INVALID_ARGUMENT,
                    message="this operation kind requires a snapshot reference",
                    remediation=(
                        "set operation.target.page_ref to a snapshot id from a previous "
                        "page.open, or set target.url to open and lens in one operation"
                    ),
                ),
            )
        return page_ref

    # -- handlers: search ---------------------------------------------------

    def _op_search(
        self,
        run: _Run,
        ledger: BudgetLedger,
        cancel: CancellationToken | threading.Event | None,
        deadline: float | None,
    ) -> None:
        provider: SearchProvider = self._require(self.search, "search provider", "search")
        run.backend = self._backend_label(provider)
        query = str(self._arg(run, "query", required=True))
        limit = int(self._arg(run, "limit", default=10))

        self._reserve(run, ledger, BudgetDimension.REQUESTS)
        started = self.clock.now()
        result_set = provider.search(query, limit)
        elapsed = max(0.0, self.clock.now() - started)
        run.effect("network-request")
        # Provider-authored: titles, URLs, and snippets are source material,
        # never WebGlass statements about the web.
        run.untrusted["results"] = [result.to_dict() for result in result_set.results]
        run.trusted["search"] = {
            "provider_id": result_set.provider_id,
            "query": query,
            "limit": limit,
            "result_count": len(result_set.results),
        }
        self._step(run, cancel, deadline)
        self._charge(run, ledger, BudgetDimension.BROWSER_SECONDS, elapsed)
        self._charge_observation(
            run,
            ledger,
            payload="\n".join(
                f"{item.title}\n{item.url}\n{item.snippet}" for item in result_set.results
            ),
        )

    # -- handlers: navigation -----------------------------------------------

    def _op_page_open(
        self,
        run: _Run,
        ledger: BudgetLedger,
        cancel: CancellationToken | threading.Event | None,
        deadline: float | None,
    ) -> None:
        self._open(run, ledger, cancel, deadline, url=self._target_url(run))

    def _op_action_follow(
        self,
        run: _Run,
        ledger: BudgetLedger,
        cancel: CancellationToken | threading.Event | None,
        deadline: float | None,
    ) -> None:
        """Follow a *link reference*, not a selector and not a raw URL.

        Issue #1 section 4 prefers ``follow(link_ref)`` over a generic click. The
        href is resolved relative to the snapshot's final URL and then
        policy-checked exactly like a caller-supplied URL — a page cannot widen
        policy by putting a private-network href in an anchor.
        """
        entry = self._entry(run, self._require_page_ref(run))
        element_ref = str(self._arg(run, "link", default=run.operation.target.element_ref) or "")
        if not element_ref:
            raise _Halt(
                LifecycleState.FAILED,
                OperationError(
                    code=ERROR_INVALID_ARGUMENT,
                    message="action.follow requires a link reference",
                    remediation="set operation.target.element_ref to a 'link:<index>' reference",
                ),
            )
        ref = self._resolve_ref(entry, element_ref, RefKind.LINK)
        try:
            link = entry.snapshot.link(ref)
        except SnapshotReferenceError as exc:
            raise _reference_halt(exc) from exc
        run.trusted["followed"] = {
            "from_snapshot_id": entry.snapshot.snapshot_id,
            "link_ref": ref.qualified,
        }
        # The href is untrusted source material; record what we were handed
        # before resolving it, so a denial stays explainable after the fact.
        run.untrusted["link"] = link.to_dict()
        self._open(run, ledger, cancel, deadline, url=urljoin(entry.snapshot.final_url, link.href))

    def _open(
        self,
        run: _Run,
        ledger: BudgetLedger,
        cancel: CancellationToken | threading.Event | None,
        deadline: float | None,
        *,
        url: str,
    ) -> None:
        browser: BrowserBackend = self._require(self.browser, "browser backend", "browser")
        run.backend = self._backend_label(browser)
        session_id, session_info = self._session_for_navigation(run)
        run.trusted["session"] = session_info

        self._require_allowed(run, self._evaluate(run, url, hop_index=0))
        self._reserve(run, ledger, BudgetDimension.REQUESTS)

        started = self.clock.now()
        opened = browser.open(session_id, url)
        elapsed = max(0.0, self.clock.now() - started)
        run.navigation.extend(opened.redirect_chain)
        run.effect("network-request")
        run.effect("navigation-occurred")
        run.effect("browser-state-may-change")
        self._step(run, cancel, deadline)
        self._charge(run, ledger, BudgetDimension.BROWSER_SECONDS, elapsed)

        if opened.blocked:
            # The adapter's own evaluator refused a hop. Record its verdict and
            # stop: the service never retries a blocked navigation by another
            # route.
            run.degraded = True
            if opened.policy_verdict is not None:
                run.verdict = opened.policy_verdict
                run.hop_verdicts.append(opened.policy_verdict)
                self._require_allowed(run, opened.policy_verdict)
            raise _Halt(
                LifecycleState.DENIED,
                OperationError(
                    code=ERROR_POLICY_DENIED,
                    message="the backend refused a navigation hop on policy grounds",
                    remediation=(
                        "inspect navigation_history and the policy verdict on this result"
                    ),
                ),
            )

        self._check_reachable(run, opened, url)
        self._check_navigation(run, url, opened.redirect_chain)
        self._check_response_size(run, opened)
        self._record_snapshot(run, ledger, opened, session_id, url)

    #: Prefix a backend stamps on the diagnostic for a navigation that never
    #: completed (see ``PlaywrightBrowserBackend.open``). Matched as a *prefix*
    #: so the reason text stays free-form.
    _NAVIGATION_FAILED_PREFIX = "navigation-failed:"

    def _check_reachable(self, run: _Run, opened: BrowserOpenResult, url: str) -> None:
        """Turn "the browser could not reach it" into a structured result.

        The adapter never raises for an ordinary web failure — a refused
        connection, an unresolvable host, or a navigation timeout all come back
        as a result with no status and no document. Letting that through as a
        *succeeded* observation of an empty page would be the worst possible
        answer for the CI/app-under-test caller this milestone serves: their
        server is simply not up, and the honest report says so and says whose
        job it is to fix.

        WebGlass never starts, stops, or supervises the app under test (spec
        honesty h33 / scope boundary) — hence the remediation's framing.
        """
        diagnostics = tuple(getattr(opened, "diagnostics", ()) or ())
        reasons = [d for d in diagnostics if d.startswith(self._NAVIGATION_FAILED_PREFIX)]
        if not reasons:
            return
        run.degraded = run.has_evidence()
        detail = _sanitize(reasons[0][len(self._NAVIGATION_FAILED_PREFIX) :].strip())
        raise _Halt(
            LifecycleState.FAILED,
            OperationError(
                code=ERROR_NAVIGATION_FAILED,
                message=f"the browser could not load {_sanitize(url, 200)}: {detail}",
                remediation=(
                    "check that the server for this target is running and reachable, and "
                    "that the host and port are the ones you meant — WebGlass never "
                    "starts, stops, or supervises the app under test; that process "
                    "belongs to you. If the target is a local app under test, also "
                    "declare its origin in the effective policy profile."
                ),
            ),
        )

    def _check_response_size(self, run: _Run, opened: BrowserOpenResult) -> None:
        limit = run.operation.limits.max_response_bytes
        size = len(opened.html.encode("utf-8"))
        if limit is not None and size > limit:
            run.degraded = True
            raise _Halt(
                LifecycleState.BLOCKED,
                OperationError(
                    code=ERROR_RESPONSE_TOO_LARGE,
                    message=f"response body is {size} bytes, over the limit of {limit}",
                    remediation="raise limits.max_response_bytes, or target a smaller page",
                ),
            )

    def _record_snapshot(
        self,
        run: _Run,
        ledger: BudgetLedger,
        opened: BrowserOpenResult,
        session_id: str,
        requested_url: str,
    ) -> None:
        snapshot = extract_page(
            opened.html,
            snapshot_id=self.ids.new_id("snapshot"),
            requested_url=requested_url,
            final_url=opened.final_url,
            retrieved_at=_iso(self.clock.now()),
            status=opened.status,
            redirect_chain=tuple(
                hop.response_url if hop.response_url is not None else hop.requested_url
                for hop in opened.redirect_chain
            ),
        )
        entry = SnapshotEntry(
            snapshot=snapshot,
            session_id=session_id,
            backend=run.backend or "",
            console_messages=opened.console_messages,
            page_errors=opened.page_errors,
            navigation_history=opened.redirect_chain,
            html=opened.html,
        )
        run.entry = entry
        if run.operation.cache_mode is CacheMode.NO_STORE:
            run.warn(
                "cache mode 'no-store': this snapshot was not retained, so its references "
                "cannot be used by a later lens operation"
            )
        else:
            self.snapshots.put(entry)
            run.effect("snapshot-retained")

        self._page_content(run, entry)
        self._charge_observation(
            run,
            ledger,
            payload="\n".join(block.text for block in snapshot.blocks),
            transferred_bytes=len(opened.html.encode("utf-8")),
        )

    def _page_content(self, run: _Run, entry: SnapshotEntry) -> None:
        """Fill the trust-zone buckets for one observed page."""
        run.trusted["snapshot"] = _snapshot_identity(entry)
        run.untrusted["page_card"] = entry.snapshot.page_card().to_dict()
        self._attach_page_diagnostics(run, entry)
        run.completeness = _completeness(entry.snapshot)

    def _attach_page_diagnostics(self, run: _Run, entry: SnapshotEntry) -> None:
        """Console output, page errors, and security warnings.

        The *codes and messages* of WebGlass-authored security warnings go on
        ``result.warnings``; the full warning (whose ``subject`` quotes page
        text) and all console/page-error text stay in ``untrusted`` — a page that
        logs "WEBGLASS WARNING: ..." cannot get that string rendered as a
        WebGlass diagnostic.
        """
        if entry.console_messages or entry.page_errors:
            run.untrusted["console_messages"] = [m.to_dict() for m in entry.console_messages]
            run.untrusted["page_errors"] = [e.to_dict() for e in entry.page_errors]
            run.warn(
                f"page emitted {len(entry.console_messages)} console message(s) and "
                f"{len(entry.page_errors)} uncaught page error(s); their text is untrusted "
                "page output, reported under content.untrusted"
            )
        if entry.snapshot.security_warnings:
            run.untrusted["security_warnings"] = [
                warning.to_dict() for warning in entry.snapshot.security_warnings
            ]
            for warning in entry.snapshot.security_warnings:
                run.warn(f"{warning.code}: {warning.message}")

    # -- handlers: lenses ---------------------------------------------------

    def _lens_entry(
        self,
        run: _Run,
        ledger: BudgetLedger,
        cancel: CancellationToken | threading.Event | None,
        deadline: float | None,
    ) -> SnapshotEntry:
        """Resolve the snapshot a lens projects.

        Three explicit paths, tried in this order, and never a fourth:

        ``target.page_ref``
            Project a retained snapshot with no network access at all. In
            particular a *stale* page_ref never quietly turns into a re-fetch.
        ``target.url``
            An explicit "open, then lens" request: it navigates (charging a
            request, applying policy) and then projects.
        ``operation.session_id`` (and neither of the above)
            Re-read the session's *live* page without navigating — see
            :meth:`_live_entry`.

        The ordering matters: an explicit reference or URL always wins over the
        live page, so "lens this exact observation" can never be reinterpreted
        as "lens whatever that session happens to be showing now".
        """
        if run.operation.target.page_ref:
            return self._entry(run, run.operation.target.page_ref)
        if run.operation.target.url:
            self._open(run, ledger, cancel, deadline, url=run.operation.target.url)
            if run.entry is not None:
                return run.entry
        if run.operation.session_id:
            return self._live_entry(run, ledger, cancel, deadline)
        return self._entry(run, self._require_page_ref(run))

    def _live_entry(
        self,
        run: _Run,
        ledger: BudgetLedger,
        cancel: CancellationToken | threading.Event | None,
        deadline: float | None,
    ) -> SnapshotEntry:
        """Observe a session's current page *without navigating it*.

        This is what makes a one-shot CLI able to see what a previous
        invocation's ``action press`` (or ``page open``) actually did: the
        retained :class:`SnapshotRegistry` is process-local, and re-opening the
        URL would destroy the very in-memory page state the observation is
        about. So the backend is asked for the live DOM instead, and a fresh
        snapshot (with fresh, correctly-scoped references) is minted from it.

        Two properties are deliberately preserved:

        * **The live URL is policy-checked.** No request is issued here, but the
          page may have navigated itself since it was opened — a script can move
          a session to a private-network or metadata target. Re-evaluating the
          *current* URL is what stops a lens from reading a page policy would
          never have opened.
        * **A backend that cannot do this says so.** Live re-read is beyond the
          :class:`~webglass.adapters.browser.BrowserBackend` protocol (launch
          and connect deliberately are too), so it is duck-typed and its
          absence is a structured ``backend_unavailable`` — never a silent
          fallback to a navigation, which would be a semantically different
          operation.
        """
        browser: BrowserBackend = self._require(self.browser, "browser backend", "browser")
        run.backend = self._backend_label(browser)
        session_id = str(run.operation.session_id)
        self._require_session(run, session_id, lease=True)
        run.trusted["session"] = {"session_id": session_id, "ephemeral": False, "live_read": True}

        reader = getattr(browser, "current", None)
        if not callable(reader):
            raise _Halt(
                LifecycleState.FAILED,
                OperationError(
                    code=ERROR_BACKEND_UNAVAILABLE,
                    message=(
                        f"backend {_sanitize(run.backend, 60)} cannot re-read a live page "
                        "without navigating"
                    ),
                    remediation=(
                        "pass target.page_ref for a retained snapshot, or target.url to "
                        "navigate; WebGlass never substitutes a navigation for a live read"
                    ),
                ),
            )

        started = self.clock.now()
        observed = reader(session_id)
        elapsed = max(0.0, self.clock.now() - started)
        run.effect("live-page-read")
        self._step(run, cancel, deadline)
        self._charge(run, ledger, BudgetDimension.BROWSER_SECONDS, elapsed)

        self._check_allowed_live_url(run, observed)
        self._check_response_size(run, observed)
        self._record_snapshot(run, ledger, observed, session_id, observed.requested_url)
        if run.entry is None:  # pragma: no cover - _record_snapshot always sets it
            raise _Halt(
                LifecycleState.FAILED,
                OperationError(
                    code=ERROR_BACKEND_FAILURE,
                    message="the live page read produced no snapshot",
                    remediation="re-run the operation and report it with this operation id",
                ),
            )
        return run.entry

    def _check_allowed_live_url(self, run: _Run, observed: BrowserOpenResult) -> None:
        """Policy-check the page a live read is about to project."""
        url = observed.final_url or observed.requested_url
        if not url:
            return
        self._require_allowed(run, self._evaluate(run, url, hop_index=0))

    def _op_page_read(
        self,
        run: _Run,
        ledger: BudgetLedger,
        cancel: CancellationToken | threading.Event | None,
        deadline: float | None,
    ) -> None:
        entry = self._lens_entry(run, ledger, cancel, deadline)
        snapshot = entry.snapshot
        content_budget = run.operation.content_budget
        budget = ReadBudget(
            max_blocks=content_budget.max_blocks,
            # max_bytes is applied as a character ceiling: a UTF-8 byte count is
            # always >= its character count, so this is conservative — it can
            # return less than the byte budget allows, never more.
            max_chars=content_budget.max_bytes,
            max_estimated_tokens=content_budget.max_tokens_estimate,
        )
        try:
            read_result = snapshot.read(cursor=self._arg(run, "cursor"), budget=budget)
        except SnapshotReferenceError as exc:
            raise _reference_halt(exc) from exc

        rendered = read_result.to_dict()
        run.trusted["snapshot"] = _snapshot_identity(entry)
        run.untrusted["blocks"] = rendered["blocks"]
        run.untrusted["omissions"] = rendered["omissions"]
        run.derived["read"] = {
            "cursor": rendered["cursor"],
            "done": rendered["done"],
            "budget": rendered["budget"],
            "usage": rendered["usage"],
            "single_block_overrun": rendered["single_block_overrun"],
        }
        run.completeness = _completeness(snapshot, read_omissions=len(read_result.omissions))
        if read_result.omissions:
            run.warn(
                "the read budget was reached; the omission record names every block left "
                "out and carries a resume cursor"
            )
        self._charge_observation(
            run, ledger, payload="\n".join(block.text for block in read_result.blocks)
        )

    def _op_page_inspect(
        self,
        run: _Run,
        ledger: BudgetLedger,
        cancel: CancellationToken | threading.Event | None,
        deadline: float | None,
    ) -> None:
        entry = self._lens_entry(run, ledger, cancel, deadline)
        snapshot = entry.snapshot
        lens = str(self._arg(run, "lens", default="outline"))
        if lens not in INSPECT_LENSES:
            raise _Halt(
                LifecycleState.FAILED,
                OperationError(
                    code=ERROR_INVALID_ARGUMENT,
                    message=f"unknown inspect lens {_sanitize(lens, 40)!r}",
                    remediation="use one of: " + ", ".join(sorted(INSPECT_LENSES)),
                ),
            )
        run.trusted["snapshot"] = _snapshot_identity(entry)
        run.trusted["lens"] = lens
        payload = self._inspect_lens(run, entry, lens)
        self._attach_page_diagnostics(run, entry)
        run.completeness = _completeness(snapshot)
        self._charge_observation(run, ledger, payload=payload)

    def _inspect_lens(self, run: _Run, entry: SnapshotEntry, lens: str) -> str:
        """Project one lens into ``untrusted``; return its agent-visible text."""
        snapshot = entry.snapshot
        if lens == "outline":
            nodes = snapshot.outline()
            run.untrusted["outline"] = [node.to_dict() for node in nodes]
            return "\n".join(_outline_text(node) for node in nodes)
        if lens == "controls":
            run.untrusted["forms"] = [form.to_dict() for form in snapshot.forms]
            run.untrusted["fields"] = [item.to_dict() for item in snapshot.fields]
            run.untrusted["buttons"] = [button.to_dict() for button in snapshot.buttons]
            return "\n".join(
                [item.label or item.name for item in snapshot.fields]
                + [button.text for button in snapshot.buttons]
            )
        if lens == "metadata":
            metadata = {
                "requested_url": snapshot.requested_url,
                "final_url": snapshot.final_url,
                "canonical_url": snapshot.canonical_url,
                "title": snapshot.title,
                "language": snapshot.language,
                "content_type": snapshot.content_type,
                "status": snapshot.status,
            }
            run.untrusted["metadata"] = metadata
            return "\n".join(str(value) for value in metadata.values() if value is not None)
        if lens == "console":
            run.untrusted["console_messages"] = [m.to_dict() for m in entry.console_messages]
            run.untrusted["page_errors"] = [e.to_dict() for e in entry.page_errors]
            return "\n".join(
                [message.text for message in entry.console_messages]
                + [error.text for error in entry.page_errors]
            )
        structure = [block for block in snapshot.blocks if block.kind.value in ("table", "list")]
        run.untrusted["structure"] = [block.to_dict() for block in structure]
        return "\n".join(block.text for block in structure)

    def _op_page_extract(
        self,
        run: _Run,
        ledger: BudgetLedger,
        cancel: CancellationToken | threading.Event | None,
        deadline: float | None,
    ) -> None:
        """Query-focused block selection — deterministic, and never a summary.

        Two modes, chosen by which argument the caller supplied:

        ``selector``
            Selector-scoped extraction (:meth:`_extract_selector`): return
            exactly the matching elements' own content, nothing else.
        ``query``
            Blocks are ranked by how many distinct query terms they contain,
            ties broken by source order, and every returned block keeps its
            original ``block:<n>`` reference so a citation still points at the
            source.

        No model is involved on either path: issue #1 section 6 forbids
        presenting a synthesized summary as page content.
        """
        entry = self._lens_entry(run, ledger, cancel, deadline)
        snapshot = entry.snapshot
        selector = self._arg(run, "selector")
        if selector:
            self._extract_selector(run, ledger, entry, str(selector))
            return
        if not self._arg(run, "query"):
            raise _Halt(
                LifecycleState.FAILED,
                OperationError(
                    code=ERROR_INVALID_ARGUMENT,
                    message="page.extract needs either a query or a selector",
                    remediation=(
                        "set normalized_args['query'] to rank readable blocks, or "
                        "normalized_args['selector'] to return one element's own content"
                    ),
                ),
            )
        query = str(self._arg(run, "query", required=True))
        terms = tuple(sorted({term for term in query.lower().split() if term}))
        max_blocks = run.operation.content_budget.max_blocks

        scored: list[tuple[int, int, Block]] = []
        for block in snapshot.blocks:
            haystack = block.normalized_text.lower()
            score = sum(1 for term in terms if term in haystack)
            if score:
                scored.append((-score, block.source_order, block))
        scored.sort(key=lambda item: (item[0], item[1]))
        selected = scored if max_blocks is None else scored[:max_blocks]

        run.trusted["snapshot"] = _snapshot_identity(entry)
        run.trusted["extract"] = {
            "query": query,
            "query_terms": list(terms),
            "ranking": "distinct-term-overlap-then-source-order",
            "blocks_scanned": len(snapshot.blocks),
            "blocks_matched": len(scored),
            "blocks_returned": len(selected),
            "model_assisted": False,
        }
        run.untrusted["matches"] = [
            {**block.to_dict(), "score": -score} for score, _order, block in selected
        ]
        omitted: list[str] = []
        if len(scored) < len(snapshot.blocks):
            omitted.append(f"non-matching:{len(snapshot.blocks) - len(scored)}")
        if len(selected) < len(scored):
            omitted.append(f"budget:{len(scored) - len(selected)}")
            run.warn("the extract block budget was reached; lower-ranked matches were omitted")
        run.completeness = Completeness(
            truncated=len(selected) < len(scored),
            omitted_regions=tuple(omitted),
            extraction_complete=not omitted,
        )
        self._charge_observation(
            run, ledger, payload="\n".join(block.text for _score, _order, block in selected)
        )

    def _extract_selector(
        self, run: _Run, ledger: BudgetLedger, entry: SnapshotEntry, selector: str
    ) -> None:
        """Return exactly the selector's own content — not the rest of the page.

        Runs against the *raw retained document* rather than the readable
        blocks, because the motivating target is precisely an element the
        readable pipeline drops on purpose: the ``<script
        type="application/json">`` state node an app under test exposes for
        machine reading (issue #9 item 3, spec honesty h27). The match text is
        returned verbatim so a caller can ``json.loads`` it.

        Three states stay distinguishable, none of them collapsed into
        "nothing came back":

        * the selector matched nothing — a success with zero matches, and an
          omission record saying the document was searched;
        * the selector was never understood — a structured
          ``invalid_argument`` naming the supported forms
          (:class:`~webglass.extraction.SelectorSyntaxError`);
        * the document was not retained (``cache_mode=no-store``) — a
          structured failure, never a silent empty result.
        """
        if not entry.html:
            raise _Halt(
                LifecycleState.FAILED,
                OperationError(
                    code=ERROR_UNKNOWN_SNAPSHOT,
                    message=(
                        "selector extraction needs the raw document, and this snapshot "
                        "was retained without one"
                    ),
                    remediation=(
                        "re-open the page with a cache mode other than 'no-store'; a "
                        "snapshot recorded without its document cannot be re-scanned"
                    ),
                ),
            )
        try:
            matches = extract_selector(entry.html, selector)
        except SelectorSyntaxError as exc:
            raise _Halt(
                LifecycleState.FAILED,
                OperationError(
                    code=ERROR_INVALID_ARGUMENT,
                    message=f"unsupported selector: {_sanitize(exc)}",
                    remediation=_sanitize(getattr(exc, "remediation", "") or "", 300),
                ),
            ) from exc

        run.trusted["snapshot"] = _snapshot_identity(entry)
        run.trusted["extract"] = {
            "mode": "selector",
            "selector": selector,
            "ranking": "document-order",
            "matches_returned": len(matches),
            "model_assisted": False,
        }
        run.untrusted["matches"] = [match.to_dict() for match in matches]
        run.completeness = Completeness(
            truncated=False,
            omitted_regions=(
                "selector-scoped: every part of the document outside "
                f"{selector!r} was deliberately not returned",
            ),
            # Complete *for what was asked*: the selector's own content is
            # returned whole, never truncated or summarized.
            extraction_complete=True,
        )
        self._charge_observation(run, ledger, payload="\n".join(match.text for match in matches))

    def _op_page_links(
        self,
        run: _Run,
        ledger: BudgetLedger,
        cancel: CancellationToken | threading.Event | None,
        deadline: float | None,
    ) -> None:
        entry = self._lens_entry(run, ledger, cancel, deadline)
        snapshot = entry.snapshot
        run.trusted["snapshot"] = _snapshot_identity(entry)
        run.trusted["links"] = {"count": len(snapshot.links)}
        run.untrusted["links"] = [link.to_dict() for link in snapshot.links]
        self._attach_page_diagnostics(run, entry)
        run.completeness = _completeness(snapshot)
        self._charge_observation(
            run, ledger, payload="\n".join(f"{link.text} {link.href}" for link in snapshot.links)
        )

    def _op_page_screenshot(
        self,
        run: _Run,
        ledger: BudgetLedger,
        cancel: CancellationToken | threading.Event | None,
        deadline: float | None,
    ) -> None:
        browser: BrowserBackend = self._require(self.browser, "browser backend", "browser")
        store: ArtifactStore = self._require(self.artifacts, "artifact store", "artifacts")
        run.backend = self._backend_label(browser)
        if run.operation.target.url:
            # "Open, then capture", the same explicit one-operation form every
            # lens verb offers. Without it a screenshot would always need a
            # separate prior invocation, which for a one-shot CLI means a
            # session the caller has to create and close by hand.
            self._open(run, ledger, cancel, deadline, url=run.operation.target.url)
        session_id = self._screenshot_session(run)

        started = self.clock.now()
        png = browser.screenshot(session_id)
        elapsed = max(0.0, self.clock.now() - started)
        run.effect("browser-state-may-change")
        self._step(run, cancel, deadline)
        self._charge(run, ledger, BudgetDimension.BROWSER_SECONDS, elapsed)

        ref = store.put(png, content_type="image/png")
        run.effect("artifact-stored")
        # A screenshot carries no page *text* into the result — only bytes the
        # caller can fetch from the artifact store by hash — so the reference is
        # trusted control metadata.
        run.trusted["artifact"] = ref.to_dict()
        run.trusted["screenshot"] = {"session_id": session_id, "size_bytes": ref.size_bytes}
        out = self._arg(run, "out")
        if out:
            run.trusted["screenshot"]["out_path"] = self._write_screenshot(run, png, str(out))
        self._charge(run, ledger, BudgetDimension.ARTIFACT_BYTES, ref.size_bytes)
        self._charge(run, ledger, BudgetDimension.TRANSFERRED_BYTES, ref.size_bytes)

    #: The **only** place WebGlass writes bytes to a path its caller chose.
    #: Grep for it: spec honesty h32 is that no other code path does, and
    #: ``tests/test_verbs_live.py`` asserts that mechanically.
    def _write_screenshot(self, run: _Run, png: bytes, out: str) -> str:
        """Write a WebGlass-rendered PNG to the caller's path.

        The distinction this method exists to keep sharp (spec scope boundary
        c35 / honesty h32): these bytes are **not** remote-origin response
        bytes. They are produced by Chromium's own screenshotter from the
        rendered page — WebGlass asked for a PNG and got a PNG, with no
        attacker-chosen filename, content type, or payload involved. That is
        why ``--out`` is a convenience here and would be a quarantine bypass
        for a download: a download's bytes and name come from the remote
        origin, so they stay in quarantine behind the shell-cli export bridge
        (issue #1 section 13, milestone M5).

        A write failure is a structured result, never an escaping ``OSError``.
        """
        path = Path(out).expanduser()
        try:
            parent = path.parent
            if str(parent):
                parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(png)
        except OSError as exc:
            raise _Halt(
                LifecycleState.FAILED,
                OperationError(
                    code=ERROR_ARTIFACT_WRITE_FAILED,
                    message=f"could not write the screenshot to {_sanitize(out, 200)}: "
                    f"{_sanitize(exc)}",
                    remediation=(
                        "choose a path in a directory you can write to; the artifact "
                        "itself is still stored content-addressed and reachable by hash"
                    ),
                ),
            ) from exc
        run.effect("artifact-written-to-caller-path")
        return str(path)

    def _screenshot_session(self, run: _Run) -> str:
        """The session a screenshot captures: explicit, just-opened, or a snapshot's."""
        if run.operation.session_id is not None:
            self._require_session(run, run.operation.session_id, lease=True)
            return run.operation.session_id
        if run.entry is not None:  # this operation opened the page itself
            return run.entry.session_id
        page_ref = run.operation.target.page_ref
        entry = self.snapshots.get(page_ref) if page_ref else None
        if entry is not None:
            return entry.session_id
        raise _Halt(
            LifecycleState.FAILED,
            OperationError(
                code=ERROR_INVALID_ARGUMENT,
                message="page.screenshot needs a session, a URL, or a retained snapshot",
                remediation=(
                    "set operation.session_id, target.url to open and capture in one "
                    "operation, or target.page_ref from a previous page.open"
                ),
            ),
        )

    def _op_action_press(
        self,
        run: _Run,
        ledger: BudgetLedger,
        cancel: CancellationToken | threading.Event | None,
        deadline: float | None,
    ) -> None:
        """Dispatch a key sequence.

        Only reachable when the effect-class resolver reports something other
        than ``remote-action``. The default resolver never does, so at M1 this
        body runs exclusively under a declared override (see
        :func:`default_effect_class`).
        """
        browser: BrowserBackend = self._require(self.browser, "browser backend", "browser")
        run.backend = self._backend_label(browser)
        keys = self._arg(run, "keys", required=True)
        if isinstance(keys, str):
            keys = [keys]
        session_id, session_info = self._session_for_navigation(run)
        run.trusted["session"] = session_info

        delay_ms = float(self._arg(run, "delay_ms", default=0))
        started = self.clock.now()
        pressed = browser.press(session_id, list(keys), delay_ms)
        elapsed = max(0.0, self.clock.now() - started)
        run.effect("browser-state-may-change")
        run.effect("keys-dispatched")
        self._step(run, cancel, deadline)
        self._charge(run, ledger, BudgetDimension.BROWSER_SECONDS, elapsed)

        # ``pressed`` echoes the caller's own request (trusted); ``key_log`` is
        # read back out of the page, so the page could have written it.
        run.trusted["press"] = {"session_id": session_id, "pressed": list(pressed.pressed)}
        run.untrusted["key_log"] = list(pressed.key_log)

    # -- handlers: sessions -------------------------------------------------

    def _op_session_create(
        self,
        run: _Run,
        ledger: BudgetLedger,
        cancel: CancellationToken | threading.Event | None,
        deadline: float | None,
    ) -> None:
        store: SessionStore = self._require(self.sessions, "session store", "sessions")
        run.backend = self._backend_label(store)
        now = self.clock.now()
        ttl = float(self._arg(run, "ttl_seconds", default=DEFAULT_SESSION_TTL_SECONDS))
        session_id = str(self._arg(run, "session_id", default="") or self.ids.new_id("session"))
        record = store.create(
            session_id=session_id,
            owner=run.context.caller,
            caller=run.context.caller,
            task=run.context.task,
            backend_id=self._backend_label(self.browser) if self.browser else "none",
            now=now,
            expires_at=now + ttl,
            capability_profile_ref=run.context.policy_profile_ref,
        )
        outcome = store.acquire_lease(
            record.session_id, self._lease_holder(run.context), now, DEFAULT_LEASE_TTL_SECONDS
        )
        run.effect("session-created")
        # to_public_dict() is the only session serialization used anywhere in
        # this module: endpoint_ref is secret-equivalent and never leaves the
        # store (issue #1 spec claim c33 / honesty h30).
        run.trusted["session"] = record.to_public_dict()
        run.trusted["lease"] = {"granted": not isinstance(outcome, LeaseRefusal)}

    def _op_session_list(
        self,
        run: _Run,
        ledger: BudgetLedger,
        cancel: CancellationToken | threading.Event | None,
        deadline: float | None,
    ) -> None:
        store: SessionStore = self._require(self.sessions, "session store", "sessions")
        run.backend = self._backend_label(store)
        # Only the caller's own sessions: a shared store must not become a
        # directory of other callers' live browsers.
        records = [record for record in store.list() if record.caller == run.context.caller]
        run.trusted["sessions"] = [record.to_public_dict() for record in records]
        run.trusted["session_count"] = len(records)

    def _op_session_show(
        self,
        run: _Run,
        ledger: BudgetLedger,
        cancel: CancellationToken | threading.Event | None,
        deadline: float | None,
    ) -> None:
        store: SessionStore = self._require(self.sessions, "session store", "sessions")
        run.backend = self._backend_label(store)
        record = self._require_session(run, self._session_argument(run, "show"), lease=False)
        run.trusted["session"] = record.to_public_dict()

    def _op_session_close(
        self,
        run: _Run,
        ledger: BudgetLedger,
        cancel: CancellationToken | threading.Event | None,
        deadline: float | None,
    ) -> None:
        store: SessionStore = self._require(self.sessions, "session store", "sessions")
        run.backend = self._backend_label(store)
        session_id = self._session_argument(run, "close")
        self._require_session(run, session_id, lease=False)
        store.close(session_id)
        if self.browser is not None:
            self.browser.close(session_id)
        run.effect("session-closed")
        record = store.get(session_id)
        run.trusted["session"] = record.to_public_dict() if record is not None else None
        # Closing a session touches no evidence record and no exploration edge
        # (issue #1 section 2, the four-state-kinds separation).

    def _op_session_clean(
        self,
        run: _Run,
        ledger: BudgetLedger,
        cancel: CancellationToken | threading.Event | None,
        deadline: float | None,
    ) -> None:
        store: SessionStore = self._require(self.sessions, "session store", "sessions")
        run.backend = self._backend_label(store)
        reaped = store.clean(self.clock.now())
        mine = [record for record in reaped if record.caller == run.context.caller]
        others = len(reaped) - len(mine)
        run.effect("sessions-expired")
        run.trusted["reaped"] = [record.to_public_dict() for record in mine]
        run.trusted["reaped_count"] = len(mine)
        if others:
            run.warn(
                f"{others} expired session(s) belonging to other callers were also reaped "
                "from this shared store and are not listed here"
            )

    def _session_argument(self, run: _Run, verb: str) -> str:
        session_id = str(self._arg(run, "session_id", default=run.operation.session_id) or "")
        if not session_id:
            raise _Halt(
                LifecycleState.FAILED,
                OperationError(
                    code=ERROR_INVALID_ARGUMENT,
                    message=f"session.{verb} requires a session id",
                    remediation="set operation.session_id",
                ),
            )
        return session_id

    # -- result assembly ----------------------------------------------------

    def _build_result(
        self,
        run: _Run,
        ledger: BudgetLedger,
        lifecycle: LifecycleState,
        error: OperationError | None,
    ) -> WebOperationResult:
        """Render the accumulated run as a result — on every path, terminal or not."""
        operation = run.operation
        trusted = dict(run.trusted)
        trusted["operation_id"] = operation.operation_id
        trusted["budget"] = ledger.to_dict()
        trusted["policy"] = {
            "source": self.policy.policy_source,
            "profile": self.policy.profile.name,
            "verdict": run.verdict.to_dict() if run.verdict is not None else None,
            "hops": [verdict.to_dict() for verdict in run.hop_verdicts],
        }
        trusted["cache"] = {
            "layer": "none",
            "note": "M1 has no cache layer; every observation on this result is live",
        }
        finished = self.clock.now()
        return WebOperationResult(
            operation_id=operation.operation_id,
            kind=operation.kind,
            lifecycle_state=lifecycle,
            content=TrustZones(
                trusted=trusted,
                untrusted=dict(run.untrusted),
                # Never populated at M1: no credential, form value, upload body,
                # or cookie passes through this service (issue #1 section 7).
                sensitive=dict(run.sensitive),
                derived=dict(run.derived),
            ),
            policy_verdict=to_result_verdict(run.verdict),
            known_effects=tuple(run.effects),
            # No evidence store exists before M3; minting ids nothing can
            # resolve would be worse than an honest empty tuple.
            evidence_refs=tuple(run.evidence_refs),
            navigation_history=tuple(run.navigation),
            cache=CacheFreshness(
                mode=operation.cache_mode, hit=False, age_seconds=None, stale=False
            ),
            completeness=run.completeness,
            warnings=tuple(run.warnings),
            degraded_evidence=run.degraded,
            timings=Timings(
                started_at=_iso(run.started_at),
                duration_seconds=max(0.0, finished - run.started_at),
            ),
            backend=run.backend,
            error=error,
        )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _resolve_kind(kind: OperationKind | str) -> OperationKind | None:
    try:
        return kind if isinstance(kind, OperationKind) else OperationKind(kind)
    except ValueError:
        return None


def _kind_value(kind: OperationKind | str) -> str:
    return kind.value if isinstance(kind, OperationKind) else str(kind)


def _chain_urls(requested_url: str, hops: Sequence[NavigationHop]) -> list[str]:
    """Flatten a navigation chain into the URL sequence policy must re-check.

    Every distinct URL the backend touched appears exactly once, in order,
    starting with the URL the caller asked for — which is what
    :meth:`~webglass.policy.WebPolicyEvaluator.evaluate_redirect_chain` expects.
    """
    urls: list[str] = [requested_url]
    for hop in hops:
        for candidate in (hop.requested_url, hop.response_url):
            if candidate and candidate != urls[-1]:
                urls.append(candidate)
    return urls


def _snapshot_identity(entry: SnapshotEntry) -> dict[str, Any]:
    """Trusted control metadata about a snapshot: identity and shape, no page text."""
    snapshot = entry.snapshot
    return {
        "schema_version": snapshot.schema_version,
        "snapshot_id": snapshot.snapshot_id,
        "generation": snapshot.generation,
        "session_id": entry.session_id,
        "backend": entry.backend,
        "status": snapshot.status,
        "retrieved_at": snapshot.retrieved_at,
        "block_count": len(snapshot.blocks),
        "link_count": len(snapshot.links),
        "form_count": len(snapshot.forms),
        "content_hash": snapshot.content_hash,
        "previous_content_hash": snapshot.previous_content_hash,
        "changed": snapshot.changed,
        "truncated": snapshot.truncated,
        "omission_counts": _omission_counts(snapshot),
    }


def _omission_counts(snapshot: PageSnapshot) -> dict[str, int]:
    counts: dict[str, int] = {}
    for omission in snapshot.omissions:
        counts[omission.kind.value] = counts.get(omission.kind.value, 0) + omission.count
    return counts


def _completeness(snapshot: PageSnapshot, *, read_omissions: int = 0) -> Completeness:
    """Declare what was left out — counts and kinds only, never page text."""
    regions = [f"{kind}:{count}" for kind, count in sorted(_omission_counts(snapshot).items())]
    if read_omissions:
        regions.append(f"read-budget:{read_omissions}")
    truncated = snapshot.truncated or bool(read_omissions)
    return Completeness(
        truncated=truncated,
        omitted_regions=tuple(regions),
        extraction_complete=not truncated,
    )


def _outline_text(node: Any) -> str:
    """Flatten one outline node (and its children) to text, for token accounting."""
    return "\n".join([node.text, *(_outline_text(child) for child in node.children)])


def _reference_halt(exc: SnapshotReferenceError) -> _Halt:
    """Map a reference failure onto a stable code — stale never looks unknown."""
    if isinstance(exc, StaleReferenceError):
        code = ERROR_STALE_REFERENCE
    elif isinstance(exc, UnknownReferenceError):
        code = ERROR_UNKNOWN_REFERENCE
    elif isinstance(exc, ReferenceSyntaxError):
        code = ERROR_INVALID_REFERENCE
    else:  # pragma: no cover - defensive: the base class is never raised directly
        code = ERROR_INVALID_REFERENCE
    return _Halt(
        LifecycleState.FAILED,
        OperationError(
            code=code,
            message=_sanitize(exc.message),
            remediation=_sanitize(exc.remediation, 400),
        ),
    )
