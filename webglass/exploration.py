"""Exploration store — typed interface stub only. Implementation deferred to M3.

Per ``CLAUDE.md`` "Target architecture" section 2, an exploration is the
**durable graph of why and how** an agent traversed the web: query -> result
-> page -> followed link -> evidence, with parent/child edges, the caller's
reason for each branch, and a status (useful / duplicate / dead-end /
blocked / deferred). It must be resumable **without** the original live
browser process — this is what makes it a separate state kind from a
browser session.

**Nothing in this module is implemented.** It exists only to fix the shape
of the M3 ``ExplorationStore`` contract (see the build plan, milestone M3,
and issue #8) so :class:`webglass.context.WebContext` can carry an
``exploration_id`` reference today without depending on any concrete
storage code. Do not add persistence, in-memory state, or business logic
here — that is explicitly out of scope for this task (t8) and belongs to
the M3 implementation task per issue #8. This module must not import
``sessions.py``, ``context.py``, or ``memory.py``.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ExplorationStore(Protocol):
    """M3 interface stub. See the module docstring — no implementation here.

    Method shapes mirror the spec vocabulary (query -> result -> page ->
    followed link -> evidence, with parent/child edges, a reason, and a
    status) without committing to a concrete edge/graph schema yet; that
    schema is an M3 planning decision, not a t8 one.
    """

    def record_edge(
        self,
        exploration_id: str,
        *,
        parent_ref: str | None,
        child_ref: str,
        reason: str,
        status: str,
        now: float,
    ) -> Any:
        """Record one parent -> child traversal edge with the caller's reason."""

    def get_graph(self, exploration_id: str) -> Any:
        """Return the full traversal graph for ``exploration_id``."""

    def resume(self, exploration_id: str) -> Any:
        """Resume an exploration without requiring a live browser process."""
