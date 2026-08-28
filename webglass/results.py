"""``WebOperationResult`` — the one result model shared by the Python API,
the CLI, and Colleague's tool adapter (issue #1 sections 1 and 14; CLAUDE.md
"Target architecture" section 1).

The library and CLI return the *same semantic result*; CLI text output is a
rendering of it, never a second contract. This module stays dependency-light
and does not import Playwright, Colleague, or sibling modules that do not
exist in this milestone's worktree (``policy.py``, ``pages.py``,
``sessions.py`` and friends) — where a slot's fully-typed shape belongs to
one of those, it is carried here as a small local dataclass or a
documented-shape ``dict``, with a note that t9 wires the concrete types in.

Trust zones (issue #1 section 7; CLAUDE.md "Target architecture" section 7)
are kept structurally separate via :class:`TrustZones`: every operation's
payload is placed into exactly one of ``trusted`` / ``untrusted`` /
``sensitive`` / ``derived`` buckets, on a *different field* from
WebGlass's own diagnostics (``policy_verdict``, ``warnings``, ``error``,
...). This is what stops untrusted page text from being rendered as if it
were a WebGlass warning, policy decision, or instruction — a hostile page
cannot spoof tool output by logging something that looks like a WebGlass
message, because that text can only ever land in ``content.untrusted``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from webglass.effects import OperationKind
from webglass.operations import CacheMode

__all__ = [
    "SCHEMA_VERSION",
    "LifecycleState",
    "TrustZones",
    "PolicyVerdict",
    "NavigationHop",
    "CacheFreshness",
    "Completeness",
    "Timings",
    "OperationError",
    "WebOperationResult",
]

# Per docs/schema-versioning.md: WebOperationResult is its own
# independently-evolving record kind and carries its own top-level
# ``schema_version: int``, starting at 1.
#
# History (docs/schema-versioning.md "History" section mirrors this):
#   v1 (M1) — initial shape.
SCHEMA_VERSION = 1


class LifecycleState(str, Enum):
    """The full set of terminal/non-terminal states a
    :class:`WebOperationResult` can report (issue #1 section 1)."""

    PREVIEWED = "previewed"
    DENIED = "denied"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class TrustZones:
    """The four trust zones every result's payload is split across
    (issue #1 section 7):

    - ``trusted`` — control metadata WebGlass itself generated (operation
      ids, policy verdicts, backend diagnostics, limits, WebGlass-authored
      warnings). Duplicating data already on typed ``WebOperationResult``
      fields here is not required; this bucket is for trusted payload
      content specific to the operation kind.
    - ``untrusted`` — source material as returned by an external provider or
      page: titles, page text, attributes, URLs, download names, search
      snippets, site messages, console/page-error text. Never rendered as a
      WebGlass diagnostic, warning, or instruction.
    - ``sensitive`` — caller data such as credentials, form values, upload
      contents, cookies. Empty on nearly every M0-M2 result; the bucket
      exists so a later milestone has nowhere else to put this data by
      accident (never emitted wholesale, never persisted by default).
    - ``derived`` — transformations WebGlass computed from source material:
      cleaned text, ranked blocks, diffs. Kept apart from ``untrusted`` so a
      derived transformation is never mistaken for a verbatim quote, and
      apart from ``trusted`` so it is never mistaken for WebGlass's own
      control-plane output.

    Each bucket is a plain ``dict`` with a documented-but-open shape at this
    milestone — the concrete per-operation-kind payload shapes are owned by
    sibling modules (``pages.py``, ``extraction.py``, ...) that t9 wires in.
    """

    trusted: dict[str, Any] = field(default_factory=dict)
    untrusted: dict[str, Any] = field(default_factory=dict)
    sensitive: dict[str, Any] = field(default_factory=dict)
    derived: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "trusted": dict(self.trusted),
            "untrusted": dict(self.untrusted),
            "sensitive": dict(self.sensitive),
            "derived": dict(self.derived),
        }


@dataclass(frozen=True)
class PolicyVerdict:
    """Policy verdict slot.

    The concrete verdict/decision shape (allow/deny reasoning, rule
    evaluation order, ...) is owned by ``policy.py``, a sibling module this
    milestone does not import. This is a small local shape — decision label
    plus matched rule ids — sufficient for M1 fakes and the CLI JSON
    contract; t9 wires in the concrete ``policy.py`` type.
    """

    decision: str | None = None
    matched_rule_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "matched_rule_ids": list(self.matched_rule_ids),
        }


@dataclass(frozen=True)
class NavigationHop:
    """One hop in a redirect/navigation chain.

    ``requested_url`` / ``response_url`` are untrusted-origin strings (the
    remote server chose them), but as WebGlass's own recorded observation of
    what happened, the chain as a whole is control-plane provenance, not
    free-form page text — see the module docstring's trust-zone rationale
    for why this stays a dedicated field rather than living in
    ``TrustZones.untrusted``.
    """

    requested_url: str
    response_url: str | None = None
    status: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested_url": self.requested_url,
            "response_url": self.response_url,
            "status": self.status,
        }


@dataclass(frozen=True)
class CacheFreshness:
    """Cache/freshness status — always reported, never a silent stale read
    (CLAUDE.md section 8)."""

    mode: CacheMode
    hit: bool = False
    age_seconds: float | None = None
    stale: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "hit": self.hit,
            "age_seconds": self.age_seconds,
            "stale": self.stale,
        }


@dataclass(frozen=True)
class Completeness:
    """Truncation and extraction-completeness declaration (CLAUDE.md
    section 6: "identify every omitted, collapsed, or truncated region")."""

    truncated: bool = False
    omitted_regions: tuple[str, ...] = ()
    extraction_complete: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "truncated": self.truncated,
            "omitted_regions": list(self.omitted_regions),
            "extraction_complete": self.extraction_complete,
        }


@dataclass(frozen=True)
class Timings:
    """Operation timings. ``started_at`` is an opaque ISO-8601 string at this
    milestone; injectable ``Clock`` providers for deterministic tests are a
    t6 adapter-layer concern, not modelled here."""

    started_at: str | None = None
    duration_seconds: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "duration_seconds": self.duration_seconds,
        }


@dataclass(frozen=True)
class OperationError:
    """Stable machine-readable error, present whenever
    ``lifecycle_state == LifecycleState.FAILED``.

    Deliberately mirrors the shape of the CLI's own
    ``webglass.cli._errors.CliError`` (code/message/remediation) without
    importing it — this module has no dependency on the CLI layer, but the
    two shapes stay easy to render through the same ``error:`` / ``hint:``
    text contract.
    """

    code: str
    message: str
    remediation: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "remediation": self.remediation,
        }


@dataclass(frozen=True)
class WebOperationResult:
    """Result of one :class:`webglass.operations.WebOperation`
    (issue #1 section 1).

    ``content`` is the structured-output slot, split across the four trust
    zones (see :class:`TrustZones`). Every other field here is WebGlass's own
    control-plane bookkeeping about the operation's execution — kept off
    ``content`` precisely so it can never be confused with page-sourced
    material.
    """

    operation_id: str
    kind: OperationKind
    lifecycle_state: LifecycleState
    schema_version: int = SCHEMA_VERSION
    content: TrustZones = field(default_factory=TrustZones)
    policy_verdict: PolicyVerdict = field(default_factory=PolicyVerdict)
    # Labels of browser/remote effects the operation is known to have caused
    # (e.g. "cookies-updated", "download-started", "navigation-occurred").
    # Free-form strings at M1 pending a closed taxonomy from a later
    # milestone (adapters own the concrete effect catalog). Not to be
    # confused with webglass.effects.EffectClass, which classifies the
    # *operation kind* itself, not an observed runtime effect.
    known_effects: tuple[str, ...] = ()
    # Public records of sessions the invocation's opportunistic session-store
    # sweep reaped as an unrequested side effect (issue #14 build plan
    # t10/t11) — never the raw ``SessionRecord``, always its
    # ``to_public_dict()`` rendering, so ``endpoint_ref`` stays out the same
    # way it stays out of ``session clean``'s ``reaped`` payload. This lives
    # here, off ``content``, for the same reason ``known_effects`` does: it is
    # WebGlass's own control-plane bookkeeping about what this invocation did
    # to the store, not operation-kind-specific payload. Always present, even
    # empty — an absent field would be ambiguous between "nothing was swept"
    # and "this build doesn't report sweeps", which is precisely the
    # observability gap issue #14 was filed out of.
    swept_sessions: tuple[dict[str, Any], ...] = ()
    evidence_refs: tuple[str, ...] = ()
    navigation_history: tuple[NavigationHop, ...] = ()
    cache: CacheFreshness | None = None
    completeness: Completeness = field(default_factory=Completeness)
    # WebGlass-authored diagnostic strings only — never raw page/source text
    # (see the module docstring; page-sourced text belongs in
    # content.untrusted, e.g. console/page-error evidence per issue #9).
    warnings: tuple[str, ...] = ()
    degraded_evidence: bool = False
    timings: Timings = field(default_factory=Timings)
    backend: str | None = None
    error: OperationError | None = None

    def __post_init__(self) -> None:
        if self.lifecycle_state == LifecycleState.FAILED and self.error is None:
            raise ValueError(
                "WebOperationResult with lifecycle_state=FAILED must carry an error "
                "(issue #1 section 1: 'a stable machine-readable error on failure')"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "operation_id": self.operation_id,
            "kind": self.kind.value if isinstance(self.kind, OperationKind) else str(self.kind),
            "lifecycle_state": self.lifecycle_state.value,
            "content": self.content.to_dict(),
            "policy_verdict": self.policy_verdict.to_dict(),
            "known_effects": list(self.known_effects),
            "swept_sessions": [dict(record) for record in self.swept_sessions],
            "evidence_refs": list(self.evidence_refs),
            "navigation_history": [hop.to_dict() for hop in self.navigation_history],
            "cache": self.cache.to_dict() if self.cache is not None else None,
            "completeness": self.completeness.to_dict(),
            "warnings": list(self.warnings),
            "degraded_evidence": self.degraded_evidence,
            "timings": self.timings.to_dict(),
            "backend": self.backend,
            "error": self.error.to_dict() if self.error is not None else None,
        }
