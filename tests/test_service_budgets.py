"""Budget, timeout, and cancellation contracts for :mod:`webglass.service`.

Spec claim c34 and honesty condition h31 (build plan t9): *every operation
charges the context budget — requests, transferred bytes, browser time,
artifact bytes, and agent-visible tokens as labeled heuristic estimates — and
enforces its timeout; exhaustion, timeout, and cancellation return structured
budget-exhausted / ``timed_out`` / ``cancelled`` lifecycle results with partial
evidence preserved, never a raw backend exception.*

The service's documented answers, all asserted below:

- **Budget exhaustion** → ``lifecycle_state = blocked`` **and**
  ``error.code = "budget_exhausted"``. Both, so a caller can switch on either
  the lifecycle or the stable code.
- **Reserve before, charge after.** A request budget with nothing left stops the
  operation before the backend is touched at all; a dimension whose size is only
  knowable after the work (bytes, seconds, artifact bytes, token estimate) is
  recorded even when it overshoots, and the overshoot blocks the *result*, not
  the accounting.
- **Timeout** → ``timed_out``; **cancellation** → ``cancelled``. Both preserve
  whatever evidence had accumulated, and both are observed at step boundaries
  using the injected clock, so every assertion here is deterministic.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from tests.test_service import (
    HOME_URL,
    NEXT_URL,
    RecordingBrowser,
    make_context,
    make_operation,
    make_service,
    open_page,
    routes,
    snapshot_id_of,
)
from webglass.adapters import FakeBrowserRoute, FixedClock
from webglass.context import WebContext
from webglass.effects import OperationKind
from webglass.operations import OperationTarget, ResourceLimits
from webglass.pages import TOKEN_ESTIMATE_METHOD
from webglass.results import LifecycleState
from webglass.service import (
    ERROR_BUDGET_EXHAUSTED,
    ERROR_CANCELLED,
    ERROR_TIMED_OUT,
    BudgetDimension,
    BudgetLedger,
    BudgetLimits,
    BudgetSpend,
    CancellationToken,
)


class SlowBrowser(RecordingBrowser):
    """Advances the injected clock on every navigation.

    This is how wall-clock behavior is tested without any real waiting: the
    service measures browser time and checks its deadline through the same
    injected :class:`~webglass.adapters.clock.Clock` this fake moves.
    """

    def __init__(self, clock: FixedClock, seconds: float, **kwargs: Any) -> None:
        super().__init__(routes(), **kwargs)
        self.clock = clock
        self.seconds = seconds

    def open(self, session_id: str, url: str):  # type: ignore[no-untyped-def]
        result = super().open(session_id, url)
        self.clock.advance(self.seconds)
        return result

    def screenshot(self, session_id: str) -> bytes:
        png = super().screenshot(session_id)
        self.clock.advance(self.seconds)
        return png


class CancellingBrowser(RecordingBrowser):
    """Cancels the caller's token from inside the backend call.

    Models the realistic case — the caller cancels while a navigation is
    already in flight — deterministically.
    """

    def __init__(self, token: CancellationToken, **kwargs: Any) -> None:
        super().__init__(routes(), **kwargs)
        self.token = token

    def open(self, session_id: str, url: str):  # type: ignore[no-untyped-def]
        result = super().open(session_id, url)
        self.token.cancel()
        return result


# ---------------------------------------------------------------------------
# BudgetLimits / BudgetSpend / BudgetLedger units
# ---------------------------------------------------------------------------


def test_limits_read_the_four_dimensions_a_web_context_carries() -> None:
    context = make_context(
        request_budget=3, byte_budget=100, time_budget_seconds=2.5, token_budget=50
    )
    limits = BudgetLimits.from_context(context)
    assert limits.requests == 3
    assert limits.transferred_bytes == 100
    assert limits.browser_seconds == 2.5
    assert limits.estimated_tokens == 50
    # WebContext has no artifact-bytes field (t8's module, not t9's to change),
    # so that ceiling is supplied at the boundary and defaults to unbounded.
    assert limits.artifact_bytes is None
    assert BudgetLimits.from_context(context, artifact_bytes=7).artifact_bytes == 7


def test_limits_cover_every_declared_dimension() -> None:
    limits = BudgetLimits()
    assert set(limits.to_dict()) == {dimension.value for dimension in BudgetDimension}
    assert all(limits.limit_for(dimension) is None for dimension in BudgetDimension)


def test_reserve_declines_without_spending_and_charge_records_the_overshoot() -> None:
    ledger = BudgetLedger(BudgetLimits(requests=1, transferred_bytes=10))

    assert ledger.reserve(BudgetDimension.REQUESTS) is None
    assert ledger.spend.requests == 1
    # The second reservation does not fit — and spends nothing.
    assert ledger.reserve(BudgetDimension.REQUESTS) is BudgetDimension.REQUESTS
    assert ledger.spend.requests == 1

    # A charge always records: the bytes really did cross the wire.
    assert ledger.charge(BudgetDimension.TRANSFERRED_BYTES, 25) is (
        BudgetDimension.TRANSFERRED_BYTES
    )
    assert ledger.spend.transferred_bytes == 25
    assert ledger.remaining(BudgetDimension.TRANSFERRED_BYTES) == 0
    assert ledger.remaining(BudgetDimension.ARTIFACT_BYTES) is None


def test_exhausted_lists_every_dimension_at_or_past_its_ceiling() -> None:
    ledger = BudgetLedger(BudgetLimits(requests=1, estimated_tokens=5))
    assert ledger.exhausted() == ()
    ledger.charge(BudgetDimension.REQUESTS, 1)
    ledger.charge(BudgetDimension.ESTIMATED_TOKENS, 9)
    assert set(ledger.exhausted()) == {BudgetDimension.REQUESTS, BudgetDimension.ESTIMATED_TOKENS}
    assert ledger.to_dict()["exhausted"] == ["requests", "estimated_tokens"]


def test_the_token_dimension_is_always_reported_as_a_labeled_estimate() -> None:
    report = BudgetLedger(BudgetLimits()).to_dict()
    assert report["token_estimate_method"] == TOKEN_ESTIMATE_METHOD
    assert "heuristic" in TOKEN_ESTIMATE_METHOD


def test_budget_spend_starts_at_zero_on_every_dimension() -> None:
    spend = BudgetSpend()
    assert spend.to_dict() == {dimension.value: 0 for dimension in BudgetDimension}


# ---------------------------------------------------------------------------
# Ledger scoping
# ---------------------------------------------------------------------------


def test_spend_accumulates_across_operations_in_one_context_scope() -> None:
    service = make_service()
    context = make_context()
    for _ in range(3):
        service.execute(
            make_operation(OperationKind.SEARCH, normalized_args={"query": "widgets"}), context
        )
    assert service.ledger_for(context).spend.requests == 3


def test_a_re_derived_context_cannot_reset_the_budget() -> None:
    service = make_service()
    parent = make_context(request_budget=5)
    service.execute(
        make_operation(OperationKind.SEARCH, normalized_args={"query": "widgets"}), parent
    )
    # Same scope triple, same ceilings — deliberately the same ledger.
    child = parent.with_reduced(evidence_namespace="child-ns")
    assert service.ledger_for(child) is service.ledger_for(parent)
    assert service.ledger_for(child).spend.requests == 1


def test_a_child_with_narrower_ceilings_gets_its_own_ledger() -> None:
    service = make_service()
    parent = make_context(request_budget=5)
    child = parent.with_reduced(request_budget=1)
    assert service.ledger_for(child) is not service.ledger_for(parent)
    assert service.ledger_for(child).limits.requests == 1


def test_reset_budgets_drops_every_ledger() -> None:
    service = make_service()
    context = make_context()
    open_page(service, context)
    assert service.ledger_for(context).spend.requests == 1
    service.reset_budgets()
    assert service.ledger_for(context).spend.requests == 0


def test_every_result_carries_the_budget_state() -> None:
    service = make_service()
    context = make_context(request_budget=4, token_budget=10_000)
    result = open_page(service, context)
    budget = result.content.trusted["budget"]

    assert budget["limits"]["requests"] == 4
    assert budget["spent"]["requests"] == 1
    assert budget["remaining"]["requests"] == 3
    assert budget["spent"]["transferred_bytes"] > 0
    assert budget["spent"]["estimated_tokens"] > 0
    assert budget["token_estimate_method"] == TOKEN_ESTIMATE_METHOD


# ---------------------------------------------------------------------------
# Exhaustion, per dimension
# ---------------------------------------------------------------------------


def test_an_exhausted_request_budget_blocks_before_the_backend_is_touched() -> None:
    browser = RecordingBrowser(routes())
    service = make_service(browser=browser)
    result = open_page(service, make_context(request_budget=0))

    assert result.lifecycle_state is LifecycleState.BLOCKED
    assert result.error is not None and result.error.code == ERROR_BUDGET_EXHAUSTED
    assert "the request was not issued" in result.error.message
    assert browser.opened == []
    assert result.degraded_evidence is False


def test_the_request_budget_is_consumed_across_operations() -> None:
    browser = RecordingBrowser(routes())
    service = make_service(browser=browser)
    context = make_context(request_budget=1)

    first = open_page(service, context)
    second = open_page(service, context, url=NEXT_URL)

    assert first.lifecycle_state is LifecycleState.SUCCEEDED
    assert second.lifecycle_state is LifecycleState.BLOCKED
    assert second.error is not None and second.error.code == ERROR_BUDGET_EXHAUSTED
    assert len(browser.opened) == 1


def test_an_exhausted_token_budget_blocks_but_preserves_the_observation() -> None:
    service = make_service()
    result = open_page(service, make_context(token_budget=1))

    assert result.lifecycle_state is LifecycleState.BLOCKED
    assert result.error is not None and result.error.code == ERROR_BUDGET_EXHAUSTED
    assert "estimated_tokens" in result.error.message
    assert TOKEN_ESTIMATE_METHOD in result.error.remediation
    # Partial evidence: the page really was fetched, so it is reported.
    assert result.content.untrusted["page_card"]["title"] == "Widget Fixture"
    assert result.degraded_evidence is True
    assert result.content.trusted["budget"]["exhausted"] == ["estimated_tokens"]


def test_an_exhausted_byte_budget_blocks_with_the_bytes_recorded() -> None:
    service = make_service()
    context = make_context(byte_budget=10)
    result = open_page(service, context)

    assert result.lifecycle_state is LifecycleState.BLOCKED
    assert result.error is not None and result.error.code == ERROR_BUDGET_EXHAUSTED
    assert "transferred_bytes" in result.error.message
    assert service.ledger_for(context).spend.transferred_bytes > 10


def test_an_exhausted_browser_time_budget_blocks() -> None:
    clock = FixedClock(500.0)
    service = make_service(clock=clock, browser=SlowBrowser(clock, seconds=5.0))
    result = open_page(service, make_context(time_budget_seconds=1.0))

    assert result.lifecycle_state is LifecycleState.BLOCKED
    assert result.error is not None and result.error.code == ERROR_BUDGET_EXHAUSTED
    assert "browser_seconds" in result.error.message


def test_an_exhausted_artifact_budget_blocks_after_the_artifact_is_stored() -> None:
    service = make_service(default_artifact_byte_budget=1)
    context = make_context()
    snapshot_id = snapshot_id_of(open_page(service, context))
    result = service.execute(
        make_operation(OperationKind.PAGE_SCREENSHOT, target=OperationTarget(page_ref=snapshot_id)),
        context,
    )

    assert result.lifecycle_state is LifecycleState.BLOCKED
    assert result.error is not None and result.error.code == ERROR_BUDGET_EXHAUSTED
    assert "artifact_bytes" in result.error.message
    # The artifact was really stored, so its reference is still reported.
    assert result.content.trusted["artifact"]["size_bytes"] > 1


def test_budget_exhaustion_is_never_raised_at_the_caller() -> None:
    service = make_service()
    context = make_context(request_budget=0, byte_budget=0, token_budget=0)
    for kind, kwargs in (
        (OperationKind.SEARCH, {"normalized_args": {"query": "widgets"}}),
        (OperationKind.PAGE_OPEN, {"target": OperationTarget(url=HOME_URL)}),
    ):
        result = service.execute(make_operation(kind, **kwargs), context)
        assert result.lifecycle_state is LifecycleState.BLOCKED
        assert result.error is not None and result.error.code == ERROR_BUDGET_EXHAUSTED


# ---------------------------------------------------------------------------
# Timeouts
# ---------------------------------------------------------------------------


def test_a_slow_navigation_times_out_with_its_partial_evidence() -> None:
    clock = FixedClock(500.0)
    service = make_service(clock=clock, browser=SlowBrowser(clock, seconds=5.0))
    result = open_page(service, make_context(), limits=ResourceLimits(timeout_seconds=1.0))

    assert result.lifecycle_state is LifecycleState.TIMED_OUT
    assert result.error is not None and result.error.code == ERROR_TIMED_OUT
    assert "1.0s timeout" in result.error.message
    # The hop that did happen is preserved rather than discarded.
    assert [hop.requested_url for hop in result.navigation_history] == [HOME_URL]
    assert result.degraded_evidence is True
    assert result.timings.duration_seconds == 5.0


def test_an_already_expired_deadline_stops_before_dispatch() -> None:
    browser = RecordingBrowser(routes())
    service = make_service(browser=browser)
    result = open_page(service, make_context(), limits=ResourceLimits(timeout_seconds=0.0))

    assert result.lifecycle_state is LifecycleState.TIMED_OUT
    assert browser.opened == [], "a zero-second budget buys no work"
    assert result.degraded_evidence is False


def test_an_operation_that_finishes_inside_its_timeout_succeeds() -> None:
    clock = FixedClock(500.0)
    service = make_service(clock=clock, browser=SlowBrowser(clock, seconds=0.5))
    result = open_page(service, make_context(), limits=ResourceLimits(timeout_seconds=10.0))

    assert result.lifecycle_state is LifecycleState.SUCCEEDED
    assert result.timings.duration_seconds == 0.5


def test_no_timeout_means_no_deadline() -> None:
    clock = FixedClock(500.0)
    service = make_service(clock=clock, browser=SlowBrowser(clock, seconds=3600.0))
    result = open_page(service, make_context())
    assert result.lifecycle_state is LifecycleState.SUCCEEDED


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


def test_cancelling_during_a_navigation_yields_cancelled_with_partial_evidence() -> None:
    token = CancellationToken()
    service = make_service(browser=CancellingBrowser(token))
    result = service.execute(
        make_operation(OperationKind.PAGE_OPEN, target=OperationTarget(url=HOME_URL)),
        make_context(),
        cancel=token,
    )

    assert result.lifecycle_state is LifecycleState.CANCELLED
    assert result.error is not None and result.error.code == ERROR_CANCELLED
    assert [hop.requested_url for hop in result.navigation_history] == [HOME_URL]
    assert result.degraded_evidence is True
    # The budget report still renders: a cancelled operation still cost something.
    assert result.content.trusted["budget"]["spent"]["requests"] == 1


def test_cancelling_before_dispatch_touches_no_backend() -> None:
    browser = RecordingBrowser(routes())
    service = make_service(browser=browser)
    token = CancellationToken()
    token.cancel()
    result = service.execute(
        make_operation(OperationKind.PAGE_OPEN, target=OperationTarget(url=HOME_URL)),
        make_context(),
        cancel=token,
    )

    assert result.lifecycle_state is LifecycleState.CANCELLED
    assert browser.opened == []
    assert result.degraded_evidence is False


def test_a_bare_threading_event_works_as_a_cancellation_handle() -> None:
    event = threading.Event()
    event.set()
    service = make_service()
    result = service.execute(
        make_operation(OperationKind.SEARCH, normalized_args={"query": "widgets"}),
        make_context(),
        cancel=event,
    )
    assert result.lifecycle_state is LifecycleState.CANCELLED


def test_an_unset_handle_never_interferes() -> None:
    service = make_service()
    result = service.execute(
        make_operation(OperationKind.PAGE_OPEN, target=OperationTarget(url=HOME_URL)),
        make_context(),
        cancel=CancellationToken(),
    )
    assert result.lifecycle_state is LifecycleState.SUCCEEDED


def test_cancellation_token_wraps_an_event_and_is_idempotent() -> None:
    event = threading.Event()
    token = CancellationToken(event)
    assert token.cancelled is False
    token.cancel()
    token.cancel()
    assert token.is_set() is True
    assert token.cancelled is True
    assert event.is_set() is True


def test_cancellation_wins_over_a_deadline_that_expired_in_the_same_instant() -> None:
    """A caller who cancelled wants ``cancelled``, not a coincidental timeout."""
    token = CancellationToken()
    token.cancel()
    service = make_service()
    result = service.execute(
        make_operation(
            OperationKind.PAGE_OPEN,
            target=OperationTarget(url=HOME_URL),
            limits=ResourceLimits(timeout_seconds=0.0),
        ),
        make_context(),
        cancel=token,
    )
    assert result.lifecycle_state is LifecycleState.CANCELLED


# ---------------------------------------------------------------------------
# Interaction with the rest of the result contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "context_kwargs, expected",
    [
        ({"request_budget": 0}, LifecycleState.BLOCKED),
        ({}, LifecycleState.SUCCEEDED),
    ],
)
def test_terminal_states_still_carry_timings_cache_and_policy(
    context_kwargs: dict[str, Any], expected: LifecycleState
) -> None:
    service = make_service()
    result = open_page(service, make_context(**context_kwargs))
    assert result.lifecycle_state is expected
    assert result.timings.started_at is not None
    assert result.cache is not None
    assert result.content.trusted["policy"]["verdict"] is not None
    assert result.backend == "RecordingBrowser"


def test_a_lens_charges_tokens_but_not_a_request() -> None:
    service = make_service()
    context = make_context()
    snapshot_id = snapshot_id_of(open_page(service, context))
    before = service.ledger_for(context).spend.requests
    result = service.execute(
        make_operation(OperationKind.PAGE_READ, target=OperationTarget(page_ref=snapshot_id)),
        context,
    )
    assert result.lifecycle_state is LifecycleState.SUCCEEDED
    assert service.ledger_for(context).spend.requests == before
    assert result.content.derived["read"]["usage"]["estimate_method"] == TOKEN_ESTIMATE_METHOD


def test_an_empty_page_charges_nothing_it_did_not_use() -> None:
    empty_url = "http://example.com/empty"
    service = make_service(
        browser=RecordingBrowser({empty_url: FakeBrowserRoute(status=204, html="")})
    )
    context = make_context()
    result = open_page(service, context, url=empty_url)
    assert result.lifecycle_state is LifecycleState.SUCCEEDED
    assert service.ledger_for(context).spend.transferred_bytes == 0
    assert service.ledger_for(context).spend.estimated_tokens == 0


def test_the_context_budget_is_read_from_the_web_context_not_the_service() -> None:
    service = make_service()
    generous = WebContext(
        caller="a", task="t", workspace="w", policy_profile_ref=None, evidence_namespace="ns"
    )
    stingy = WebContext(
        caller="b",
        task="t",
        workspace="w",
        policy_profile_ref=None,
        evidence_namespace="ns",
        request_budget=0,
    )
    assert open_page(service, generous).lifecycle_state is LifecycleState.SUCCEEDED
    assert open_page(service, stingy).lifecycle_state is LifecycleState.BLOCKED
