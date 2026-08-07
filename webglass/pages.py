"""PageSnapshot — the agent-facing unit of page state, plus its lenses.

Issue #1 section 5: agents never consume raw HTML, a full DOM, or a browser
handle. They consume a :class:`PageSnapshot`: page identity, ordered readable
blocks, an outline, the interactive surface (links, forms, fields, buttons),
content hashes, declared omissions, and WebGlass-generated security warnings —
all addressed by stable, snapshot-scoped references
(:mod:`webglass.references`).

**Lenses project one snapshot; they never re-fetch and never renumber.**

.. code-block:: text

    page_card()  -> identity, status, outline, warnings, change state (small)
    outline()    -> the heading tree, each node carrying its block ref
    read()       -> ordered blocks with a resume cursor and a declared budget

Because every lens projects the same ``blocks`` tuple, a block id seen in the
outline is the same id seen in ``read`` output and the same id an evidence
citation will later carry — that continuity is the whole point (section 5:
"an agent moves from compact outline to exact source text without re-fetching
or losing provenance").

Trust zones (issue #1 section 7) are a documented property of every field:

- *trusted control metadata* — refs, ``snapshot_id``, ``generation``,
  ``status``, hashes, :class:`OmittedRegion`, :class:`SecurityWarning` codes
  and messages, budget/usage numbers. WebGlass authored all of it.
- *untrusted source material* — ``title``, block ``text``, ``Link.href`` and
  ``Link.text``, field names/labels, button text, ``SecurityWarning.subject``,
  ``OmittedRegion.sample``. The page authored it; a renderer must never let it
  masquerade as a WebGlass warning or instruction.
- *derived transformations* — normalized text, hashes, the outline tree, the
  read projection.

This module holds no clock and no randomness: ``retrieved_at`` is injected by
the caller, and ids are supplied by whoever built the snapshot. Producing a
snapshot from HTML lives one layer up, in :mod:`webglass.extraction`.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import StrEnum

from webglass.references import (
    ReferenceSyntaxError,
    RefKind,
    SnapshotRef,
    UnknownReferenceError,
    parse_qualified_ref,
)

__all__ = [
    "PAGE_SNAPSHOT_SCHEMA_VERSION",
    "TOKEN_ESTIMATE_METHOD",
    "Block",
    "BlockKind",
    "BudgetUsage",
    "Button",
    "Field",
    "Form",
    "Link",
    "OmissionKind",
    "OmittedRegion",
    "OutlineNode",
    "PageCard",
    "PageSnapshot",
    "ReadBudget",
    "ReadResult",
    "SecurityWarning",
    "estimate_tokens",
    "hash_text",
    "normalize_text",
    "outline",
    "page_card",
    "read",
]

#: Independently-versioned record kind, per ``docs/schema-versioning.md``:
#: one field, one integer, starting at 1, bumped only on a breaking shape
#: change, and carried *in* the record so a snapshot handed off in isolation
#: still says how to read it.
PAGE_SNAPSHOT_SCHEMA_VERSION = 1

#: How :func:`estimate_tokens` guesses. Reported next to every token figure so
#: no caller mistakes the estimate for a tokenizer's answer (spec h31: "token
#: figures are labeled as estimates"; no tokenizer dependency is taken).
# nosec B105 - bandit flags any constant whose name contains "token" as a
# possible hardcoded credential; this is an LLM-token estimator label, not a
# secret. Suppressed locally rather than repo-wide so a real one still trips.
TOKEN_ESTIMATE_METHOD = "chars-div-4-heuristic"  # nosec B105

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    """Collapse all whitespace runs to single spaces and strip the ends."""
    return _WHITESPACE_RE.sub(" ", text).strip()


def hash_text(text: str) -> str:
    """Return ``sha256:<hex>`` for ``text`` — self-describing, so a stored hash
    stays interpretable if the algorithm ever changes."""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def estimate_tokens(text: str) -> int:
    """A deterministic, dependency-free token *estimate* (see
    :data:`TOKEN_ESTIMATE_METHOD`). Never presented as an exact count."""
    return len(text) // 4


class BlockKind(StrEnum):
    """What a readable block is, structurally."""

    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST = "list"
    TABLE = "table"
    CODE = "code"


class OmissionKind(StrEnum):
    """Why content is absent from a snapshot. Every absence gets one of these —
    nothing is ever dropped silently (issue #1 section 6)."""

    BOILERPLATE = "boilerplate"
    DUPLICATE = "duplicate"
    BUDGET = "budget"
    NON_CONTENT = "non-content"


def _require_kind(ref: SnapshotRef, kind: RefKind, label: str) -> SnapshotRef:
    if ref.kind is not kind:
        raise ReferenceSyntaxError(
            f"{label} requires a {kind.value} reference, got {ref.short!r}",
            remediation=f"pass a '{kind.value}:<index>' reference",
        )
    return ref


@dataclass(frozen=True, slots=True)
class Block:
    """One readable block of page content, in source order.

    ``text`` is *untrusted source material*. ``ref``, ``source_order``, and
    the derived hash/estimate are trusted control metadata.
    """

    ref: SnapshotRef
    kind: BlockKind
    text: str
    source_order: int
    heading_level: int | None = None

    def __post_init__(self) -> None:
        _require_kind(self.ref, RefKind.BLOCK, "Block")
        object.__setattr__(self, "kind", BlockKind(self.kind))

    @property
    def normalized_text(self) -> str:
        return normalize_text(self.text)

    @property
    def content_hash(self) -> str:
        return hash_text(self.normalized_text)

    @property
    def estimated_tokens(self) -> int:
        return estimate_tokens(self.text)

    def to_dict(self) -> dict[str, object]:
        return {
            "ref": self.ref.short,
            "kind": self.kind.value,
            "heading_level": self.heading_level,
            "source_order": self.source_order,
            "text": self.text,
            "content_hash": self.content_hash,
            "estimated_tokens": self.estimated_tokens,
        }


@dataclass(frozen=True, slots=True)
class Link:
    """A navigable link. ``href`` and ``text`` are untrusted source material."""

    ref: SnapshotRef
    href: str
    text: str = ""

    def __post_init__(self) -> None:
        _require_kind(self.ref, RefKind.LINK, "Link")

    def to_dict(self) -> dict[str, object]:
        return {"ref": self.ref.short, "href": self.href, "text": self.text}


@dataclass(frozen=True, slots=True)
class Field:
    """An input/select/textarea. Names and labels are untrusted source material."""

    ref: SnapshotRef
    name: str = ""
    field_type: str = ""
    label: str = ""
    required: bool = False
    form_ref: SnapshotRef | None = None

    def __post_init__(self) -> None:
        _require_kind(self.ref, RefKind.FIELD, "Field")

    def to_dict(self) -> dict[str, object]:
        return {
            "ref": self.ref.short,
            "name": self.name,
            "field_type": self.field_type,
            "label": self.label,
            "required": self.required,
            "form_ref": self.form_ref.short if self.form_ref else None,
        }


@dataclass(frozen=True, slots=True)
class Button:
    """A button or submit control. ``text`` is untrusted source material."""

    ref: SnapshotRef
    text: str = ""
    button_type: str = ""
    form_ref: SnapshotRef | None = None

    def __post_init__(self) -> None:
        _require_kind(self.ref, RefKind.BUTTON, "Button")

    def to_dict(self) -> dict[str, object]:
        return {
            "ref": self.ref.short,
            "text": self.text,
            "button_type": self.button_type,
            "form_ref": self.form_ref.short if self.form_ref else None,
        }


@dataclass(frozen=True, slots=True)
class Form:
    """A form and the refs of the controls it owns."""

    ref: SnapshotRef
    name: str = ""
    action: str = ""
    method: str = "get"
    field_refs: tuple[SnapshotRef, ...] = ()
    button_refs: tuple[SnapshotRef, ...] = ()

    def __post_init__(self) -> None:
        _require_kind(self.ref, RefKind.FORM, "Form")

    def to_dict(self) -> dict[str, object]:
        return {
            "ref": self.ref.short,
            "name": self.name,
            "action": self.action,
            "method": self.method,
            "field_refs": [ref.short for ref in self.field_refs],
            "button_refs": [ref.short for ref in self.button_refs],
        }


@dataclass(frozen=True, slots=True)
class OutlineNode:
    """One heading in the outline tree, carrying the *block* ref it came from."""

    ref: SnapshotRef
    level: int
    text: str
    children: tuple["OutlineNode", ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "ref": self.ref.short,
            "level": self.level,
            "text": self.text,
            "children": [child.to_dict() for child in self.children],
        }


@dataclass(frozen=True, slots=True)
class OmittedRegion:
    """A declared absence: what was removed, why, how much, and how to get it.

    ``reason`` is WebGlass-authored (trusted). ``sample`` quotes the omitted
    page text (untrusted) so an agent can judge the collapse without having to
    re-fetch — it is evidence of the omission, not a WebGlass statement.
    """

    kind: OmissionKind
    reason: str
    count: int
    refs: tuple[SnapshotRef, ...] = ()
    sample: str = ""
    bytes_omitted: int = 0
    estimated_tokens_omitted: int = 0
    resume_cursor: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", OmissionKind(self.kind))

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "reason": self.reason,
            "count": self.count,
            "refs": [ref.short for ref in self.refs],
            "sample": self.sample,
            "bytes_omitted": self.bytes_omitted,
            "estimated_tokens_omitted": self.estimated_tokens_omitted,
            "estimate_method": TOKEN_ESTIMATE_METHOD,
            "resume_cursor": self.resume_cursor,
        }


@dataclass(frozen=True, slots=True)
class SecurityWarning:
    """A WebGlass-generated warning about the page.

    ``code`` and ``message`` are trusted control metadata — WebGlass wrote
    them. Any page-supplied text that motivated the warning goes in
    ``subject`` (untrusted) and never inside ``message``, so remote text can
    never impersonate a WebGlass warning (issue #1 section 7).
    """

    code: str
    message: str
    ref: SnapshotRef | None = None
    subject: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": self.message,
            "ref": self.ref.short if self.ref else None,
            "subject": self.subject,
        }


@dataclass(frozen=True, slots=True)
class ReadBudget:
    """An explicit, declared budget for one ``read`` call. ``None`` means
    unbounded on that dimension — stated, not assumed."""

    max_blocks: int | None = None
    max_chars: int | None = None
    max_estimated_tokens: int | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "max_blocks": self.max_blocks,
            "max_chars": self.max_chars,
            "max_estimated_tokens": self.max_estimated_tokens,
            "estimate_method": TOKEN_ESTIMATE_METHOD,
        }


@dataclass(frozen=True, slots=True)
class BudgetUsage:
    """What a ``read`` call actually consumed, with the estimator labeled."""

    blocks: int = 0
    chars: int = 0
    estimated_tokens: int = 0
    estimate_method: str = TOKEN_ESTIMATE_METHOD

    def to_dict(self) -> dict[str, object]:
        return {
            "blocks": self.blocks,
            "chars": self.chars,
            "estimated_tokens": self.estimated_tokens,
            "estimate_method": self.estimate_method,
        }


@dataclass(frozen=True, slots=True)
class ReadResult:
    """The ``read`` lens output: ordered blocks plus everything needed to
    continue and to know what was left out."""

    snapshot_id: str
    generation: int
    blocks: tuple[Block, ...]
    cursor: SnapshotRef | None
    done: bool
    budget: ReadBudget
    usage: BudgetUsage
    omissions: tuple[OmittedRegion, ...] = ()
    single_block_overrun: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "snapshot_id": self.snapshot_id,
            "generation": self.generation,
            "blocks": [block.to_dict() for block in self.blocks],
            # Qualified: a cursor outlives this payload (it is handed back on a
            # later invocation), so it must carry its own scope.
            "cursor": self.cursor.qualified if self.cursor else None,
            "done": self.done,
            "budget": self.budget.to_dict(),
            "usage": self.usage.to_dict(),
            "omissions": [omission.to_dict() for omission in self.omissions],
            "single_block_overrun": self.single_block_overrun,
        }


@dataclass(frozen=True, slots=True)
class PageCard:
    """The small ``open`` lens: who the page is, not what it says."""

    snapshot_id: str
    generation: int
    requested_url: str
    final_url: str
    canonical_url: str | None
    title: str
    language: str | None
    content_type: str | None
    status: int | None
    retrieved_at: str
    redirect_chain: tuple[str, ...]
    outline: tuple[OutlineNode, ...]
    block_count: int
    link_count: int
    form_count: int
    content_hash: str
    previous_content_hash: str | None
    changed: bool | None
    truncated: bool
    omissions: dict[str, int]
    security_warnings: tuple[SecurityWarning, ...]
    schema_version: int = PAGE_SNAPSHOT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "generation": self.generation,
            "requested_url": self.requested_url,
            "final_url": self.final_url,
            "canonical_url": self.canonical_url,
            "title": self.title,
            "language": self.language,
            "content_type": self.content_type,
            "status": self.status,
            "retrieved_at": self.retrieved_at,
            "redirect_chain": list(self.redirect_chain),
            "outline": [node.to_dict() for node in self.outline],
            "block_count": self.block_count,
            "link_count": self.link_count,
            "form_count": self.form_count,
            "content_hash": self.content_hash,
            "previous_content_hash": self.previous_content_hash,
            "changed": self.changed,
            "truncated": self.truncated,
            "omissions": dict(self.omissions),
            "security_warnings": [w.to_dict() for w in self.security_warnings],
        }


@dataclass(frozen=True, slots=True)
class PageSnapshot:
    """One observation of one page, addressed by stable references.

    Constructing a snapshot validates that every embedded reference belongs to
    *this* snapshot generation: a snapshot can never silently carry a handle
    onto a different page.
    """

    snapshot_id: str
    requested_url: str
    final_url: str
    retrieved_at: str
    generation: int = 0
    canonical_url: str | None = None
    title: str = ""
    language: str | None = None
    content_type: str | None = None
    status: int | None = None
    redirect_chain: tuple[str, ...] = ()
    blocks: tuple[Block, ...] = ()
    links: tuple[Link, ...] = ()
    forms: tuple[Form, ...] = ()
    fields: tuple[Field, ...] = ()
    buttons: tuple[Button, ...] = ()
    omissions: tuple[OmittedRegion, ...] = ()
    security_warnings: tuple[SecurityWarning, ...] = field(default_factory=tuple)
    previous_content_hash: str | None = None
    schema_version: int = PAGE_SNAPSHOT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in (
            "redirect_chain",
            "blocks",
            "links",
            "forms",
            "fields",
            "buttons",
            "omissions",
            "security_warnings",
        ):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        for ref in self._embedded_refs():
            ref.require_scope(self.snapshot_id, self.generation)

    def _embedded_refs(self) -> list[SnapshotRef]:
        refs: list[SnapshotRef] = []
        for item in (*self.blocks, *self.links, *self.fields, *self.buttons, *self.forms):
            refs.append(item.ref)
        for form in self.forms:
            refs.extend(form.field_refs)
            refs.extend(form.button_refs)
        for omission in self.omissions:
            refs.extend(omission.refs)
        for warning in self.security_warnings:
            if warning.ref is not None:
                refs.append(warning.ref)
        return refs

    # --- derived properties -------------------------------------------------

    @property
    def content_hash(self) -> str:
        """sha256 over the normalized text of the retained blocks, in order."""
        return hash_text("\n".join(block.normalized_text for block in self.blocks))

    @property
    def truncated(self) -> bool:
        return any(omission.kind is OmissionKind.BUDGET for omission in self.omissions)

    @property
    def changed(self) -> bool | None:
        """``None`` when there is nothing to compare against — never a guess."""
        if self.previous_content_hash is None:
            return None
        return self.content_hash != self.previous_content_hash

    # --- reference resolution ----------------------------------------------

    def _lookup(
        self,
        ref: SnapshotRef,
        kind: RefKind,
        items: tuple[object, ...],
    ) -> object:
        ref.require_scope(self.snapshot_id, self.generation)
        if ref.kind is not kind:
            raise UnknownReferenceError(
                f"expected a {kind.value} reference, got {ref.short!r}",
                remediation=f"pass a '{kind.value}:<index>' reference from this snapshot",
            )
        for item in items:
            if getattr(item, "ref") == ref:
                return item
        for omission in self.omissions:
            if ref in omission.refs:
                raise UnknownReferenceError(
                    f"{ref.short} was omitted from this snapshot "
                    f"({omission.kind.value}: {omission.reason})",
                    remediation=(
                        "the omission record lists what was collapsed; re-run the "
                        "operation with a larger budget or without collapsing to "
                        "retain it"
                    ),
                )
        raise UnknownReferenceError(
            f"{ref.short} names nothing in snapshot {self.snapshot_id}@{self.generation}",
            remediation="list the snapshot's references again (open/inspect) and retry",
        )

    def block(self, ref: SnapshotRef) -> Block:
        return self._lookup(ref, RefKind.BLOCK, self.blocks)  # type: ignore[return-value]

    def link(self, ref: SnapshotRef) -> Link:
        return self._lookup(ref, RefKind.LINK, self.links)  # type: ignore[return-value]

    def form(self, ref: SnapshotRef) -> Form:
        return self._lookup(ref, RefKind.FORM, self.forms)  # type: ignore[return-value]

    def field(self, ref: SnapshotRef) -> Field:
        return self._lookup(ref, RefKind.FIELD, self.fields)  # type: ignore[return-value]

    def button(self, ref: SnapshotRef) -> Button:
        return self._lookup(ref, RefKind.BUTTON, self.buttons)  # type: ignore[return-value]

    def resolve(self, ref: SnapshotRef) -> object:
        """Resolve any reference kind against this snapshot."""
        resolvers = {
            RefKind.BLOCK: self.block,
            RefKind.LINK: self.link,
            RefKind.FORM: self.form,
            RefKind.FIELD: self.field,
            RefKind.BUTTON: self.button,
        }
        return resolvers[ref.kind](ref)

    # --- lenses -------------------------------------------------------------

    def outline(self) -> tuple[OutlineNode, ...]:
        """Project the heading blocks into a tree, preserving their block refs.

        The outline is *derived* from ``blocks`` rather than stored beside
        them, so an outline node can never drift from the block it names.
        """
        roots: list[dict[str, object]] = []
        stack: list[dict[str, object]] = []
        for block in self.blocks:
            if block.kind is not BlockKind.HEADING:
                continue
            level = block.heading_level or 1
            node: dict[str, object] = {
                "ref": block.ref,
                "level": level,
                "text": block.text,
                "children": [],
            }
            while stack and int(stack[-1]["level"]) >= level:  # type: ignore[call-overload]
                stack.pop()
            if stack:
                stack[-1]["children"].append(node)  # type: ignore[union-attr]
            else:
                roots.append(node)
            stack.append(node)
        return tuple(_freeze_outline(node) for node in roots)

    def page_card(self) -> PageCard:
        """Project the small identity/status/outline card (the ``open`` lens)."""
        omissions: dict[str, int] = {}
        for omission in self.omissions:
            omissions[omission.kind.value] = omissions.get(omission.kind.value, 0) + omission.count
        return PageCard(
            snapshot_id=self.snapshot_id,
            generation=self.generation,
            requested_url=self.requested_url,
            final_url=self.final_url,
            canonical_url=self.canonical_url,
            title=self.title,
            language=self.language,
            content_type=self.content_type,
            status=self.status,
            retrieved_at=self.retrieved_at,
            redirect_chain=self.redirect_chain,
            outline=self.outline(),
            block_count=len(self.blocks),
            link_count=len(self.links),
            form_count=len(self.forms),
            content_hash=self.content_hash,
            previous_content_hash=self.previous_content_hash,
            changed=self.changed,
            truncated=self.truncated,
            omissions=omissions,
            security_warnings=self.security_warnings,
            schema_version=self.schema_version,
        )

    def read(
        self,
        *,
        cursor: SnapshotRef | str | None = None,
        budget: ReadBudget | None = None,
    ) -> ReadResult:
        """Project ordered readable blocks under a declared budget.

        ``cursor`` resumes a previous read: pass back
        :attr:`ReadResult.cursor` (or its qualified string). A cursor from a
        different snapshot generation raises
        :class:`~webglass.references.StaleReferenceError` — it is never
        re-interpreted against whatever now sits at that index.

        A budget that cannot fit even one block still returns one block (so a
        reader always makes progress) and says so via
        :attr:`ReadResult.single_block_overrun`.
        """
        budget = budget or ReadBudget()
        start = self._cursor_index(cursor)
        selected: list[Block] = []
        chars = 0
        tokens = 0
        overrun = False
        for block in self.blocks[start:]:
            if budget.max_blocks is not None and len(selected) + 1 > budget.max_blocks:
                break
            block_chars = len(block.text)
            block_tokens = block.estimated_tokens
            over_chars = budget.max_chars is not None and chars + block_chars > budget.max_chars
            over_tokens = (
                budget.max_estimated_tokens is not None
                and tokens + block_tokens > budget.max_estimated_tokens
            )
            if over_chars or over_tokens:
                if selected:
                    break
                overrun = True
            selected.append(block)
            chars += block_chars
            tokens += block_tokens
        remaining = self.blocks[start + len(selected) :]
        omissions: tuple[OmittedRegion, ...] = ()
        if remaining:
            omissions = (
                OmittedRegion(
                    kind=OmissionKind.BUDGET,
                    reason="read budget reached; the remaining blocks were not returned",
                    count=len(remaining),
                    refs=tuple(block.ref for block in remaining),
                    bytes_omitted=sum(len(block.text.encode("utf-8")) for block in remaining),
                    estimated_tokens_omitted=sum(block.estimated_tokens for block in remaining),
                    resume_cursor=remaining[0].ref.qualified,
                ),
            )
        return ReadResult(
            snapshot_id=self.snapshot_id,
            generation=self.generation,
            blocks=tuple(selected),
            cursor=remaining[0].ref if remaining else None,
            done=not remaining,
            budget=budget,
            usage=BudgetUsage(blocks=len(selected), chars=chars, estimated_tokens=tokens),
            omissions=omissions,
            single_block_overrun=overrun,
        )

    def _cursor_index(self, cursor: SnapshotRef | str | None) -> int:
        if cursor is None:
            return 0
        ref = parse_qualified_ref(cursor) if isinstance(cursor, str) else cursor
        ref.require_scope(self.snapshot_id, self.generation)
        for index, block in enumerate(self.blocks):
            if block.ref == ref:
                return index
        raise UnknownReferenceError(
            f"read cursor {ref.short} names no retained block in snapshot "
            f"{self.snapshot_id}@{self.generation}",
            remediation="re-read from the start, or use a cursor returned by this snapshot",
        )

    # --- serialization ------------------------------------------------------

    def to_dict(self) -> dict[str, object]:
        """A JSON-safe projection. Nested refs render in the short form; the
        snapshot scope is carried once, at the top level."""
        return {
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "generation": self.generation,
            "requested_url": self.requested_url,
            "final_url": self.final_url,
            "canonical_url": self.canonical_url,
            "title": self.title,
            "language": self.language,
            "content_type": self.content_type,
            "status": self.status,
            "retrieved_at": self.retrieved_at,
            "redirect_chain": list(self.redirect_chain),
            "content_hash": self.content_hash,
            "previous_content_hash": self.previous_content_hash,
            "changed": self.changed,
            "truncated": self.truncated,
            "blocks": [block.to_dict() for block in self.blocks],
            "outline": [node.to_dict() for node in self.outline()],
            "links": [link.to_dict() for link in self.links],
            "forms": [form.to_dict() for form in self.forms],
            "fields": [field_.to_dict() for field_ in self.fields],
            "buttons": [button.to_dict() for button in self.buttons],
            "omissions": [omission.to_dict() for omission in self.omissions],
            "security_warnings": [warning.to_dict() for warning in self.security_warnings],
        }


def _freeze_outline(node: dict[str, object]) -> OutlineNode:
    children = node["children"]
    assert isinstance(children, list)
    return OutlineNode(
        ref=node["ref"],  # type: ignore[arg-type]
        level=int(node["level"]),  # type: ignore[call-overload]
        text=str(node["text"]),
        children=tuple(_freeze_outline(child) for child in children),
    )


# --- module-level lens aliases ---------------------------------------------
#
# The lenses are methods (they project one snapshot), but callers that read
# more naturally as ``page_card(snapshot)`` — a CLI handler, say — get the
# same projection through these thin aliases.


def page_card(snapshot: PageSnapshot) -> PageCard:
    """Module-level alias for :meth:`PageSnapshot.page_card`."""
    return snapshot.page_card()


def outline(snapshot: PageSnapshot) -> tuple[OutlineNode, ...]:
    """Module-level alias for :meth:`PageSnapshot.outline`."""
    return snapshot.outline()


def read(
    snapshot: PageSnapshot,
    *,
    cursor: SnapshotRef | str | None = None,
    budget: ReadBudget | None = None,
) -> ReadResult:
    """Module-level alias for :meth:`PageSnapshot.read`."""
    return snapshot.read(cursor=cursor, budget=budget)
