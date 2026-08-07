"""Injectable ``Clock`` and ID-provider seams (build plan task t6).

Neither the operation core (``operations.py``/``results.py``) nor any store
module reads the wall clock or generates its own ids directly — every place
that needs "now" or a fresh id takes one as an explicit parameter (see e.g.
``webglass.sessions``'s module docstring: "Nothing here calls
``datetime.now()`` or ``time.time()``"). These two tiny protocols are the
seam a caller plugs a real or fake source into: :class:`FixedClock` and
:class:`SequentialIds` give t9's service layer (and this module's own tests)
fully deterministic operation ids and timestamps without any store or
operation model needing to know a fake is in play.

Both protocols are synchronous and ``@runtime_checkable`` — the CLI is a
one-shot process, so there is no async boundary to model here; a real
async-driven backend (if one ever exists) hides that internally.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

__all__ = ["Clock", "FixedClock", "IdProvider", "SequentialIds"]


@runtime_checkable
class Clock(Protocol):
    """A source of "now", injectable so tests never depend on wall-clock time."""

    def now(self) -> float:
        """Return the current time as epoch seconds."""
        ...  # pragma: no cover - protocol method


@dataclass
class FixedClock:
    """Deterministic :class:`Clock`: returns a fixed time, advanceable on demand.

    The default ``time=0.0`` is an arbitrary deterministic epoch, not "now" —
    callers that care about a realistic-looking timestamp pass one in.
    """

    time: float = 0.0

    def now(self) -> float:
        return self.time

    def advance(self, seconds: float) -> float:
        """Move the clock forward and return the new time."""
        self.time += seconds
        return self.time

    def set(self, time: float) -> None:
        """Jump the clock directly to ``time``."""
        self.time = time


@runtime_checkable
class IdProvider(Protocol):
    """A source of fresh ids, injectable for deterministic operation/session ids."""

    def new_id(self, kind: str) -> str:
        """Return a new id for ``kind`` (e.g. ``"operation"``, ``"session"``).

        ``kind`` namespaces the id space only — it is never validated against
        a closed set, so a caller can mint ids for a kind this module has
        never heard of.
        """
        ...  # pragma: no cover - protocol method


@dataclass
class SequentialIds:
    """Deterministic :class:`IdProvider`: ``"<kind>-1"``, ``"<kind>-2"``, ...

    Each ``kind`` is counted independently, so ``new_id("operation")`` and
    ``new_id("session")`` each start their own sequence at 1. This is what
    lets a test assert on an exact, reproducible id rather than merely
    asserting "an id was produced".
    """

    _counters: dict[str, int] = field(default_factory=dict)

    def new_id(self, kind: str) -> str:
        self._counters[kind] = self._counters.get(kind, 0) + 1
        return f"{kind}-{self._counters[kind]}"

    def peek(self, kind: str) -> int:
        """Return how many ids have been minted for ``kind`` so far, without minting one."""
        return self._counters.get(kind, 0)
