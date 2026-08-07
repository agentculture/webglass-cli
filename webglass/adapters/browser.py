"""``BrowserBackend`` seam: the browser session behind ``webglass page``/``action``.

Issue #1 section 8 and CLAUDE.md "Target architecture" section 8 are explicit
that Playwright is "the first backend, not the operation model" — Playwright
types must never cross this seam. This module defines that seam as four
methods that speak only in session ids (plain strings), URLs, HTML text, and
local dataclasses built from stdlib types:

- :meth:`BrowserBackend.open` — navigate a session to a URL and return the
  raw page state (final URL, redirect chain, HTML, console messages, page
  errors). Producing a :class:`~webglass.pages.PageSnapshot` from that HTML
  is a separate step (:func:`open_page_snapshot`) built on
  :func:`webglass.extraction.extract_page` — this module does not duplicate
  extraction, it only supplies the raw material.
- :meth:`BrowserBackend.press` — dispatch a key sequence and report it back.
- :meth:`BrowserBackend.screenshot` — capture the current page as PNG bytes.
- :meth:`BrowserBackend.close` — release whatever resources a session holds.

**Launch/connect lifecycle is deliberately absent from this protocol.** A
concrete backend resolves ``session_id`` to a live connection (for t11's
Playwright adapter: a CDP endpoint, reattached from
``webglass.sessions.SessionRecord.endpoint_ref``) using whatever session-store
reference *it* was constructed with — that resolution is the concrete
backend's own business, not part of the seam every backend must implement
identically. This keeps the protocol implementable by something that has
never heard of a live browser process at all (this module's fakes).

**Policy consumption seam.** Exactly like
:mod:`webglass.adapters.fetch`, an optional
:class:`webglass.policy.WebPolicyEvaluator` constructor argument lets a
backend re-evaluate every redirect hop before following it (CLAUDE.md
section 7). A denied hop stops navigation and is reported as
:attr:`BrowserOpenResult.blocked` with the verdict attached — never an
exception.

**Console/page-error text is untrusted source material** (issue #1
section 7): :class:`ConsoleMessage` and :class:`PageError` carry it as plain
strings, structurally apart from anything WebGlass authored. Routing it into
:class:`webglass.results.TrustZones.untrusted` is t9/t13's job — this module
only captures and carries it.
"""

from __future__ import annotations

import struct
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from webglass.extraction import extract_page
from webglass.pages import PageSnapshot
from webglass.policy import PolicyVerdict, WebPolicyEvaluator
from webglass.results import NavigationHop

__all__ = [
    "ConsoleMessage",
    "PageError",
    "BrowserOpenResult",
    "PressResult",
    "BrowserBackend",
    "FakeBrowserRoute",
    "FakeBrowserBackend",
    "open_page_snapshot",
    "encode_solid_png",
    "is_decodable_png",
]

#: Status reported when a session opens a URL absent from a fake's route
#: table. An ordinary HTTP not-found, not a WebGlass sentinel.
_DEFAULT_NOT_FOUND_STATUS = 404
_MAX_HOPS = 20


@dataclass(frozen=True)
class ConsoleMessage:
    """One console message observed while a page was open.

    ``text`` and ``source_url`` are *untrusted source material* — the page
    authored them (issue #1 section 7). See the hostile
    ``tests/fixtures/pages/spoofed_console.html`` fixture: a page can log
    text designed to impersonate a WebGlass warning, and this dataclass's
    only job is to carry that text intact and clearly labeled as page-sourced
    — never to interpret or render it.
    """

    level: str
    text: str
    source_url: str | None = None
    line: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "text": self.text,
            "source_url": self.source_url,
            "line": self.line,
        }


@dataclass(frozen=True)
class PageError:
    """One uncaught page error (exception) observed while a page was open.

    ``text`` and ``source_url`` are untrusted source material — see
    :class:`ConsoleMessage`. See
    ``tests/fixtures/pages/throw.html``, which throws synchronously on load.
    """

    text: str
    source_url: str | None = None
    line: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "source_url": self.source_url, "line": self.line}


@dataclass(frozen=True)
class BrowserOpenResult:
    """Raw result of :meth:`BrowserBackend.open` — plain data, no browser handle.

    ``html`` is exactly what the browser rendered/served — untrusted source
    material, same as a fetched page body. ``final_url``/``redirect_chain``
    are WebGlass's own recorded observation of navigation.

    An unknown URL (nothing a backend can resolve) reports as an ordinary
    not-found ``status`` with empty ``html`` — never an exception. A
    policy-denied hop reports as ``blocked=True`` with ``policy_verdict``
    set and empty ``html`` — also never an exception.
    """

    requested_url: str
    final_url: str
    status: int | None
    html: str = ""
    redirect_chain: tuple[NavigationHop, ...] = ()
    console_messages: tuple[ConsoleMessage, ...] = ()
    page_errors: tuple[PageError, ...] = ()
    blocked: bool = False
    policy_verdict: PolicyVerdict | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested_url": self.requested_url,
            "final_url": self.final_url,
            "status": self.status,
            "html": self.html,
            "redirect_chain": [hop.to_dict() for hop in self.redirect_chain],
            "console_messages": [message.to_dict() for message in self.console_messages],
            "page_errors": [error.to_dict() for error in self.page_errors],
            "blocked": self.blocked,
            "policy_verdict": self.policy_verdict.to_dict() if self.policy_verdict else None,
        }


@dataclass(frozen=True)
class PressResult:
    """Result of one :meth:`BrowserBackend.press` call.

    ``pressed`` is exactly the key sequence this call dispatched; ``key_log``
    is the cumulative sequence observed on the session so far (mirrors
    ``window.__webglassKeyLog`` in ``tests/fixtures/pages/keydown.html`` — a
    real backend populates this by reading that state back from the page
    after dispatching real key events).
    """

    session_id: str
    pressed: tuple[str, ...]
    key_log: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "pressed": list(self.pressed),
            "key_log": list(self.key_log),
        }


@runtime_checkable
class BrowserBackend(Protocol):
    """The contract any browser backend must satisfy.

    Every method takes ``session_id`` as a plain string, never a live handle
    — see the module docstring for why launch/connect lifecycle is
    deliberately absent from this surface.
    """

    def open(self, session_id: str, url: str) -> BrowserOpenResult:
        """Navigate ``session_id`` to ``url`` and return the raw page state."""
        ...  # pragma: no cover - protocol method

    def press(self, session_id: str, keys: Sequence[str], delay_ms: float = 0) -> PressResult:
        """Dispatch ``keys`` in order on the currently open page."""
        ...  # pragma: no cover - protocol method

    def screenshot(self, session_id: str) -> bytes:
        """Capture the current page as PNG bytes."""
        ...  # pragma: no cover - protocol method

    def close(self, session_id: str) -> None:
        """Release whatever resources ``session_id`` holds in this backend."""
        ...  # pragma: no cover - protocol method


def open_page_snapshot(
    backend: BrowserBackend,
    session_id: str,
    url: str,
    *,
    snapshot_id: str,
    retrieved_at: str,
    generation: int = 0,
) -> PageSnapshot:
    """Open ``url`` through ``backend`` and extract a :class:`PageSnapshot`.

    The seam this function bridges: a browser backend hands back raw HTML
    (:class:`BrowserOpenResult`), and turning that into the agent-facing
    snapshot is :func:`webglass.extraction.extract_page`'s job, applied
    identically whether the HTML came from a fake or from t11's Playwright
    adapter. Neither backend performs extraction itself.

    An unknown URL or a policy-denied hop both flow through unremarkably:
    ``result.html`` is empty, so the returned snapshot is simply an empty
    one carrying the reported ``status`` — never an exception.
    """
    result = backend.open(session_id, url)
    return extract_page(
        result.html,
        snapshot_id=snapshot_id,
        requested_url=url,
        final_url=result.final_url,
        retrieved_at=retrieved_at,
        generation=generation,
        status=result.status,
        redirect_chain=tuple(
            hop.response_url if hop.response_url is not None else hop.requested_url
            for hop in result.redirect_chain
        ),
    )


@dataclass(frozen=True)
class FakeBrowserRoute:
    """One canned route in a :class:`FakeBrowserBackend`'s table.

    ``redirect_to`` set means this route is a redirect hop, not terminal
    content — ``html``/``console_messages``/``page_errors`` are ignored for a
    redirect route. No JavaScript is executed by the fake — it has no JS
    engine — so ``console_messages``/``page_errors`` are pre-scripted here
    rather than produced by evaluating a page's actual ``<script>`` content.
    """

    status: int = 200
    redirect_to: str | None = None
    html: str = ""
    console_messages: tuple[ConsoleMessage, ...] = ()
    page_errors: tuple[PageError, ...] = ()


@dataclass
class _FakeSessionState:
    last_url: str | None = None
    key_log: list[str] = field(default_factory=list)


class FakeBrowserBackend:
    """In-memory :class:`BrowserBackend` driven by a canned URL -> route table.

    Suitable for exercising the full open/press/screenshot/close lifecycle in
    tests without a real browser process. ``routes`` can be built from
    literal HTML strings or by reading files under
    ``tests/fixtures/pages/`` (see ``tests/fixtures/server.py``), letting a
    conformance test point the exact same fixture markup at both this fake
    and, later, t11's Playwright adapter driving a real fixture HTTP server.
    """

    def __init__(
        self,
        routes: Mapping[str, FakeBrowserRoute],
        *,
        policy: WebPolicyEvaluator | None = None,
        not_found_status: int = _DEFAULT_NOT_FOUND_STATUS,
    ) -> None:
        self._routes = dict(routes)
        self._policy = policy
        self._not_found_status = not_found_status
        self._sessions: dict[str, _FakeSessionState] = {}

    def _state(self, session_id: str) -> _FakeSessionState:
        return self._sessions.setdefault(session_id, _FakeSessionState())

    def open(self, session_id: str, url: str) -> BrowserOpenResult:
        state = self._state(session_id)
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
                    state.last_url = current
                    return BrowserOpenResult(
                        requested_url=url,
                        final_url=current,
                        status=None,
                        redirect_chain=tuple(chain),
                        blocked=True,
                        policy_verdict=verdict,
                    )

            if current in seen or len(chain) > _MAX_HOPS:
                raise RuntimeError(f"redirect loop detected at {current!r} for open({url!r})")
            seen.add(current)

            route = self._routes.get(current)
            if route is None:
                chain.append(
                    NavigationHop(
                        requested_url=current, response_url=None, status=self._not_found_status
                    )
                )
                state.last_url = current
                return BrowserOpenResult(
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
            state.last_url = current
            return BrowserOpenResult(
                requested_url=url,
                final_url=current,
                status=route.status,
                html=route.html,
                redirect_chain=tuple(chain),
                console_messages=route.console_messages,
                page_errors=route.page_errors,
            )

    def press(
        self,
        session_id: str,
        keys: Sequence[str],
        # Part of the BrowserBackend protocol signature: an in-memory fake
        # has nothing to delay, but dropping the parameter would stop this
        # adapter conforming to the seam it stands in for.
        delay_ms: float = 0,  # NOSONAR(S1172)
    ) -> PressResult:
        state = self._state(session_id)
        pressed = tuple(keys)
        state.key_log.extend(pressed)
        return PressResult(session_id=session_id, pressed=pressed, key_log=tuple(state.key_log))

    def screenshot(self, session_id: str) -> bytes:
        # Touch session state so a screenshot on a never-opened session still
        # behaves like every other method (no KeyError for an unknown id).
        self._state(session_id)
        return encode_solid_png(2, 2, (12, 34, 56))

    def close(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)


# ---------------------------------------------------------------------------
# Hand-rolled PNG encode/decode (stdlib zlib + struct only; no new deps).
# ---------------------------------------------------------------------------

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + chunk_type
        + data
        + struct.pack(">I", zlib.crc32(chunk_type + data) & 0xFFFFFFFF)
    )


def encode_solid_png(width: int, height: int, rgb: tuple[int, int, int] = (0, 0, 0)) -> bytes:
    """Encode a minimal, valid solid-color PNG using only :mod:`zlib`/:mod:`struct`.

    This exists so :meth:`FakeBrowserBackend.screenshot` returns *decodable*
    PNG bytes (this task's acceptance criterion 3) without taking an imaging
    dependency (Pillow, etc.) — CLAUDE.md section 8: adapters stay
    dependency-light and this package's runtime dependency list stays empty.
    Produces an 8-bit truecolor (RGB, no alpha, no filtering) image.
    """
    if width <= 0 or height <= 0:
        raise ValueError("width and height must be positive")
    row = bytes(rgb) * width
    raw = b"".join(b"\x00" + row for _ in range(height))  # filter-type-0 (None) per scanline
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    idat = zlib.compress(raw)
    return (
        _PNG_SIGNATURE
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", idat)
        + _png_chunk(b"IEND", b"")
    )


def is_decodable_png(data: bytes) -> bool:
    """Structural PNG validation using only :mod:`zlib`/:mod:`struct`.

    Verifies the 8-byte signature, walks every chunk checking its declared
    length and CRC32, confirms ``IHDR`` is first and ``IEND`` is last, and
    confirms the concatenated ``IDAT`` payload decompresses cleanly. Used by
    the adapter conformance suite to assert "screenshot returns decodable PNG
    bytes" against *any* backend's output — this module's own hand-rolled
    solid-color PNG today, and a real Chromium-captured PNG (which carries
    extra ancillary chunks this function tolerates) at t11 — without taking
    an imaging dependency.
    """
    if not data.startswith(_PNG_SIGNATURE):
        return False
    walked = _walk_png_chunks(data)
    if walked is None:
        return False
    chunk_types, idat_payload = walked
    if not chunk_types or chunk_types[0] != b"IHDR" or chunk_types[-1] != b"IEND":
        return False
    if b"IDAT" not in chunk_types:
        return False
    try:
        zlib.decompress(idat_payload)
    except (zlib.error, ValueError):
        return False
    return True


def _walk_png_chunks(data: bytes) -> tuple[list[bytes], bytes] | None:
    """Walk the chunk stream after the signature, or ``None`` if it is malformed.

    Returns the chunk types in file order plus the concatenated ``IDAT``
    payload; whether that sequence is a *valid* PNG is the caller's judgement.
    """
    offset = len(_PNG_SIGNATURE)
    chunk_types: list[bytes] = []
    idat_payload = bytearray()
    while offset < len(data):
        chunk = _read_png_chunk(data, offset)
        if chunk is None:
            return None
        chunk_type, chunk_data, offset = chunk
        chunk_types.append(chunk_type)
        if chunk_type == b"IDAT":
            idat_payload.extend(chunk_data)
        if chunk_type == b"IEND":
            break
    return chunk_types, bytes(idat_payload)


def _read_png_chunk(data: bytes, offset: int) -> tuple[bytes, bytes, int] | None:
    """Read one length/type/payload/CRC chunk, or ``None`` if it does not check out.

    Every bound is verified before it is used, so a truncated or corrupt
    stream returns ``None`` rather than raising out of the walk. Returns the
    chunk type, its payload, and the offset the next chunk starts at.
    """
    if offset + 8 > len(data):
        return None
    (length,) = struct.unpack(">I", data[offset : offset + 4])
    chunk_type = data[offset + 4 : offset + 8]
    data_start = offset + 8
    data_end = data_start + length
    if data_end + 4 > len(data):
        return None
    chunk_data = data[data_start:data_end]
    (crc,) = struct.unpack(">I", data[data_end : data_end + 4])
    if zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF != crc:
        return None
    return chunk_type, chunk_data, data_end + 4
