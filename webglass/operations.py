"""``WebOperation`` — the one operation model shared by the Python API, the
CLI, and Colleague's tool adapter (issue #1 sections 1 and 14; CLAUDE.md
"Target architecture" section 1).

WebGlass is architected around a single operation lifecycle, not around CLI
handlers or direct Playwright calls: a caller builds a ``WebOperation``, one
service executes it (t9, not present in this milestone slice), and the
result is a :class:`webglass.results.WebOperationResult`. CLI text output is
only a *rendering* of that same result — never a second contract.

This module is intentionally dependency-light and knows nothing about
Playwright, Colleague, or the concrete shapes owned by sibling modules
(``policy.py``, ``pages.py``, ``sessions.py`` and friends — none of which
exist in this milestone's worktree). Where a field's fully-typed shape
belongs to a sibling module, it is carried here as an opaque reference
string or a small local dataclass, with a docstring note that a later
integration task (t9) wires the concrete types in.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from webglass.effects import EffectClass, OperationKind, classify

__all__ = [
    "SCHEMA_VERSION",
    "CacheMode",
    "ApplyState",
    "OperationTarget",
    "ContentBudget",
    "ResourceLimits",
    "CallerContext",
    "WebOperation",
]

# Per docs/schema-versioning.md: each independently-evolving record kind
# carries its own top-level ``schema_version: int``, starting at 1. This is
# the WebOperation *envelope* shape's version — distinct from
# ``WebOperation.kind_version``, which versions a single operation kind's own
# argument/target shape (e.g. "search" v1 vs a future "search" v2) while the
# envelope stays unchanged.
#
# History (docs/schema-versioning.md "History" section mirrors this):
#   v1 (M1) — initial shape.
SCHEMA_VERSION = 1


class CacheMode(str, Enum):
    """Explicit cache-freshness mode for one operation (CLAUDE.md section 8).

    There is no silent stale read: every result reports which mode was in
    effect alongside age and freshness (see
    ``webglass.results.CacheFreshness``).
    """

    LIVE = "live"
    PREFER_CACHE = "prefer-cache"
    REFRESH = "refresh"
    CACHE_ONLY = "cache-only"
    NO_STORE = "no-store"


class ApplyState(str, Enum):
    """Preview/apply state for one operation (issue #1 section 3).

    Only meaningful for ``REMOTE_ACTION``-classified kinds, which preview by
    default and require an explicit ``APPLY`` plus a valid, unexpired
    ``ActionPlan`` id (the prepare -> commit -> verify protocol; a
    service-layer concern, t9). ``OBSERVE`` and ``LOCAL_STATE`` kinds execute
    directly when authorized and ignore this field.
    """

    PREVIEW = "preview"
    APPLY = "apply"


@dataclass(frozen=True)
class OperationTarget:
    """The union of target shapes issue #1 section 1 calls out: a URL, or a
    reference into WebGlass-owned state (page/element/evidence).

    The concrete reference *types* (stable snapshot-scoped refs like
    ``link:12``, ``field:3``, ``block:27``) are owned by ``pages.py`` /
    ``references.py`` / ``evidence.py`` — sibling modules this milestone does
    not import. Until t9 wires them in, a reference is carried as an opaque
    string in the matching field.
    """

    url: str | None = None
    page_ref: str | None = None
    element_ref: str | None = None
    evidence_ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "page_ref": self.page_ref,
            "element_ref": self.element_ref,
            "evidence_ref": self.evidence_ref,
        }


@dataclass(frozen=True)
class ContentBudget:
    """Content and token budgets for one operation (CLAUDE.md sections 1, 6, 14).

    ``max_tokens_estimate`` is a caller-declared budget the service layer
    enforces with a deterministic, labeled *estimate* heuristic — never a
    tokenizer dependency (issue #1 section 14, t9's job). ``None`` means "no
    explicit limit supplied"; the service layer applies its own defaults,
    this model does not guess one.
    """

    max_bytes: int | None = None
    max_blocks: int | None = None
    max_tokens_estimate: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_bytes": self.max_bytes,
            "max_blocks": self.max_blocks,
            "max_tokens_estimate": self.max_tokens_estimate,
        }


@dataclass(frozen=True)
class ResourceLimits:
    """Timeout and resource limits for one operation."""

    timeout_seconds: float | None = None
    max_redirects: int | None = None
    max_response_bytes: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "timeout_seconds": self.timeout_seconds,
            "max_redirects": self.max_redirects,
            "max_response_bytes": self.max_response_bytes,
        }


@dataclass(frozen=True)
class CallerContext:
    """Caller/task/workspace metadata plus the caller's stated intent, and
    references to the capability and policy profile in effect.

    ``capability_profile_ref`` / ``policy_profile_ref`` are opaque ids, not
    the profiles themselves — consistent with a ``WebContext`` holding
    "references and policy, not copies" (CLAUDE.md section 2). The concrete
    policy-profile shape belongs to ``policy.py``, a sibling module this task
    does not import.
    """

    caller_id: str | None = None
    task_id: str | None = None
    workspace_id: str | None = None
    intent: str | None = None
    capability_profile_ref: str | None = None
    policy_profile_ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "caller_id": self.caller_id,
            "task_id": self.task_id,
            "workspace_id": self.workspace_id,
            "intent": self.intent,
            "capability_profile_ref": self.capability_profile_ref,
            "policy_profile_ref": self.policy_profile_ref,
        }


@dataclass(frozen=True)
class WebOperation:
    """One normalized web operation (issue #1 section 1).

    ``effect_class`` is deliberately **not** a settable field: it is derived
    from ``kind`` via :func:`webglass.effects.classify` on every access, so a
    ``WebOperation`` can never carry a classification that disagrees with its
    own kind (the acceptance condition "every operation kind declares exactly
    one effect class").
    """

    operation_id: str
    kind: OperationKind
    normalized_args: Mapping[str, Any] = field(default_factory=dict)
    caller: CallerContext = field(default_factory=CallerContext)
    session_id: str | None = None
    exploration_id: str | None = None
    target: OperationTarget = field(default_factory=OperationTarget)
    cache_mode: CacheMode = CacheMode.LIVE
    content_budget: ContentBudget = field(default_factory=ContentBudget)
    limits: ResourceLimits = field(default_factory=ResourceLimits)
    apply_state: ApplyState = ApplyState.PREVIEW
    schema_version: int = SCHEMA_VERSION
    kind_version: int = 1

    @property
    def effect_class(self) -> EffectClass:
        """The effect class declared for this operation's kind.

        Computed, not stored — see the class docstring. Unknown/ambiguous
        kinds classify upward to ``EffectClass.REMOTE_ACTION`` per
        :func:`webglass.effects.classify`.
        """
        return classify(self.kind)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "operation_id": self.operation_id,
            "kind": _kind_value(self.kind),
            "kind_version": self.kind_version,
            "normalized_args": dict(self.normalized_args),
            "caller": self.caller.to_dict(),
            "session_id": self.session_id,
            "exploration_id": self.exploration_id,
            "target": self.target.to_dict(),
            "cache_mode": self.cache_mode.value,
            "content_budget": self.content_budget.to_dict(),
            "limits": self.limits.to_dict(),
            "effect_class": self.effect_class.value,
            "apply_state": self.apply_state.value,
        }


def _kind_value(kind: OperationKind | str) -> str:
    return kind.value if isinstance(kind, OperationKind) else str(kind)
