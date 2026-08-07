"""The WebGlass policy core: explicit policy data in, structured verdict out.

This is the module that decides whether a web operation is allowed to touch a
given URL. It is pure: it takes a :class:`WebPolicyProfile` (plain data) and a
URL string, and returns a :class:`PolicyVerdict`. It reads no configuration
file, consults no environment variable, resolves no name, and makes no network
connection -- per ``docs/boundaries.md``, the caller resolves whatever
overlays it needs and hands WebGlass an *effective* profile. That keeps
WebGlass usable by a bare CLI invocation, a CI pipeline, or a mesh agent with
no Colleague runtime at all.

Four properties are load-bearing (issue #1 sections 10, 11 and 15; build-plan
task t5):

**Deny by default.** ``file:``, ``javascript:``, ``data:``, ``blob:`` and the
browser-internal schemes are refused, as are loopback, link-local,
private-network, and cloud-metadata targets. Anything not on the scheme
allowlist is refused too, so a scheme nobody thought about fails closed.

**Every redirect hop is re-evaluated.** :meth:`WebPolicyEvaluator.evaluate`
takes exactly one URL and knows nothing about where the caller came from, so
there is no chain state a caller could forget to re-check --- a caller
following a live redirect has nothing to call but ``evaluate(next_url)``.
:meth:`WebPolicyEvaluator.evaluate_redirect_chain` is a convenience for a
chain that is already known; it returns one verdict per hop and stops at the
first denial.

**The test/CI allowance is scoped data, not a bypass.** Driving a locally
served app under test requires naming it in ``declared_targets``; the default
denylists are never relaxed wholesale, and there is deliberately no
``allow_private_targets``-style switch. Cloud-metadata endpoints are refused
even when declared.

**Absent policy and malformed policy are different states.** No profile means
the built-in deny-by-default profile (a normal, non-error posture). A profile
WebGlass cannot parse raises :class:`PolicyError`; if the caller would rather
have verdicts than exceptions, :func:`build_evaluator` returns a *blocked*
evaluator that answers every question with ``decision=error``. Neither path
ever produces an allow.

DNS resolution belongs to the fetch/browser adapters (M2), not here --- but
the revalidation rule does: an adapter that has resolved a hostname calls
:meth:`WebPolicyEvaluator.evaluate_resolved` so a name that passed the
name-layer check cannot smuggle a private address in behind it.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from ipaddress import AddressValueError, IPv4Address, IPv6Address, ip_address
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

__all__ = [
    "DEFAULT_MAX_REDIRECTS",
    "DEFAULT_MAX_RESPONSE_BYTES",
    "MANDATORY_DENIED_SCHEMES",
    "METADATA_HOSTNAMES",
    "METADATA_IP_ADDRESSES",
    "RULE_IDS",
    "DeclaredTarget",
    "PolicyDecision",
    "PolicyError",
    "PolicyVerdict",
    "WebPolicyEvaluator",
    "WebPolicyProfile",
    "build_evaluator",
]


# ---------------------------------------------------------------------------
# Rule inventory
# ---------------------------------------------------------------------------

#: Every rule id a :class:`PolicyVerdict` may cite. Ids are stable strings so
#: a result, an evidence record, or a ``policy explain`` rendering can name
#: exactly which rule fired without re-deriving it. Grouped by the layer that
#: emits them; the evaluation order within each layer is documented on
#: :meth:`WebPolicyEvaluator.evaluate`.
RULE_IDS: frozenset[str] = frozenset(
    {
        # URL / scheme layer
        "url-unparseable",
        "url-missing-scheme",
        "url-missing-host",
        "url-deny-userinfo",
        "scheme-deny-file",
        "scheme-deny-javascript",
        "scheme-deny-data",
        "scheme-deny-blob",
        "scheme-deny-browser-internal",
        "scheme-deny-profile",
        "scheme-not-allowed",
        "scheme-allowed",
        # Target layer
        "target-deny-metadata",
        "target-deny-loopback",
        "target-deny-link-local",
        "target-deny-private",
        "target-deny-reserved",
        "target-deny-ambiguous-host",
        "target-deny-private-name",
        "target-allow-public",
        "profile-allow-declared-target",
        # Resolved-address layer (DNS revalidation, driven by the adapters)
        "resolved-address-unparseable",
        "resolved-target-deny-metadata",
        "resolved-target-deny-loopback",
        "resolved-target-deny-link-local",
        "resolved-target-deny-private",
        "resolved-target-deny-reserved",
        "resolved-target-allow-declared",
        "resolved-target-allow-public",
        # Redirect layer
        "redirect-depth-exceeded",
        # Policy state
        "policy-malformed",
    }
)


# ---------------------------------------------------------------------------
# Non-overridable floors
# ---------------------------------------------------------------------------

#: Browser-internal and pseudo schemes. Navigating to any of these either
#: escapes the web plane entirely (``file:``), executes caller-controlled code
#: in a page's origin (``javascript:``), or reaches into the browser's own
#: surfaces (``chrome:``, ``devtools:``). A profile may *add* denied schemes;
#: it can never subtract one of these.
MANDATORY_DENIED_SCHEMES: frozenset[str] = frozenset(
    {
        "file",
        "javascript",
        "data",
        "blob",
        "about",
        "chrome",
        "chrome-error",
        "chrome-extension",
        "chrome-search",
        "chrome-untrusted",
        "devtools",
        "edge",
        "filesystem",
        "moz-extension",
        "res",
        "resource",
        "view-source",
        "vbscript",
    }
)

_SPECIFIC_SCHEME_RULE_IDS: Mapping[str, str] = {
    "file": "scheme-deny-file",
    "javascript": "scheme-deny-javascript",
    "data": "scheme-deny-data",
    "blob": "scheme-deny-blob",
}

#: Cloud instance-metadata endpoints (AWS/Azure/GCP/Oracle/DigitalOcean share
#: 169.254.169.254; ECS task metadata, AWS IPv6 IMDS, and Alibaba have their
#: own). Reaching one is the canonical SSRF payoff, so these are refused
#: unconditionally --- including when a profile declares them as targets.
METADATA_IP_ADDRESSES: frozenset[IPv4Address | IPv6Address] = frozenset(
    {
        ip_address("169.254.169.254"),
        ip_address("169.254.170.2"),
        ip_address("100.100.100.200"),
        ip_address("192.0.0.192"),
        ip_address("fd00:ec2::254"),
    }
)

#: Hostnames that name a metadata endpoint without an IP literal.
METADATA_HOSTNAMES: frozenset[str] = frozenset(
    {
        "metadata",
        "metadata.goog",
        "metadata.google.internal",
        "instance-data",
        "instance-data.ec2.internal",
    }
)

#: Name suffixes reserved for private/intranet use. ``.local`` is mDNS,
#: ``.internal`` and ``.home.arpa`` are private-use zones --- all of them
#: resolve to something on the caller's own network.
_PRIVATE_NAME_SUFFIXES: tuple[str, ...] = (".local", ".internal", ".home.arpa", ".lan")

#: Loopback names per RFC 6761.
_LOOPBACK_NAMES: frozenset[str] = frozenset({"localhost", "localhost.localdomain"})

DEFAULT_ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})
DEFAULT_MAX_REDIRECTS: int = 5
DEFAULT_MAX_RESPONSE_BYTES: int = 5 * 1024 * 1024

_DEFAULT_PORTS: Mapping[str, int] = {"http": 80, "https": 443}
_REASON_LIMIT = 160


def _sanitize(text: str, limit: int = _REASON_LIMIT) -> str:
    """Make URL-derived text safe to embed in WebGlass-authored prose.

    Verdict ``reason`` strings are *trusted control metadata* --- they are
    rendered next to WebGlass's own warnings. The scheme and host they quote
    come from an untrusted URL, so a newline or a control character could let
    remote text forge a second diagnostic line. Strip anything unprintable and
    bound the length.
    """
    cleaned = "".join(ch for ch in text if ch.isprintable())
    if len(cleaned) > limit:
        cleaned = cleaned[:limit] + "..."
    return cleaned


# ---------------------------------------------------------------------------
# Verdicts and errors
# ---------------------------------------------------------------------------


class PolicyDecision(StrEnum):
    """The three outcomes of a policy evaluation.

    ``ERROR`` is deliberately distinct from ``DENIED``: a denial is policy
    working as designed, an error means policy could not be evaluated at all.
    Both block the operation; only one of them is the caller's fault.
    """

    ALLOWED = "allowed"
    DENIED = "denied"
    ERROR = "error"


@dataclass(frozen=True)
class PolicyVerdict:
    """One policy decision about one URL.

    Immutable, because a verdict is cited by results and evidence records
    after the fact. ``matched_rule_ids`` is a tuple for the same reason; the
    JSON rendering in :meth:`to_dict` turns it into a list.
    """

    decision: PolicyDecision
    url: str
    matched_rule_ids: tuple[str, ...]
    reason: str
    #: Position in a redirect chain when produced by
    #: :meth:`WebPolicyEvaluator.evaluate_redirect_chain`; ``None`` for a
    #: standalone evaluation.
    hop_index: int | None = None

    @property
    def allowed(self) -> bool:
        return self.decision is PolicyDecision.ALLOWED

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": str(self.decision),
            "url": self.url,
            "matched_rule_ids": list(self.matched_rule_ids),
            "reason": self.reason,
            "hop_index": self.hop_index,
        }


class PolicyError(Exception):
    """Malformed policy data: structured, and always fail-closed.

    Raised by :meth:`WebPolicyProfile.from_dict` and
    :meth:`DeclaredTarget.parse`. Carries the offending field so a caller can
    point at it, and converts to a ``decision=error`` verdict for callers that
    prefer verdicts to exceptions.
    """

    def __init__(self, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.field = field

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": "policy-malformed",
            "message": self.message,
            "field": self.field,
        }

    def to_verdict(self, url: str, hop_index: int | None = None) -> PolicyVerdict:
        return PolicyVerdict(
            decision=PolicyDecision.ERROR,
            url=url,
            matched_rule_ids=("policy-malformed",),
            reason=f"policy could not be evaluated: {_sanitize(self.message)}",
            hop_index=hop_index,
        )


# ---------------------------------------------------------------------------
# Declared targets
# ---------------------------------------------------------------------------


def _parse_port(raw: str, spec: str) -> int:
    if not raw.isdigit():
        raise PolicyError(
            f"declared target {spec!r} has a non-numeric port {raw!r}",
            field="declared_targets",
        )
    port = int(raw)
    if not 1 <= port <= 65535:
        raise PolicyError(
            f"declared target {spec!r} has an out-of-range port {port}",
            field="declared_targets",
        )
    return port


def _normalize_host(raw: str, spec: str) -> str:
    host = raw.strip().rstrip(".").lower()
    if not host:
        raise PolicyError(f"declared target {spec!r} names no host", field="declared_targets")
    if any(ch.isspace() for ch in host) or "*" in host or "/" in host:
        raise PolicyError(
            f"declared target {spec!r} has an invalid host {raw!r}; host wildcards "
            "are not supported (declare each origin explicitly)",
            field="declared_targets",
        )
    try:
        return str(ip_address(host))
    except ValueError:
        return host


@dataclass(frozen=True)
class DeclaredTarget:
    """One app-under-test origin an explicit profile authorizes.

    Matching is exact: scheme, host, and port must all line up. There is no
    subdomain wildcard --- only a port wildcard (``:*``), which is what a CI
    job needs when its dev server picks an ephemeral port. A missing scheme
    means "any scheme the profile already allows", which can never widen the
    scheme allowlist because the scheme layer is checked first.
    """

    spec: str
    scheme: str | None
    host: str
    #: ``None`` means any port; otherwise the exact ports authorized.
    ports: frozenset[int] | None

    @classmethod
    def parse(cls, spec: str) -> DeclaredTarget:
        """Parse ``scheme://host[:port|:*]`` (scheme and port both optional)."""
        if not isinstance(spec, str):
            raise PolicyError(
                f"declared target must be a string, got {type(spec).__name__}",
                field="declared_targets",
            )
        text = spec.strip()
        if not text:
            raise PolicyError("declared target must not be empty", field="declared_targets")

        scheme: str | None = None
        rest = text
        if "://" in text:
            scheme_part, rest = text.split("://", 1)
            scheme = scheme_part.strip().lower()
            if not scheme:
                raise PolicyError(
                    f"declared target {spec!r} has an empty scheme", field="declared_targets"
                )
        if any(sep in rest for sep in ("/", "?", "#", "@")):
            raise PolicyError(
                f"declared target {spec!r} must be an origin only "
                "(no path, query, fragment, or credentials)",
                field="declared_targets",
            )

        host_part, port_part = _split_host_port(rest, spec)
        host = _normalize_host(host_part, spec)

        ports: frozenset[int] | None
        if port_part == "*":
            ports = None
        elif port_part is not None:
            ports = frozenset({_parse_port(port_part, spec)})
        elif scheme is not None:
            default = _DEFAULT_PORTS.get(scheme)
            if default is None:
                raise PolicyError(
                    f"declared target {spec!r} needs an explicit port "
                    f"(no default port known for scheme {scheme!r})",
                    field="declared_targets",
                )
            ports = frozenset({default})
        else:
            ports = frozenset(_DEFAULT_PORTS.values())

        return cls(spec=spec, scheme=scheme, host=host, ports=ports)

    def matches(
        self, scheme: str, host: str, port: int | None, allowed_schemes: frozenset[str]
    ) -> bool:
        """True when a request origin falls inside this declaration."""
        if self.scheme is None:
            if scheme not in allowed_schemes:
                return False
        elif scheme != self.scheme:
            return False
        if host != self.host:
            return False
        if self.ports is None:
            return True
        effective = port if port is not None else _DEFAULT_PORTS.get(scheme)
        return effective is not None and effective in self.ports


def _split_host_port(rest: str, spec: str) -> tuple[str, str | None]:
    """Split ``host[:port]`` handling bracketed IPv6 literals."""
    if rest.startswith("["):
        end = rest.find("]")
        if end == -1:
            raise PolicyError(
                f"declared target {spec!r} has an unterminated IPv6 literal",
                field="declared_targets",
            )
        host_part = rest[1:end]
        tail = rest[end + 1 :]
        if not tail:
            return host_part, None
        if not tail.startswith(":"):
            raise PolicyError(
                f"declared target {spec!r} has trailing text after the IPv6 literal",
                field="declared_targets",
            )
        return host_part, tail[1:]
    if rest.count(":") > 1:
        # A bare IPv6 literal must be bracketed; anything else with two colons
        # is ambiguous between host separators and a port.
        raise PolicyError(
            f"declared target {spec!r} is ambiguous; bracket IPv6 literals as [::1]:port",
            field="declared_targets",
        )
    if ":" in rest:
        host_part, port_part = rest.split(":", 1)
        return host_part, port_part
    return rest, None


# ---------------------------------------------------------------------------
# The profile
# ---------------------------------------------------------------------------

_PROFILE_KEYS = frozenset(
    {
        "name",
        "allowed_schemes",
        "denied_schemes",
        "declared_targets",
        "max_redirects",
        "max_response_bytes",
    }
)


def _require_str_sequence(value: object, key: str) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise PolicyError(f"{key} must be a list of strings, got {type(value).__name__}", field=key)
    items: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise PolicyError(
                f"{key} entries must be strings, got {type(item).__name__}", field=key
            )
        items.append(item)
    return tuple(items)


def _require_positive_int(value: object, key: str, minimum: int) -> int:
    # bool is an int subclass; a boolean here is a type confusion, not a limit.
    if isinstance(value, bool) or not isinstance(value, int):
        raise PolicyError(f"{key} must be an integer, got {type(value).__name__}", field=key)
    if value < minimum:
        raise PolicyError(f"{key} must be >= {minimum}, got {value}", field=key)
    return value


@dataclass(frozen=True)
class WebPolicyProfile:
    """The effective web policy: explicit data, no config-file knowledge.

    Constructed either directly (for tests and library callers that already
    hold typed values) or from a plain mapping via :meth:`from_dict`, which
    validates strictly --- unknown keys, wrong types, and contradictions are
    all malformed policy, never a best-effort partial parse.
    """

    name: str = "built-in-default"
    allowed_schemes: frozenset[str] = DEFAULT_ALLOWED_SCHEMES
    #: Always a superset of :data:`MANDATORY_DENIED_SCHEMES`.
    denied_schemes: frozenset[str] = MANDATORY_DENIED_SCHEMES
    declared_targets: tuple[DeclaredTarget, ...] = ()
    max_redirects: int = DEFAULT_MAX_REDIRECTS
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES

    def __post_init__(self) -> None:
        # Enforce the floor even for direct construction: a caller that hands
        # in a narrower denylist gets the mandatory schemes back regardless.
        object.__setattr__(self, "allowed_schemes", frozenset(self.allowed_schemes))
        object.__setattr__(
            self, "denied_schemes", frozenset(self.denied_schemes) | MANDATORY_DENIED_SCHEMES
        )
        object.__setattr__(self, "declared_targets", tuple(self.declared_targets))

    @classmethod
    def default(cls) -> WebPolicyProfile:
        """The built-in deny-by-default profile used when no policy is given."""
        return cls()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> WebPolicyProfile:
        """Validate and build a profile from plain data.

        :raises PolicyError: on any malformation --- non-mapping input,
            unknown keys, wrong types, out-of-range limits, unparseable
            declared targets, or contradictions (a scheme both allowed and
            denied, a declared target whose scheme could never be reached).
        """
        if not isinstance(data, Mapping):
            raise PolicyError(
                f"policy must be a mapping, got {type(data).__name__}",
            )
        unknown = sorted(set(data) - _PROFILE_KEYS)
        if unknown:
            raise PolicyError(
                f"unknown policy key(s): {', '.join(unknown)}; known keys are "
                f"{', '.join(sorted(_PROFILE_KEYS))}",
                field=unknown[0],
            )

        name = data.get("name", "explicit")
        if not isinstance(name, str):
            raise PolicyError(f"name must be a string, got {type(name).__name__}", field="name")

        if "allowed_schemes" in data:
            allowed = frozenset(
                s.strip().lower()
                for s in _require_str_sequence(data["allowed_schemes"], "allowed_schemes")
            )
        else:
            allowed = DEFAULT_ALLOWED_SCHEMES

        extra_denied = (
            frozenset(
                s.strip().lower()
                for s in _require_str_sequence(data["denied_schemes"], "denied_schemes")
            )
            if "denied_schemes" in data
            else frozenset()
        )
        denied = extra_denied | MANDATORY_DENIED_SCHEMES

        contradictory = sorted(allowed & denied)
        if contradictory:
            raise PolicyError(
                f"scheme(s) both allowed and denied: {', '.join(contradictory)}; "
                "the mandatory denylist cannot be overridden",
                field="allowed_schemes",
            )

        targets = tuple(
            DeclaredTarget.parse(spec)
            for spec in _require_str_sequence(data.get("declared_targets", ()), "declared_targets")
        )
        for target in targets:
            if target.scheme is not None and target.scheme not in allowed:
                raise PolicyError(
                    f"declared target {target.spec!r} uses scheme {target.scheme!r}, "
                    "which is not in allowed_schemes",
                    field="declared_targets",
                )

        max_redirects = _require_positive_int(
            data.get("max_redirects", DEFAULT_MAX_REDIRECTS), "max_redirects", 0
        )
        max_response_bytes = _require_positive_int(
            data.get("max_response_bytes", DEFAULT_MAX_RESPONSE_BYTES), "max_response_bytes", 1
        )

        return cls(
            name=name,
            allowed_schemes=allowed,
            denied_schemes=denied,
            declared_targets=targets,
            max_redirects=max_redirects,
            max_response_bytes=max_response_bytes,
        )

    def to_dict(self) -> dict[str, Any]:
        """A JSON-shaped rendering that round-trips through :meth:`from_dict`."""
        return {
            "name": self.name,
            "allowed_schemes": sorted(self.allowed_schemes),
            "denied_schemes": sorted(self.denied_schemes),
            "declared_targets": [target.spec for target in self.declared_targets],
            "max_redirects": self.max_redirects,
            "max_response_bytes": self.max_response_bytes,
        }

    def with_declared_targets(self, specs: Iterable[str]) -> WebPolicyProfile:
        """A copy of this profile with additional declared targets."""
        added = tuple(DeclaredTarget.parse(spec) for spec in specs)
        return replace(self, declared_targets=self.declared_targets + added)


# ---------------------------------------------------------------------------
# Address classification
# ---------------------------------------------------------------------------


def _unmap(ip: IPv4Address | IPv6Address) -> IPv4Address | IPv6Address:
    """Return the embedded IPv4 address of a mapped/6to4/Teredo IPv6 address.

    ``::ffff:127.0.0.1`` is loopback in every way that matters, but
    ``IPv6Address.is_loopback`` is False for it --- so classification has to
    look through the wrapper or the check is bypassable.
    """
    if isinstance(ip, IPv6Address):
        if ip.ipv4_mapped is not None:
            return ip.ipv4_mapped
        if ip.sixtofour is not None:
            return ip.sixtofour
        if ip.teredo is not None:
            return ip.teredo[1]
    return ip


def _classify_address(ip: IPv4Address | IPv6Address) -> tuple[str, str] | None:
    """Classify an address for denial.

    Returns ``(rule_suffix, human_label)`` when the address must be refused,
    or ``None`` when it is an ordinary globally routable target. The suffix is
    shared between the URL layer (``target-deny-<suffix>``) and the resolved
    layer (``resolved-target-deny-<suffix>``) so the two stay in step.
    """
    candidates = {ip, _unmap(ip)}
    if candidates & METADATA_IP_ADDRESSES:
        return "metadata", "cloud instance-metadata endpoint"
    for candidate in (_unmap(ip), ip):
        if candidate.is_loopback:
            return "loopback", "loopback address"
        if candidate.is_link_local:
            return "link-local", "link-local address"
        if candidate.is_unspecified:
            return "reserved", "unspecified address"
        if candidate.is_multicast:
            return "reserved", "multicast address"
        if candidate.is_reserved:
            return "reserved", "reserved address"
        if candidate.is_private:
            return "private", "private-network address"
        if not candidate.is_global:
            return "reserved", "non-globally-routable address"
    return None


def _parse_ip_literal(host: str) -> IPv4Address | IPv6Address | None:
    """Parse a host as a strict IP literal, or return ``None`` for a name."""
    try:
        return ip_address(host)
    except (ValueError, AddressValueError):
        return None


def _decode_legacy_ipv4(host: str) -> IPv4Address | None:
    """Decode the legacy ``inet_aton`` forms browsers still accept.

    ``http://2130706433/``, ``http://0177.0.0.1/`` and ``http://127.1/`` all
    reach 127.0.0.1 in a browser, but :mod:`ipaddress` rejects every one of
    them. Without this, they would fall through the IP-literal branch and be
    classified as ordinary public hostnames --- a straight loopback bypass.
    """
    parts = host.split(".")
    if len(parts) > 4 or not all(parts):
        return None
    values: list[int] = []
    for part in parts:
        try:
            if part.lower().startswith("0x"):
                values.append(int(part, 16))
            elif part.startswith("0") and len(part) > 1:
                values.append(int(part, 8))
            elif part.isdigit():
                values.append(int(part))
            else:
                return None
        except ValueError:
            return None
    # inet_aton: the final part absorbs all remaining low-order octets.
    limits = [256] * (len(values) - 1) + [256 ** (4 - len(values) + 1)]
    if any(value < 0 or value >= limit for value, limit in zip(values, limits)):
        return None
    packed = 0
    for index, value in enumerate(values[:-1]):
        packed |= value << (8 * (3 - index))
    packed |= values[-1]
    try:
        return IPv4Address(packed)
    except (ValueError, AddressValueError):
        return None


def _classify_hostname(host: str) -> tuple[str, str] | None:
    """Classify a non-IP hostname for denial, or ``None`` if it looks public."""
    if host in METADATA_HOSTNAMES:
        return "metadata", "cloud instance-metadata hostname"
    if host in _LOOPBACK_NAMES or host.endswith(".localhost"):
        return "loopback", "loopback hostname"
    if any(host.endswith(suffix) for suffix in _PRIVATE_NAME_SUFFIXES):
        return "private-name", "private-use name suffix"
    if "." not in host:
        # A single-label name resolves through the caller's search domains to
        # something on their own network. Classify upward.
        return "private-name", "unqualified intranet hostname"
    return None


# ---------------------------------------------------------------------------
# Parsed URL
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ParsedTarget:
    scheme: str
    host: str
    port: int | None
    has_userinfo: bool


def _parse_url(url: str) -> _ParsedTarget | PolicyVerdict:
    """Parse a URL into the pieces policy cares about, or a denial verdict."""
    if not isinstance(url, str) or not url.strip():
        return _deny(url if isinstance(url, str) else "", "url-unparseable", "URL is empty")
    try:
        split = urlsplit(url)
        port = split.port
    except ValueError as exc:
        return _deny(url, "url-unparseable", f"URL cannot be parsed: {_sanitize(str(exc))}")

    scheme = split.scheme.strip().lower()
    if not scheme:
        return _deny(url, "url-missing-scheme", "URL declares no scheme")

    host = (split.hostname or "").rstrip(".").lower()
    # Normalize IP literals so every spelling of one address compares equal:
    # ``[0:0:0:0:0:0:0:1]`` and ``[::1]`` must not be two different targets.
    # This runs before the character check because a bare IPv6 literal
    # legitimately contains colons.
    literal = _parse_ip_literal(host)
    if literal is not None:
        host = str(literal)
    elif host and not _is_wellformed_host(host):
        return _deny(url, "url-unparseable", "URL host contains illegal characters")
    has_userinfo = "@" in split.netloc
    return _ParsedTarget(scheme=scheme, host=host, port=port, has_userinfo=has_userinfo)


#: Characters that can never legally appear in a host. Whitespace and control
#: characters make a URL ambiguous (different parsers disagree about where the
#: host ends), and the delimiters below belong to other URL components -- if
#: one survived into ``hostname`` the URL was malformed. Unicode letters are
#: left alone: an IDN host is legitimate and the browser punycodes it.
_ILLEGAL_HOST_CHARS = frozenset('/?#@[]:\\"<>^`{|}')


def _is_wellformed_host(host: str) -> bool:
    return not any(ch.isspace() or not ch.isprintable() or ch in _ILLEGAL_HOST_CHARS for ch in host)


def _deny(url: str, rule_id: str, reason: str, hop_index: int | None = None) -> PolicyVerdict:
    return PolicyVerdict(
        decision=PolicyDecision.DENIED,
        url=url,
        matched_rule_ids=(rule_id,),
        reason=reason,
        hop_index=hop_index,
    )


# ---------------------------------------------------------------------------
# The evaluator
# ---------------------------------------------------------------------------


@dataclass
class WebPolicyEvaluator:
    """Evaluates one URL at a time against one effective profile.

    Three states, distinguishable via :attr:`policy_source`:

    ``"default"``
        No profile was supplied --- the built-in deny-by-default profile
        applies. This is a normal posture, not an error.
    ``"explicit"``
        The caller supplied a parsed profile.
    ``"blocked"``
        The caller's policy data was malformed. Every evaluation returns
        ``decision=error``; nothing is ever allowed. Reachable only through
        :func:`build_evaluator` or :meth:`blocked`.
    """

    profile: WebPolicyProfile = field(default_factory=WebPolicyProfile.default)
    #: Set only for the blocked state; ``None`` otherwise.
    error: PolicyError | None = None
    _explicit: bool = field(default=False, repr=False)

    def __init__(
        self,
        profile: WebPolicyProfile | None = None,
        *,
        error: PolicyError | None = None,
    ) -> None:
        self._explicit = profile is not None
        self.profile = profile if profile is not None else WebPolicyProfile.default()
        self.error = error

    @classmethod
    def blocked(cls, error: PolicyError) -> WebPolicyEvaluator:
        """An evaluator that refuses to evaluate anything: malformed policy."""
        return cls(error=error)

    @classmethod
    def from_policy_data(cls, data: Mapping[str, Any] | None) -> WebPolicyEvaluator:
        """Build from plain policy data; ``None`` means the absent-policy state.

        :raises PolicyError: if ``data`` is present but malformed. Use
            :func:`build_evaluator` for the non-raising form.
        """
        if data is None:
            return cls()
        return cls(WebPolicyProfile.from_dict(data))

    @property
    def policy_source(self) -> str:
        if self.error is not None:
            return "blocked"
        return "explicit" if self._explicit else "default"

    # -- evaluation ---------------------------------------------------------

    def evaluate(self, url: str, hop_index: int | None = None) -> PolicyVerdict:
        """Decide whether one URL may be contacted.

        Rule order (first match wins, and the order is the security property):

        1. **URL shape** --- unparseable, scheme-less, or credential-bearing
           URLs are refused before anything else is inspected.
        2. **Scheme denylist**, then the scheme **allowlist**. A ``file:`` URL
           naming a public host is refused for its scheme, not its host.
        3. **Host presence.**
        4. **Cloud-metadata floor** --- checked *before* the declared-target
           allow, which is what makes metadata endpoints unallowable even when
           a profile names them.
        5. **Declared-target allow** --- an exact scheme/host/port match
           against the profile's declared app-under-test origins.
        6. **Ambiguous host encodings** (legacy ``inet_aton`` forms).
        7. **Address classification** for IP literals; **name
           classification** otherwise.
        8. Otherwise: allowed as an ordinary public target.

        ``hop_index`` is stamped onto the verdict for redirect-chain callers;
        it has no effect on the decision.
        """
        if self.error is not None:
            return self.error.to_verdict(url, hop_index)

        parsed = _parse_url(url)
        if isinstance(parsed, PolicyVerdict):
            return replace(parsed, hop_index=hop_index)

        scheme_verdict = self._check_scheme(url, parsed, hop_index)
        if scheme_verdict is not None:
            return scheme_verdict

        if not parsed.host:
            return _deny(url, "url-missing-host", "URL names no host", hop_index)

        return self._check_target(url, parsed, hop_index)

    def _check_scheme(
        self, url: str, parsed: _ParsedTarget, hop_index: int | None
    ) -> PolicyVerdict | None:
        if parsed.has_userinfo:
            return _deny(
                url,
                "url-deny-userinfo",
                "URL carries embedded credentials; credentials are supplied out of band",
                hop_index,
            )
        scheme = parsed.scheme
        if scheme in self.profile.denied_schemes:
            if scheme in _SPECIFIC_SCHEME_RULE_IDS:
                rule_id = _SPECIFIC_SCHEME_RULE_IDS[scheme]
            elif scheme in MANDATORY_DENIED_SCHEMES:
                rule_id = "scheme-deny-browser-internal"
            else:
                rule_id = "scheme-deny-profile"
            return _deny(url, rule_id, f"scheme {_sanitize(scheme, 40)!r} is denied", hop_index)
        if scheme not in self.profile.allowed_schemes:
            return _deny(
                url,
                "scheme-not-allowed",
                f"scheme {_sanitize(scheme, 40)!r} is not on the allowlist",
                hop_index,
            )
        return None

    def _check_target(
        self, url: str, parsed: _ParsedTarget, hop_index: int | None
    ) -> PolicyVerdict:
        host = parsed.host
        safe_host = _sanitize(host, 80)
        literal = _parse_ip_literal(host)

        # 4. Metadata floor -- never overridable, so it precedes the allow.
        if literal is not None:
            classified = _classify_address(literal)
            if classified is not None and classified[0] == "metadata":
                return _deny(
                    url,
                    "target-deny-metadata",
                    f"{safe_host} is a {classified[1]}, which is never reachable",
                    hop_index,
                )
        name_class = _classify_hostname(host) if literal is None else None
        if name_class is not None and name_class[0] == "metadata":
            return _deny(
                url,
                "target-deny-metadata",
                f"{safe_host} is a {name_class[1]}, which is never reachable",
                hop_index,
            )

        # 5. Declared app-under-test targets.
        for target in self.profile.declared_targets:
            if target.matches(parsed.scheme, host, parsed.port, self.profile.allowed_schemes):
                return PolicyVerdict(
                    decision=PolicyDecision.ALLOWED,
                    url=url,
                    matched_rule_ids=("scheme-allowed", "profile-allow-declared-target"),
                    reason=(
                        f"{safe_host} matches declared target "
                        f"{_sanitize(target.spec, 80)!r} in policy {self.profile.name!r}"
                    ),
                    hop_index=hop_index,
                )

        # 6-7. Classification.
        if literal is None:
            legacy = _decode_legacy_ipv4(host)
            if legacy is not None:
                return _deny(
                    url,
                    "target-deny-ambiguous-host",
                    (
                        f"{safe_host} is an ambiguous legacy address encoding "
                        f"(resolves to {legacy}); declare an unambiguous origin"
                    ),
                    hop_index,
                )
            if name_class is not None:
                suffix, label = name_class
                return _deny(url, f"target-deny-{suffix}", f"{safe_host} is a {label}", hop_index)
        else:
            classified = _classify_address(literal)
            if classified is not None:
                suffix, label = classified
                return _deny(url, f"target-deny-{suffix}", f"{safe_host} is a {label}", hop_index)

        # 8. Ordinary public target.
        return PolicyVerdict(
            decision=PolicyDecision.ALLOWED,
            url=url,
            matched_rule_ids=("scheme-allowed", "target-allow-public"),
            reason=f"{safe_host} is a public target on an allowed scheme",
            hop_index=hop_index,
        )

    def evaluate_redirect_chain(self, urls: Sequence[str]) -> list[PolicyVerdict]:
        """Evaluate a known chain hop by hop, stopping at the first refusal.

        ``urls[0]`` is the initially requested URL; each later entry is a
        redirect destination. Every entry is evaluated independently --- a
        chain whose first hop is a declared app under test does *not* inherit
        that allow for a destination the app advertises. The returned list is
        as long as the number of hops actually examined, so a caller checks
        the last verdict, not the length.

        A chain longer than ``profile.max_redirects`` hops is refused with
        ``redirect-depth-exceeded`` at the hop that crosses the limit.

        :raises ValueError: if ``urls`` is empty. A chain always has at least
            the URL the caller asked for.
        """
        if not urls:
            raise ValueError("a redirect chain must contain at least the initial URL")

        verdicts: list[PolicyVerdict] = []
        for index, url in enumerate(urls):
            if index > self.profile.max_redirects:
                verdicts.append(
                    _deny(
                        url,
                        "redirect-depth-exceeded",
                        (
                            f"redirect depth {index} exceeds the limit of "
                            f"{self.profile.max_redirects}"
                        ),
                        index,
                    )
                )
                return verdicts
            verdict = self.evaluate(url, hop_index=index)
            verdicts.append(verdict)
            if not verdict.allowed:
                return verdicts
        return verdicts

    def evaluate_resolved(
        self, url: str, ip: str | IPv4Address | IPv6Address, hop_index: int | None = None
    ) -> PolicyVerdict:
        """Re-check a URL against the address its hostname actually resolved to.

        DNS resolution is the adapters' job (M2); this is the policy half of
        it. A hostname that passed the name-layer check can still resolve to a
        private address --- classic DNS rebinding --- so an adapter calls this
        once it knows the address, before it connects.

        A declared app-under-test target survives resolving to a loopback or
        private address (that is the normal case for a local dev server), but
        never survives resolving to a cloud-metadata endpoint.
        """
        if self.error is not None:
            return self.error.to_verdict(url, hop_index)

        verdict = self.evaluate(url, hop_index=hop_index)
        if not verdict.allowed:
            return verdict

        address = ip if isinstance(ip, (IPv4Address, IPv6Address)) else _parse_ip_literal(str(ip))
        if address is None:
            return _deny(
                url,
                "resolved-address-unparseable",
                f"resolved address {_sanitize(str(ip), 60)!r} is not a valid IP address",
                hop_index,
            )

        classified = _classify_address(address)
        declared = "profile-allow-declared-target" in verdict.matched_rule_ids

        if classified is not None and classified[0] == "metadata":
            return _deny(
                url,
                "resolved-target-deny-metadata",
                f"resolves to {address}, a {classified[1]}, which is never reachable",
                hop_index,
            )
        if declared:
            return PolicyVerdict(
                decision=PolicyDecision.ALLOWED,
                url=url,
                matched_rule_ids=verdict.matched_rule_ids + ("resolved-target-allow-declared",),
                reason=f"{verdict.reason}; resolved address {address} is inside the declaration",
                hop_index=hop_index,
            )
        if classified is not None:
            suffix, label = classified
            return _deny(
                url,
                f"resolved-target-deny-{suffix}",
                f"resolves to {address}, a {label}",
                hop_index,
            )
        return PolicyVerdict(
            decision=PolicyDecision.ALLOWED,
            url=url,
            matched_rule_ids=verdict.matched_rule_ids + ("resolved-target-allow-public",),
            reason=f"{verdict.reason}; resolved address {address} is publicly routable",
            hop_index=hop_index,
        )


def build_evaluator(data: Mapping[str, Any] | None) -> WebPolicyEvaluator:
    """Build an evaluator from plain policy data without ever raising.

    ``None`` yields the absent-policy state (built-in deny-by-default
    profile); well-formed data yields an explicit-policy evaluator; malformed
    data yields a *blocked* evaluator whose every verdict is
    ``decision=error``. There is no input for which this returns something
    that allows more than the built-in default.
    """
    try:
        return WebPolicyEvaluator.from_policy_data(data)
    except PolicyError as exc:
        return WebPolicyEvaluator.blocked(exc)
