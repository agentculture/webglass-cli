"""PageSnapshot model and its lens projections (``webglass.pages``).

Issue #1 section 5: the snapshot is the agent-facing unit, and every lens
(``page_card`` / ``outline`` / ``read``) projects *the same* snapshot while
preserving stable block ids -- "an agent moves from compact outline to exact
source text without re-fetching or losing provenance". These tests build
snapshots by hand (no HTML parsing -- that is ``tests/test_extraction.py``)
so the model and the lenses are pinned independently of the extractor.

Nothing here calls a clock: ``retrieved_at`` is always injected.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from webglass.pages import (
    PAGE_SNAPSHOT_SCHEMA_VERSION,
    TOKEN_ESTIMATE_METHOD,
    Block,
    BlockKind,
    Button,
    Field,
    Form,
    Link,
    OmissionKind,
    OmittedRegion,
    PageSnapshot,
    ReadBudget,
    SecurityWarning,
    estimate_tokens,
    normalize_text,
    outline,
    page_card,
    read,
)
from webglass.references import (
    ReferenceSyntaxError,
    RefKind,
    SnapshotRef,
    StaleReferenceError,
    UnknownReferenceError,
)

SNAP = "snap-1"
OTHER = "snap-2"
WHEN = "2026-08-07T12:00:00Z"


def _ref(kind: RefKind, index: int, *, snapshot_id: str = SNAP, generation: int = 0) -> SnapshotRef:
    return SnapshotRef(snapshot_id, generation, kind, index)


def _block(index: int, kind: BlockKind, text: str, level: int | None = None) -> Block:
    return Block(
        ref=_ref(RefKind.BLOCK, index),
        kind=kind,
        text=text,
        source_order=index,
        heading_level=level,
    )


BLOCKS = (
    _block(0, BlockKind.HEADING, "Real Article Title", 1),
    _block(1, BlockKind.PARAGRAPH, "First paragraph of the article body."),
    _block(2, BlockKind.HEADING, "A Subsection", 2),
    _block(3, BlockKind.PARAGRAPH, "Second paragraph, under the subsection."),
)


def _snapshot(**overrides: object) -> PageSnapshot:
    kwargs: dict[str, object] = {
        "snapshot_id": SNAP,
        "generation": 0,
        "requested_url": "http://127.0.0.1:8080/article",
        "final_url": "http://127.0.0.1:8080/article",
        "retrieved_at": WHEN,
        "title": "Real Article Title",
        "language": "en",
        "content_type": "text/html; charset=utf-8",
        "status": 200,
        "blocks": BLOCKS,
    }
    kwargs.update(overrides)
    return PageSnapshot(**kwargs)  # type: ignore[arg-type]


# --- model ------------------------------------------------------------------


def test_schema_version_defaults_to_one() -> None:
    assert _snapshot().schema_version == PAGE_SNAPSHOT_SCHEMA_VERSION == 1


def test_content_hash_is_sha256_of_the_normalized_block_text() -> None:
    snapshot = _snapshot()
    expected = hashlib.sha256(
        "\n".join(b.normalized_text for b in BLOCKS).encode("utf-8")
    ).hexdigest()
    assert snapshot.content_hash == f"sha256:{expected}"


def test_block_carries_its_own_content_hash_and_token_estimate() -> None:
    block = BLOCKS[1]
    assert block.content_hash.startswith("sha256:")
    assert block.estimated_tokens == estimate_tokens(block.text)


def test_token_estimate_is_a_labeled_deterministic_heuristic() -> None:
    assert estimate_tokens("a" * 40) == 10
    assert TOKEN_ESTIMATE_METHOD


def test_normalize_text_collapses_whitespace() -> None:
    assert normalize_text("  a\n\t b  \n") == "a b"


def test_change_state_is_declared_relative_to_a_previous_hash() -> None:
    assert _snapshot().changed is None
    same = _snapshot()
    assert _snapshot(previous_content_hash=same.content_hash).changed is False
    assert _snapshot(previous_content_hash="sha256:deadbeef").changed is True


def test_truncated_is_true_only_when_a_budget_omission_is_declared() -> None:
    assert _snapshot().truncated is False
    truncated = _snapshot(
        omissions=(
            OmittedRegion(
                kind=OmissionKind.BUDGET,
                reason="content budget exhausted",
                count=2,
                refs=(_ref(RefKind.BLOCK, 2), _ref(RefKind.BLOCK, 3)),
                resume_cursor=_ref(RefKind.BLOCK, 2).qualified,
            ),
        )
    )
    assert truncated.truncated is True


def test_a_snapshot_refuses_to_embed_a_reference_from_another_snapshot() -> None:
    foreign = Block(
        ref=_ref(RefKind.BLOCK, 0, snapshot_id=OTHER),
        kind=BlockKind.PARAGRAPH,
        text="from somewhere else",
        source_order=0,
    )
    with pytest.raises(StaleReferenceError):
        _snapshot(blocks=(foreign,))


def test_a_block_ref_must_be_of_block_kind() -> None:
    link_ref = _ref(RefKind.LINK, 0)
    with pytest.raises(ReferenceSyntaxError):
        Block(ref=link_ref, kind=BlockKind.PARAGRAPH, text="x", source_order=0)


# --- resolution -------------------------------------------------------------


def test_resolve_returns_the_element_for_each_ref_kind() -> None:
    link = Link(ref=_ref(RefKind.LINK, 0), href="/about", text="About")
    form = Form(ref=_ref(RefKind.FORM, 0), name="search", action="/s", method="get")
    field = Field(ref=_ref(RefKind.FIELD, 0), name="q", field_type="text", label="Query")
    button = Button(ref=_ref(RefKind.BUTTON, 0), text="Go", button_type="submit")
    snapshot = _snapshot(links=(link,), forms=(form,), fields=(field,), buttons=(button,))
    assert snapshot.resolve(link.ref) is link
    assert snapshot.resolve(form.ref) is form
    assert snapshot.resolve(field.ref) is field
    assert snapshot.resolve(button.ref) is button
    assert snapshot.resolve(BLOCKS[2].ref) is BLOCKS[2]


def test_resolving_a_ref_from_another_snapshot_fails_as_stale() -> None:
    snapshot = _snapshot()
    foreign = _ref(RefKind.BLOCK, 0, snapshot_id=OTHER)
    with pytest.raises(StaleReferenceError):
        snapshot.resolve(foreign)


def test_resolving_a_ref_from_an_older_generation_fails_as_stale() -> None:
    snapshot = _snapshot(generation=2, blocks=tuple())
    stale = _ref(RefKind.BLOCK, 0, generation=1)
    with pytest.raises(StaleReferenceError):
        snapshot.resolve(stale)


def test_resolving_an_in_scope_but_absent_ref_fails_as_unknown() -> None:
    snapshot = _snapshot()
    absent = _ref(RefKind.BLOCK, 99)
    with pytest.raises(UnknownReferenceError):
        snapshot.resolve(absent)


def test_resolving_an_omitted_block_says_why_it_is_missing() -> None:
    snapshot = _snapshot(
        omissions=(
            OmittedRegion(
                kind=OmissionKind.BOILERPLATE,
                reason="repeated navigation chrome",
                count=1,
                refs=(_ref(RefKind.BLOCK, 42),),
            ),
        )
    )
    omitted_ref = _ref(RefKind.BLOCK, 42)
    with pytest.raises(UnknownReferenceError) as excinfo:
        snapshot.resolve(omitted_ref)
    assert "boilerplate" in str(excinfo.value)


def test_asking_for_a_block_with_a_link_ref_is_a_clear_type_failure() -> None:
    snapshot = _snapshot()
    link_ref = _ref(RefKind.LINK, 0)
    with pytest.raises(UnknownReferenceError):
        snapshot.block(link_ref)


# --- lens: page_card --------------------------------------------------------


def test_page_card_carries_identity_status_outline_and_warnings() -> None:
    warning = SecurityWarning(code="unsafe-link-scheme", message="link uses a blocked scheme")
    snapshot = _snapshot(security_warnings=(warning,), redirect_chain=("http://a/", "http://b/"))
    card = snapshot.page_card()
    assert card.requested_url == snapshot.requested_url
    assert card.final_url == snapshot.final_url
    assert card.status == 200
    assert card.title == "Real Article Title"
    assert card.content_hash == snapshot.content_hash
    assert card.redirect_chain == ("http://a/", "http://b/")
    assert card.security_warnings == (warning,)
    assert [node.text for node in card.outline] == ["Real Article Title"]
    assert card.block_count == len(BLOCKS)


def test_page_card_is_small_it_omits_body_text() -> None:
    payload = json.dumps(_snapshot().page_card().to_dict())
    assert "A Subsection" in payload  # headings travel: they are the outline
    assert "First paragraph of the article body." not in payload


def test_page_card_summarizes_omissions_by_kind() -> None:
    snapshot = _snapshot(
        omissions=(
            OmittedRegion(kind=OmissionKind.BOILERPLATE, reason="chrome", count=2),
            OmittedRegion(kind=OmissionKind.DUPLICATE, reason="dupe", count=1),
        )
    )
    assert snapshot.page_card().omissions == {"boilerplate": 2, "duplicate": 1}


def test_page_card_module_level_lens_matches_the_method() -> None:
    snapshot = _snapshot()
    assert page_card(snapshot).to_dict() == snapshot.page_card().to_dict()


# --- lens: outline ----------------------------------------------------------


def test_outline_nests_by_heading_level_and_keeps_block_refs() -> None:
    tree = _snapshot().outline()
    assert len(tree) == 1
    root = tree[0]
    assert root.ref == BLOCKS[0].ref
    assert root.level == 1
    assert [child.text for child in root.children] == ["A Subsection"]
    assert root.children[0].ref == BLOCKS[2].ref


def test_outline_module_level_lens_matches_the_method() -> None:
    snapshot = _snapshot()
    assert outline(snapshot) == snapshot.outline()


def test_outline_closes_a_section_when_a_sibling_or_shallower_heading_arrives() -> None:
    blocks = (
        _block(0, BlockKind.HEADING, "A", 1),
        _block(1, BlockKind.HEADING, "B", 2),
        _block(2, BlockKind.HEADING, "C", 2),
        _block(3, BlockKind.HEADING, "D", 1),
    )
    tree = _snapshot(blocks=blocks).outline()
    assert [node.text for node in tree] == ["A", "D"]
    assert [child.text for child in tree[0].children] == ["B", "C"]
    assert tree[1].children == ()


def test_outline_handles_a_deeper_heading_without_an_intermediate_level() -> None:
    blocks = (
        _block(0, BlockKind.HEADING, "Top", 1),
        _block(1, BlockKind.HEADING, "Deep", 3),
    )
    tree = _snapshot(blocks=blocks).outline()
    assert tree[0].children[0].text == "Deep"


# --- lens: read -------------------------------------------------------------


def test_read_without_a_budget_returns_every_block_and_no_cursor() -> None:
    result = _snapshot().read()
    assert [b.ref for b in result.blocks] == [b.ref for b in BLOCKS]
    assert result.cursor is None
    assert result.done is True
    assert result.omissions == ()
    assert result.usage.blocks == 4
    assert result.usage.estimate_method == TOKEN_ESTIMATE_METHOD


def test_read_module_level_lens_matches_the_method() -> None:
    snapshot = _snapshot()
    assert read(snapshot).to_dict() == snapshot.read().to_dict()


def test_read_truncates_at_the_block_budget_and_declares_the_omission() -> None:
    result = _snapshot().read(budget=ReadBudget(max_blocks=2))
    assert [b.ref for b in result.blocks] == [BLOCKS[0].ref, BLOCKS[1].ref]
    assert result.done is False
    assert result.cursor == BLOCKS[2].ref
    assert len(result.omissions) == 1
    omission = result.omissions[0]
    assert omission.kind is OmissionKind.BUDGET
    assert omission.count == 2
    assert omission.refs == (BLOCKS[2].ref, BLOCKS[3].ref)
    assert omission.resume_cursor == BLOCKS[2].ref.qualified


def test_read_resumes_from_a_cursor_and_finishes() -> None:
    first = _snapshot().read(budget=ReadBudget(max_blocks=2))
    rest = _snapshot().read(cursor=first.cursor, budget=ReadBudget(max_blocks=2))
    assert [b.ref for b in rest.blocks] == [BLOCKS[2].ref, BLOCKS[3].ref]
    assert rest.done is True
    assert rest.cursor is None


def test_read_accepts_a_qualified_cursor_string() -> None:
    result = _snapshot().read(cursor=BLOCKS[3].ref.qualified)
    assert [b.ref for b in result.blocks] == [BLOCKS[3].ref]


def test_read_refuses_a_cursor_from_another_snapshot() -> None:
    snapshot = _snapshot()
    foreign_cursor = SnapshotRef(OTHER, 0, RefKind.BLOCK, 0).qualified
    with pytest.raises(StaleReferenceError):
        snapshot.read(cursor=foreign_cursor)


def test_read_refuses_a_cursor_that_names_no_block_in_this_snapshot() -> None:
    snapshot = _snapshot()
    absent_cursor = _ref(RefKind.BLOCK, 99)
    with pytest.raises(UnknownReferenceError):
        snapshot.read(cursor=absent_cursor)


def test_read_honors_a_character_budget() -> None:
    result = _snapshot().read(budget=ReadBudget(max_chars=len(BLOCKS[0].text) + 1))
    assert [b.ref for b in result.blocks] == [BLOCKS[0].ref]
    assert result.usage.chars == len(BLOCKS[0].text)
    assert result.single_block_overrun is False


def test_read_honors_a_token_budget() -> None:
    result = _snapshot().read(
        budget=ReadBudget(max_estimated_tokens=estimate_tokens(BLOCKS[0].text))
    )
    assert [b.ref for b in result.blocks] == [BLOCKS[0].ref]


def test_read_always_makes_progress_and_declares_a_single_block_overrun() -> None:
    result = _snapshot().read(budget=ReadBudget(max_chars=1))
    assert [b.ref for b in result.blocks] == [BLOCKS[0].ref]
    assert result.single_block_overrun is True
    assert result.done is False
    assert result.cursor == BLOCKS[1].ref


def test_read_with_a_zero_block_budget_returns_nothing_but_still_declares_it() -> None:
    result = _snapshot().read(budget=ReadBudget(max_blocks=0))
    assert result.blocks == ()
    assert result.cursor == BLOCKS[0].ref
    assert result.omissions[0].count == 4


# --- id preservation across lenses -----------------------------------------


def test_every_lens_preserves_the_same_block_ids() -> None:
    snapshot = _snapshot()
    card_refs = {node.ref for node in snapshot.page_card().outline}
    outline_refs = {node.ref for node in snapshot.outline()}
    read_refs = {block.ref for block in snapshot.read().blocks}
    assert card_refs == outline_refs
    assert outline_refs <= read_refs
    # And the ids are the snapshot's own, not re-derived per lens.
    assert read_refs == {block.ref for block in snapshot.blocks}


def test_lens_payloads_render_block_ids_in_the_short_form() -> None:
    snapshot = _snapshot()
    read_payload = snapshot.read().to_dict()
    assert read_payload["blocks"][0]["ref"] == "block:0"
    assert read_payload["snapshot_id"] == SNAP
    outline_payload = snapshot.page_card().to_dict()["outline"]
    assert outline_payload[0]["ref"] == "block:0"


def test_a_cursor_travels_in_the_qualified_form_because_it_outlives_one_call() -> None:
    payload = _snapshot().read(budget=ReadBudget(max_blocks=1)).to_dict()
    assert payload["cursor"] == BLOCKS[1].ref.qualified


# --- serialization ----------------------------------------------------------


def test_snapshot_to_dict_is_json_serializable_and_declares_everything() -> None:
    warning = SecurityWarning(
        code="unsafe-link-scheme",
        message="link uses a blocked scheme",
        ref=_ref(RefKind.LINK, 0),
        subject="javascript:alert(1)",
    )
    snapshot = _snapshot(
        links=(Link(ref=_ref(RefKind.LINK, 0), href="javascript:alert(1)", text="x"),),
        security_warnings=(warning,),
        omissions=(OmittedRegion(kind=OmissionKind.NON_CONTENT, reason="script", count=1),),
    )
    payload = json.loads(json.dumps(snapshot.to_dict()))
    assert payload["schema_version"] == 1
    assert payload["snapshot_id"] == SNAP
    assert payload["retrieved_at"] == WHEN
    assert payload["content_hash"] == snapshot.content_hash
    assert payload["security_warnings"][0]["code"] == "unsafe-link-scheme"
    assert payload["security_warnings"][0]["subject"] == "javascript:alert(1)"
    assert payload["omissions"][0]["kind"] == "non-content"
    assert payload["links"][0]["ref"] == "link:0"
    assert payload["truncated"] is False


def test_control_serialization_carries_typed_refs_and_form_membership() -> None:
    form_ref = _ref(RefKind.FORM, 0)
    field_ref = _ref(RefKind.FIELD, 0)
    button_ref = _ref(RefKind.BUTTON, 0)
    snapshot = _snapshot(
        forms=(
            Form(
                ref=form_ref,
                name="search",
                action="/s",
                method="get",
                field_refs=(field_ref,),
                button_refs=(button_ref,),
            ),
        ),
        fields=(
            Field(
                ref=field_ref,
                name="q",
                field_type="text",
                label="Query",
                required=True,
                form_ref=form_ref,
            ),
        ),
        buttons=(Button(ref=button_ref, text="Go", button_type="submit", form_ref=form_ref),),
    )
    payload = json.loads(json.dumps(snapshot.to_dict()))
    assert payload["forms"][0] == {
        "ref": "form:0",
        "name": "search",
        "action": "/s",
        "method": "get",
        "field_refs": ["field:0"],
        "button_refs": ["button:0"],
    }
    assert payload["fields"][0]["form_ref"] == "form:0"
    assert payload["fields"][0]["required"] is True
    assert payload["buttons"][0]["text"] == "Go"
