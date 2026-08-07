"""``BraveSearchProvider`` — the first real :class:`~webglass.adapters.search.SearchProvider`.

Build plan task t15 (spec claims c21/h13, plan risk r3 — Brave Search API,
finalized as the M2 vendor). Everything :mod:`webglass.adapters.search`
promises a real backend must honor applies here unchanged: ``search()``
never raises for an ordinary zero-hit query, and this module additionally
never raises a raw ``urllib``/``json`` exception for a provider-side failure
— every failure this module cannot recover from becomes a typed
:class:`SearchProviderError`.

**Keys are runtime configuration, never product state** (CLAUDE.md "Target
architecture" section 8: "never a credential store"; spec claim c21: "no
search API key ever appears in logs, evidence, memory, artifacts, or JSON
results; keys are read from environment or config at call time only"):

- :meth:`BraveSearchProvider.from_env` is the only place this module reads
  the process environment, and it reads exactly one variable —
  :data:`WEBGLASS_BRAVE_API_KEY_ENV`. A missing key is a structured
  :class:`SearchProviderError` (``category="configuration"``) naming that
  exact variable, never a bare ``KeyError`` — this is what lets a future
  ``doctor``-style check (t13+) surface an actionable capability diagnostic
  instead of a crash.
- The constructor also takes the key as an explicit parameter — a caller
  with its own config layer never has to round-trip through the environment.
- The key lives only on the instance (``self._api_key``); nothing here
  writes it to a class attribute, a module-level cache, a log record, or any
  return value. :meth:`BraveSearchProvider.__repr__` redacts it, mirroring
  ``webglass.sessions.SessionRecord.__repr__``'s treatment of
  ``endpoint_ref`` as secret-equivalent.
- The key is transmitted exactly once per call, as the ``X-Subscription-Token``
  request header — never as a URL query parameter (so it cannot leak into
  proxy/access logs that only ever record request lines), and never logged:
  this module's own ``logger.debug``/``logger.warning`` calls carry only
  query text, result counts, and HTTP status/category — never headers.
  ``tests/test_brave_search.py`` plants a sentinel key and asserts it is
  absent from every result, error, and ``caplog`` capture this module can
  produce.

**Transport is injectable, real network access is opt-in.** ``search()``
never calls ``urllib`` directly — it calls ``self._transport(url, headers,
timeout)``, a plain callable (:data:`Transport`) defaulting to
:func:`_urllib_transport`, a thin stdlib-only wrapper (no runtime dependency
gained — CLAUDE.md's ``dependencies = []`` stays true; see
``tests/test_import_boundaries.py::test_runtime_dependencies_stay_empty_at_m0``).
A transport's contract: return a :class:`TransportResponse` for *any* HTTP
response, success or error status alike (mirroring how :func:`_urllib_transport`
normalizes ``urllib.error.HTTPError`` — itself a readable response — into
the same shape as a 200); raise only for a connection-level failure (DNS,
refused connection, timeout). This is exactly what lets
``tests/test_brave_search.py`` replay canned Brave JSON — including 401/429/5xx
error bodies — through a fake transport with zero network access (CLAUDE.md
"Test ownership": "no default test may depend on a live public website").

**Trust zones (issue #1 section 7).** Every ``title``/``url``/``snippet`` on
a returned :class:`~webglass.adapters.search.SearchResult` is untrusted
source material Brave (or the page it names) authored, exactly as
``webglass.adapters.search``'s module docstring already establishes for the
seam in general — this module does not add any new trust-zone
consideration, it only fills in a real HTTP call behind it.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from webglass.adapters.search import SearchResult, SearchResultSet

__all__ = [
    "BRAVE_SEARCH_ENDPOINT",
    "WEBGLASS_BRAVE_API_KEY_ENV",
    "SearchProviderError",
    "TransportResponse",
    "Transport",
    "BraveSearchProvider",
]

logger = logging.getLogger(__name__)

#: The Brave Search API's web-search endpoint (task t15 acceptance criterion 1).
BRAVE_SEARCH_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"

#: The one environment variable :meth:`BraveSearchProvider.from_env` reads.
#: Documented here so a CLI wiring layer (t13+) and this module's own error
#: messages name exactly the same variable.
WEBGLASS_BRAVE_API_KEY_ENV = "WEBGLASS_BRAVE_API_KEY"

_REDACTED = "<redacted>"
_DEFAULT_TIMEOUT_SECONDS = 10.0

#: Brave's own documented ceiling for the ``count`` query parameter.
_MAX_COUNT = 20


@dataclass(frozen=True)
class TransportResponse:
    """What a :data:`Transport` callable returns for *any* HTTP response.

    Covers both a plain success and an HTTP-level error (4xx/5xx) — a
    transport never raises for those, only for a connection-level failure
    (DNS, refused connection, timeout). ``body`` is raw bytes; parsing (JSON
    decode) is :meth:`BraveSearchProvider.search`'s job, not the
    transport's, so a fake test transport can hand back arbitrary bytes
    (including deliberately malformed ones) without needing to know
    anything about JSON.
    """

    status: int
    body: bytes = b""


#: A transport performs exactly one GET request and reports back status +
#: body. Injectable so ``tests/test_brave_search.py`` replays canned Brave
#: responses — success and error alike — with zero network access. The
#: default, :func:`_urllib_transport`, is the only place in this module that
#: touches the network or the ``urllib`` error hierarchy directly.
Transport = Callable[[str, Mapping[str, str], float], TransportResponse]


def _validated_endpoint(endpoint: str) -> str:
    """Constrain the search endpoint to ``https``, or ``http`` on loopback.

    The ``endpoint`` parameter exists so tests can point the default
    transport at a local fixture server — it must never widen into the
    scheme-confusion surface (``file:``, ``ftp:``, redirects to local
    resources) that bandit's B310 exists to catch. Anything but ``https://``
    to any host, or ``http://`` to 127.0.0.1/::1/localhost, is a structured
    configuration error.
    """
    parsed = urllib.parse.urlsplit(endpoint)
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "https" and host:
        return endpoint
    if parsed.scheme == "http" and host in ("127.0.0.1", "::1", "localhost"):
        return endpoint
    raise SearchProviderError(
        f"search endpoint must be https:// (or http:// to loopback for tests), got {endpoint!r}",
        category="configuration",
    )


def _urllib_transport(url: str, headers: Mapping[str, str], timeout: float) -> TransportResponse:
    """Default :data:`Transport`: stdlib ``urllib.request`` only.

    ``url`` is :data:`BRAVE_SEARCH_ENDPOINT` (or a :func:`_validated_endpoint`
    — ``https``, or loopback ``http`` for test fixtures) plus a query string
    this module built itself from caller-supplied *values*, never a
    caller-supplied URL or scheme — so the scheme-confusion class of issue
    bandit's B310 check exists to catch cannot arise; suppressed locally with
    that justification, exactly as ``webglass/pages.py`` does for its own
    narrowly-scoped B105 suppression.
    """
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310
            return TransportResponse(status=response.status, body=response.read())
    except urllib.error.HTTPError as exc:
        # HTTPError is itself a readable response (has .code and .read()) —
        # normalize it into the same shape a 200 would produce so `search()`
        # has exactly one response shape to interpret either way.
        return TransportResponse(status=exc.code, body=exc.read())


class SearchProviderError(Exception):
    """A structured search-provider failure — never a raw ``urllib``/``json`` exception.

    ``category`` is one of:

    - ``"configuration"`` — no usable API key (:meth:`BraveSearchProvider.from_env`
      found no environment variable, or the constructor was given an empty key).
    - ``"auth"`` — Brave rejected the request as unauthenticated/unauthorized
      (HTTP 401/403): the key is missing, revoked, or otherwise invalid.
    - ``"rate-limit"`` — HTTP 429: back off and retry later.
    - ``"server"`` — HTTP 5xx, or a transport-level failure (timeout, DNS,
      connection refused) that never produced an HTTP status at all.
    - ``"malformed"`` — a 2xx response whose body is not the JSON shape this
      module expects (not valid JSON, or missing/mistyped ``web``/``web.results``).

    ``message`` is written to describe the *failure* — it never echoes
    request headers or the API key. ``tests/test_brave_search.py``'s
    planted-key redaction test asserts a sentinel key is absent from every
    rendering of this exception (``str()``, ``repr()``, and :meth:`to_dict`).
    """

    def __init__(self, message: str, *, category: str) -> None:
        super().__init__(message)
        self.message = message
        self.category = category

    def __repr__(self) -> str:
        return f"{type(self).__name__}(category={self.category!r}, message={self.message!r})"

    def to_dict(self) -> dict[str, Any]:
        return {"category": self.category, "message": self.message}


class BraveSearchProvider:
    """:class:`~webglass.adapters.search.SearchProvider` over the Brave Search API.

    Structurally satisfies the ``SearchProvider`` protocol (a ``search(self,
    query, limit=10) -> SearchResultSet`` method) without importing it as a
    base class — the same duck-typed relationship
    :class:`~webglass.adapters.search.FakeSearchProvider` already has to the
    protocol. ``tests/test_brave_search.py`` subclasses the reusable
    :class:`~tests.test_adapter_conformance.SearchProviderConformance` mixin
    against an instance of this class, driven by a fake transport, to prove
    conformance without a live API call.
    """

    #: Fixed by this module — Brave is the only vendor this adapter speaks.
    provider_id = "brave"

    def __init__(
        self,
        api_key: str,
        *,
        transport: Transport = _urllib_transport,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
        endpoint: str = BRAVE_SEARCH_ENDPOINT,
    ) -> None:
        if not api_key:
            raise SearchProviderError(
                "Brave Search API key must not be empty", category="configuration"
            )
        self._api_key = api_key
        self._transport = transport
        self._timeout = timeout
        self._endpoint = _validated_endpoint(endpoint)

    def __repr__(self) -> str:
        # api_key is secret-equivalent — redacted exactly like
        # webglass.sessions.SessionRecord.__repr__ redacts endpoint_ref.
        return (
            f"{type(self).__name__}(endpoint={self._endpoint!r}, "
            f"timeout={self._timeout!r}, api_key={_REDACTED})"
        )

    @classmethod
    def from_env(
        cls,
        *,
        transport: Transport = _urllib_transport,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
        endpoint: str = BRAVE_SEARCH_ENDPOINT,
        env: Mapping[str, str] | None = None,
    ) -> "BraveSearchProvider":
        """Build a provider from :data:`WEBGLASS_BRAVE_API_KEY_ENV`.

        ``env`` defaults to the real process environment
        (``os.environ``); tests pass an explicit mapping so the missing-key
        and present-key paths are both exercisable without mutating global
        process state. A missing/empty key raises a structured
        :class:`SearchProviderError` (``category="configuration"``) naming
        the exact variable to set — this is the "degrade actionably with no
        key" contract a future ``doctor``-style capability check builds on.
        """
        source = os.environ if env is None else env
        api_key = source.get(WEBGLASS_BRAVE_API_KEY_ENV)
        if not api_key:
            raise SearchProviderError(
                f"{WEBGLASS_BRAVE_API_KEY_ENV} is not set. Set it to a Brave Search API "
                "subscription token (obtain one at https://api.search.brave.com/) to "
                "enable the Brave search provider.",
                category="configuration",
            )
        return cls(api_key, transport=transport, timeout=timeout, endpoint=endpoint)

    def search(self, query: str, limit: int = 10) -> SearchResultSet:
        # Brave requires count >= 1, so a caller-requested limit of 0 (or
        # negative) still asks Brave for one result; the final slice below —
        # not this request-side clamp — is what actually enforces "up to
        # limit results, never more" against whatever Brave sends back.
        count = min(max(limit, 1), _MAX_COUNT)
        query_string = urllib.parse.urlencode({"q": query, "count": count})
        url = f"{self._endpoint}?{query_string}"
        headers = {
            "Accept": "application/json",
            "X-Subscription-Token": self._api_key,
        }

        logger.debug("brave search request: query=%r count=%d", query, count)

        try:
            response = self._transport(url, headers, self._timeout)
        except SearchProviderError:
            raise
        except TimeoutError as exc:
            raise SearchProviderError(
                f"Brave search request timed out after {self._timeout}s", category="server"
            ) from exc
        except urllib.error.URLError as exc:
            raise SearchProviderError(
                f"Brave search request failed to connect: {exc.reason}", category="server"
            ) from exc
        except Exception as exc:  # a transport contract violation, not an ordinary network error
            raise SearchProviderError(
                f"Brave search transport raised an unexpected error: {type(exc).__name__}",
                category="server",
            ) from exc

        if response.status != 200:
            error = self._error_for_status(response.status)
            logger.warning(
                "brave search failed: status=%d category=%s", response.status, error.category
            )
            raise error

        results = self._parse_results(self._decode_json(response.body))
        results = results[: max(limit, 0)]
        logger.debug("brave search response: query=%r result_count=%d", query, len(results))
        return SearchResultSet(query=query, provider_id=self.provider_id, results=tuple(results))

    @staticmethod
    def _error_for_status(status: int) -> SearchProviderError:
        if status in (401, 403):
            return SearchProviderError(
                f"Brave search rejected the request (HTTP {status}): check that "
                f"{WEBGLASS_BRAVE_API_KEY_ENV} holds a valid, active subscription token.",
                category="auth",
            )
        if status == 429:
            return SearchProviderError(
                "Brave search rate limit exceeded (HTTP 429); back off and retry later.",
                category="rate-limit",
            )
        if status >= 500:
            return SearchProviderError(
                f"Brave search backend error (HTTP {status}).", category="server"
            )
        return SearchProviderError(
            f"Brave search returned an unexpected status (HTTP {status}).",
            category="malformed",
        )

    @staticmethod
    def _decode_json(body: bytes) -> Any:
        try:
            return json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SearchProviderError(
                f"Brave search returned a response that was not valid JSON: {exc}",
                category="malformed",
            ) from exc

    @staticmethod
    def _parse_results(payload: Any) -> list[SearchResult]:
        if not isinstance(payload, dict):
            raise SearchProviderError(
                "Brave search response was not a JSON object.", category="malformed"
            )
        web = payload.get("web")
        if web is None:
            # Brave omits "web" entirely for a query with no web results at
            # all — an ordinary empty result, not a malformed response.
            return []
        if not isinstance(web, dict):
            raise SearchProviderError(
                "Brave search response 'web' field was not a JSON object.", category="malformed"
            )
        raw_results = web.get("results", [])
        if not isinstance(raw_results, list):
            raise SearchProviderError(
                "Brave search response 'web.results' field was not a JSON array.",
                category="malformed",
            )

        results: list[SearchResult] = []
        for item in raw_results:
            if not isinstance(item, dict):
                raise SearchProviderError(
                    "Brave search response 'web.results' contained a non-object entry.",
                    category="malformed",
                )
            url = item.get("url")
            if not url:
                raise SearchProviderError(
                    "Brave search response 'web.results' entry was missing 'url'.",
                    category="malformed",
                )
            results.append(
                SearchResult(
                    title=str(item.get("title", "")),
                    url=str(url),
                    snippet=str(item.get("description", "")),
                )
            )
        return results
