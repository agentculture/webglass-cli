"""``owner_token`` on session records — reuse eligibility only (build plan t7).

Issue #14's investigation found ``_DEFAULT_CALLER = "cli"``
(``webglass/cli/_factory.py``) hardcoded for every CLI invocation: all 134
records measured on the reporting host carried ``caller="cli"``, so
``caller`` distinguishes nothing between concurrent invocations. The owner
token is the fix — but scoped narrowly, per claims c38/c39 (which supersede
the earlier, broader c35 wording): it gates **reuse matching only**.
``clean()`` stays liveness-gated and owner-agnostic (t6's concern, not
touched here).

Three things this module characterizes:

1. A unique token is minted per invocation and stored additively, with
   ``RECORD_SCHEMA_VERSION`` unchanged.
2. A pre-upgrade record (no ``owner_token`` key in its payload at all) reads
   back with the "claimed by nobody" default, not a value equal to any real
   owner's token.
3. ``clean()`` is untouched: it is never handed an owner/token argument and
   reaps expired records regardless of whose token they carry.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.helpers.session_seed import seed_records
from webglass.adapters.session_store import (
    RECORD_SCHEMA_VERSION,
    FileSessionRecord,
    FileSessionStore,
)
from webglass.cli import _factory
from webglass.sessions import SessionStatus

# ---------------------------------------------------------------------------
# 1. Additive field, schema version unchanged
# ---------------------------------------------------------------------------


def test_record_schema_version_is_unchanged() -> None:
    """The owner token lands additively -- it must never force a version bump."""
    assert RECORD_SCHEMA_VERSION == 1


def test_create_accepts_and_persists_an_owner_token(tmp_path: Path) -> None:
    store = FileSessionStore(tmp_path / "sessions")
    record = store.create(
        session_id="s1",
        owner="cli",
        caller="cli",
        task="cli",
        backend_id="none",
        now=1_700_000_000.0,
        expires_at=1_700_000_300.0,
        owner_token="token-abc",
    )
    assert record.owner_token == "token-abc"
    reloaded = store.get("s1")
    assert reloaded is not None
    assert reloaded.owner_token == "token-abc"


def test_create_defaults_owner_token_to_empty_string(tmp_path: Path) -> None:
    """A caller that never passes ``owner_token`` gets the safe default."""
    store = FileSessionStore(tmp_path / "sessions")
    record = store.create(
        session_id="s1",
        owner="cli",
        caller="cli",
        task="cli",
        backend_id="none",
        now=1_700_000_000.0,
        expires_at=1_700_000_300.0,
    )
    assert record.owner_token == ""


def test_owner_token_round_trips_through_the_on_disk_payload(tmp_path: Path) -> None:
    directory = tmp_path / "sessions"
    store = FileSessionStore(directory)
    store.create(
        session_id="s1",
        owner="cli",
        caller="cli",
        task="cli",
        backend_id="none",
        now=1_700_000_000.0,
        expires_at=1_700_000_300.0,
        owner_token="token-xyz",
    )
    payload = json.loads((directory / "s1.json").read_text())
    assert payload["schema_version"] == RECORD_SCHEMA_VERSION
    assert payload["owner_token"] == "token-xyz"


def test_owner_token_is_visible_in_to_public_dict(tmp_path: Path) -> None:
    """Not a secret like ``endpoint_ref`` -- it is a deliberate, disclosed field."""
    store = FileSessionStore(tmp_path / "sessions")
    record = store.create(
        session_id="s1",
        owner="cli",
        caller="cli",
        task="cli",
        backend_id="none",
        now=1_700_000_000.0,
        expires_at=1_700_000_300.0,
        owner_token="token-abc",
    )
    assert record.to_public_dict()["owner_token"] == "token-abc"


# ---------------------------------------------------------------------------
# 2. A pre-upgrade record is claimed by nobody
# ---------------------------------------------------------------------------


def test_a_pre_upgrade_record_with_no_owner_token_key_defaults_to_unclaimed(
    tmp_path: Path,
) -> None:
    """Simulates a record written before t7: the payload has no such key at all."""
    directory = tmp_path / "sessions"
    directory.mkdir(parents=True)
    payload = {
        "schema_version": RECORD_SCHEMA_VERSION,
        "session_id": "old",
        "generation": 0,
        "owner": "cli",
        "caller": "cli",
        "task": "cli",
        "backend_id": "none",
        "created_at": 1_700_000_000.0,
        "last_used_at": 1_700_000_000.0,
        "expires_at": 1_700_000_300.0,
        "capability_profile_ref": None,
        "status": "active",
        "lease": None,
        "endpoint_ref": "",
        "pid": None,
        "user_data_dir": "",
        "sandboxed": True,
        "diagnostics": [],
        "browser_reaped": False,
        "browser_was_running": None,
        # deliberately no "owner_token" key
    }
    (directory / "old.json").write_text(json.dumps(payload))
    store = FileSessionStore(directory)
    record = store.get("old")
    assert record is not None
    assert record.owner_token == ""
    # "" is never a real owner's token, so a reuse query for any real owner
    # token must not treat this record as a match.
    assert record.owner_token != "some-real-owner-token"


def test_seed_harness_owner_token_seam_is_now_live() -> None:
    """The t1 harness's forward-compat ``owner_token=`` kwarg now lands for real."""
    from dataclasses import fields

    assert "owner_token" in {f.name for f in fields(FileSessionRecord)}


def test_seed_records_writes_the_requested_owner_token(tmp_path: Path) -> None:
    store = FileSessionStore(tmp_path / "sessions")
    records = seed_records(
        store,
        count=1,
        status=SessionStatus.ACTIVE,
        owner_token="tok-seeded",
    )
    assert records[0].owner_token == "tok-seeded"
    reloaded = store.get(records[0].session_id)
    assert reloaded is not None
    assert reloaded.owner_token == "tok-seeded"


# ---------------------------------------------------------------------------
# 3. clean() is untouched: no owner filtering, liveness/expiry only
# ---------------------------------------------------------------------------


def test_clean_reaps_expired_records_regardless_of_owner_token(tmp_path: Path) -> None:
    store = FileSessionStore(tmp_path / "sessions")
    store.create(
        session_id="mine",
        owner="cli",
        caller="cli",
        task="cli",
        backend_id="none",
        now=1_700_000_000.0,
        expires_at=1_700_000_000.0 - 1.0,  # already expired
        owner_token="tok-a",
    )
    store.create(
        session_id="not-mine",
        owner="cli",
        caller="cli",
        task="cli",
        backend_id="none",
        now=1_700_000_000.0,
        expires_at=1_700_000_000.0 - 1.0,  # already expired
        owner_token="tok-b",
    )
    reaped = store.clean(1_700_000_060.0)
    reaped_ids = {record.session_id for record in reaped}
    # clean() takes no owner/token argument and does not filter on it: both
    # expired records are reaped even though they carry different tokens.
    assert reaped_ids == {"mine", "not-mine"}


def test_clean_signature_takes_no_owner_or_token_argument() -> None:
    """Criterion 3, structurally: t7 must not have added owner filtering to clean()."""
    import inspect

    params = inspect.signature(FileSessionStore.clean).parameters
    assert set(params) == {"self", "now"}


# ---------------------------------------------------------------------------
# Minting: each CLI-provisioned throwaway session gets a fresh token
# ---------------------------------------------------------------------------


def test_new_owner_token_mints_unique_values() -> None:
    tokens = {_factory._new_owner_token() for _ in range(50)}
    assert len(tokens) == 50
    assert all(token for token in tokens)


def test_ephemeral_session_provisions_a_record_with_a_fresh_owner_token(
    tmp_path: Path,
) -> None:
    from webglass.adapters import FakeArtifactStore, FixedClock, SequentialIds
    from webglass.adapters.session_store import LaunchedBrowser
    from webglass.service import WebGlassService

    def fake_launcher(session_id: str, user_data_dir: Path) -> LaunchedBrowser:
        return LaunchedBrowser(
            endpoint=f"http://127.0.0.1:0/{session_id}",
            pid=0,
            user_data_dir=str(user_data_dir),
            sandboxed=True,
        )

    store = FileSessionStore(tmp_path / "sessions", launcher=fake_launcher)
    service = WebGlassService(
        clock=FixedClock(1_700_000_000.0),
        ids=SequentialIds(),
        browser=object(),  # any non-None sentinel: only truthiness is checked
        sessions=store,
        artifacts=FakeArtifactStore(),
    )
    with _factory.ephemeral_session(service, None) as provisioned:
        assert provisioned.session_id is not None
        record = store.get(provisioned.session_id)
        assert record is not None
        assert record.owner_token != ""
