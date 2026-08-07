"""``ArtifactStore`` seam: content-addressed storage behind evidence/downloads.

CLAUDE.md "Target architecture" section 8 lists ``ArtifactStore`` alongside
``SearchProvider``/``FetchBackend``/``BrowserBackend``/``BrowserSessionStore``/
``WebPolicyEvaluator`` as one of the protocols that keep concrete storage
choices out of the public API. Section 8 also names the eventual real
implementation: "SQLite plus a content-addressed artifact store is the
stdlib-first baseline" — that persistent store is a later milestone's module
(a top-level ``webglass/artifacts.py``, alongside ``sessions.py``, per section
9's suggested package shape), not built here. This module fixes the
*protocol* shape that implementation must satisfy, plus
:class:`FakeArtifactStore`, an in-memory reference implementation for M1
tests (t9's operation service, and this task's own conformance tests).

Content addressing means the store's own identity for a blob *is* its hash:
:meth:`ArtifactStore.put` is idempotent — storing the same bytes twice
returns the same :class:`ArtifactRef` and does not duplicate storage. This is
what "no rollback for the web, but at least no duplicate evidence" looks like
in practice.

Trust zones: :class:`ArtifactRef` is trusted control metadata — WebGlass
computed the hash itself, and the ref never carries the artifact's content.
Whatever the artifact bytes represent (a downloaded file, a screenshot, an
uploaded blob) is a separate, opaque payload this module never inspects.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

__all__ = ["ArtifactRef", "ArtifactStore", "FakeArtifactStore"]


def _content_hash(content: bytes) -> str:
    """``sha256:<hex>`` — self-describing, matching ``webglass.pages.hash_text``'s
    format so a hash printed from either module is readable the same way."""
    return "sha256:" + hashlib.sha256(content).hexdigest()


@dataclass(frozen=True)
class ArtifactRef:
    """A content-addressed handle onto stored bytes. Trusted control metadata."""

    artifact_id: str
    content_hash: str
    size_bytes: int
    content_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "content_hash": self.content_hash,
            "size_bytes": self.size_bytes,
            "content_type": self.content_type,
        }


@runtime_checkable
class ArtifactStore(Protocol):
    """The contract any content-addressed artifact store must satisfy."""

    def put(self, content: bytes, *, content_type: str | None = None) -> ArtifactRef:
        """Store ``content`` and return its ref. Idempotent by content hash."""
        ...  # pragma: no cover - protocol method

    def get(self, ref: ArtifactRef | str) -> bytes:
        """Return the stored bytes for ``ref`` (an :class:`ArtifactRef` or its
        ``content_hash`` string).

        :raises KeyError: if nothing is stored under that hash.
        """
        ...  # pragma: no cover - protocol method

    def exists(self, ref: ArtifactRef | str) -> bool:
        """Whether ``ref`` names something currently stored."""
        ...  # pragma: no cover - protocol method


class FakeArtifactStore:
    """In-memory, process-local :class:`ArtifactStore` reference implementation.

    Not suitable for anything beyond a single test process — no persistence,
    no cross-invocation durability (that is the deferred real
    ``webglass/artifacts.py``'s job, per the module docstring). Suitable for
    driving the operation service against fake backends end to end.
    """

    def __init__(self) -> None:
        self._content: dict[str, bytes] = {}
        self._content_type: dict[str, str | None] = {}

    def put(self, content: bytes, *, content_type: str | None = None) -> ArtifactRef:
        digest = _content_hash(content)
        if digest not in self._content:
            self._content[digest] = content
            self._content_type[digest] = content_type
        return ArtifactRef(
            artifact_id=digest,
            content_hash=digest,
            size_bytes=len(self._content[digest]),
            content_type=self._content_type[digest],
        )

    def get(self, ref: ArtifactRef | str) -> bytes:
        key = ref.content_hash if isinstance(ref, ArtifactRef) else ref
        try:
            return self._content[key]
        except KeyError as exc:
            raise KeyError(f"unknown artifact: {key}") from exc

    def exists(self, ref: ArtifactRef | str) -> bool:
        key = ref.content_hash if isinstance(ref, ArtifactRef) else ref
        return key in self._content
