"""Snapshot-scoped reference contract (``webglass.references``).

Issue #1 section 5: "Element references are valid only for the snapshot /
session generation that produced them -- a stale reference must fail clearly
rather than hit a different element." These tests pin the two halves of that
promise: a lossless parse/format round-trip (so a reference survives a CLI
``--json`` hop and comes back identical), and a loud, typed failure whenever a
reference is applied outside the snapshot generation that issued it.
"""

from __future__ import annotations

import pytest

from webglass.references import (
    ReferenceSyntaxError,
    RefKind,
    SnapshotRef,
    SnapshotReferenceError,
    StaleReferenceError,
    UnknownReferenceError,
    parse_qualified_ref,
    parse_ref,
    validate_snapshot_id,
)

SNAP_A = "snap-a"
SNAP_B = "snap-b"


# --- formatting -------------------------------------------------------------


def test_short_form_is_kind_colon_index() -> None:
    assert SnapshotRef(SNAP_A, 0, RefKind.BLOCK, 27).short == "block:27"


def test_qualified_form_carries_the_snapshot_scope() -> None:
    assert SnapshotRef(SNAP_A, 3, RefKind.LINK, 12).qualified == "snap-a@3/link:12"


def test_str_renders_the_qualified_form_so_logs_are_never_ambiguous() -> None:
    assert str(SnapshotRef(SNAP_A, 0, RefKind.FORM, 1)) == "snap-a@0/form:1"


def test_every_ref_kind_formats_and_round_trips() -> None:
    for kind, index in (
        (RefKind.BLOCK, 27),
        (RefKind.LINK, 12),
        (RefKind.FIELD, 3),
        (RefKind.FORM, 1),
        (RefKind.BUTTON, 2),
    ):
        ref = SnapshotRef(SNAP_A, 2, kind, index)
        assert ref.short == f"{kind.value}:{index}"
        assert parse_qualified_ref(ref.qualified) == ref


def test_short_round_trip_requires_the_caller_to_supply_the_scope() -> None:
    ref = SnapshotRef(SNAP_A, 4, RefKind.FIELD, 3)
    assert parse_ref(ref.short, snapshot_id=SNAP_A, generation=4) == ref


def test_kind_accepts_a_plain_string_and_normalizes_to_the_enum() -> None:
    assert SnapshotRef(SNAP_A, 0, "block", 1).kind is RefKind.BLOCK


def test_to_dict_is_json_safe_and_complete() -> None:
    ref = SnapshotRef(SNAP_A, 1, RefKind.BUTTON, 2)
    assert ref.to_dict() == {
        "snapshot_id": SNAP_A,
        "generation": 1,
        "kind": "button",
        "index": 2,
        "ref": "button:2",
    }


# --- parsing rejects ambiguity ---------------------------------------------


def test_parse_ref_rejects_an_unknown_kind() -> None:
    with pytest.raises(ReferenceSyntaxError) as excinfo:
        parse_ref("widget:1", snapshot_id=SNAP_A, generation=0)
    assert "widget" in str(excinfo.value)


def test_parse_ref_rejects_a_malformed_string() -> None:
    for bad in ("block", "block:", ":1", "block:one", "block:-1", "", "block:1:2"):
        with pytest.raises(ReferenceSyntaxError):
            parse_ref(bad, snapshot_id=SNAP_A, generation=0)


def test_parse_ref_rejects_a_qualified_string() -> None:
    # The scope must come from the caller, never be smuggled in via the text.
    with pytest.raises(ReferenceSyntaxError):
        parse_ref("snap-a@0/block:1", snapshot_id=SNAP_A, generation=0)


def test_parse_qualified_ref_rejects_the_short_form() -> None:
    with pytest.raises(ReferenceSyntaxError):
        parse_qualified_ref("block:1")


def test_parse_qualified_ref_rejects_a_malformed_string() -> None:
    for bad in ("snap-a/block:1", "snap-a@x/block:1", "@0/block:1", "snap-a@0/block"):
        with pytest.raises(ReferenceSyntaxError):
            parse_qualified_ref(bad)


def test_syntax_error_is_also_a_value_error() -> None:
    assert issubclass(ReferenceSyntaxError, ValueError)


# --- construction validation ------------------------------------------------


def test_snapshot_id_must_not_contain_the_qualified_form_delimiters() -> None:
    for bad in ("bad@id", "bad/id", "bad:id", "bad id"):
        with pytest.raises(ReferenceSyntaxError):
            SnapshotRef(bad, 0, RefKind.BLOCK, 0)


def test_snapshot_id_must_not_be_empty() -> None:
    with pytest.raises(ReferenceSyntaxError):
        validate_snapshot_id("")


def test_negative_index_and_generation_are_rejected() -> None:
    with pytest.raises(ReferenceSyntaxError):
        SnapshotRef(SNAP_A, 0, RefKind.BLOCK, -1)
    with pytest.raises(ReferenceSyntaxError):
        SnapshotRef(SNAP_A, -1, RefKind.BLOCK, 0)


def test_ref_is_frozen_and_hashable() -> None:
    ref = SnapshotRef(SNAP_A, 0, RefKind.BLOCK, 1)
    assert {ref, SnapshotRef(SNAP_A, 0, RefKind.BLOCK, 1)} == {ref}
    with pytest.raises(Exception):
        ref.index = 2  # type: ignore[misc]


# --- staleness fails clearly ------------------------------------------------


def test_a_ref_from_another_snapshot_is_stale_never_silently_resolved() -> None:
    ref = SnapshotRef(SNAP_A, 0, RefKind.BLOCK, 3)
    assert ref.scoped_to(SNAP_A, 0) is True
    assert ref.scoped_to(SNAP_B, 0) is False
    with pytest.raises(StaleReferenceError):
        ref.require_scope(SNAP_B, 0)


def test_a_ref_from_an_older_generation_of_the_same_snapshot_is_stale() -> None:
    ref = SnapshotRef(SNAP_A, 0, RefKind.FIELD, 3)
    with pytest.raises(StaleReferenceError):
        ref.require_scope(SNAP_A, 1)


def test_require_scope_is_silent_when_the_scope_matches() -> None:
    ref = SnapshotRef(SNAP_A, 7, RefKind.LINK, 0)
    assert ref.require_scope(SNAP_A, 7) is None


def test_stale_error_names_both_scopes_and_carries_a_remediation() -> None:
    ref = SnapshotRef(SNAP_A, 0, RefKind.BLOCK, 3)
    with pytest.raises(StaleReferenceError) as excinfo:
        ref.require_scope(SNAP_B, 2)
    error = excinfo.value
    text = str(error)
    assert "snap-a@0/block:3" in text
    assert SNAP_B in text and "2" in text
    assert error.remediation
    assert error.ref == ref


def test_every_reference_error_shares_one_base() -> None:
    for cls in (StaleReferenceError, UnknownReferenceError, ReferenceSyntaxError):
        assert issubclass(cls, SnapshotReferenceError)


def test_unknown_reference_error_carries_message_and_remediation() -> None:
    error = UnknownReferenceError("no such block", remediation="re-read the page")
    assert error.message == "no such block"
    assert error.remediation == "re-read the page"
