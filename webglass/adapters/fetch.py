"""``FetchBackend`` seam: a plain (non-browser) HTTP fetch behind WebGlass.

This is the lighter-weight sibling of :mod:`webglass.adapters.browser` — no
sessions, no JavaScript, no console/page-error capture, just "give me the
bytes at this URL and tell me how you got there". A real implementation
(``urllib``/``httpx``, deferred past M2's Playwright-first path) sits behind
this seam later; :class:`FakeFetchBackend` is the M1 reference so the
operation core (t9) and CLI (t10) can be built and tested against a
deterministic canned URL -> response mapping.

**Redirect hops are reported one at a time, never collapsed** (issue #1
section 1: "redirect and navigation history"; CLAUDE.md section 7:
"revalidate every redirect hop") — :attr:`FetchResult.redirect_chain` is a
tuple of :class:`webglass.results.NavigationHop`, the same shape the result
model already carries, so a real backend's redirect trace and this fake's
both flow into ``WebOperationResult.navigation_history`` without translation
at t9.

**Policy consumption seam.** A :class:`webglass.policy.WebPolicyEvaluator` is
an optional constructor argument. When supplied, every hop in a redirect
chain is evaluated before it is followed — mirroring what a real fetch
backend must do (CLAUDE.md section 7: "revalidate every redirect hop"). A
denied hop stops the chain and is reported as :attr:`FetchResult.blocked`
with the denying :class:`~webglass.policy.PolicyVerdict` attached, never as
an exception. This module does not *redefine* policy evaluation — it only
consumes the evaluator already defined in :mod:`webglass.policy`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from webglass.policy import PolicyVerdict, WebPolicyEvaluator
from webglass.results import NavigationHop

__all__ = ["FetchBackend", "FetchResult", "FakeFetchRoute", "FakeFetchBackend"]

#: Status reported for a URL absent from a fake's route table — an ordinary
#: HTTP not-found, not a sentinel WebGlass invented.
_DEFAULT_NOT_FOUND_STATUS = 404

_MAX_HOPS = 20


@dataclass(frozen=True)
class FetchResult:
    """Result of one :meth:`FetchBackend.fetch` call.

    ``body``/``headers``/``content_type`` are untrusted source material (the
    remote server authored them); ``redirect_chain`` and ``policy_verdict``
    are WebGlass's own recorded observation of what happened, kept apart for
    the same reason :class:`webglass.results.NavigationHop`'s docstring
    gives.
    """

    requested_url: str
    final_url: str
    status: int | None
    body: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)
    content_type: str | None = None
    redirect_chain: tuple[NavigationHop, ...] = ()
    blocked: bool = False
    policy_verdict: PolicyVerdict | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested_url": self.requested_url,
            "final_url": self.final_url,
            "status": self.status,
            "body": self.body,
            "headers": dict(self.headers),
            "content_type": self.content_type,
            "redirect_chain": [hop.to_dict() for hop in self.redirect_chain],
            "blocked": self.blocked,
            "policy_verdict": self.policy_verdict.to_dict() if self.policy_verdict else None,
        }


@runtime_checkable
class FetchBackend(Protocol):
    """The contract any non-browser fetch backend must satisfy."""

    def fetch(self, url: str, *, method: str = "GET") -> FetchResult:
        """Fetch ``url``, following redirects, and return a structured result.

        An unknown/unreachable URL returns a :class:`FetchResult` with a
        not-found-shaped status (e.g. 404) — never an exception. A policy
        denial returns ``blocked=True`` with :attr:`FetchResult.policy_verdict`
        set — also never an exception.
        """
        ...  # pragma: no cover - protocol method


@dataclass(frozen=True)
class FakeFetchRoute:
    """One canned route in a :class:`FakeFetchBackend`'s table.

    ``redirect_to`` set means this route is a redirect hop, not terminal
    content — ``body``/``content_type`` are ignored for a redirect route.
    """

    status: int = 200
    redirect_to: str | None = None
    body: str = ""
    content_type: str | None = "text/html; charset=utf-8"
    headers: Mapping[str, str] = field(default_factory=dict)


class FakeFetchBackend:
    """In-memory :class:`FetchBackend` driven by a canned URL -> route table.

    Follows ``redirect_to`` chains in-process, exactly mirroring what a real
    HTTP client does when it receives a 3xx — this is what lets a redirect
    chain built entirely from :class:`FakeFetchRoute` entries exercise the
    same "reported hop by hop" contract a live backend must honor.
    """

    def __init__(
        self,
        routes: Mapping[str, FakeFetchRoute],
        *,
        policy: WebPolicyEvaluator | None = None,
        not_found_status: int = _DEFAULT_NOT_FOUND_STATUS,
    ) -> None:
        self._routes = dict(routes)
        self._policy = policy
        self._not_found_status = not_found_status
        self.requested_urls: list[str] = []

    def fetch(self, url: str, *, method: str = "GET") -> FetchResult:
        self.requested_urls.append(url)
        chain: list[NavigationHop] = []
        current = url
        seen: set[str] = set()

        while True:
            if self._policy is not None:
                verdict = self._policy.evaluate(current, hop_index=len(chain))
                if not verdict.allowed:
                    chain.append(
                        NavigationHop(requested_url=current, response_url=None, status=None)
                    )
                    return FetchResult(
                        requested_url=url,
                        final_url=current,
                        status=None,
                        redirect_chain=tuple(chain),
                        blocked=True,
                        policy_verdict=verdict,
                    )

            if current in seen or len(chain) > _MAX_HOPS:
                raise RuntimeError(f"redirect loop detected at {current!r} for fetch({url!r})")
            seen.add(current)

            route = self._routes.get(current)
            if route is None:
                chain.append(
                    NavigationHop(
                        requested_url=current, response_url=None, status=self._not_found_status
                    )
                )
                return FetchResult(
                    requested_url=url,
                    final_url=current,
                    status=self._not_found_status,
                    redirect_chain=tuple(chain),
                )

            if route.redirect_to is not None:
                chain.append(
                    NavigationHop(
                        requested_url=current, response_url=route.redirect_to, status=route.status
                    )
                )
                current = route.redirect_to
                continue

            chain.append(
                NavigationHop(requested_url=current, response_url=current, status=route.status)
            )
            return FetchResult(
                requested_url=url,
                final_url=current,
                status=route.status,
                body=route.body,
                headers=route.headers,
                content_type=route.content_type,
                redirect_chain=tuple(chain),
            )
