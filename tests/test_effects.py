"""Tests for webglass.effects — effect classification and classify-upward.

Covers build-plan task t4's acceptance criterion: "every operation kind
declares exactly one effect class and ambiguous classification resolves
upward (unit-tested)".
"""

from __future__ import annotations

import pytest

from webglass.effects import EFFECT_CLASS_BY_KIND, EffectClass, OperationKind, classify


def test_effect_class_values_match_issue_1_vocabulary() -> None:
    assert EffectClass.OBSERVE.value == "observe"
    assert EffectClass.LOCAL_STATE.value == "local-state"
    assert EffectClass.REMOTE_ACTION.value == "remote-action"


def test_every_operation_kind_has_exactly_one_declared_effect_class() -> None:
    # Completeness guard: if a new OperationKind member is ever added without
    # updating the mapping, this fails loudly instead of silently falling
    # through to classify()'s upward default.
    assert set(EFFECT_CLASS_BY_KIND.keys()) == set(OperationKind)
    for kind in OperationKind:
        assert isinstance(EFFECT_CLASS_BY_KIND[kind], EffectClass)


@pytest.mark.parametrize("kind", list(OperationKind))
def test_classify_known_kind_returns_declared_class(kind: OperationKind) -> None:
    assert classify(kind) == EFFECT_CLASS_BY_KIND[kind]


@pytest.mark.parametrize(
    "kind",
    [
        OperationKind.SEARCH,
        OperationKind.PAGE_OPEN,
        OperationKind.PAGE_READ,
        OperationKind.PAGE_INSPECT,
        OperationKind.PAGE_EXTRACT,
        OperationKind.PAGE_LINKS,
        OperationKind.PAGE_SCREENSHOT,
        OperationKind.ACTION_FOLLOW,
    ],
)
def test_read_oriented_kinds_classify_observe(kind: OperationKind) -> None:
    assert classify(kind) == EffectClass.OBSERVE


@pytest.mark.parametrize(
    "kind",
    [
        OperationKind.SESSION_CREATE,
        OperationKind.SESSION_LIST,
        OperationKind.SESSION_SHOW,
        OperationKind.SESSION_CLOSE,
        OperationKind.SESSION_CLEAN,
    ],
)
def test_session_kinds_classify_local_state(kind: OperationKind) -> None:
    assert classify(kind) == EffectClass.LOCAL_STATE


def test_action_press_classifies_remote_action_by_default() -> None:
    # honesty condition: "ambiguous controls classify as remote-action" —
    # outside an explicit test-profile override (a policy-layer decision,
    # not this module's job), a key press cannot be proven navigational.
    assert classify(OperationKind.ACTION_PRESS) == EffectClass.REMOTE_ACTION


@pytest.mark.parametrize(
    "unknown_kind",
    [
        "action.submit",  # a real future verb, just not implemented at M0-M2
        "memory.forget",
        "banana",
        "",
        "PAGE.OPEN",  # case mismatch must not accidentally resolve
    ],
)
def test_classify_unknown_kind_string_resolves_upward(unknown_kind: str) -> None:
    assert classify(unknown_kind) == EffectClass.REMOTE_ACTION


def test_classify_accepts_known_kind_as_plain_string() -> None:
    # A kind string off the wire (CLI arg, JSON payload) should not need to
    # be constructed into the enum by the caller first.
    assert classify("search") == EffectClass.OBSERVE
    assert classify("session.create") == EffectClass.LOCAL_STATE


def test_classify_never_returns_a_more_permissive_class_than_declared() -> None:
    # Sweep: no unknown value ever classifies as anything other than the
    # most conservative class.
    for candidate in ["totally-unknown-kind", "page.delete", "action.click"]:
        assert classify(candidate) is EffectClass.REMOTE_ACTION
