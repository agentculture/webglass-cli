"""Web-memory store — typed interface stub only. Implementation deferred to M3.

Per ``CLAUDE.md`` "Target architecture" section 2, web-memory is a
**searchable index** over prior explorations/snapshots/extractions/evidence.
It answers "have we seen this?" and "what changed?", never silently injects
old content into a new answer, and is explicitly **never a credential
store**.

**Nothing in this module is implemented.** As with ``exploration.py``, this
fixes the shape of the M3 ``MemoryStore`` contract (see the build plan,
milestone M3, and issue #8) so :class:`webglass.context.WebContext` can
carry ``memory_read_scope`` / ``memory_write_scope`` references today
without depending on any concrete storage or indexing code. Do not add
persistence, in-memory state, or business logic here — that is explicitly
out of scope for this task (t8) and belongs to the M3 implementation task
per issue #8. This module must not import ``sessions.py``, ``context.py``,
or ``exploration.py``.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class MemoryStore(Protocol):
    """M3 interface stub. See the module docstring — no implementation here.

    Method shapes mirror the spec's ``find`` / ``show`` / ``forget`` /
    ``compact`` vocabulary (issue #1 section 2 lens list); the concrete
    index technology and record schema are M3 planning decisions, not t8
    ones.
    """

    def find(self, query: str, *, scope: str) -> Any:
        """Search the index within ``scope``; return ranked matches."""

    def show(self, memory_id: str) -> Any:
        """Return one stored memory record by id."""

    def forget(self, memory_id: str) -> Any:
        """Remove one stored memory record by id."""

    def compact(self, *, scope: str, now: float) -> Any:
        """Compact/retire stale records within ``scope`` as of ``now``."""
