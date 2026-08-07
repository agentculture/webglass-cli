"""WebContext — connects the four separate state kinds for one task.

Per ``CLAUDE.md`` "Target architecture" section 2, a :class:`WebContext`
holds *references and policy for one task*, not copies of state:
caller/task/workspace scope, an effective policy profile reference, budgets,
an optional live-session reference, an exploration reference, an evidence
namespace, and memory read/write scope. It is what lets Colleague hand a
child a reduced capability set (e.g. read the shared evidence graph, a
fresh anonymous session, no remote-action capability) without either side
needing to know the other's internals.

Two constraints make this module deliberately thin:

- **No store imports.** ``WebContext`` must not import ``sessions.py``,
  ``exploration.py``, or ``memory.py`` — it never holds a store object, only
  an opaque id/reference a caller resolves against the appropriate store
  elsewhere. This is checked structurally in ``tests/test_context.py`` (an
  AST scan of this module's imports, and a scan of every field's type
  annotation for a leaked store/record type name).
- **No policy import.** ``policy_profile_ref`` is typed as ``Any`` — this
  module must not import ``policy.py`` (a sibling task's module under
  concurrent development); WebGlass's policy core evaluates the referenced
  profile elsewhere, this object just carries the reference.

Constructing a ``WebContext`` is therefore always a pure, side-effect-free
operation: it never touches a database, a file, or a live session — the
dataclass constructor sets fields, nothing more.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class WebContext:
    """References and policy for one task — never copies of live state.

    Required fields come first (no default), optional/derivable ones
    (budgets, the live-session reference, memory scopes) follow with
    ``None`` defaults so a minimal context can be constructed with just
    caller/task/workspace/policy/evidence-namespace.
    """

    caller: str
    task: str
    workspace: str
    policy_profile_ref: Any
    evidence_namespace: str
    request_budget: int | None = None
    byte_budget: int | None = None
    time_budget_seconds: float | None = None
    token_budget: int | None = None
    session_id: str | None = None
    exploration_id: str | None = None
    memory_read_scope: str | None = None
    memory_write_scope: str | None = None

    def with_reduced(self, **overrides: Any) -> "WebContext":
        """Derive a child :class:`WebContext` with the given fields overridden.

        Named for its expected direction of use — dropping or narrowing
        references for a child task, e.g. ``parent.with_reduced(session_id=None,
        evidence_namespace="child-ns")``. ``WebContext`` holds no policy logic
        of its own (see the module docstring), so this method cannot verify
        an override is actually a *narrowing* — it is a plain field-override
        helper built on :func:`dataclasses.replace`. Callers (Colleague) are
        responsible for only ever passing values that reduce capability, not
        widen it.
        """
        return dataclasses.replace(self, **overrides)
