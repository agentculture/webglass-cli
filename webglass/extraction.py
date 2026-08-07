"""Deterministic HTML -> :class:`~webglass.pages.PageSnapshot` extraction.

Issue #1 section 6 spells out what "token efficiency" is allowed to mean:
strip repeated navigation and boilerplate, preserve headings and source order,
segment into stable blocks, dedup by hash, apply explicit byte/token budgets
with deterministic cursors — and **identify every omitted, collapsed, or
truncated region**. What it is *not* allowed to mean is equally explicit:

    WebGlass must not silently summarize with a model and present it as page
    content.

So this module is a plain, auditable HTML reader built on :mod:`html.parser`
from the standard library (no lxml, no bs4 — this package's empty runtime
dependency list is a property worth defending, per CLAUDE.md section 8). It
holds no clock, no randomness, and no network: given the same bytes and the
same injected identity/timestamp, it returns a byte-identical snapshot.

What gets collapsed, and how it is declared
-------------------------------------------

============  ==========================================================
kind          rule
============  ==========================================================
non-content   ``<script>`` / ``<style>`` / ``<noscript>`` / ``<template>``
              are never readable content. Counted and declared.
boilerplate   identical normalized text appearing 2+ times where *every*
              occurrence sits inside page chrome (``nav`` / ``aside`` /
              ``header`` / ``footer``): all occurrences are dropped.
duplicate     identical normalized text appearing 2+ times elsewhere: the
              first occurrence is kept, the rest are dropped.
budget        blocks beyond the caller's byte/token/block budget, with a
              resume cursor.
============  ==========================================================

Headings are exempt from collapsing: the outline depends on them, and two
sections legitimately named "Notes" are structure, not chrome.

Block *ids* are assigned before collapsing and never renumbered — the gap in
``block:0, block:3`` is itself part of the disclosure, and the omission record
names the missing refs so :meth:`PageSnapshot.resolve` can explain them.

Selector-scoped extraction (:func:`extract_selector`) is a separate, narrower
pass: it returns exactly one element's content — the app-under-test case from
issue #9, where a page exposes machine-readable state in a
``<script type="application/json" id="agent-state">`` node that the readable
extractor deliberately skips.

Trust zones: every string this module lifts out of the document (block text,
titles, hrefs, labels, selector matches, omission samples) is *untrusted
source material*. The only trusted strings it produces are the ones it writes
itself: omission reasons and :class:`~webglass.pages.SecurityWarning` codes
and messages, which never interpolate page text (the offending value travels
in ``subject``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from html.parser import HTMLParser

from webglass.pages import (
    Block,
    BlockKind,
    Button,
    Field,
    Form,
    Link,
    OmissionKind,
    OmittedRegion,
    PageSnapshot,
    SecurityWarning,
    normalize_text,
)
from webglass.references import ReferenceSyntaxError, RefKind, SnapshotRef

__all__ = [
    "BOILERPLATE_MIN_OCCURRENCES",
    "UNSAFE_LINK_SCHEMES",
    "ExtractionBudget",
    "SelectorMatch",
    "SelectorSyntaxError",
    "extract_page",
    "extract_selector",
]

#: How many identical blocks it takes before repetition is treated as
#: structure rather than content.
BOILERPLATE_MIN_OCCURRENCES = 2

#: Link schemes worth a WebGlass-authored warning on sight. Enforcement is
#: policy's job (issue #1 section 10) — extraction only flags what it saw.
UNSAFE_LINK_SCHEMES = ("javascript:", "vbscript:", "data:", "file:")

_SAMPLE_CHARS = 200

_SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "svg", "math"})
_CHROME_TAGS = frozenset({"nav", "aside", "header", "footer"})
_HEADING_TAGS = {f"h{level}": level for level in range(1, 7)}
_PARAGRAPH_TAGS = frozenset(
    {"p", "blockquote", "figcaption", "dd", "dt", "address", "caption", "summary"}
)
_CONTROL_TEXT_TAGS = frozenset({"title", "label", "button", "option", "textarea"})
_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
_BUTTON_INPUT_TYPES = frozenset({"submit", "button", "reset", "image"})


class SelectorSyntaxError(ReferenceSyntaxError):
    """A selector uses a form this extractor does not support.

    Raised rather than returning no matches: "the selector matched nothing"
    and "the selector was never understood" are different states, and
    silently conflating them is exactly the kind of quiet failure issue #1
    forbids.
    """


@dataclass(frozen=True, slots=True)
class ExtractionBudget:
    """An explicit content budget. ``None`` means unbounded on that dimension."""

    max_blocks: int | None = None
    max_chars: int | None = None
    max_estimated_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class SelectorMatch:
    """One element's own content, extracted without the rest of the page.

    ``tag``, ``attributes`` and ``text`` are untrusted source material.
    """

    selector: str
    tag: str
    attributes: Mapping[str, str]
    text: str
    normalized_text: str
    source_order: int

    def to_dict(self) -> dict[str, object]:
        return {
            "selector": self.selector,
            "tag": self.tag,
            "attributes": dict(self.attributes),
            "text": self.text,
            "normalized_text": self.normalized_text,
            "source_order": self.source_order,
        }


# --- raw parse products ------------------------------------------------------


@dataclass(slots=True)
class _RawBlock:
    kind: BlockKind
    text: str
    heading_level: int | None = None
    chrome: str | None = None


@dataclass(slots=True)
class _RawLink:
    href: str
    text: str = ""


@dataclass(slots=True)
class _RawField:
    name: str
    field_type: str
    element_id: str = ""
    aria_label: str = ""
    placeholder: str = ""
    required: bool = False
    form_index: int | None = None


@dataclass(slots=True)
class _RawButton:
    text: str
    button_type: str
    form_index: int | None = None


@dataclass(slots=True)
class _RawForm:
    name: str
    action: str
    method: str
    field_indexes: list[int] = field(default_factory=list)
    button_indexes: list[int] = field(default_factory=list)


class _DocumentParser(HTMLParser):
    """Single-pass reader turning markup into raw blocks and controls.

    Deliberately tolerant: real pages have unclosed tags. Every recovery path
    is deterministic (flush what is pending, keep going) so the same bytes
    always produce the same blocks.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[_RawBlock] = []
        self.links: list[_RawLink] = []
        self.fields: list[_RawField] = []
        self.buttons: list[_RawButton] = []
        self.forms: list[_RawForm] = []
        self.labels: dict[str, str] = {}
        self.title: str = ""
        self.language: str | None = None
        self.canonical_url: str | None = None
        self.non_content_elements: int = 0

        self._buffer: list[str] = []
        self._leaf_stack: list[tuple[str, str, int | None]] = []
        self._chrome_stack: list[str] = []
        self._control_stack: list[tuple[str, str, list[str]]] = []
        self._link_stack: list[tuple[int, list[str]]] = []
        self._form_stack: list[int] = []
        self._list_depth = 0
        self._list_items: list[str] = []
        self._table_depth = 0
        self._table_rows: list[str] = []
        self._row_cells: list[str] = []
        self._skip_tag: str | None = None
        self._skip_depth = 0

    # --- text routing -------------------------------------------------------

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._control_stack:
            for _tag, _key, buffer in self._control_stack:
                buffer.append(data)
            return
        self._buffer.append(data)
        for _index, buffer in self._link_stack:
            buffer.append(data)

    def _current_leaf(self) -> tuple[str, int | None]:
        if self._leaf_stack:
            _tag, role, level = self._leaf_stack[-1]
            return role, level
        return "loose", None

    def _flush(self) -> None:
        raw = "".join(self._buffer)
        self._buffer.clear()
        role, level = self._current_leaf()
        # Code keeps its own whitespace: collapsing it would change meaning.
        text = raw.strip() if role == "code" else normalize_text(raw)
        if not text:
            return
        if role == "cell" and self._table_depth:
            self._row_cells.append(text)
            return
        if role == "item" and self._list_depth:
            self._list_items.append(text)
            return
        kind = {"heading": BlockKind.HEADING, "code": BlockKind.CODE}.get(role, BlockKind.PARAGRAPH)
        self._add_block(kind, text, level if kind is BlockKind.HEADING else None)

    def _add_block(self, kind: BlockKind, text: str, level: int | None) -> None:
        self.blocks.append(
            _RawBlock(
                kind=kind,
                text=text,
                heading_level=level,
                chrome=self._chrome_stack[-1] if self._chrome_stack else None,
            )
        )

    def _open_leaf(self, tag: str, role: str, level: int | None = None) -> None:
        self._flush()
        self._leaf_stack.append((tag, role, level))

    def _close_leaf(self, tag: str) -> None:
        self._flush()
        if self._leaf_stack and self._leaf_stack[-1][0] == tag:
            self._leaf_stack.pop()

    # --- structure ----------------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._skip_depth:
            if tag == self._skip_tag:
                self._skip_depth += 1
            return
        if tag in _SKIP_TAGS:
            self._flush()
            self._skip_tag = tag
            self._skip_depth = 1
            self.non_content_elements += 1
            return

        attributes = {name: (value or "") for name, value in attrs}
        if tag == "html":
            self.language = attributes.get("lang") or self.language
        elif tag == "link" and attributes.get("rel", "").lower() == "canonical":
            self.canonical_url = attributes.get("href") or self.canonical_url
        elif tag == "br":
            self._buffer.append(" ")

        if tag in _CONTROL_TEXT_TAGS:
            self._open_control(tag, attributes)
            return
        if tag == "a":
            self._open_anchor(attributes)
            return
        if tag in _HEADING_TAGS:
            self._open_leaf(tag, "heading", _HEADING_TAGS[tag])
            return
        if tag in _PARAGRAPH_TAGS:
            self._open_leaf(tag, "paragraph")
            return
        if tag == "pre":
            self._open_leaf(tag, "code")
            return
        if tag == "li":
            self._open_leaf(tag, "item")
            return
        if tag in ("td", "th"):
            self._open_leaf(tag, "cell")
            return
        if tag in ("ul", "ol"):
            self._flush()
            self._list_depth += 1
            return
        if tag == "table":
            self._flush()
            self._table_depth += 1
            return
        if tag == "tr":
            self._flush()
            return
        if tag == "form":
            self._open_form(attributes)
            return
        if tag in ("input", "select"):
            self._add_control(tag, attributes)
            return
        if tag in _CHROME_TAGS:
            self._flush()
            self._chrome_stack.append(tag)
            return
        if tag not in _INLINE_TAGS:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        if self._skip_depth:
            if tag == self._skip_tag:
                self._skip_depth -= 1
                if self._skip_depth == 0:
                    self._skip_tag = None
            return
        if tag in _VOID_TAGS:
            return
        if self._control_stack and self._control_stack[-1][0] == tag:
            self._close_control()
            return
        if tag == "a":
            self._close_anchor()
            return
        if tag in _HEADING_TAGS or tag in _PARAGRAPH_TAGS or tag in ("pre", "li", "td", "th"):
            self._close_leaf(tag)
            return
        if tag == "tr":
            self._flush()
            self._close_row()
            return
        if tag in ("ul", "ol"):
            self._close_list()
            return
        if tag == "table":
            self._close_table()
            return
        if tag == "form":
            self._flush()
            if self._form_stack:
                self._form_stack.pop()
            return
        if tag in _CHROME_TAGS:
            self._flush()
            if self._chrome_stack and self._chrome_stack[-1] == tag:
                self._chrome_stack.pop()
            return
        if tag not in _INLINE_TAGS:
            self._flush()

    def close(self) -> None:
        super().close()
        while self._control_stack:
            self._close_control()
        while self._link_stack:
            self._close_anchor()
        self._flush()
        self._close_row()
        if self._list_depth:
            self._list_depth = 1
            self._close_list()
        if self._table_depth:
            self._table_depth = 1
            self._close_table()

    # --- lists and tables ---------------------------------------------------

    def _close_list(self) -> None:
        self._flush()
        if self._list_depth <= 1:
            if self._list_items:
                self._add_block(BlockKind.LIST, "\n".join(self._list_items), None)
                self._list_items = []
            self._list_depth = 0
            return
        self._list_depth -= 1

    def _close_row(self) -> None:
        if self._row_cells:
            self._table_rows.append(" | ".join(self._row_cells))
            self._row_cells = []

    def _close_table(self) -> None:
        self._flush()
        self._close_row()
        if self._table_depth <= 1:
            if self._table_rows:
                self._add_block(BlockKind.TABLE, "\n".join(self._table_rows), None)
                self._table_rows = []
            self._table_depth = 0
            return
        self._table_depth -= 1

    # --- controls -----------------------------------------------------------

    def _open_control(self, tag: str, attributes: Mapping[str, str]) -> None:
        key = ""
        if tag == "label":
            key = attributes.get("for", "")
        elif tag == "button":
            self.buttons.append(
                _RawButton(
                    text="",
                    button_type=attributes.get("type", "submit"),
                    form_index=self._form_stack[-1] if self._form_stack else None,
                )
            )
            key = str(len(self.buttons) - 1)
            self._register_control(len(self.buttons) - 1, is_button=True)
        elif tag == "textarea":
            self._add_control(tag, attributes)
        self._control_stack.append((tag, key, []))

    def _close_control(self) -> None:
        tag, key, buffer = self._control_stack.pop()
        text = normalize_text("".join(buffer))
        if tag == "title":
            self.title = self.title or text
        elif tag == "label" and key:
            self.labels.setdefault(key, text)
        elif tag == "button" and key:
            self.buttons[int(key)].text = text

    def _open_anchor(self, attributes: Mapping[str, str]) -> None:
        href = attributes.get("href")
        if href is None:
            return
        self.links.append(_RawLink(href=href))
        self._link_stack.append((len(self.links) - 1, []))

    def _close_anchor(self) -> None:
        if not self._link_stack:
            return
        index, buffer = self._link_stack.pop()
        self.links[index].text = normalize_text("".join(buffer))

    def _open_form(self, attributes: Mapping[str, str]) -> None:
        self._flush()
        self.forms.append(
            _RawForm(
                name=attributes.get("name", "") or attributes.get("id", ""),
                action=attributes.get("action", ""),
                method=(attributes.get("method", "get") or "get").lower(),
            )
        )
        self._form_stack.append(len(self.forms) - 1)

    def _add_control(self, tag: str, attributes: Mapping[str, str]) -> None:
        field_type = tag if tag != "input" else attributes.get("type", "text").lower()
        form_index = self._form_stack[-1] if self._form_stack else None
        if tag == "input" and field_type in _BUTTON_INPUT_TYPES:
            self.buttons.append(
                _RawButton(
                    text=attributes.get("value", ""),
                    button_type=field_type,
                    form_index=form_index,
                )
            )
            self._register_control(len(self.buttons) - 1, is_button=True)
            return
        self.fields.append(
            _RawField(
                name=attributes.get("name", ""),
                field_type=field_type,
                element_id=attributes.get("id", ""),
                aria_label=attributes.get("aria-label", ""),
                placeholder=attributes.get("placeholder", ""),
                required="required" in attributes,
                form_index=form_index,
            )
        )
        self._register_control(len(self.fields) - 1, is_button=False)

    def _register_control(self, index: int, *, is_button: bool) -> None:
        if not self._form_stack:
            return
        form = self.forms[self._form_stack[-1]]
        (form.button_indexes if is_button else form.field_indexes).append(index)


_INLINE_TAGS = frozenset(
    {
        "a",
        "abbr",
        "b",
        "bdi",
        "bdo",
        "br",
        "cite",
        "code",
        "data",
        "dfn",
        "em",
        "i",
        "img",
        "kbd",
        "mark",
        "meta",
        "q",
        "s",
        "samp",
        "small",
        "span",
        "strong",
        "sub",
        "sup",
        "time",
        "u",
        "var",
        "wbr",
    }
)


# --- public API --------------------------------------------------------------


def extract_page(
    html: str,
    *,
    snapshot_id: str,
    requested_url: str,
    retrieved_at: str,
    generation: int = 0,
    final_url: str | None = None,
    canonical_url: str | None = None,
    language: str | None = None,
    status: int | None = None,
    content_type: str | None = None,
    redirect_chain: Sequence[str] = (),
    previous_content_hash: str | None = None,
    budget: ExtractionBudget | None = None,
    security_warnings: Sequence[SecurityWarning] = (),
) -> PageSnapshot:
    """Turn HTML into a :class:`~webglass.pages.PageSnapshot`.

    Every piece of identity the document cannot know — the URL it was fetched
    from, the HTTP status, the retrieval time, the redirect chain — is
    injected by the caller. Nothing is read from a clock or the network here,
    which is what makes the output reproducible byte for byte.

    ``canonical_url`` / ``language`` given by the caller win over the
    document's own ``<link rel="canonical">`` / ``<html lang>``: the caller
    saw the HTTP layer, the document only claims things.
    """
    parser = _DocumentParser()
    parser.feed(html)
    parser.close()

    blocks = _build_blocks(parser.blocks, snapshot_id, generation)
    retained, omissions = _collapse(blocks, parser.blocks)
    retained, budget_omission = _apply_budget(retained, budget)
    if parser.non_content_elements:
        omissions.append(
            OmittedRegion(
                kind=OmissionKind.NON_CONTENT,
                reason="script/style/template elements are never readable content",
                count=parser.non_content_elements,
            )
        )
    if budget_omission is not None:
        omissions.append(budget_omission)

    links = _build_links(parser.links, snapshot_id, generation)
    fields, buttons, forms = _build_controls(parser, snapshot_id, generation)
    warnings = tuple(security_warnings) + _link_scheme_warnings(links)

    return PageSnapshot(
        snapshot_id=snapshot_id,
        generation=generation,
        requested_url=requested_url,
        final_url=final_url if final_url is not None else requested_url,
        retrieved_at=retrieved_at,
        canonical_url=canonical_url if canonical_url is not None else parser.canonical_url,
        title=parser.title,
        language=language if language is not None else parser.language,
        content_type=content_type,
        status=status,
        redirect_chain=tuple(redirect_chain),
        blocks=tuple(retained),
        links=links,
        forms=forms,
        fields=fields,
        buttons=buttons,
        omissions=tuple(omissions),
        security_warnings=warnings,
        previous_content_hash=previous_content_hash,
    )


def _build_blocks(raw: Sequence[_RawBlock], snapshot_id: str, generation: int) -> list[Block]:
    return [
        Block(
            ref=SnapshotRef(snapshot_id, generation, RefKind.BLOCK, index),
            kind=item.kind,
            text=item.text,
            source_order=index,
            heading_level=item.heading_level,
        )
        for index, item in enumerate(raw)
    ]


def _collapse(
    blocks: Sequence[Block], raw: Sequence[_RawBlock]
) -> tuple[list[Block], list[OmittedRegion]]:
    """Drop repeated chrome and duplicate bodies, declaring both."""
    groups: dict[str, list[int]] = {}
    for index, block in enumerate(blocks):
        if block.kind is BlockKind.HEADING:
            # Headings carry the outline; two identical ones are structure.
            continue
        groups.setdefault(block.normalized_text, []).append(index)

    dropped: set[int] = set()
    boilerplate: list[tuple[int, list[int]]] = []
    duplicates: list[tuple[int, list[int]]] = []
    for indexes in groups.values():
        if len(indexes) < BOILERPLATE_MIN_OCCURRENCES:
            continue
        if all(raw[index].chrome is not None for index in indexes):
            boilerplate.append((indexes[0], indexes))
            dropped.update(indexes)
        else:
            duplicates.append((indexes[0], indexes[1:]))
            dropped.update(indexes[1:])

    omissions = [
        _omission(
            OmissionKind.BOILERPLATE,
            "repeated page chrome (identical text in nav/aside/header/footer)",
            blocks,
            indexes,
        )
        for _first, indexes in sorted(boilerplate)
    ]
    omissions += [
        _omission(
            OmissionKind.DUPLICATE,
            "identical block text already returned earlier on this page",
            blocks,
            indexes,
        )
        for _first, indexes in sorted(duplicates)
    ]
    retained = [block for index, block in enumerate(blocks) if index not in dropped]
    return retained, omissions


def _omission(
    kind: OmissionKind, reason: str, blocks: Sequence[Block], indexes: Iterable[int]
) -> OmittedRegion:
    chosen = [blocks[index] for index in indexes]
    return OmittedRegion(
        kind=kind,
        reason=reason,
        count=len(chosen),
        refs=tuple(block.ref for block in chosen),
        sample=chosen[0].text[:_SAMPLE_CHARS] if chosen else "",
        bytes_omitted=sum(len(block.text.encode("utf-8")) for block in chosen),
        estimated_tokens_omitted=sum(block.estimated_tokens for block in chosen),
    )


def _apply_budget(
    blocks: Sequence[Block], budget: ExtractionBudget | None
) -> tuple[list[Block], OmittedRegion | None]:
    if budget is None:
        return list(blocks), None
    kept: list[Block] = []
    chars = 0
    tokens = 0
    for block in blocks:
        if budget.max_blocks is not None and len(kept) + 1 > budget.max_blocks:
            break
        block_chars = len(block.text)
        block_tokens = block.estimated_tokens
        over = (budget.max_chars is not None and chars + block_chars > budget.max_chars) or (
            budget.max_estimated_tokens is not None
            and tokens + block_tokens > budget.max_estimated_tokens
        )
        if over and kept:
            break
        kept.append(block)
        chars += block_chars
        tokens += block_tokens
    dropped = list(blocks[len(kept) :])
    if not dropped:
        return kept, None
    omission = _omission(
        OmissionKind.BUDGET,
        "extraction budget reached; the remaining blocks were not retained",
        dropped,
        range(len(dropped)),
    )
    return kept, OmittedRegion(
        kind=omission.kind,
        reason=omission.reason,
        count=omission.count,
        refs=omission.refs,
        sample=omission.sample,
        bytes_omitted=omission.bytes_omitted,
        estimated_tokens_omitted=omission.estimated_tokens_omitted,
        resume_cursor=dropped[0].ref.qualified,
    )


def _build_links(raw: Sequence[_RawLink], snapshot_id: str, generation: int) -> tuple[Link, ...]:
    return tuple(
        Link(
            ref=SnapshotRef(snapshot_id, generation, RefKind.LINK, index),
            href=item.href,
            text=item.text,
        )
        for index, item in enumerate(raw)
    )


def _build_controls(
    parser: _DocumentParser, snapshot_id: str, generation: int
) -> tuple[tuple[Field, ...], tuple[Button, ...], tuple[Form, ...]]:
    def ref(kind: RefKind, index: int) -> SnapshotRef:
        return SnapshotRef(snapshot_id, generation, kind, index)

    fields = tuple(
        Field(
            ref=ref(RefKind.FIELD, index),
            name=item.name,
            field_type=item.field_type,
            label=(parser.labels.get(item.element_id, "") or item.aria_label or item.placeholder),
            required=item.required,
            form_ref=ref(RefKind.FORM, item.form_index) if item.form_index is not None else None,
        )
        for index, item in enumerate(parser.fields)
    )
    buttons = tuple(
        Button(
            ref=ref(RefKind.BUTTON, index),
            text=item.text,
            button_type=item.button_type,
            form_ref=ref(RefKind.FORM, item.form_index) if item.form_index is not None else None,
        )
        for index, item in enumerate(parser.buttons)
    )
    forms = tuple(
        Form(
            ref=ref(RefKind.FORM, index),
            name=item.name,
            action=item.action,
            method=item.method,
            field_refs=tuple(ref(RefKind.FIELD, i) for i in item.field_indexes),
            button_refs=tuple(ref(RefKind.BUTTON, i) for i in item.button_indexes),
        )
        for index, item in enumerate(parser.forms)
    )
    return fields, buttons, forms


def _link_scheme_warnings(links: Sequence[Link]) -> tuple[SecurityWarning, ...]:
    warnings: list[SecurityWarning] = []
    for link in links:
        scheme = link.href.strip().lower()
        if scheme.startswith(UNSAFE_LINK_SCHEMES):
            warnings.append(
                SecurityWarning(
                    code="unsafe-link-scheme",
                    # WebGlass-authored: the untrusted href goes in `subject`,
                    # never inside this message.
                    message="link target uses a scheme WebGlass never navigates to",
                    ref=link.ref,
                    subject=link.href,
                )
            )
    return tuple(warnings)


# --- selector-scoped extraction ---------------------------------------------

_SEL_TAG_RE = re.compile(r"[A-Za-z][A-Za-z0-9-]*")
_SEL_NAME_RE = re.compile(r"[A-Za-z0-9_-]+")
_SUPPORTED_FORMS = (
    "supported selector forms: 'tag', '#id', '.class', '[attr]', "
    "'[attr=value]', '[attr=\"value\"]', and combinations such as "
    "'tag#id' or 'tag.class' — combinators, commas and pseudo-classes are not supported"
)


@dataclass(frozen=True, slots=True)
class _Selector:
    tag: str | None
    element_id: str | None
    classes: tuple[str, ...]
    attributes: tuple[tuple[str, str | None], ...]


def _parse_selector(selector: str) -> _Selector:
    text = selector or ""
    if not text or text != text.strip():
        raise SelectorSyntaxError(
            f"empty or padded selector {selector!r}", remediation=_SUPPORTED_FORMS
        )
    position = 0
    tag: str | None = None
    element_id: str | None = None
    classes: list[str] = []
    attributes: list[tuple[str, str | None]] = []

    match = _SEL_TAG_RE.match(text)
    if match:
        tag = match.group(0).lower()
        position = match.end()

    while position < len(text):
        char = text[position]
        if char in "#.":
            name = _SEL_NAME_RE.match(text, position + 1)
            if name is None:
                raise SelectorSyntaxError(
                    f"malformed selector {selector!r}", remediation=_SUPPORTED_FORMS
                )
            if char == "#":
                element_id = name.group(0)
            else:
                classes.append(name.group(0))
            position = name.end()
            continue
        if char == "[":
            end = text.find("]", position)
            if end == -1:
                raise SelectorSyntaxError(
                    f"unterminated attribute selector in {selector!r}",
                    remediation=_SUPPORTED_FORMS,
                )
            attributes.append(_parse_attribute(text[position + 1 : end], selector))
            position = end + 1
            continue
        raise SelectorSyntaxError(
            f"unsupported selector syntax at {char!r} in {selector!r}",
            remediation=_SUPPORTED_FORMS,
        )

    # No "nothing matched" case can reach here: the scanner either consumes a
    # component or raises, and an empty/padded selector was rejected above.
    return _Selector(tag, element_id, tuple(classes), tuple(attributes))


def _parse_attribute(body: str, selector: str) -> tuple[str, str | None]:
    if "=" not in body:
        name = body.strip()
        if not name:
            raise SelectorSyntaxError(
                f"empty attribute selector in {selector!r}", remediation=_SUPPORTED_FORMS
            )
        return name.lower(), None
    name, _, value = body.partition("=")
    name = name.strip()
    value = value.strip()
    if not name:
        raise SelectorSyntaxError(
            f"empty attribute name in {selector!r}", remediation=_SUPPORTED_FORMS
        )
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return name.lower(), value


def _selector_matches(selector: _Selector, tag: str, attributes: Mapping[str, str]) -> bool:
    if selector.tag is not None and selector.tag != tag:
        return False
    if selector.element_id is not None and attributes.get("id") != selector.element_id:
        return False
    if selector.classes:
        present = set(attributes.get("class", "").split())
        if not set(selector.classes) <= present:
            return False
    for name, value in selector.attributes:
        if name not in attributes:
            return False
        if value is not None and attributes[name] != value:
            return False
    return True


class _SelectorParser(HTMLParser):
    """Capture the raw inner content of every element matching one selector.

    Unlike :class:`_DocumentParser` this does *not* skip ``<script>``: the
    whole point (issue #9 item 3) is to read a machine-readable state node the
    readable-content extractor deliberately ignores.
    """

    def __init__(self, selector: _Selector, selector_text: str) -> None:
        super().__init__(convert_charrefs=True)
        self._selector = selector
        self._selector_text = selector_text
        self._stack: list[str] = []
        self._active: list[dict[str, object]] = []
        self._order = 0
        self.matches: list[SelectorMatch] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name: (value or "") for name, value in attrs}
        matched = _selector_matches(self._selector, tag, attributes)
        if tag in _VOID_TAGS:
            if matched:
                self._emit(tag, attributes, "")
            return
        self._stack.append(tag)
        if matched:
            self._active.append(
                {
                    "tag": tag,
                    "attributes": attributes,
                    "depth": len(self._stack),
                    "buffer": [],
                    "order": self._next_order(),
                }
            )

    def handle_data(self, data: str) -> None:
        for capture in self._active:
            buffer = capture["buffer"]
            assert isinstance(buffer, list)
            buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID_TAGS:
            return
        for capture in list(self._active):
            if capture["depth"] == len(self._stack) and capture["tag"] == tag:
                self._finish(capture)
        if tag in self._stack:
            while self._stack:
                if self._stack.pop() == tag:
                    break

    def close(self) -> None:
        super().close()
        for capture in list(self._active):
            self._finish(capture)
        self.matches.sort(key=lambda match: match.source_order)

    def _next_order(self) -> int:
        self._order += 1
        return self._order - 1

    def _emit(
        self, tag: str, attributes: Mapping[str, str], text: str, order: int | None = None
    ) -> None:
        self.matches.append(
            SelectorMatch(
                selector=self._selector_text,
                tag=tag,
                attributes=dict(attributes),
                text=text,
                normalized_text=normalize_text(text),
                source_order=self._next_order() if order is None else order,
            )
        )

    def _finish(self, capture: dict[str, object]) -> None:
        self._active.remove(capture)
        buffer = capture["buffer"]
        assert isinstance(buffer, list)
        attributes = capture["attributes"]
        assert isinstance(attributes, dict)
        self._emit(
            str(capture["tag"]),
            attributes,
            "".join(buffer).strip(),
            order=int(capture["order"]),  # type: ignore[call-overload]
        )


def extract_selector(html: str, selector: str) -> tuple[SelectorMatch, ...]:
    """Return the content of every element matching ``selector``, in document order.

    Supports the narrow selector grammar an agent actually needs — ``tag``,
    ``#id``, ``.class``, ``[attr]``, ``[attr=value]`` and their combinations.
    Anything else raises :class:`SelectorSyntaxError` rather than quietly
    matching nothing, because "no such element" and "I did not understand
    you" must never look alike to a caller.
    """
    parsed = _parse_selector(selector)
    parser = _SelectorParser(parsed, selector)
    parser.feed(html)
    parser.close()
    return tuple(parser.matches)
