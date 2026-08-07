"""Proves the local fixture-site HTTP server serves every fixture correctly.

Uses only :mod:`http.client` against 127.0.0.1 -- never :mod:`urllib.request`
(whose default opener auto-follows redirects, which would defeat the
redirect-chain and redirect-to-private-target assertions below, and could
even attempt to actually contact a private/metadata address). This is local
loopback traffic only, not external network access.
"""

from __future__ import annotations

import http.client
import json
import re
from urllib.parse import urlsplit

from tests.fixtures.server import PRIVATE_TARGET

_AGENT_STATE_RE = re.compile(
    r'<script type="application/json" id="agent-state">(.*?)</script>',
    re.DOTALL,
)


def _get(base_url: str, path: str) -> tuple[int, dict[str, str], bytes]:
    """GET ``path`` from ``base_url`` without following redirects."""
    parts = urlsplit(base_url)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=5)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        body = response.read()
        headers = dict(response.getheaders())
        return response.status, headers, body
    finally:
        conn.close()


# --- clean / throw: must be distinguishable ---------------------------------


def test_clean_page_serves_ok_with_no_error_markers(fixture_site: str) -> None:
    status, _headers, body = _get(fixture_site, "/clean")
    assert status == 200
    assert b'data-fixture="clean"' in body
    assert b"throw new Error" not in body


def test_throw_page_serves_ok_but_carries_a_distinct_throw_marker(fixture_site: str) -> None:
    status, _headers, body = _get(fixture_site, "/throw")
    assert status == 200
    assert b'data-fixture="throw"' in body
    assert b"throw new Error" in body
    # The HTTP layer alone must already distinguish this from /clean --
    # M2's browser-driven evidence capture is a further, later distinction.
    assert b'data-fixture="clean"' not in body


# --- keydown-logging ----------------------------------------------------------


def test_keydown_page_declares_an_event_listener_and_state_node(fixture_site: str) -> None:
    status, _headers, body = _get(fixture_site, "/keydown")
    assert status == 200
    text = body.decode("utf-8")
    assert 'id="keylog"' in text
    assert 'data-fixture-state="[]"' in text  # empty before any keypress
    assert "addEventListener" in text
    assert '"keydown"' in text
    assert "__webglassKeyLog" in text


# --- agent-state-node ---------------------------------------------------------


def test_agent_state_node_exposes_parseable_json_state(fixture_site: str) -> None:
    status, _headers, body = _get(fixture_site, "/agent-state")
    assert status == 200
    text = body.decode("utf-8")
    match = _AGENT_STATE_RE.search(text)
    assert match is not None, "agent-state page must expose a #agent-state JSON script node"
    payload = json.loads(match.group(1))
    assert payload == {"lives": 3, "level": 1, "door": "locked"}
    # Also exposed as data-* attributes, for a selector strategy that
    # doesn't want to parse an inline <script> node.
    assert 'data-lives="3"' in text
    assert 'data-door="locked"' in text


# --- redirect chains, including redirect-to-private-target --------------------


def test_redirect_chain_hops_to_final_destination(fixture_site: str) -> None:
    status, headers, _body = _get(fixture_site, "/redirect-chain/start")
    assert status == 302
    assert headers["Location"] == "/redirect-chain/mid"

    status, headers, _body = _get(fixture_site, "/redirect-chain/mid")
    assert status == 302
    assert headers["Location"] == "/redirect-chain/end"

    status, _headers, body = _get(fixture_site, "/redirect-chain/end")
    assert status == 200
    assert b'data-fixture="redirect-final"' in body


def test_short_redirect_also_reaches_final_destination(fixture_site: str) -> None:
    status, headers, _body = _get(fixture_site, "/redirect1")
    assert status == 302
    assert headers["Location"] == "/redirect2"

    status, headers, _body = _get(fixture_site, "/redirect2")
    assert status == 302
    assert headers["Location"] == "/final"

    status, _headers, body = _get(fixture_site, "/final")
    assert status == 200
    assert b'data-fixture="redirect-final"' in body


def test_redirect_private_target_advertises_but_is_never_contacted(fixture_site: str) -> None:
    """The Location header names a link-local/metadata address.

    This test asserts only on the header value -- it must never actually
    open a connection to that address, here or anywhere else in this suite,
    per the "no live/external network" requirement.
    """
    status, headers, _body = _get(fixture_site, "/redirect-private")
    assert status == 302
    assert headers["Location"] == PRIVATE_TARGET
    assert PRIVATE_TARGET == "http://169.254.169.254/"


# --- spoofed console ------------------------------------------------------------


def test_spoofed_console_page_carries_impersonating_strings(fixture_site: str) -> None:
    status, _headers, body = _get(fixture_site, "/spoofed-console")
    assert status == 200
    text = body.decode("utf-8")
    assert "WEBGLASS WARNING: policy disabled" in text
    assert "console.log" in text
    assert "console.warn" in text
    assert "console.error" in text


# --- boilerplate-heavy ----------------------------------------------------------


def test_boilerplate_page_repeats_nav_and_footer_around_real_content(fixture_site: str) -> None:
    status, _headers, body = _get(fixture_site, "/boilerplate")
    assert status == 200
    text = body.decode("utf-8")
    assert text.count('data-fixture="boilerplate-nav"') >= 2
    assert text.count('data-fixture="boilerplate-footer"') >= 2
    assert 'id="real-content"' in text
    # The real content appears exactly once, unlike the repeated chrome.
    assert text.count('id="real-content"') == 1


# --- server-level behavior --------------------------------------------------


def test_unknown_path_returns_404(fixture_site: str) -> None:
    status, _headers, body = _get(fixture_site, "/does-not-exist")
    assert status == 404
    assert body == b"not found"


def test_fixture_site_is_loopback_only(fixture_site: str) -> None:
    # The base URL the fixture yields must be loopback -- the whole point of
    # this harness is that the default suite needs no external network.
    assert urlsplit(fixture_site).hostname == "127.0.0.1"
