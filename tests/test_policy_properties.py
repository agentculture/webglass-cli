"""Corpus-driven property tests for the policy core.

Build-plan task t5 asks for the absent-vs-malformed distinction to be
*property*-tested. There is no hypothesis dependency in this repo (and adding
one would be an architectural decision of its own, like Playwright or PyYAML),
so the properties here are checked by exhausting deterministic corpora with
plain ``pytest.mark.parametrize``: every mandatory-denied scheme, sampled
addresses from every private/reserved network :mod:`ipaddress` knows about,
the legacy address encodings browsers still honour, and a bank of malformed
policy payloads.

The properties, stated once:

* **P1** nothing in the hostile corpus is ever allowed, under any well-formed
  profile the public API can express;
* **P2** malformed policy always yields ``decision=error`` and never an allow,
  for every URL --- including URLs a good profile would have allowed;
* **P3** absent policy is never an *error* --- it decides, deny-by-default;
* **P4** verdicts are total, deterministic, and cite only declared rule ids;
* **P5** a declared-target allow never leaks to a neighbouring origin.
"""

from __future__ import annotations

import ipaddress
from typing import Any

import pytest

from webglass.policy import (
    MANDATORY_DENIED_SCHEMES,
    METADATA_HOSTNAMES,
    METADATA_IP_ADDRESSES,
    RULE_IDS,
    PolicyDecision,
    PolicyError,
    WebPolicyEvaluator,
    WebPolicyProfile,
    build_evaluator,
)

# ---------------------------------------------------------------------------
# Corpora
# ---------------------------------------------------------------------------

#: Networks that must never be reachable. Sampled rather than exhausted --
#: three addresses per network (first, a middle one, last) is enough to catch
#: an off-by-one or a missing branch without a combinatorial blow-up.
_NON_PUBLIC_NETWORKS: tuple[str, ...] = (
    "0.0.0.0/8",
    "10.0.0.0/8",
    "100.64.0.0/10",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.0.0.0/24",
    "192.0.2.0/24",
    "192.168.0.0/16",
    "198.18.0.0/15",
    "198.51.100.0/24",
    "203.0.113.0/24",
    "224.0.0.0/4",
    "240.0.0.0/4",
    "::/128",
    "::1/128",
    "100::/64",
    "2001:db8::/32",
    "fc00::/7",
    "fe80::/10",
    "ff00::/8",
)


def _sample_addresses(cidr: str) -> list[str]:
    network = ipaddress.ip_network(cidr)
    size = network.num_addresses
    offsets = sorted({0, size // 2, size - 1})
    return [str(network[offset]) for offset in offsets]


def _as_url(address: str) -> str:
    parsed = ipaddress.ip_address(address)
    host = f"[{parsed}]" if parsed.version == 6 else str(parsed)
    return f"http://{host}/probe"


NON_PUBLIC_URLS: tuple[str, ...] = tuple(
    _as_url(address) for cidr in _NON_PUBLIC_NETWORKS for address in _sample_addresses(cidr)
)

DENIED_SCHEME_URLS: tuple[str, ...] = tuple(
    f"{scheme}://example.com/payload" for scheme in sorted(MANDATORY_DENIED_SCHEMES)
) + tuple(f"{scheme}:opaque-payload" for scheme in sorted(MANDATORY_DENIED_SCHEMES))

METADATA_URLS: tuple[str, ...] = tuple(
    f"http://{name}/latest/meta-data/" for name in sorted(METADATA_HOSTNAMES)
) + tuple(
    f"http://{f'[{ip}]' if ip.version == 6 else ip}/latest/"
    for ip in sorted(METADATA_IP_ADDRESSES, key=str)
)

#: Legacy ``inet_aton`` spellings of 127.0.0.1 and friends, plus the IPv6
#: wrappers that embed an IPv4 address.
OBFUSCATED_URLS: tuple[str, ...] = (
    "http://2130706433/",
    "http://017700000001/",
    "http://0x7f000001/",
    "http://0177.0.0.1/",
    "http://0x7f.0.0.1/",
    "http://127.1/",
    "http://127.0.1/",
    "http://192.168.257/",
    "http://[::ffff:127.0.0.1]/",
    "http://[::ffff:10.0.0.1]/",
    "http://[::ffff:169.254.169.254]/",
    "http://[2002:7f00:1::]/",
)

NAME_BASED_PRIVATE_URLS: tuple[str, ...] = (
    "http://localhost/",
    "http://LOCALHOST/",
    "http://localhost./",
    "http://localhost.localdomain/",
    "http://app.localhost/",
    "http://nas.local/",
    "http://svc.internal/",
    "http://thermostat.home.arpa/",
    "http://printer.lan/",
    "http://intranet/",
)

MALFORMED_URLS: tuple[str, ...] = (
    "",
    "   ",
    "no-scheme.example.com/path",
    "http://",
    "http://:8080/",
    "://example.com",
    "http://exam ple.com/",
    "http://example.com:notaport/",
    "http://user:secret@example.com/",
    "https://user@example.com/",
)

HOSTILE_URLS: tuple[str, ...] = (
    NON_PUBLIC_URLS
    + DENIED_SCHEME_URLS
    + METADATA_URLS
    + OBFUSCATED_URLS
    + NAME_BASED_PRIVATE_URLS
    + MALFORMED_URLS
)

PUBLIC_URLS: tuple[str, ...] = (
    "https://example.com/",
    "http://example.com/a/b?c=d#e",
    "https://docs.python.org/3/library/ipaddress.html",
    "http://1.1.1.1/",
    "http://8.8.8.8/",
    "http://93.184.216.34/",
    "http://[2606:4700::1111]/",
    "https://sub.domain.example.co.uk:8443/path",
)

ALL_URLS: tuple[str, ...] = HOSTILE_URLS + PUBLIC_URLS

#: Well-formed profiles the public API can express, including the most
#: permissive ones available, each paired with the *exact* subset of the
#: hostile corpus its declarations legitimately unlock. Spelling that subset
#: out by hand is the point: a declaration must buy access to precisely the
#: origins it names and nothing else, so any drift shows up as a corpus URL
#: the profile was never supposed to reach.
#:
#: Ids are spelled explicitly rather than derived from the values: a
#: ``frozenset``'s ``repr`` order depends on string hashing, which pytest-xdist
#: randomizes per worker, so a derived id makes workers disagree about which
#: tests exist.
_PROFILE_CASES: tuple[tuple[str, dict[str, Any], frozenset[str]], ...] = (
    ("empty", {}, frozenset()),
    ("named", {"name": "ci"}, frozenset()),
    ("wide-schemes", {"allowed_schemes": ["http", "https", "ftp", "ws", "wss"]}, frozenset()),
    ("emptied-denylist", {"denied_schemes": []}, frozenset()),
    ("no-redirects", {"max_redirects": 0}, frozenset()),
    ("loose-limits", {"max_redirects": 100, "max_response_bytes": 1}, frozenset()),
    # A declared origin with an explicit port unlocks nothing in the corpus,
    # because no corpus URL names port 8123.
    ("declared-port", {"declared_targets": ["http://127.0.0.1:8123"]}, frozenset()),
    # A port wildcard covers the scheme default port too, so the three
    # spellings of ``http://localhost/`` become reachable -- and only those.
    (
        "declared-wildcard",
        {"declared_targets": ["localhost:*"]},
        frozenset({"http://localhost/", "http://LOCALHOST/", "http://localhost./"}),
    ),
    (
        "declared-two",
        {"declared_targets": ["http://localhost:*", "https://app.test:*"]},
        frozenset({"http://localhost/", "http://LOCALHOST/", "http://localhost./"}),
    ),
    # Declaring a metadata endpoint buys nothing: the floor outranks it.
    ("declared-metadata-ip", {"declared_targets": ["http://169.254.169.254:*"]}, frozenset()),
    (
        "declared-metadata-name",
        {"declared_targets": ["http://metadata.google.internal:*"]},
        frozenset(),
    ),
    (
        "kitchen-sink",
        {
            "name": "kitchen-sink",
            "allowed_schemes": ["http", "https"],
            "denied_schemes": ["ftp"],
            "declared_targets": ["http://127.0.0.1:*", "[::1]:*"],
            "max_redirects": 9,
            "max_response_bytes": 999,
        },
        frozenset({"http://[::1]/probe"}),
    ),
)

PROFILE_CASES: tuple[tuple[dict[str, Any], frozenset[str]], ...] = tuple(
    (data, expected) for _, data, expected in _PROFILE_CASES
)
PROFILE_IDS: tuple[str, ...] = tuple(name for name, _, _ in _PROFILE_CASES)

WELL_FORMED_PROFILES: tuple[dict[str, Any], ...] = tuple(data for data, _ in PROFILE_CASES)

MALFORMED_POLICIES: tuple[Any, ...] = (
    [],
    ["allowed_schemes"],
    "http",
    42,
    3.14,
    True,
    {"unknown_key": 1},
    {"allow_private_targets": True},
    {"allow_loopback": True},
    {"allowedSchemes": ["http"]},
    {"allowed_schemes": "http"},
    {"allowed_schemes": ["http", None]},
    {"allowed_schemes": [1]},
    {"allowed_schemes": {"http": True}},
    {"denied_schemes": "file"},
    {"denied_schemes": [None]},
    {"declared_targets": "http://127.0.0.1:8123"},
    {"declared_targets": [None]},
    {"declared_targets": [{"host": "localhost", "port": 8123}]},
    {"declared_targets": [""]},
    {"declared_targets": ["http://"]},
    {"declared_targets": ["http://localhost:0"]},
    {"declared_targets": ["http://localhost:65536"]},
    {"declared_targets": ["http://localhost:eight"]},
    {"declared_targets": ["http://localhost:8123/app"]},
    {"declared_targets": ["http://*.example.com"]},
    {"declared_targets": ["ftp://localhost:8123"]},
    {"declared_targets": ["ws://localhost:8123"]},
    {"max_redirects": "5"},
    {"max_redirects": 5.0},
    {"max_redirects": -1},
    {"max_redirects": True},
    {"max_response_bytes": "big"},
    {"max_response_bytes": 0},
    {"max_response_bytes": -1},
    {"name": None},
    {"name": 5},
    {"name": ["ci"]},
    # Contradictions: a scheme both allowed and denied, in either direction.
    {"allowed_schemes": ["http", "file"]},
    {"allowed_schemes": ["javascript"]},
    {"allowed_schemes": ["http"], "denied_schemes": ["http"]},
    {"allowed_schemes": ["https"], "denied_schemes": ["https", "http"]},
)


# ---------------------------------------------------------------------------
# P1 -- the hostile corpus is never allowed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url", HOSTILE_URLS, ids=str)
def test_p1_default_profile_never_allows_a_hostile_url(url: str) -> None:
    verdict = WebPolicyEvaluator().evaluate(url)
    assert verdict.decision is PolicyDecision.DENIED, verdict


@pytest.mark.parametrize(("data", "expected"), PROFILE_CASES, ids=PROFILE_IDS)
def test_p1_a_profile_unlocks_exactly_what_it_declares(
    data: dict[str, Any], expected: frozenset[str]
) -> None:
    """h8/h12: the denylists are a floor a profile can only carve named holes in.

    Not "no profile allows anything hostile" --- a declared app under test is
    *supposed* to become reachable. The property is tighter: the set of
    hostile-corpus URLs a profile unlocks equals the set its declarations name,
    exactly.
    """
    evaluator = WebPolicyEvaluator(WebPolicyProfile.from_dict(data))
    allowed = {url for url in HOSTILE_URLS if evaluator.evaluate(url).allowed}
    assert allowed == expected, f"profile {data} unlocked {sorted(allowed ^ expected)}"


@pytest.mark.parametrize("url", METADATA_URLS, ids=str)
def test_p1_metadata_is_unreachable_under_every_profile(url: str) -> None:
    for data in WELL_FORMED_PROFILES:
        evaluator = WebPolicyEvaluator(WebPolicyProfile.from_dict(data))
        verdict = evaluator.evaluate(url)
        assert verdict.decision is PolicyDecision.DENIED
        assert "target-deny-metadata" in verdict.matched_rule_ids


@pytest.mark.parametrize("url", PUBLIC_URLS, ids=str)
def test_p1_the_denials_are_not_vacuous(url: str) -> None:
    """Guards the corpus itself: an evaluator that denied everything would
    pass every assertion above."""
    assert WebPolicyEvaluator().evaluate(url).allowed is True


# ---------------------------------------------------------------------------
# P2 -- malformed policy always errors, never allows
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("data", MALFORMED_POLICIES, ids=str)
def test_p2_malformed_policy_raises_from_from_dict(data: Any) -> None:
    with pytest.raises(PolicyError):
        WebPolicyProfile.from_dict(data)


@pytest.mark.parametrize("data", MALFORMED_POLICIES, ids=str)
def test_p2_malformed_policy_blocks_every_url(data: Any) -> None:
    evaluator = build_evaluator(data)
    assert evaluator.policy_source == "blocked"
    for url in ALL_URLS:
        verdict = evaluator.evaluate(url)
        assert verdict.decision is PolicyDecision.ERROR, (data, url)
        assert verdict.allowed is False
        assert verdict.matched_rule_ids == ("policy-malformed",)


@pytest.mark.parametrize("data", MALFORMED_POLICIES, ids=str)
def test_p2_malformed_policy_blocks_chains_and_resolution(data: Any) -> None:
    evaluator = build_evaluator(data)
    chain = evaluator.evaluate_redirect_chain(list(PUBLIC_URLS))
    assert len(chain) == 1
    assert chain[0].decision is PolicyDecision.ERROR
    resolved = evaluator.evaluate_resolved(PUBLIC_URLS[0], "93.184.216.34")
    assert resolved.decision is PolicyDecision.ERROR


@pytest.mark.parametrize("data", MALFORMED_POLICIES, ids=str)
def test_p2_malformed_policy_error_is_structured(data: Any) -> None:
    evaluator = build_evaluator(data)
    assert isinstance(evaluator.error, PolicyError)
    payload = evaluator.error.to_dict()
    assert payload["rule_id"] == "policy-malformed"
    assert isinstance(payload["message"], str)
    assert payload["message"]


# ---------------------------------------------------------------------------
# P3 -- absent policy decides; it does not error
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url", ALL_URLS, ids=str)
def test_p3_absent_policy_never_errors(url: str) -> None:
    evaluator = WebPolicyEvaluator()
    assert evaluator.policy_source == "default"
    assert evaluator.error is None
    assert evaluator.evaluate(url).decision is not PolicyDecision.ERROR


@pytest.mark.parametrize("data", WELL_FORMED_PROFILES, ids=PROFILE_IDS)
def test_p3_well_formed_policy_never_errors(data: dict[str, Any]) -> None:
    evaluator = build_evaluator(data)
    assert evaluator.policy_source == "explicit"
    for url in ALL_URLS:
        assert evaluator.evaluate(url).decision is not PolicyDecision.ERROR


# ---------------------------------------------------------------------------
# P4 -- verdicts are total, deterministic, and self-describing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url", ALL_URLS, ids=str)
def test_p4_every_verdict_cites_at_least_one_declared_rule(url: str) -> None:
    evaluator = WebPolicyEvaluator(
        WebPolicyProfile.from_dict({"declared_targets": ["http://127.0.0.1:8123"]})
    )
    verdict = evaluator.evaluate(url)
    assert verdict.matched_rule_ids
    assert set(verdict.matched_rule_ids) <= RULE_IDS
    assert verdict.reason
    assert verdict.url == url


@pytest.mark.parametrize("url", ALL_URLS, ids=str)
def test_p4_evaluation_is_deterministic(url: str) -> None:
    evaluator = WebPolicyEvaluator()
    first = evaluator.evaluate(url)
    second = evaluator.evaluate(url)
    assert first == second


@pytest.mark.parametrize("url", ALL_URLS, ids=str)
def test_p4_reason_text_stays_single_line_and_bounded(url: str) -> None:
    reason = WebPolicyEvaluator().evaluate(url).reason
    assert "\n" not in reason
    assert "\r" not in reason
    assert len(reason) < 500


@pytest.mark.parametrize("url", HOSTILE_URLS, ids=str)
def test_p4_a_hostile_url_anywhere_in_a_chain_stops_it(url: str) -> None:
    evaluator = WebPolicyEvaluator()
    chain = ["https://entry.example/", "https://second.example/", url, "https://after.example/"]
    verdicts = evaluator.evaluate_redirect_chain(chain)
    assert len(verdicts) == 3, "evaluation must stop at the denied hop"
    assert verdicts[-1].allowed is False
    assert verdicts[-1].hop_index == 2


# ---------------------------------------------------------------------------
# P5 -- a declared allow never leaks to a neighbouring origin
# ---------------------------------------------------------------------------

_NEIGHBOURING_ORIGINS: tuple[str, ...] = (
    "http://127.0.0.1:8124/",
    "http://127.0.0.1:80/",
    "http://127.0.0.1/",
    "http://127.0.0.2:8123/",
    "http://127.1.0.1:8123/",
    "http://localhost:8123/",
    "http://[::1]:8123/",
    "https://127.0.0.1:8123/",
    "http://10.0.0.1:8123/",
    "http://192.168.0.1:8123/",
    "http://169.254.169.254:8123/",
    # The userinfo confusion attack: a reader skimming for "127.0.0.1:8123"
    # sees the declared origin, but the real host is evil.example.
    "http://127.0.0.1:8123@evil.example/",
)


@pytest.mark.parametrize("url", _NEIGHBOURING_ORIGINS, ids=str)
def test_p5_declared_allow_does_not_leak(url: str) -> None:
    evaluator = WebPolicyEvaluator(
        WebPolicyProfile.from_dict({"declared_targets": ["http://127.0.0.1:8123"]})
    )
    verdict = evaluator.evaluate(url)
    assert verdict.decision is PolicyDecision.DENIED, verdict
    assert "profile-allow-declared-target" not in verdict.matched_rule_ids


def test_p5_the_declared_origin_itself_is_reachable() -> None:
    """Guards the corpus above: if nothing were reachable the test would be
    vacuous."""
    evaluator = WebPolicyEvaluator(
        WebPolicyProfile.from_dict({"declared_targets": ["http://127.0.0.1:8123"]})
    )
    for path in ("", "/", "/index.html", "/a/b?c=d#e"):
        assert evaluator.evaluate(f"http://127.0.0.1:8123{path}").allowed is True


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "192.168.1.1", "::1"], ids=str)
def test_p5_declared_target_resolution_stays_inside_the_declaration(address: str) -> None:
    evaluator = WebPolicyEvaluator(
        WebPolicyProfile.from_dict({"declared_targets": ["http://app.test:8123"]})
    )
    inside = evaluator.evaluate_resolved("http://app.test:8123/", address)
    assert inside.allowed is True
    outside = evaluator.evaluate_resolved("http://other.test:8123/", address)
    assert outside.allowed is False
