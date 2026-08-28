"""Top-level navigation hosts on the session record (build plan task t8).

The session record used to carry no URL or host at all. This file
characterizes the field that changes that — ``FileSessionRecord.hosts`` — and
the single seam that writes it.

The field is deliberately *narrow*, and every property below is one of the
four acceptance criteria of build plan t8:

1. **Only navigated document hosts.** WebGlass hooks navigation commit and
   nothing else — never a subresource request — so a page pulling images,
   fonts, analytics and third-party frames from a dozen CDNs records exactly
   one host: its own. That keeps the set to a handful of entries and keeps
   the privacy surface to "which sites did this session go to", not "every
   host the browser touched".
2. **Deduplicated and bounded**, with a redirect chain recording the hosts
   actually navigated to (every hop, not just the destination).
3. **Additive**: ``RECORD_SCHEMA_VERSION`` stays 1, and a record written
   before the field existed reads as *unknown* hosts, not *no* hosts — a
   distinction task t9's ``session clean --site`` depends on, because
   "we have no idea where this session went" must never be mistaken for
   "this session demonstrably never went there".
4. **Out of the redacting ``repr()`` and out of every log line**, and
   host-level facts only — never page content, cookies, or credentials.

Everything here runs against the file store and the fake browser backend; no
test in this file needs Chromium or a network.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from webglass.adapters import (
    FakeArtifactStore,
    FakeBrowserBackend,
    FakeBrowserRoute,
    FixedClock,
    SequentialIds,
)
from webglass.adapters.session_store import (
    MAX_NAVIGATED_HOSTS,
    NAVIGATED_HOSTS_TRUNCATED,
    RECORD_SCHEMA_VERSION,
    FileSessionRecord,
    FileSessionStore,
)
from webglass.context import WebContext
from webglass.effects import OperationKind
from webglass.operations import OperationTarget, WebOperation
from webglass.results import LifecycleState
from webglass.sessions import InMemorySessionStore, SessionStatus

NOW = 1_700_000_000.0
CALLER = "colleague"

DOC_URL = "http://example.com/page"
CROSS_HOST_HTML = """<html lang="en"><head><title>Cross host</title>
<link rel="stylesheet" href="http://fonts.example.net/style.css"></head><body>
<h1>Subresources</h1>
<p>This document is served by one host and pulls assets from several others.</p>
<img src="http://cdn.example.net/hero.png" alt="hero">
<script src="http://analytics.example.org/track.js"></script>
<iframe src="http://frame.example.io/widget"></iframe>
<a href="http://elsewhere.example.com/">A link we never followed</a>
</body></html>"""

REDIRECT_START = "http://start.example.com/go"
REDIRECT_MID = "http://middle.example.com/go"
REDIRECT_END = "http://end.example.com/arrived"
REDIRECT_END_HTML = "<html><head><title>Arrived</title></head><body><p>Here.</p></body></html>"


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _store(tmp_path: Path) -> FileSessionStore:
    return FileSessionStore(tmp_path / "sessions")


def _create(store: FileSessionStore, session_id: str = "sess-1") -> FileSessionRecord:
    return store.create(
        session_id=session_id,
        owner="owner-1",
        caller=CALLER,
        task="task-1",
        backend_id="chromium",
        now=NOW,
        expires_at=NOW + 300.0,
        endpoint_ref="ws://127.0.0.1:9999/devtools/browser/SECRET-TOKEN",
    )


def _routes() -> dict[str, FakeBrowserRoute]:
    return {
        DOC_URL: FakeBrowserRoute(status=200, html=CROSS_HOST_HTML),
        REDIRECT_START: FakeBrowserRoute(status=302, redirect_to=REDIRECT_MID),
        REDIRECT_MID: FakeBrowserRoute(status=302, redirect_to=REDIRECT_END),
        REDIRECT_END: FakeBrowserRoute(status=200, html=REDIRECT_END_HTML),
    }


def _service(sessions: Any) -> Any:
    from webglass.service import WebGlassService

    return WebGlassService(
        clock=FixedClock(NOW),
        ids=SequentialIds(),
        browser=FakeBrowserBackend(_routes()),
        sessions=sessions,
        artifacts=FakeArtifactStore(),
    )


def _open(service: Any, url: str, session_id: str | None = None) -> Any:
    return service.execute(
        WebOperation(
            operation_id="op-open",
            kind=OperationKind.PAGE_OPEN,
            target=OperationTarget(url=url),
            session_id=session_id,
        ),
        WebContext(
            caller=CALLER,
            task="task-1",
            workspace="ws-1",
            policy_profile_ref="built-in-default",
            evidence_namespace="ns-1",
        ),
    )


# ---------------------------------------------------------------------------
# Criterion 3 — additive, and unknown is not empty
# ---------------------------------------------------------------------------


def test_a_pre_upgrade_record_reads_as_unknown_hosts_not_no_hosts(tmp_path: Path) -> None:
    """A record file with no ``hosts`` key at all loads as ``None``.

    ``None`` and ``()`` are two different facts and t9's ``--site`` filter
    turns on the difference: ``()`` licenses "this session demonstrably never
    visited that site", ``None`` licenses nothing at all.
    """
    store = _store(tmp_path)
    record = _create(store)
    path = tmp_path / "sessions" / "sess-1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    del payload["hosts"]
    path.write_text(json.dumps(payload), encoding="utf-8")

    reread = store.get("sess-1")
    assert reread is not None
    assert reread.hosts is None
    assert reread.hosts != ()
    assert record.hosts == ()


def test_the_host_field_did_not_bump_the_record_schema_version() -> None:
    assert RECORD_SCHEMA_VERSION == 1


def test_a_created_session_starts_with_a_known_empty_host_set(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = _create(store)
    assert record.hosts == ()
    reread = store.get("sess-1")
    assert reread is not None
    assert reread.hosts == ()


def test_an_unknown_host_set_stays_unknown_rather_than_becoming_partial(tmp_path: Path) -> None:
    """Recording onto a pre-upgrade record is a no-op, on purpose.

    Merging into ``None`` would manufacture a set that *looks* complete while
    silently omitting everywhere that session went before the upgrade. The
    store only ever claims completeness for a set it has tracked from
    creation, so an untracked record stays honestly untracked.
    """
    store = _store(tmp_path)
    _create(store)
    path = tmp_path / "sessions" / "sess-1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    del payload["hosts"]
    path.write_text(json.dumps(payload), encoding="utf-8")

    store.record_navigated_urls("sess-1", ["http://example.com/page"])

    reread = store.get("sess-1")
    assert reread is not None
    assert reread.hosts is None


def test_a_malformed_host_list_is_refused_rather_than_read_as_empty(tmp_path: Path) -> None:
    from webglass.adapters.session_store import SessionRecordError

    store = _store(tmp_path)
    _create(store)
    path = tmp_path / "sessions" / "sess-1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["hosts"] = {"example.com": True}
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SessionRecordError):
        store.get("sess-1")


# ---------------------------------------------------------------------------
# Criterion 2 — deduplicated, bounded, host-level only
# ---------------------------------------------------------------------------


def test_recording_dedups_and_keeps_first_seen_order(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store)
    store.record_navigated_urls("sess-1", ["http://b.example/1", "http://a.example/2"])
    store.record_navigated_urls("sess-1", ["http://a.example/3", "http://c.example/4"])

    reread = store.get("sess-1")
    assert reread is not None
    assert reread.hosts == ("b.example", "a.example", "c.example")


def test_recording_keeps_host_level_facts_only(tmp_path: Path) -> None:
    """Path, query, fragment, port, and above all userinfo never land.

    ``urlsplit(...).hostname`` is what does it: it lower-cases, drops the
    port, and drops any ``user:password@`` prefix — so a credential smuggled
    into a URL cannot reach the record even by accident.
    """
    store = _store(tmp_path)
    _create(store)
    store.record_navigated_urls(
        "sess-1",
        ["https://USER:hunter2@Example.COM:8443/secret/path?token=abc#frag"],
    )

    reread = store.get("sess-1")
    assert reread is not None
    assert reread.hosts == ("example.com",)
    serialized = (tmp_path / "sessions" / "sess-1.json").read_text(encoding="utf-8")
    assert "hunter2" not in serialized
    assert "secret/path" not in serialized
    assert "token=abc" not in serialized


def test_a_url_with_no_host_records_nothing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store)
    store.record_navigated_urls("sess-1", ["about:blank", "data:text/html,<p>x", ""])

    reread = store.get("sess-1")
    assert reread is not None
    assert reread.hosts == ()


def test_the_host_set_is_bounded_and_says_so_when_it_fills_up(tmp_path: Path) -> None:
    """At the cap, further *new* hosts are dropped and the record declares it.

    Silently dropping would make the set read as a complete history when it
    is a bounded prefix — "every omission is declared" (CLAUDE.md section 11),
    so the overflow shows up as a WebGlass-generated diagnostic on the record.
    """
    store = _store(tmp_path)
    _create(store)
    urls = [f"http://h{index}.example/" for index in range(MAX_NAVIGATED_HOSTS + 5)]
    store.record_navigated_urls("sess-1", urls)

    reread = store.get("sess-1")
    assert reread is not None
    assert len(reread.hosts or ()) == MAX_NAVIGATED_HOSTS
    assert reread.hosts == tuple(f"h{index}.example" for index in range(MAX_NAVIGATED_HOSTS))
    assert NAVIGATED_HOSTS_TRUNCATED in reread.diagnostics
    # ...and the marker is not appended once per overflowing host.
    store.record_navigated_urls("sess-1", ["http://another.example/"])
    reread = store.get("sess-1")
    assert reread is not None
    assert reread.diagnostics.count(NAVIGATED_HOSTS_TRUNCATED) == 1


def test_a_url_the_parser_rejects_is_skipped_rather_than_recorded(tmp_path: Path) -> None:
    """An unparseable URL is not a host, and not a reason to fail a navigation."""
    store = _store(tmp_path)
    _create(store)
    store.record_navigated_urls("sess-1", ["http://[::1", "http://good.example/"])

    reread = store.get("sess-1")
    assert reread is not None
    assert reread.hosts == ("good.example",)


def test_recording_against_an_unknown_session_is_a_no_op(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.record_navigated_urls("nope", ["http://example.com/"]) is None
    assert not (tmp_path / "sessions" / "nope.json").exists()
    # An id that is not even a legal session id never reaches the filesystem.
    assert store.record_navigated_urls("../escape", ["http://example.com/"]) is None


def test_recording_against_a_corrupt_record_leaves_it_for_clean(tmp_path: Path) -> None:
    """``clean`` is the one path that repairs a corrupt record; this is not it."""
    store = _store(tmp_path)
    _create(store)
    path = tmp_path / "sessions" / "sess-1.json"
    path.write_text("{not json", encoding="utf-8")

    assert store.record_navigated_urls("sess-1", ["http://example.com/"]) is None
    assert path.read_text(encoding="utf-8") == "{not json"


# ---------------------------------------------------------------------------
# Criterion 4 — out of repr(), out of the logs, in the inspectable dict
# ---------------------------------------------------------------------------


def test_hosts_stay_out_of_the_redacting_repr(tmp_path: Path) -> None:
    """Browsing history does not belong in a traceback or an assertion message.

    ``repr()`` shows up in places nothing scoped this data to: debuggers,
    pytest failure output, and any third-party log line that formats an
    object. ``endpoint_ref`` is redacted there for the stronger reason that
    it is secret-equivalent; ``hosts`` is redacted because it is history.
    """
    store = _store(tmp_path)
    _create(store)
    store.record_navigated_urls("sess-1", ["http://private-intranet.example/"])
    reread = store.get("sess-1")
    assert reread is not None

    rendered = repr(reread)
    assert "private-intranet.example" not in rendered
    assert "hosts=<redacted>" in rendered
    assert "endpoint_ref=<redacted>" in rendered
    # The repr is still useful for debugging.
    assert "session_id='sess-1'" in rendered


def test_hosts_are_inspectable_through_the_public_dict(tmp_path: Path) -> None:
    """Retained data a caller cannot see is retained data they cannot forget.

    ``to_public_dict`` is the caller-scoped rendering behind ``session
    show``/``session list`` (and ``session list`` shows only the caller's own
    sessions), so the host set is disclosed there — that is what makes t9's
    ``clean --site`` an inspectable decision rather than a hidden one.
    """
    store = _store(tmp_path)
    _create(store)
    store.record_navigated_urls("sess-1", ["http://example.com/page"])
    reread = store.get("sess-1")
    assert reread is not None

    public = reread.to_public_dict()
    assert public["hosts"] == ["example.com"]
    assert json.loads(json.dumps(public))["hosts"] == ["example.com"]

    unknown = FileSessionRecord(
        session_id="s2",
        generation=0,
        owner="o",
        caller=CALLER,
        task="t",
        backend_id="b",
        created_at=NOW,
        last_used_at=NOW,
        expires_at=NOW + 1,
        status=SessionStatus.ACTIVE,
    )
    assert unknown.hosts is None
    assert unknown.to_public_dict()["hosts"] is None


def test_no_log_line_carries_a_navigated_host(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = _store(tmp_path)
    _create(store)
    service = _service(store)
    with caplog.at_level(logging.DEBUG):
        result = _open(service, DOC_URL, session_id="sess-1")
    assert result.lifecycle_state is LifecycleState.SUCCEEDED
    assert "example.com" not in caplog.text
    assert caplog.text.strip() == ""


# ---------------------------------------------------------------------------
# Criterion 1 — only navigated document hosts, through the real service seam
# ---------------------------------------------------------------------------


def test_a_navigation_records_only_the_document_host(tmp_path: Path) -> None:
    """A page pulling assets from four other hosts records exactly its own."""
    store = _store(tmp_path)
    _create(store)
    service = _service(store)

    result = _open(service, DOC_URL, session_id="sess-1")
    assert result.lifecycle_state is LifecycleState.SUCCEEDED

    reread = store.get("sess-1")
    assert reread is not None
    assert reread.hosts == ("example.com",)
    for subresource_host in (
        "cdn.example.net",
        "fonts.example.net",
        "analytics.example.org",
        "frame.example.io",
        "elsewhere.example.com",
    ):
        assert subresource_host not in (reread.hosts or ())


def test_a_redirect_chain_records_every_host_it_navigated_to(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store)
    service = _service(store)

    result = _open(service, REDIRECT_START, session_id="sess-1")
    assert result.lifecycle_state is LifecycleState.SUCCEEDED

    reread = store.get("sess-1")
    assert reread is not None
    assert reread.hosts == ("start.example.com", "middle.example.com", "end.example.com")


def test_an_unstored_ephemeral_session_records_nothing_and_still_navigates(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    service = _service(store)

    result = _open(service, DOC_URL)
    assert result.lifecycle_state is LifecycleState.SUCCEEDED
    assert list((tmp_path / "sessions").glob("*.json")) == []


def test_a_store_without_the_recording_hook_still_navigates() -> None:
    """The hook is optional: ``hosts`` is a file-store field, not protocol.

    :class:`~webglass.sessions.InMemorySessionStore` implements the
    ``SessionStore`` protocol and knows nothing about navigated hosts. The
    service must degrade to "nothing recorded", never to a failed navigation.
    """
    sessions = InMemorySessionStore()
    sessions.create(
        session_id="mem-1",
        owner="owner-1",
        caller=CALLER,
        task="task-1",
        backend_id="chromium",
        now=NOW,
        expires_at=NOW + 300.0,
    )
    service = _service(sessions)
    result = _open(service, DOC_URL, session_id="mem-1")
    assert result.lifecycle_state is LifecycleState.SUCCEEDED
    assert not hasattr(sessions.get("mem-1"), "hosts")


def test_navigation_commit_is_the_only_seam_that_records_a_host() -> None:
    """Structural: nothing else in the package may write the host set.

    Criterion 1 is a property of *where* the recording happens. A future
    subresource hook (a ``page.on("request")`` listener, a route handler)
    calling the same store method would quietly turn this narrow field into
    "every host the browser touched" — so the call site count is asserted,
    not just the values it produces today.
    """
    import webglass

    root = Path(webglass.__file__).parent
    callers = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if "record_navigated_urls" in path.read_text(encoding="utf-8")
    )
    assert callers == ["adapters/session_store.py", "service.py"]
