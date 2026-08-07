"""Reusable adapter conformance suites (build plan task t6).

Any real implementation of a ``webglass.adapters`` protocol has to satisfy
more than "the method signatures line up" — a backend that raises on an
unknown URL still type-checks against ``BrowserBackend``. These mixin
classes pin the *behavioral* properties every implementation must hold to,
so the exact same test methods run against the fakes today and against
t11's Playwright adapter / t15's real search provider once they exist:
a concrete test class overrides a handful of fixtures (what backend to
build, which URLs/queries to use) and inherits every ``test_*`` method
unchanged.

Mixin classes here are deliberately **not** named ``Test*`` so pytest's
default collection (``python_classes = Test*``) does not try to collect and
run them on their own — only a concrete ``class TestSomething(FooConformance):``
subclass (defined below, against the fakes) is collected. A future t11/t15
test module imports the relevant mixin from here and subclasses it the same
way against its real backend; it does not need to duplicate any assertion.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from webglass.adapters.browser import (
    BrowserBackend,
    FakeBrowserBackend,
    FakeBrowserRoute,
    PageError,
    is_decodable_png,
    open_page_snapshot,
)
from webglass.adapters.fetch import FakeFetchBackend, FakeFetchRoute, FetchBackend
from webglass.adapters.search import FakeSearchProvider, SearchProvider, SearchResult

_FIXTURE_PAGES = Path(__file__).parent / "fixtures" / "pages"


def _read_fixture(name: str) -> str:
    return (_FIXTURE_PAGES / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# BrowserBackend conformance.
# ---------------------------------------------------------------------------


class BrowserBackendConformance:
    """Behavioral conformance every ``BrowserBackend`` implementation must satisfy.

    Corresponds to this task's acceptance criterion 3: open returns a
    ``PageSnapshot`` with in-scope refs; redirect chains are reported hop by
    hop; an unknown URL yields a structured not-found result, never an
    exception; console/page-error capture is an explicitly empty list on a
    clean page and carries source-location entries on a throwing page;
    pressed keys are observable; screenshots decode as PNG.
    """

    @pytest.fixture
    def backend(self) -> BrowserBackend:
        raise NotImplementedError  # pragma: no cover - overridden by subclasses

    @pytest.fixture
    def session_id(self) -> str:
        return "conformance-session"

    @pytest.fixture
    def clean_url(self) -> str:
        raise NotImplementedError  # pragma: no cover - overridden by subclasses

    @pytest.fixture
    def throw_url(self) -> str:
        raise NotImplementedError  # pragma: no cover - overridden by subclasses

    @pytest.fixture
    def keydown_url(self) -> str:
        raise NotImplementedError  # pragma: no cover - overridden by subclasses

    @pytest.fixture
    def redirect_chain_urls(self) -> tuple[str, ...]:
        """``(start_url, ..., final_url)`` — at least two entries."""
        raise NotImplementedError  # pragma: no cover - overridden by subclasses

    @pytest.fixture
    def unknown_url(self) -> str:
        raise NotImplementedError  # pragma: no cover - overridden by subclasses

    def test_open_produces_snapshot_with_in_scope_refs(self, backend, session_id, clean_url):
        snapshot = open_page_snapshot(
            backend,
            session_id,
            clean_url,
            snapshot_id="conf-snap",
            retrieved_at="2026-08-07T00:00:00Z",
        )
        assert snapshot.snapshot_id == "conf-snap"
        assert snapshot.requested_url == clean_url
        refs = [block.ref for block in snapshot.blocks]
        refs += [link.ref for link in snapshot.links]
        assert refs, "expected at least one block or link ref on the clean fixture page"
        for ref in refs:
            assert ref.scoped_to("conf-snap", snapshot.generation)

    def test_redirect_chain_reported_hop_by_hop(self, backend, session_id, redirect_chain_urls):
        start, *_, final = redirect_chain_urls
        result = backend.open(session_id, start)
        assert result.final_url == final
        assert len(result.redirect_chain) == len(redirect_chain_urls) - 1 or (
            # A backend is free to report the terminal hop too; either
            # length is acceptable as long as the endpoints line up.
            result.redirect_chain
            and result.redirect_chain[-1].response_url == final
        )
        assert result.redirect_chain[0].requested_url == start

    def test_unknown_url_yields_structured_not_found_not_an_exception(
        self, backend, session_id, unknown_url
    ):
        result = backend.open(session_id, unknown_url)  # must not raise
        assert result.html == ""
        assert result.status is None or result.status >= 400

    def test_clean_page_has_explicitly_empty_console_and_page_error_lists(
        self, backend, session_id, clean_url
    ):
        result = backend.open(session_id, clean_url)
        assert result.console_messages == ()
        assert result.page_errors == ()

    def test_throwing_page_reports_page_error_with_source_location(
        self, backend, session_id, throw_url
    ):
        result = backend.open(session_id, throw_url)
        assert len(result.page_errors) >= 1
        error = result.page_errors[0]
        assert isinstance(error, PageError)
        assert error.text
        assert error.source_url is not None

    def test_pressed_keys_are_observable(self, backend, session_id, keydown_url):
        backend.open(session_id, keydown_url)
        pressed = ("a", "b", "Enter")
        result = backend.press(session_id, pressed)
        assert result.pressed == pressed
        assert result.key_log[-len(pressed) :] == pressed

    def test_screenshot_returns_decodable_png(self, backend, session_id, clean_url):
        backend.open(session_id, clean_url)
        png = backend.screenshot(session_id)
        assert isinstance(png, bytes)
        assert is_decodable_png(png)

    def test_close_does_not_raise(self, backend, session_id, clean_url):
        backend.open(session_id, clean_url)
        backend.close(session_id)  # must not raise


_BROWSER_ROUTES: dict[str, FakeBrowserRoute] = {
    "https://conformance.test/clean": FakeBrowserRoute(html=_read_fixture("clean.html")),
    "https://conformance.test/throw": FakeBrowserRoute(
        html=_read_fixture("throw.html"),
        page_errors=(
            PageError(
                text="Uncaught Error: WebGlass fixture: deliberate synchronous throw on load",
                source_url="https://conformance.test/throw",
                line=16,
            ),
        ),
    ),
    "https://conformance.test/keydown": FakeBrowserRoute(html=_read_fixture("keydown.html")),
    "https://conformance.test/redirect1": FakeBrowserRoute(
        status=302, redirect_to="https://conformance.test/redirect2"
    ),
    "https://conformance.test/redirect2": FakeBrowserRoute(
        status=302, redirect_to="https://conformance.test/final"
    ),
    "https://conformance.test/final": FakeBrowserRoute(html=_read_fixture("clean.html")),
}


class TestFakeBrowserBackendConformance(BrowserBackendConformance):
    """The fake's own pass through the shared suite — the M1 reference instance."""

    @pytest.fixture
    def backend(self) -> BrowserBackend:
        return FakeBrowserBackend(_BROWSER_ROUTES)

    @pytest.fixture
    def clean_url(self) -> str:
        return "https://conformance.test/clean"

    @pytest.fixture
    def throw_url(self) -> str:
        return "https://conformance.test/throw"

    @pytest.fixture
    def keydown_url(self) -> str:
        return "https://conformance.test/keydown"

    @pytest.fixture
    def redirect_chain_urls(self) -> tuple[str, ...]:
        return (
            "https://conformance.test/redirect1",
            "https://conformance.test/redirect2",
            "https://conformance.test/final",
        )

    @pytest.fixture
    def unknown_url(self) -> str:
        return "https://conformance.test/never-registered"


# ---------------------------------------------------------------------------
# FetchBackend conformance.
# ---------------------------------------------------------------------------


class FetchBackendConformance:
    """Behavioral conformance every ``FetchBackend`` implementation must satisfy."""

    @pytest.fixture
    def backend(self) -> FetchBackend:
        raise NotImplementedError  # pragma: no cover - overridden by subclasses

    @pytest.fixture
    def known_url(self) -> str:
        raise NotImplementedError  # pragma: no cover - overridden by subclasses

    @pytest.fixture
    def redirect_chain_urls(self) -> tuple[str, ...]:
        raise NotImplementedError  # pragma: no cover - overridden by subclasses

    @pytest.fixture
    def unknown_url(self) -> str:
        raise NotImplementedError  # pragma: no cover - overridden by subclasses

    def test_known_url_returns_content(self, backend, known_url):
        result = backend.fetch(known_url)
        assert result.status == 200
        assert result.final_url == known_url

    def test_redirect_chain_reported_hop_by_hop(self, backend, redirect_chain_urls):
        start, *_, final = redirect_chain_urls
        result = backend.fetch(start)
        assert result.final_url == final
        assert result.redirect_chain[0].requested_url == start

    def test_unknown_url_yields_structured_not_found_not_an_exception(self, backend, unknown_url):
        result = backend.fetch(unknown_url)  # must not raise
        assert result.body == ""
        assert result.status is None or result.status >= 400


_FETCH_ROUTES: dict[str, FakeFetchRoute] = {
    "https://conformance.test/known": FakeFetchRoute(body="known content"),
    "https://conformance.test/r1": FakeFetchRoute(
        status=302, redirect_to="https://conformance.test/r2"
    ),
    "https://conformance.test/r2": FakeFetchRoute(
        status=302, redirect_to="https://conformance.test/r-final"
    ),
    "https://conformance.test/r-final": FakeFetchRoute(body="final content"),
}


class TestFakeFetchBackendConformance(FetchBackendConformance):
    @pytest.fixture
    def backend(self) -> FetchBackend:
        return FakeFetchBackend(_FETCH_ROUTES)

    @pytest.fixture
    def known_url(self) -> str:
        return "https://conformance.test/known"

    @pytest.fixture
    def redirect_chain_urls(self) -> tuple[str, ...]:
        return (
            "https://conformance.test/r1",
            "https://conformance.test/r2",
            "https://conformance.test/r-final",
        )

    @pytest.fixture
    def unknown_url(self) -> str:
        return "https://conformance.test/never-registered"


# ---------------------------------------------------------------------------
# SearchProvider conformance.
# ---------------------------------------------------------------------------


class SearchProviderConformance:
    """Behavioral conformance every ``SearchProvider`` implementation must satisfy.

    t15's acceptance criterion cites this directly: "a SearchProvider
    implementation for the chosen API vendor sits behind the seam and passes
    the adapter conformance suite."
    """

    @pytest.fixture
    def provider(self) -> SearchProvider:
        raise NotImplementedError  # pragma: no cover - overridden by subclasses

    @pytest.fixture
    def known_query(self) -> str:
        raise NotImplementedError  # pragma: no cover - overridden by subclasses

    @pytest.fixture
    def unknown_query(self) -> str:
        raise NotImplementedError  # pragma: no cover - overridden by subclasses

    def test_known_query_returns_results(self, provider, known_query):
        result = provider.search(known_query)
        assert result.query == known_query
        assert result.provider_id
        assert len(result.results) >= 1
        first = result.results[0]
        assert first.title
        assert first.url

    def test_unknown_query_yields_empty_results_not_an_exception(self, provider, unknown_query):
        result = provider.search(unknown_query)  # must not raise
        assert result.results == ()

    def test_limit_is_respected(self, provider, known_query):
        result = provider.search(known_query, limit=1)
        assert len(result.results) <= 1


class TestFakeSearchProviderConformance(SearchProviderConformance):
    @pytest.fixture
    def provider(self) -> SearchProvider:
        return FakeSearchProvider(
            "conformance-search",
            {
                "webglass": [
                    SearchResult(title="WebGlass", url="https://example.test/1", snippet="a"),
                    SearchResult(title="WebGlass docs", url="https://example.test/2", snippet="b"),
                ]
            },
        )

    @pytest.fixture
    def known_query(self) -> str:
        return "webglass"

    @pytest.fixture
    def unknown_query(self) -> str:
        return "definitely not indexed"
