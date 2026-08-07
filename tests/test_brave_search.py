"""Tests for ``webglass.adapters.brave`` (build plan task t15).

Covers t15's acceptance criteria in full:

1. ``BraveSearchProvider`` sits behind the ``SearchProvider`` seam and passes
   ``SearchProviderConformance`` (``TestBraveSearchProviderConformance``
   below).
2. Keys come only from environment/config at call time
   (``TestFromEnv``/``TestConstructor``), never stored beyond the instance
   attribute, and that attribute has a redacting ``__repr__``
   (``test_repr_never_contains_the_api_key``).
3. A planted-key redaction test exercising a mocked-transport search
   including an HTTP-error path, asserting the sentinel is absent from the
   result, its ``to_dict()``/repr, raised error messages/reprs, and
   ``caplog`` output (``test_planted_key_is_redacted_everywhere``).
4. Every test here drives ``BraveSearchProvider`` through an injected fake
   transport — no test in this module makes a live HTTP call, and
   ``BRAVE_SAMPLE_WEB_SEARCH_RESPONSE`` is a realistic captured-shape sample
   (modeled on Brave's publicly documented Web Search API response shape,
   not fetched live — CLAUDE.md "Test ownership": "no default test may
   depend on a live public website").
5. Structured failure semantics for HTTP 401/429/5xx and malformed JSON
   (``TestErrorCategories``), with timeouts honored via the ``timeout``
   parameter (``test_timeout_error_is_wrapped_as_server_category``).
"""

from __future__ import annotations

import http.server
import json
import logging
import threading
import urllib.error
import urllib.parse

import pytest

from tests.test_adapter_conformance import SearchProviderConformance
from webglass.adapters.brave import (
    BRAVE_SEARCH_ENDPOINT,
    WEBGLASS_BRAVE_API_KEY_ENV,
    BraveSearchProvider,
    SearchProviderError,
    TransportResponse,
)
from webglass.adapters.search import SearchResultSet

# ---------------------------------------------------------------------------
# A realistic captured-shape sample response.
#
# Modeled on Brave's publicly documented Web Search API response shape
# (https://api.search.brave.com/app/documentation/web-search/response-headers,
# the "web" result-list shape) -- not a live capture. No test in this module
# makes a network call; this constant is what the fake transport hands back.
# ---------------------------------------------------------------------------

BRAVE_SAMPLE_WEB_SEARCH_RESPONSE: bytes = json.dumps(
    {
        "query": {
            "original": "webglass",
            "show_strict_warning": False,
            "is_navigational": False,
            "more_results_available": True,
        },
        "type": "search",
        "mixed": {"type": "mixed", "main": [{"type": "web", "index": 0, "all": False}]},
        "web": {
            "type": "search",
            "family_friendly": True,
            "results": [
                {
                    "title": "WebGlass -- guarded web operations for AI agents",
                    "url": "https://example.test/webglass",
                    "is_source_local": False,
                    "is_source_both": False,
                    "description": "WebGlass turns agent intent into normalized "
                    "<strong>web</strong> operations with policy and evidence.",
                    "profile": {
                        "name": "example.test",
                        "url": "https://example.test/",
                        "long_name": "example.test",
                    },
                    "language": "en",
                    "family_friendly": True,
                    "type": "search_result",
                    "subtype": "generic",
                    "meta_url": {
                        "scheme": "https",
                        "netloc": "example.test",
                        "hostname": "example.test",
                        "path": "/webglass",
                    },
                    "age": "2026-08-01T00:00:00",
                },
                {
                    "title": "WebGlass docs",
                    "url": "https://example.test/webglass/docs",
                    "is_source_local": False,
                    "is_source_both": False,
                    "description": "Documentation for the WebGlass operation contract.",
                    "profile": {
                        "name": "example.test",
                        "url": "https://example.test/",
                        "long_name": "example.test",
                    },
                    "language": "en",
                    "family_friendly": True,
                    "type": "search_result",
                    "subtype": "generic",
                    "meta_url": {
                        "scheme": "https",
                        "netloc": "example.test",
                        "hostname": "example.test",
                        "path": "/webglass/docs",
                    },
                    "age": "2026-08-02T00:00:00",
                },
            ],
        },
    }
).encode("utf-8")

#: Brave omits "web" entirely for a query with zero web results.
BRAVE_SAMPLE_EMPTY_RESPONSE: bytes = json.dumps(
    {"query": {"original": "no hits"}, "type": "search"}
).encode("utf-8")


class _FakeTransport:
    """A canned :data:`~webglass.adapters.brave.Transport` for tests.

    Maps the ``q`` query parameter to a pre-built :class:`TransportResponse`
    (or a raised exception, for connection-level failure simulation).
    Records every call so tests can assert the header/query shape the
    provider actually sent -- this is what proves criterion 1 ("q + count
    params", "X-Subscription-Token header") without any network access.
    """

    def __init__(self, responses: dict[str, object]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, dict[str, str], float]] = []

    def __call__(self, url: str, headers, timeout: float) -> TransportResponse:
        self.calls.append((url, dict(headers), timeout))
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        q = query.get("q", [""])[0]
        if q not in self._responses:
            raise AssertionError(f"_FakeTransport got an unexpected query: {q!r}")
        outcome = self._responses[q]
        if isinstance(outcome, BaseException):
            raise outcome
        assert isinstance(outcome, TransportResponse)
        return outcome


def _provider(
    api_key: str = "conformance-test-key", **responses: object
) -> tuple[BraveSearchProvider, _FakeTransport]:
    transport = _FakeTransport(responses)
    return BraveSearchProvider(api_key, transport=transport), transport


# ---------------------------------------------------------------------------
# Conformance (acceptance criterion 1).
# ---------------------------------------------------------------------------


class TestBraveSearchProviderConformance(SearchProviderConformance):
    """The same behavioral suite every ``SearchProvider`` must pass, run
    against ``BraveSearchProvider`` behind a fake transport."""

    @pytest.fixture
    def provider(self) -> BraveSearchProvider:
        transport = _FakeTransport(
            {
                "webglass": TransportResponse(status=200, body=BRAVE_SAMPLE_WEB_SEARCH_RESPONSE),
                "definitely not indexed": TransportResponse(
                    status=200, body=BRAVE_SAMPLE_EMPTY_RESPONSE
                ),
            }
        )
        return BraveSearchProvider("conformance-test-key", transport=transport)

    @pytest.fixture
    def known_query(self) -> str:
        return "webglass"

    @pytest.fixture
    def unknown_query(self) -> str:
        return "definitely not indexed"


# ---------------------------------------------------------------------------
# Request shape: endpoint, q/count params, X-Subscription-Token header.
# ---------------------------------------------------------------------------


class TestRequestShape:
    def test_sends_q_and_count_params_to_the_brave_endpoint(self) -> None:
        provider, transport = _provider(
            webglass=TransportResponse(status=200, body=BRAVE_SAMPLE_WEB_SEARCH_RESPONSE)
        )
        provider.search("webglass", limit=5)

        assert len(transport.calls) == 1
        url, headers, timeout = transport.calls[0]
        assert url.startswith(BRAVE_SEARCH_ENDPOINT + "?")
        params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        assert params["q"] == ["webglass"]
        assert params["count"] == ["5"]
        assert timeout == pytest.approx(10.0)

    def test_sends_x_subscription_token_header(self) -> None:
        provider, transport = _provider(
            api_key="sk-header-check",
            webglass=TransportResponse(status=200, body=BRAVE_SAMPLE_WEB_SEARCH_RESPONSE),
        )
        provider.search("webglass")

        _, headers, _ = transport.calls[0]
        assert headers["X-Subscription-Token"] == "sk-header-check"

    def test_count_is_clamped_to_braves_maximum(self) -> None:
        provider, transport = _provider(
            big=TransportResponse(status=200, body=BRAVE_SAMPLE_EMPTY_RESPONSE)
        )
        provider.search("big", limit=1000)

        _, _, _ = transport.calls[0]
        url = transport.calls[0][0]
        params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        assert params["count"] == ["20"]

    def test_limit_zero_requests_minimum_but_returns_no_results(self) -> None:
        provider, transport = _provider(
            webglass=TransportResponse(status=200, body=BRAVE_SAMPLE_WEB_SEARCH_RESPONSE)
        )
        result = provider.search("webglass", limit=0)

        assert result.results == ()
        params = urllib.parse.parse_qs(urllib.parse.urlparse(transport.calls[0][0]).query)
        assert params["count"] == ["1"]

    def test_custom_timeout_is_passed_to_the_transport(self) -> None:
        provider, transport = _provider(
            webglass=TransportResponse(status=200, body=BRAVE_SAMPLE_WEB_SEARCH_RESPONSE)
        )
        provider = BraveSearchProvider(
            "k", transport=transport, timeout=2.5, endpoint=BRAVE_SEARCH_ENDPOINT
        )
        provider.search("webglass")
        assert transport.calls[0][2] == pytest.approx(2.5)


# ---------------------------------------------------------------------------
# Result parsing.
# ---------------------------------------------------------------------------


class TestResultParsing:
    def test_parses_title_url_description_into_search_result(self) -> None:
        provider, _ = _provider(
            webglass=TransportResponse(status=200, body=BRAVE_SAMPLE_WEB_SEARCH_RESPONSE)
        )
        result = provider.search("webglass")

        assert isinstance(result, SearchResultSet)
        assert result.query == "webglass"
        assert result.provider_id == "brave"
        assert len(result.results) == 2
        first = result.results[0]
        assert first.title == "WebGlass -- guarded web operations for AI agents"
        assert first.url == "https://example.test/webglass"
        assert "web" in first.snippet

    def test_missing_web_key_yields_empty_results_not_an_error(self) -> None:
        provider, _ = _provider(
            nohits=TransportResponse(status=200, body=BRAVE_SAMPLE_EMPTY_RESPONSE)
        )
        result = provider.search("nohits")
        assert result.results == ()

    def test_non_dict_web_field_is_malformed(self) -> None:
        body = json.dumps({"web": "not-an-object"}).encode("utf-8")
        provider, _ = _provider(bad=TransportResponse(status=200, body=body))
        with pytest.raises(SearchProviderError) as exc_info:
            provider.search("bad")
        assert exc_info.value.category == "malformed"

    def test_non_list_results_field_is_malformed(self) -> None:
        body = json.dumps({"web": {"results": "nope"}}).encode("utf-8")
        provider, _ = _provider(bad=TransportResponse(status=200, body=body))
        with pytest.raises(SearchProviderError) as exc_info:
            provider.search("bad")
        assert exc_info.value.category == "malformed"

    def test_non_object_result_entry_is_malformed(self) -> None:
        body = json.dumps({"web": {"results": ["not-an-object"]}}).encode("utf-8")
        provider, _ = _provider(bad=TransportResponse(status=200, body=body))
        with pytest.raises(SearchProviderError) as exc_info:
            provider.search("bad")
        assert exc_info.value.category == "malformed"

    def test_result_missing_url_is_malformed(self) -> None:
        body = json.dumps({"web": {"results": [{"title": "no url"}]}}).encode("utf-8")
        provider, _ = _provider(bad=TransportResponse(status=200, body=body))
        with pytest.raises(SearchProviderError) as exc_info:
            provider.search("bad")
        assert exc_info.value.category == "malformed"

    def test_top_level_non_object_body_is_malformed(self) -> None:
        body = json.dumps(["not", "an", "object"]).encode("utf-8")
        provider, _ = _provider(bad=TransportResponse(status=200, body=body))
        with pytest.raises(SearchProviderError) as exc_info:
            provider.search("bad")
        assert exc_info.value.category == "malformed"

    def test_invalid_json_body_is_malformed(self) -> None:
        provider, _ = _provider(bad=TransportResponse(status=200, body=b"{not json"))
        with pytest.raises(SearchProviderError) as exc_info:
            provider.search("bad")
        assert exc_info.value.category == "malformed"


# ---------------------------------------------------------------------------
# Structured failure semantics (acceptance criterion 5).
# ---------------------------------------------------------------------------


class TestErrorCategories:
    @pytest.mark.parametrize("status", [401, 403])
    def test_401_403_are_auth_category(self, status: int) -> None:
        provider, _ = _provider(q=TransportResponse(status=status, body=b'{"error":"nope"}'))
        with pytest.raises(SearchProviderError) as exc_info:
            provider.search("q")
        assert exc_info.value.category == "auth"
        assert "WEBGLASS_BRAVE_API_KEY" in exc_info.value.message

    def test_429_is_rate_limit_category(self) -> None:
        provider, _ = _provider(q=TransportResponse(status=429, body=b'{"error":"slow down"}'))
        with pytest.raises(SearchProviderError) as exc_info:
            provider.search("q")
        assert exc_info.value.category == "rate-limit"

    @pytest.mark.parametrize("status", [500, 502, 503])
    def test_5xx_is_server_category(self, status: int) -> None:
        provider, _ = _provider(q=TransportResponse(status=status, body=b"internal error"))
        with pytest.raises(SearchProviderError) as exc_info:
            provider.search("q")
        assert exc_info.value.category == "server"

    def test_unexpected_status_is_malformed_category(self) -> None:
        provider, _ = _provider(q=TransportResponse(status=418, body=b"teapot"))
        with pytest.raises(SearchProviderError) as exc_info:
            provider.search("q")
        assert exc_info.value.category == "malformed"

    def test_timeout_error_is_wrapped_as_server_category(self) -> None:
        provider, _ = _provider(q=TimeoutError("timed out"))
        with pytest.raises(SearchProviderError) as exc_info:
            provider.search("q")
        assert exc_info.value.category == "server"

    def test_url_error_is_wrapped_as_server_category(self) -> None:
        provider, _ = _provider(q=urllib.error.URLError("name resolution failed"))
        with pytest.raises(SearchProviderError) as exc_info:
            provider.search("q")
        assert exc_info.value.category == "server"

    def test_unexpected_transport_exception_is_wrapped_as_server_category(self) -> None:
        provider, _ = _provider(q=RuntimeError("transport bug"))
        with pytest.raises(SearchProviderError) as exc_info:
            provider.search("q")
        assert exc_info.value.category == "server"

    def test_search_provider_error_never_raises_from_search_itself(self) -> None:
        # A SearchProviderError raised *by the transport* (a caller-supplied
        # transport is free to do this) must propagate unchanged, not get
        # double-wrapped into a second, less-specific error.
        planted = SearchProviderError("already structured", category="rate-limit")
        provider, _ = _provider(q=planted)
        with pytest.raises(SearchProviderError) as exc_info:
            provider.search("q")
        assert exc_info.value is planted


# ---------------------------------------------------------------------------
# Constructor / from_env: keys as runtime config only (acceptance criterion 2).
# ---------------------------------------------------------------------------


class TestConstructor:
    def test_empty_api_key_raises_configuration_error(self) -> None:
        with pytest.raises(SearchProviderError) as exc_info:
            BraveSearchProvider("")
        assert exc_info.value.category == "configuration"

    def test_repr_never_contains_the_api_key(self) -> None:
        provider = BraveSearchProvider("sk-should-never-appear-in-repr")
        rendered = repr(provider)
        assert "sk-should-never-appear-in-repr" not in rendered
        assert "<redacted>" in rendered

    def test_key_is_not_exposed_as_a_public_attribute(self) -> None:
        provider = BraveSearchProvider("sk-private")
        assert not hasattr(provider, "api_key")
        assert not hasattr(provider, "key")
        # It lives only on the private instance attribute this module owns.
        assert provider._api_key == "sk-private"


class TestEndpointValidation:
    """The endpoint parameter never widens into a scheme-confusion surface.

    It exists so tests can target a loopback http fixture server; anything
    else must be https. This is what keeps the transport's B310 suppression
    honest (PR #11 review thread).
    """

    @pytest.mark.parametrize(
        "endpoint",
        [
            "file:///etc/passwd",
            "ftp://api.search.brave.com/res/v1/web/search",
            "http://api.search.brave.com/res/v1/web/search",
            "http://169.254.169.254/latest/meta-data",
            "not-a-url",
        ],
    )
    def test_non_https_non_loopback_endpoints_are_refused(self, endpoint: str) -> None:
        with pytest.raises(SearchProviderError) as exc_info:
            BraveSearchProvider("some-key", endpoint=endpoint)
        assert exc_info.value.category == "configuration"

    @pytest.mark.parametrize(
        "endpoint",
        [
            "https://api.search.brave.com/res/v1/web/search",
            "http://127.0.0.1:8123/search",
            "http://localhost:8123/search",
        ],
    )
    def test_https_and_loopback_http_endpoints_are_accepted(self, endpoint: str) -> None:
        provider = BraveSearchProvider("some-key", endpoint=endpoint)
        assert isinstance(provider, BraveSearchProvider)


class TestFromEnv:
    def test_missing_env_var_names_it_in_the_error(self) -> None:
        with pytest.raises(SearchProviderError) as exc_info:
            BraveSearchProvider.from_env(env={})
        assert exc_info.value.category == "configuration"
        assert WEBGLASS_BRAVE_API_KEY_ENV in exc_info.value.message

    def test_empty_env_var_is_treated_as_missing(self) -> None:
        with pytest.raises(SearchProviderError) as exc_info:
            BraveSearchProvider.from_env(env={WEBGLASS_BRAVE_API_KEY_ENV: ""})
        assert exc_info.value.category == "configuration"

    def test_present_env_var_builds_a_working_provider(self) -> None:
        transport = _FakeTransport(
            {"webglass": TransportResponse(status=200, body=BRAVE_SAMPLE_WEB_SEARCH_RESPONSE)}
        )
        provider = BraveSearchProvider.from_env(
            env={WEBGLASS_BRAVE_API_KEY_ENV: "sk-from-env"}, transport=transport
        )
        result = provider.search("webglass")
        assert len(result.results) == 2
        assert transport.calls[0][1]["X-Subscription-Token"] == "sk-from-env"

    def test_default_env_source_is_the_real_process_environment(self, monkeypatch) -> None:
        monkeypatch.setenv(WEBGLASS_BRAVE_API_KEY_ENV, "sk-real-environ")
        transport = _FakeTransport(
            {"webglass": TransportResponse(status=200, body=BRAVE_SAMPLE_WEB_SEARCH_RESPONSE)}
        )
        provider = BraveSearchProvider.from_env(transport=transport)
        provider.search("webglass")
        assert transport.calls[0][1]["X-Subscription-Token"] == "sk-real-environ"

    def test_default_env_source_missing_var_raises(self, monkeypatch) -> None:
        monkeypatch.delenv(WEBGLASS_BRAVE_API_KEY_ENV, raising=False)
        with pytest.raises(SearchProviderError) as exc_info:
            BraveSearchProvider.from_env()
        assert exc_info.value.category == "configuration"


# ---------------------------------------------------------------------------
# Planted-key redaction (acceptance criterion 3) -- the load-bearing test.
# ---------------------------------------------------------------------------


class TestPlantedKeyRedaction:
    # Deliberately fake and low-entropy so secret scanners (GitGuardian) do
    # not flag it as a real Brave key; distinctive enough that finding it in
    # any output is still an unambiguous redaction failure.
    SENTINEL = "webglass-planted-test-key-not-a-real-secret"

    def test_planted_key_is_redacted_everywhere(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG, logger="webglass.adapters.brave")

        success_transport = _FakeTransport(
            {"webglass": TransportResponse(status=200, body=BRAVE_SAMPLE_WEB_SEARCH_RESPONSE)}
        )
        provider = BraveSearchProvider(self.SENTINEL, transport=success_transport)

        # 1. Constructor repr.
        assert self.SENTINEL not in repr(provider)

        # 2. A successful search: SearchResultSet, its to_dict(), and repr.
        result = provider.search("webglass")
        assert self.SENTINEL not in repr(result)
        assert self.SENTINEL not in json.dumps(result.to_dict())
        for hit in result.results:
            assert self.SENTINEL not in repr(hit)
            assert self.SENTINEL not in json.dumps(hit.to_dict())

        # 3. The HTTP-error path: raised error message/repr/to_dict.
        error_transport = _FakeTransport(
            {"denied": TransportResponse(status=401, body=b'{"error":"unauthorized"}')}
        )
        provider_for_error = BraveSearchProvider(self.SENTINEL, transport=error_transport)
        with pytest.raises(SearchProviderError) as exc_info:
            provider_for_error.search("denied")
        err = exc_info.value
        assert self.SENTINEL not in str(err)
        assert self.SENTINEL not in repr(err)
        assert self.SENTINEL not in json.dumps(err.to_dict())
        assert self.SENTINEL not in repr(provider_for_error)

        # 4. A connection-level failure path too.
        timeout_transport = _FakeTransport({"slow": TimeoutError("timed out")})
        provider_for_timeout = BraveSearchProvider(self.SENTINEL, transport=timeout_transport)
        with pytest.raises(SearchProviderError) as exc_info:
            provider_for_timeout.search("slow")
        assert self.SENTINEL not in str(exc_info.value)
        assert self.SENTINEL not in repr(exc_info.value)

        # 5. Every log record emitted anywhere in this test, from any logger.
        assert self.SENTINEL not in caplog.text
        for record in caplog.records:
            assert self.SENTINEL not in record.getMessage()
            assert self.SENTINEL not in repr(record.__dict__)

    def test_planted_key_absent_from_from_env_configuration_error(self) -> None:
        # The configuration-error path never had a key to begin with, but
        # guard it anyway: a caller-visible env mapping value must not leak
        # into the *error message* even if some future refactor started
        # echoing config back.
        with pytest.raises(SearchProviderError) as exc_info:
            BraveSearchProvider.from_env(env={WEBGLASS_BRAVE_API_KEY_ENV: ""})
        assert self.SENTINEL not in str(exc_info.value)


# ---------------------------------------------------------------------------
# The real default transport (``_urllib_transport``), exercised against a
# throwaway local server -- not a live public website (CLAUDE.md "Test
# ownership"), the same 127.0.0.1-ephemeral-port pattern
# ``tests/fixtures/server.py`` already uses for the browser/fetch adapter
# suites. Every other test in this module replaces the transport with a
# fake; this class is what actually proves the stdlib ``urllib.request``
# wiring -- header passthrough, status/body extraction, and
# ``HTTPError``-as-a-readable-response normalization -- works end to end.
# ---------------------------------------------------------------------------


class _BraveLikeHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass  # silence the default per-request access log during tests

    def do_GET(self) -> None:  # http.server's own required method name
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        self.server.last_headers = dict(self.headers.items())
        q = query.get("q", [""])[0]
        if q == "unauthorized":
            body = b'{"error":"unauthorized"}'
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = BRAVE_SAMPLE_WEB_SEARCH_RESPONSE
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def local_brave_like_server():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _BraveLikeHandler)
    server.last_headers = {}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)


class TestDefaultUrllibTransport:
    def test_default_transport_hits_local_server_and_sends_the_header(
        self, local_brave_like_server: http.server.ThreadingHTTPServer
    ) -> None:
        host, port = local_brave_like_server.server_address
        endpoint = f"http://{host}:{port}/res/v1/web/search"
        provider = BraveSearchProvider("sk-local-default-transport", endpoint=endpoint)

        result = provider.search("webglass")

        assert len(result.results) == 2
        sent_headers = local_brave_like_server.last_headers
        assert sent_headers.get("X-Subscription-Token") == "sk-local-default-transport"

    def test_default_transport_normalizes_http_error_status(
        self, local_brave_like_server: http.server.ThreadingHTTPServer
    ) -> None:
        host, port = local_brave_like_server.server_address
        endpoint = f"http://{host}:{port}/res/v1/web/search"
        provider = BraveSearchProvider("sk-local-default-transport", endpoint=endpoint)

        with pytest.raises(SearchProviderError) as exc_info:
            provider.search("unauthorized")
        assert exc_info.value.category == "auth"
        assert "sk-local-default-transport" not in str(exc_info.value)
