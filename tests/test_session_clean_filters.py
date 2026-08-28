"""``session clean``'s filter flags (build plan task t9, issue #14).

Covers spec claims c13/c5/h3/h5: ``--older-than``/``--status``/``--site``
compose as AND, are evaluated inside :meth:`FileSessionStore.clean` under its
per-record lock (never post-filtered by the CLI), a malformed duration is a
structured user-input error rather than a silent default, and ``--site``
never reaps a record whose navigation history is *unknown* (``hosts is
None``) -- only one that is *tracked and known not to include the site*.
"""

from __future__ import annotations

import json
import time

import pytest

from tests.conftest import SeedSessionRecords
from webglass.adapters.session_store import FileSessionStore
from webglass.cli import main
from webglass.sessions import Lease, SessionStatus

CALLER = "cli"


# --- store-level: filters evaluated inside clean(), compose as AND --------


def test_older_than_only_reaps_records_at_least_that_old(
    session_store,
    seed_session_records,
) -> None:
    now = 10_000_000.0

    # Expired 5 minutes ago -- too young for a 1-hour --older-than.
    young = seed_session_records(
        count=1,
        status=SessionStatus.ACTIVE,
        now=now - 300.0,
        expires_at=now - 300.0,
        session_id_prefix="young",
    )[0]
    # Expired 2 hours ago -- old enough.
    old = seed_session_records(
        count=1,
        status=SessionStatus.ACTIVE,
        now=now - 7200.0,
        expires_at=now - 7200.0,
        session_id_prefix="old",
    )[0]

    reaped = session_store.clean(now, older_than_seconds=3600.0)

    reaped_ids = {record.session_id for record in reaped}
    assert reaped_ids == {old.session_id}
    assert young.session_id not in reaped_ids


def test_older_than_with_no_match_reaps_nothing(
    session_store,
    seed_session_records,
) -> None:
    """An unmatched filter reaps zero records -- never falls back to unfiltered."""
    now = 10_000_000.0
    seed_session_records(
        count=1,
        status=SessionStatus.ACTIVE,
        now=now - 300.0,
        expires_at=now - 300.0,
        session_id_prefix="expired-but-young",
    )

    reaped = session_store.clean(now, older_than_seconds=30 * 24 * 3600.0)

    assert reaped == []
    # The record itself is untouched -- still active, still on disk, ready
    # to be reaped by a later, less restrictive sweep.
    remaining = session_store.list()
    assert len(remaining) == 1
    assert remaining[0].status is SessionStatus.ACTIVE


def test_status_filter_restricts_which_records_are_eligible(
    session_store,
    seed_session_records,
) -> None:
    now = 10_000_000.0
    active_expired = seed_session_records(
        count=1,
        status=SessionStatus.ACTIVE,
        now=now - 3600.0,
        expires_at=now - 3600.0,
        session_id_prefix="active",
    )[0]
    # Closed, well past retention -- purgeable, but filtered to only 'active'.
    seed_session_records(
        count=1,
        status=SessionStatus.CLOSED,
        now=now - 30 * 24 * 3600.0,
        expires_at=now - 30 * 24 * 3600.0,
        session_id_prefix="closed",
    )

    reaped = session_store.clean(now, status=SessionStatus.ACTIVE)

    assert {record.session_id for record in reaped} == {active_expired.session_id}
    # The closed record was not purged: it must still be listable.
    remaining_ids = {record.session_id for record in session_store.list()}
    assert any(sid.startswith("closed-") for sid in remaining_ids)


def test_status_filter_gates_the_purge_job_too(
    session_store,
    seed_session_records,
) -> None:
    """``--status closed`` lets a long-dead closed record purge, ignoring actives."""
    now = 10_000_000.0
    seed_session_records(
        count=1,
        status=SessionStatus.CLOSED,
        now=now - 30 * 24 * 3600.0,
        expires_at=now - 30 * 24 * 3600.0,
        session_id_prefix="closed",
    )
    seed_session_records(
        count=1,
        status=SessionStatus.ACTIVE,
        now=now - 3600.0,
        expires_at=now - 3600.0,
        session_id_prefix="active",
    )

    reaped = session_store.clean(now, status=SessionStatus.CLOSED)

    # Purges are never in the returned list (only case 1 is), but the active
    # record must survive since it doesn't match status=closed.
    assert reaped == []
    remaining_ids = {record.session_id for record in session_store.list()}
    assert not any(sid.startswith("closed-") for sid in remaining_ids)
    assert any(sid.startswith("active-") for sid in remaining_ids)


def test_site_never_reaps_a_record_with_unknown_hosts(
    session_store,
    seed_session_records,
) -> None:
    """``hosts is None`` (never tracked) must never be treated as 'confirmed absent'."""
    now = 10_000_000.0
    seed_session_records(
        count=1,
        status=SessionStatus.ACTIVE,
        now=now - 3600.0,
        expires_at=now - 3600.0,
        session_id_prefix="unknown-hosts",
        # hosts omitted entirely -> None, "never tracked".
    )

    reaped = session_store.clean(now, site="example.com")

    assert reaped == []


def test_site_reaps_a_record_with_a_matching_tracked_host(
    session_store,
    seed_session_records,
) -> None:
    now = 10_000_000.0
    matching = seed_session_records(
        count=1,
        status=SessionStatus.ACTIVE,
        now=now - 3600.0,
        expires_at=now - 3600.0,
        session_id_prefix="matches",
        hosts=("example.com", "other.example"),
    )[0]
    seed_session_records(
        count=1,
        status=SessionStatus.ACTIVE,
        now=now - 3600.0,
        expires_at=now - 3600.0,
        session_id_prefix="no-match",
        hosts=("elsewhere.example",),
    )
    seed_session_records(
        count=1,
        status=SessionStatus.ACTIVE,
        now=now - 3600.0,
        expires_at=now - 3600.0,
        session_id_prefix="tracked-empty",
        hosts=(),
    )

    reaped = session_store.clean(now, site="example.com")

    assert {record.session_id for record in reaped} == {matching.session_id}


def test_filters_compose_as_and(
    session_store,
    seed_session_records,
) -> None:
    now = 10_000_000.0
    # Old enough and right status, but wrong site -- must not be reaped.
    seed_session_records(
        count=1,
        status=SessionStatus.ACTIVE,
        now=now - 7200.0,
        expires_at=now - 7200.0,
        session_id_prefix="wrong-site",
        hosts=("nope.example",),
    )
    # Matches every filter.
    matches_all = seed_session_records(
        count=1,
        status=SessionStatus.ACTIVE,
        now=now - 7200.0,
        expires_at=now - 7200.0,
        session_id_prefix="matches-all",
        hosts=("example.com",),
    )[0]
    # Right site and status, but too young for --older-than.
    seed_session_records(
        count=1,
        status=SessionStatus.ACTIVE,
        now=now - 60.0,
        expires_at=now - 60.0,
        session_id_prefix="too-young",
        hosts=("example.com",),
    )

    reaped = session_store.clean(
        now,
        older_than_seconds=3600.0,
        status=SessionStatus.ACTIVE,
        site="example.com",
    )

    assert {record.session_id for record in reaped} == {matches_all.session_id}


def test_clean_still_accepts_no_filters_at_all(
    session_store,
    seed_session_records,
) -> None:
    """The historical no-argument call stays exactly as permissive as before."""
    now = 10_000_000.0
    expired = seed_session_records(
        count=1,
        status=SessionStatus.ACTIVE,
        now=now - 60.0,
        expires_at=now - 60.0,
        session_id_prefix="expired",
    )[0]

    reaped = session_store.clean(now)

    assert {record.session_id for record in reaped} == {expired.session_id}


# --- CLI-level: malformed duration is a structured, loud user-input error -


@pytest.mark.parametrize("bogus", ["banana", "10x", "5 m", "--older-than=-5m"])
def test_older_than_malformed_duration_is_a_structured_error(
    bogus: str, capsys: pytest.CaptureFixture[str]
) -> None:
    if bogus.startswith("--older-than="):
        argv = ["session", "clean", bogus]
    else:
        argv = ["session", "clean", "--older-than", bogus]

    rc = main(argv)

    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err
    assert "older-than" in err or "older_than" in err.lower()


def test_older_than_malformed_duration_json_mode(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["session", "clean", "--older-than", "banana", "--json"])

    assert rc == 1
    payload = json.loads(capsys.readouterr().err)
    assert "code" in payload
    assert "message" in payload
    assert "remediation" in payload


def test_older_than_accepts_bare_seconds_and_suffixed_durations(
    capsys: pytest.CaptureFixture[str],
) -> None:
    for value in ["90", "30s", "10m", "2h", "7d"]:
        rc = main(["session", "clean", "--older-than", value, "--json"])
        assert rc == 0, capsys.readouterr()


def test_status_rejects_unknown_value(capsys: pytest.CaptureFixture[str]) -> None:
    """An unknown ``--status`` is an argparse-level ``choices`` rejection.

    Unlike ``--older-than`` (a hand-parsed duration, so the CliError comes
    from the handler and ``main()`` returns its exit code), an invalid
    ``choices`` value is rejected by argparse itself before any handler
    runs -- that path calls ``sys.exit`` (see ``_CliArgumentParser.error``),
    same as any other bad-flag argparse error in this CLI.
    """
    with pytest.raises(SystemExit) as excinfo:
        main(["session", "clean", "--status", "bogus-status"])

    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err


# --- End-to-end through the CLI/service, exercising the wiring -----------


def test_cli_clean_with_filters_reaps_only_matching_record(
    session_store,
    seed_session_records,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The CLI's normalized args reach the store and actually filter (end to end).

    Uses the real wall clock (``session_store``/``seed_session_records`` sit
    on top of the autouse ``isolated_session_state`` fixture, and so does
    ``main()``'s own service/store construction) rather than an injected
    fake, so this exercises the exact CLI -> service -> store wiring a real
    invocation uses.
    """
    real_now = time.time()
    seed_session_records(
        count=1,
        status=SessionStatus.ACTIVE,
        caller=CALLER,
        now=real_now - 3600.0,
        expires_at=real_now - 3600.0,
        session_id_prefix="target",
        hosts=("example.com",),
    )
    seed_session_records(
        count=1,
        status=SessionStatus.ACTIVE,
        caller=CALLER,
        now=real_now - 3600.0,
        expires_at=real_now - 3600.0,
        session_id_prefix="other",
        hosts=("elsewhere.example",),
    )

    rc = main(["session", "clean", "--site", "example.com", "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    reaped = payload["content"]["trusted"]["reaped"]
    assert [r["session_id"] for r in reaped] == ["target-active-0"]

    remaining_ids = {record.session_id for record in session_store.list()}
    assert "other-active-0" in remaining_ids


def test_site_filter_is_case_insensitive(
    session_store: FileSessionStore, seed_session_records: SeedSessionRecords
) -> None:
    """`--site EXAMPLE.com` matches a host recorded as `example.com`.

    Hosts land lower-cased (``_hosts_from_urls`` uses ``urlsplit().hostname``)
    but the filter value comes straight off the command line. Hostnames are
    case-insensitive, so a caller who types the host the way the site brands
    it must still match. Reported on PR #15.
    """
    now = time.time()
    seed_session_records(
        count=1,
        status=SessionStatus.CLOSED,
        session_id_prefix="cased",
        # Older than the 3-day retention window so job 3 (purge) applies.
        now=now - 400_000,
        expires_at=now - 399_000,
        hosts=("example.com",),
    )
    reaped_ids = {r.session_id for r in session_store.clean(now, site="EXAMPLE.com")}
    assert session_store.get("cased-closed-0") is None, "the record should have been purged"
    assert reaped_ids == set(), "a purge is job 3; it is not reported as reaped"


def test_a_filtered_out_record_still_loses_its_dead_lease(
    session_store: FileSessionStore, seed_session_records: SeedSessionRecords
) -> None:
    """A record spared by a filter still has its stale lease dropped.

    Job 2 (drop a lease whose holder is gone) is independent of the
    ``--older-than``/``--status``/``--site`` filters, which select what to
    *reap*. Leaving an expired lease on a spared record would make it read as
    in-use to the next caller and to ``_lease_is_live``. Reported on PR #15.
    """
    now = time.time()
    seed_session_records(
        count=1,
        status=SessionStatus.ACTIVE,
        session_id_prefix="spared",
        now=now - 100,
        expires_at=now - 50,
    )
    record = session_store.get("spared-active-0")
    assert record is not None
    record.lease = Lease(holder="gone", acquired_at=now - 100, expires_at=now - 60)
    session_store._write_unlocked(record)  # noqa: SLF001 - test seeds a dead lease

    # A --site filter no record can satisfy: nothing is reaped...
    session_store.clean(now, site="nowhere.invalid")

    after = session_store.get("spared-active-0")
    assert after is not None
    assert after.status is SessionStatus.ACTIVE, "the filter spared it from job 1"
    assert after.lease is None, "but its dead lease is still stale and must be dropped"
