"""Stdlib-only local HTTP server serving the fixture pages.

Zero third-party dependencies: only :mod:`http.server` and :mod:`threading`
from the standard library. Every route resolves to a file under
``tests/fixtures/pages/`` or to a redirect response computed in-process --
nothing here ever reaches out to a real network, including the
``/redirect-private`` route, whose ``Location`` header names a private
target but is never itself followed by this server.

Usage (see ``tests/conftest.py`` for the pytest fixture that wraps this):

    site = FixtureSite()
    base_url = site.start()   # e.g. "http://127.0.0.1:54321"
    ...
    site.stop()
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_PAGES_DIR = Path(__file__).parent / "pages"

# Well-known link-local / cloud-metadata address. WebGlass's own policy core
# must deny navigation here by default (issue #1 section 10) -- this fixture
# only needs to advertise it as a redirect target so a policy/redirect test
# can assert the Location header without WebGlass (or this harness) ever
# actually connecting to it.
PRIVATE_TARGET = "http://169.254.169.254/"


def _read(name: str) -> bytes:
    return (_PAGES_DIR / name).read_bytes()


# path -> (content-type, body). Populated once at import time; the files are
# small and static, so there is no benefit to re-reading them per request.
_STATIC_ROUTES: dict[str, tuple[str, bytes]] = {
    "/clean": ("text/html; charset=utf-8", _read("clean.html")),
    "/throw": ("text/html; charset=utf-8", _read("throw.html")),
    "/keydown": ("text/html; charset=utf-8", _read("keydown.html")),
    "/agent-state": ("text/html; charset=utf-8", _read("agent_state.html")),
    "/spoofed-console": ("text/html; charset=utf-8", _read("spoofed_console.html")),
    "/boilerplate": ("text/html; charset=utf-8", _read("boilerplate.html")),
    "/final": ("text/html; charset=utf-8", _read("final.html")),
    "/redirect-chain/end": ("text/html; charset=utf-8", _read("final.html")),
}

# path -> Location header value. A plain 302 with no body.
_REDIRECT_ROUTES: dict[str, str] = {
    "/redirect1": "/redirect2",
    "/redirect2": "/final",
    "/redirect-chain/start": "/redirect-chain/mid",
    "/redirect-chain/mid": "/redirect-chain/end",
    "/redirect-private": PRIVATE_TARGET,
}


class FixtureRequestHandler(BaseHTTPRequestHandler):
    """Routes GET requests to the static fixture pages or redirect targets."""

    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        # Keep pytest output quiet; the tests assert on responses, not logs.
        return

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler method name
        path = self.path
        if path in _REDIRECT_ROUTES:
            self._send_redirect(_REDIRECT_ROUTES[path])
            return
        if path in _STATIC_ROUTES:
            content_type, body = _STATIC_ROUTES[path]
            self._send(200, content_type, body)
            return
        self._send(404, "text/plain; charset=utf-8", b"not found")

    def _send_redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FixtureSite:
    """Owns a background HTTP server thread serving the fixture pages.

    Binds to 127.0.0.1 on an OS-assigned ephemeral port (port 0) so parallel
    test workers never collide, and never touches any interface other than
    loopback.
    """

    def __init__(self) -> None:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureRequestHandler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> str:
        """Start serving in a background thread and return the base URL."""
        self._thread.start()
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def stop(self) -> None:
        """Shut down the server and join the background thread."""
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
