"""``SearchProvider`` seam: the search backend behind ``webglass search``.

Issue #1 lists ranking and page content among what "external providers own";
this module only defines the shape WebGlass asks a provider to fill in and a
deterministic :class:`FakeSearchProvider` that fills it in from canned data,
so the M1 operation core can be built and tested (t9/t10) without a real
search API key. ``t15`` implements a real vendor (proposed: Brave Search API)
behind this same seam.

Trust zones (issue #1 section 7): every field on :class:`SearchResult` is
*untrusted source material* — a provider or the page it names authored the
title/url/snippet, not WebGlass. Nothing here classifies or labels that text;
routing it into :class:`webglass.results.TrustZones.untrusted` is t9's job.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "SearchProvider",
    "SearchResult",
    "SearchResultSet",
    "FakeSearchProvider",
]


@dataclass(frozen=True)
class SearchResult:
    """One search hit. ``title``/``url``/``snippet`` are untrusted source material."""

    title: str
    url: str
    snippet: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"title": self.title, "url": self.url, "snippet": self.snippet}


@dataclass(frozen=True)
class SearchResultSet:
    """The result of one :meth:`SearchProvider.search` call.

    ``query`` echoes back the caller's own input (trusted: WebGlass already
    had it before asking the provider), ``provider_id`` names which provider
    answered (trusted, WebGlass-authored), ``results`` is untrusted — see
    :class:`SearchResult`.
    """

    query: str
    provider_id: str
    results: tuple[SearchResult, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "provider_id": self.provider_id,
            "results": [result.to_dict() for result in self.results],
        }


@runtime_checkable
class SearchProvider(Protocol):
    """The contract any search backend must satisfy.

    Synchronous by design (CLAUDE.md "Target architecture" section 8; this
    task's design guidance): the CLI is a one-shot process, so async is at
    most an adapter-internal implementation detail, never part of this seam.
    """

    def search(self, query: str, limit: int = 10) -> SearchResultSet:
        """Return up to ``limit`` results for ``query``.

        An unknown or zero-hit query returns a :class:`SearchResultSet` with
        an empty ``results`` tuple — never an exception. A provider-side
        failure (network error, malformed response, rejected key) is a
        structured concern for the concrete implementation to surface (t15);
        this seam does not prescribe an error channel beyond "do not raise
        for an ordinary empty result".
        """
        ...  # pragma: no cover - protocol method


class FakeSearchProvider:
    """Deterministic in-memory :class:`SearchProvider` driven by a canned index.

    ``index`` maps a query string to the results it should return. A query
    absent from ``index`` returns an empty :class:`SearchResultSet` rather
    than raising ``KeyError`` — the same "unknown input is a structured empty
    result, not an exception" property the fetch/browser fakes hold to.
    """

    def __init__(self, provider_id: str, index: Mapping[str, Sequence[SearchResult]]) -> None:
        self.provider_id = provider_id
        self._index = {query: tuple(results) for query, results in index.items()}
        self.queries: list[str] = []

    def search(self, query: str, limit: int = 10) -> SearchResultSet:
        self.queries.append(query)
        results = self._index.get(query, ())
        return SearchResultSet(
            query=query, provider_id=self.provider_id, results=tuple(results[:limit])
        )
