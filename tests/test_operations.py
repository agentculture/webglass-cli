"""Tests for webglass.operations — the WebOperation model.

Covers build-plan task t4's acceptance criteria: WebOperation carries the
issue #1 section-1 field set including schema_version (starting at 1), and
every operation kind declares exactly one effect class via a derived,
non-settable property.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from webglass.effects import EffectClass, OperationKind, classify
from webglass.operations import (
    SCHEMA_VERSION,
    ApplyState,
    CacheMode,
    CallerContext,
    ContentBudget,
    OperationTarget,
    ResourceLimits,
    WebOperation,
)


def _minimal(kind: OperationKind = OperationKind.SEARCH, **overrides: object) -> WebOperation:
    return WebOperation(operation_id="op-1", kind=kind, **overrides)  # type: ignore[arg-type]


def test_schema_version_starts_at_1() -> None:
    assert SCHEMA_VERSION == 1
    assert _minimal().schema_version == 1


def test_minimal_construction_applies_field_set_defaults() -> None:
    op = _minimal()
    assert op.operation_id == "op-1"
    assert op.kind == OperationKind.SEARCH
    assert op.normalized_args == {}
    assert op.caller == CallerContext()
    assert op.session_id is None
    assert op.exploration_id is None
    assert op.target == OperationTarget()
    assert op.cache_mode == CacheMode.LIVE
    assert op.content_budget == ContentBudget()
    assert op.limits == ResourceLimits()
    assert op.apply_state == ApplyState.PREVIEW
    assert op.kind_version == 1


@pytest.mark.parametrize("kind", list(OperationKind))
def test_effect_class_is_derived_from_kind_for_every_m0_m2_kind(kind: OperationKind) -> None:
    op = _minimal(kind=kind)
    assert op.effect_class == classify(kind)


def test_effect_class_cannot_be_assigned_directly() -> None:
    op = _minimal()
    with pytest.raises(AttributeError):
        op.effect_class = EffectClass.OBSERVE  # type: ignore[misc]


def test_action_press_operation_is_remote_action_by_default() -> None:
    op = _minimal(kind=OperationKind.ACTION_PRESS)
    assert op.effect_class == EffectClass.REMOTE_ACTION


def test_session_create_operation_is_local_state() -> None:
    op = _minimal(kind=OperationKind.SESSION_CREATE)
    assert op.effect_class == EffectClass.LOCAL_STATE


def test_operation_is_frozen() -> None:
    op = _minimal()
    with pytest.raises(dataclasses.FrozenInstanceError):
        op.operation_id = "op-2"  # type: ignore[misc]


def test_full_field_set_round_trips_through_to_dict() -> None:
    op = WebOperation(
        operation_id="op-42",
        kind=OperationKind.PAGE_OPEN,
        normalized_args={"url": "https://example.test/"},
        caller=CallerContext(
            caller_id="colleague-1",
            task_id="task-9",
            workspace_id="ws-3",
            intent="research the pricing page",
            capability_profile_ref="cap:default",
            policy_profile_ref="policy:default",
        ),
        session_id="session-1",
        exploration_id="exploration-1",
        target=OperationTarget(url="https://example.test/pricing"),
        cache_mode=CacheMode.REFRESH,
        content_budget=ContentBudget(max_bytes=65536, max_blocks=50, max_tokens_estimate=2000),
        limits=ResourceLimits(timeout_seconds=30.0, max_redirects=5, max_response_bytes=1 << 20),
        apply_state=ApplyState.PREVIEW,
    )
    payload = op.to_dict()

    assert payload["schema_version"] == 1
    assert payload["operation_id"] == "op-42"
    assert payload["kind"] == "page.open"
    assert payload["kind_version"] == 1
    assert payload["normalized_args"] == {"url": "https://example.test/"}
    assert payload["caller"]["caller_id"] == "colleague-1"
    assert payload["caller"]["intent"] == "research the pricing page"
    assert payload["session_id"] == "session-1"
    assert payload["exploration_id"] == "exploration-1"
    assert payload["target"]["url"] == "https://example.test/pricing"
    assert payload["cache_mode"] == "refresh"
    assert payload["content_budget"]["max_tokens_estimate"] == 2000
    assert payload["limits"]["timeout_seconds"] == 30.0
    assert payload["effect_class"] == "observe"
    assert payload["apply_state"] == "preview"

    # Round trip: to_dict()'s output is plain JSON-safe data (no leftover
    # Enum/dataclass instances) — encode/decode reproduces the same shape.
    round_tripped = json.loads(json.dumps(payload))
    assert round_tripped == payload


def test_all_cache_modes_are_the_five_documented_values() -> None:
    assert {mode.value for mode in CacheMode} == {
        "live",
        "prefer-cache",
        "refresh",
        "cache-only",
        "no-store",
    }


def test_apply_state_defaults_to_preview_for_safety() -> None:
    assert ApplyState.PREVIEW.value == "preview"
    assert ApplyState.APPLY.value == "apply"
    assert _minimal().apply_state == ApplyState.PREVIEW


def test_operation_target_to_dict_carries_all_four_reference_kinds() -> None:
    target = OperationTarget(
        url="https://example.test/",
        page_ref="page:1",
        element_ref="link:12",
        evidence_ref="evidence:abc",
    )
    assert target.to_dict() == {
        "url": "https://example.test/",
        "page_ref": "page:1",
        "element_ref": "link:12",
        "evidence_ref": "evidence:abc",
    }


def test_kind_stored_as_plain_string_still_serializes_in_to_dict() -> None:
    # kind is typed as OperationKind, but to_dict()'s _kind_value helper must
    # not assume it always got a real enum member (defence in depth against
    # a caller constructing the dataclass with a bare string despite typing).
    op = _minimal()
    object.__setattr__(op, "kind", "search")
    assert op.to_dict()["kind"] == "search"
