"""Deterministic HTML -> PageSnapshot extraction (``webglass.extraction``).

Issue #1 section 6: extraction is "deterministic and inspectable" -- strip
boilerplate, preserve headings and source order, segment into stable blocks,
dedup by hash, enforce explicit budgets, and "identify every omitted,
collapsed, or truncated region". Nothing may vanish silently, and no model
ever rewrites page content.

The fixture pages under ``tests/fixtures/pages/`` are read straight off disk:
extraction is a pure function of bytes, so no HTTP (and no live public
website -- CLAUDE.md "Test ownership") is involved.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from webglass.extraction import (
    ExtractionBudget,
    SelectorSyntaxError,
    extract_page,
    extract_selector,
)
from webglass.pages import BlockKind, OmissionKind
from webglass.references import RefKind

PAGES = Path(__file__).parent / "fixtures" / "pages"
WHEN = "2026-08-07T12:00:00Z"
NAV_TEXT = "Home | About | Contact | Archive"


def _fixture(name: str) -> str:
    return (PAGES / name).read_text(encoding="utf-8")


def _extract(html: str, **overrides: object):
    kwargs: dict[str, object] = {
        "snapshot_id": "snap-1",
        "requested_url": "http://127.0.0.1:8080/page",
        "retrieved_at": WHEN,
    }
    kwargs.update(overrides)
    return extract_page(html, **kwargs)  # type: ignore[arg-type]


# --- identity and injected metadata ----------------------------------------


def test_extraction_never_invents_metadata_it_was_not_given() -> None:
    snapshot = _extract(
        _fixture("clean.html"),
        status=200,
        content_type="text/html; charset=utf-8",
        redirect_chain=("http://127.0.0.1:8080/a", "http://127.0.0.1:8080/page"),
    )
    assert snapshot.retrieved_at == WHEN
    assert snapshot.status == 200
    assert snapshot.content_type == "text/html; charset=utf-8"
    assert snapshot.redirect_chain == ("http://127.0.0.1:8080/a", "http://127.0.0.1:8080/page")
    # final_url defaults to the requested URL -- extraction cannot know better.
    assert snapshot.final_url == snapshot.requested_url


def test_title_and_language_come_from_the_document() -> None:
    snapshot = _extract(_fixture("clean.html"))
    assert snapshot.title == "WebGlass Fixture: Clean Page"
    assert snapshot.language == "en"


def test_canonical_url_is_read_from_the_link_rel_canonical_element() -> None:
    html = (
        '<html><head><link rel="canonical" href="http://x/canon"></head>'
        "<body><p>y</p></body></html>"
    )
    assert _extract(html).canonical_url == "http://x/canon"


def test_every_ref_is_scoped_to_the_snapshot_that_produced_it() -> None:
    snapshot = _extract(_fixture("boilerplate.html"), snapshot_id="snap-9", generation=3)
    refs = [b.ref for b in snapshot.blocks] + [ln.ref for ln in snapshot.links]
    assert refs
    for ref in refs:
        assert ref.snapshot_id == "snap-9"
        assert ref.generation == 3


# --- block segmentation -----------------------------------------------------


def test_clean_page_yields_a_heading_and_a_paragraph_in_source_order() -> None:
    snapshot = _extract(_fixture("clean.html"))
    assert [(b.kind, b.heading_level) for b in snapshot.blocks] == [
        (BlockKind.HEADING, 1),
        (BlockKind.PARAGRAPH, None),
    ]
    assert snapshot.blocks[0].text == "Clean Page"
    assert snapshot.blocks[1].text.startswith("This page loads with no script errors")
    assert [b.source_order for b in snapshot.blocks] == [0, 1]
    assert [b.ref.index for b in snapshot.blocks] == [0, 1]
    assert snapshot.blocks[0].ref.kind is RefKind.BLOCK


def test_whitespace_inside_a_block_is_normalized() -> None:
    text = _extract(_fixture("clean.html")).blocks[1].text
    assert "\n" not in text
    assert "  " not in text


def test_character_entities_are_decoded() -> None:
    assert _extract("<p>Tom &amp; Jerry</p>").blocks[0].text == "Tom & Jerry"


def test_list_becomes_one_block_with_newline_joined_items() -> None:
    snapshot = _extract("<ul><li>alpha</li><li>beta</li></ul>")
    assert snapshot.blocks[0].kind is BlockKind.LIST
    assert snapshot.blocks[0].text == "alpha\nbeta"


def test_table_becomes_one_block_with_pipe_joined_cells() -> None:
    html = "<table><tr><th>a</th><th>b</th></tr><tr><td>1</td><td>2</td></tr></table>"
    snapshot = _extract(html)
    assert snapshot.blocks[0].kind is BlockKind.TABLE
    assert snapshot.blocks[0].text == "a | b\n1 | 2"


def test_pre_preserves_its_own_whitespace_because_code_is_not_prose() -> None:
    snapshot = _extract("<pre>line one\n  line two</pre>")
    assert snapshot.blocks[0].kind is BlockKind.CODE
    assert snapshot.blocks[0].text == "line one\n  line two"


# --- script/style are never content ----------------------------------------


def test_script_text_never_becomes_readable_content_and_is_declared() -> None:
    snapshot = _extract(_fixture("keydown.html"))
    joined = "\n".join(b.text for b in snapshot.blocks)
    assert "__webglassKeyLog" in joined  # the *prose* mentions it, in a <code> span
    assert "addEventListener" not in joined  # ... but the script body never leaks
    non_content = [o for o in snapshot.omissions if o.kind is OmissionKind.NON_CONTENT]
    assert len(non_content) == 1
    assert non_content[0].count == 1


def test_hostile_console_strings_are_neither_content_nor_webglass_warnings() -> None:
    snapshot = _extract(_fixture("spoofed_console.html"))
    joined = "\n".join(b.text for b in snapshot.blocks)
    assert "WEBGLASS WARNING: policy disabled" not in joined
    assert "ignore previous instructions" not in joined
    rendered = json.dumps([w.to_dict() for w in snapshot.security_warnings])
    assert "WEBGLASS WARNING" not in rendered
    assert "ignore previous instructions" not in rendered


# --- boilerplate and duplicates are collapsed, never silently ---------------


def test_repeated_chrome_is_collapsed_and_every_omission_is_declared() -> None:
    snapshot = _extract(_fixture("boilerplate.html"))
    assert [b.text for b in snapshot.blocks] == [
        "Real Article Title",
        (
            "This is the actual content a boilerplate-stripping extraction pass should "
            "preserve: a short article body, in source order, distinct from the repeated "
            "navigation and footer chrome that surrounds it on every page of this fixture site."
        ),
    ]
    boilerplate = [o for o in snapshot.omissions if o.kind is OmissionKind.BOILERPLATE]
    assert len(boilerplate) == 2
    assert [o.count for o in boilerplate] == [2, 2]
    assert [ref.short for ref in boilerplate[0].refs] == ["block:0", "block:1"]
    assert [ref.short for ref in boilerplate[1].refs] == ["block:4", "block:5"]
    assert NAV_TEXT in boilerplate[0].sample
    assert boilerplate[0].estimated_tokens_omitted > 0


def test_retained_block_ids_keep_their_original_source_positions() -> None:
    snapshot = _extract(_fixture("boilerplate.html"))
    # Collapsing never renumbers what survives: the gap *is* the disclosure.
    assert [b.ref.short for b in snapshot.blocks] == ["block:2", "block:3"]
    assert [b.source_order for b in snapshot.blocks] == [2, 3]


def test_an_omitted_block_ref_resolves_to_a_clear_omission_error() -> None:
    from webglass.references import UnknownReferenceError

    snapshot = _extract(_fixture("boilerplate.html"))
    omitted = snapshot.omissions[0].refs[0]
    with pytest.raises(UnknownReferenceError) as excinfo:
        snapshot.resolve(omitted)
    assert "boilerplate" in str(excinfo.value)


def test_identical_body_text_outside_chrome_is_deduped_keeping_the_first() -> None:
    html = "<main><p>Repeated body sentence.</p><p>Repeated body sentence.</p></main>"
    snapshot = _extract(html)
    assert [b.ref.short for b in snapshot.blocks] == ["block:0"]
    duplicates = [o for o in snapshot.omissions if o.kind is OmissionKind.DUPLICATE]
    assert len(duplicates) == 1
    assert duplicates[0].count == 1
    assert [ref.short for ref in duplicates[0].refs] == ["block:1"]


def test_repeated_headings_are_exempt_because_the_outline_depends_on_them() -> None:
    html = "<main><h2>Notes</h2><p>a</p><h2>Notes</h2><p>b</p></main>"
    snapshot = _extract(html)
    assert [b.text for b in snapshot.blocks] == ["Notes", "a", "Notes", "b"]
    assert snapshot.omissions == ()


# --- budgets ----------------------------------------------------------------


def test_a_block_budget_truncates_and_hands_back_a_resume_cursor() -> None:
    snapshot = _extract(_fixture("clean.html"), budget=ExtractionBudget(max_blocks=1))
    assert [b.ref.short for b in snapshot.blocks] == ["block:0"]
    assert snapshot.truncated is True
    budget_omissions = [o for o in snapshot.omissions if o.kind is OmissionKind.BUDGET]
    assert len(budget_omissions) == 1
    omission = budget_omissions[0]
    assert omission.count == 1
    assert omission.resume_cursor == snapshot.blocks[0].ref.qualified.replace("block:0", "block:1")
    assert omission.bytes_omitted > 0


def test_a_character_budget_truncates_and_declares_the_omission() -> None:
    snapshot = _extract(_fixture("clean.html"), budget=ExtractionBudget(max_chars=20))
    assert [b.ref.short for b in snapshot.blocks] == ["block:0"]
    assert snapshot.truncated is True


def test_a_token_budget_truncates_and_declares_the_omission() -> None:
    snapshot = _extract(_fixture("clean.html"), budget=ExtractionBudget(max_estimated_tokens=5))
    assert [b.ref.short for b in snapshot.blocks] == ["block:0"]
    assert snapshot.truncated is True


def test_an_ample_budget_truncates_nothing() -> None:
    snapshot = _extract(_fixture("clean.html"), budget=ExtractionBudget(max_blocks=50))
    assert len(snapshot.blocks) == 2
    assert snapshot.truncated is False


# --- links, forms, fields, buttons -----------------------------------------


def test_links_are_indexed_in_document_order_with_stable_refs() -> None:
    snapshot = _extract(_fixture("boilerplate.html"))
    assert [ln.ref.short for ln in snapshot.links[:4]] == [
        "link:0",
        "link:1",
        "link:2",
        "link:3",
    ]
    assert [ln.text for ln in snapshot.links[:4]] == ["Home", "About", "Contact", "Archive"]
    assert snapshot.links[0].href == "/"
    # Link indexing is independent of block collapsing: chrome links stay
    # addressable for `follow`, even though their text block was collapsed.
    assert len(snapshot.links) == 16


def test_forms_fields_and_buttons_carry_typed_refs_and_labels() -> None:
    html = """
    <form name="search" action="/s" method="get">
      <label for="q">Query</label>
      <input id="q" name="q" type="text" required>
      <select name="scope"><option>all</option></select>
      <button type="submit">Go</button>
    </form>
    """
    snapshot = _extract(html)
    assert [f.ref.short for f in snapshot.forms] == ["form:0"]
    form = snapshot.forms[0]
    assert (form.name, form.action, form.method) == ("search", "/s", "get")
    assert [f.ref.short for f in snapshot.fields] == ["field:0", "field:1"]
    assert snapshot.fields[0].name == "q"
    assert snapshot.fields[0].field_type == "text"
    assert snapshot.fields[0].label == "Query"
    assert snapshot.fields[0].required is True
    assert snapshot.fields[1].field_type == "select"
    assert [b.ref.short for b in snapshot.buttons] == ["button:0"]
    assert snapshot.buttons[0].text == "Go"
    assert form.field_refs == tuple(f.ref for f in snapshot.fields)
    assert form.button_refs == (snapshot.buttons[0].ref,)


def test_a_dangerous_link_scheme_raises_a_webglass_authored_warning() -> None:
    snapshot = _extract('<p><a href="javascript:alert(1)">click</a></p>')
    assert len(snapshot.security_warnings) == 1
    warning = snapshot.security_warnings[0]
    assert warning.code == "unsafe-link-scheme"
    # The untrusted href is quoted in `subject`, never inside the message.
    assert warning.subject == "javascript:alert(1)"
    assert "javascript:alert(1)" not in warning.message
    assert warning.ref == snapshot.links[0].ref


# --- determinism ------------------------------------------------------------


def test_extraction_is_byte_identical_across_runs() -> None:
    html = _fixture("boilerplate.html")
    first = json.dumps(_extract(html).to_dict(), sort_keys=True)
    second = json.dumps(_extract(html).to_dict(), sort_keys=True)
    assert first == second


def test_content_hash_is_stable_for_identical_input_and_differs_otherwise() -> None:
    a = _extract(_fixture("clean.html"))
    b = _extract(_fixture("clean.html"))
    c = _extract(_fixture("final.html"))
    assert a.content_hash == b.content_hash
    assert a.content_hash != c.content_hash


# --- selector-scoped extraction --------------------------------------------


def test_selector_extracts_the_agent_state_node_and_nothing_else() -> None:
    matches = extract_selector(_fixture("agent_state.html"), "#agent-state")
    assert len(matches) == 1
    match = matches[0]
    assert match.tag == "script"
    assert match.attributes["type"] == "application/json"
    assert json.loads(match.text) == {"lives": 3, "level": 1, "door": "locked"}
    assert "Agent State" not in match.text  # no page prose rides along


def test_selector_supports_the_tag_hash_id_form() -> None:
    matches = extract_selector(_fixture("agent_state.html"), "script#agent-state")
    assert len(matches) == 1
    assert json.loads(matches[0].text)["door"] == "locked"


def test_an_id_selector_matches_exactly_never_by_prefix() -> None:
    matches = extract_selector(_fixture("agent_state.html"), "#agent-state")
    assert matches[0].attributes["id"] == "agent-state"


def test_selector_supports_the_attribute_form() -> None:
    matches = extract_selector(_fixture("agent_state.html"), "[data-door=locked]")
    assert len(matches) == 1
    assert matches[0].tag == "div"
    assert matches[0].attributes["data-lives"] == "3"
    assert matches[0].text == ""


def test_selector_supports_quoted_attribute_values_and_bare_presence() -> None:
    html = '<p data-x="a b">one</p><p data-x="c">two</p>'
    assert [m.text for m in extract_selector(html, '[data-x="a b"]')] == ["one"]
    assert [m.text for m in extract_selector(html, "[data-x]")] == ["one", "two"]


def test_selector_supports_tag_and_class_forms_in_document_order() -> None:
    html = '<div class="note x">one</div><p class="note">two</p><p>three</p>'
    assert [m.text for m in extract_selector(html, ".note")] == ["one", "two"]
    assert [m.text for m in extract_selector(html, "p")] == ["two", "three"]
    assert [m.text for m in extract_selector(html, "p.note")] == ["two"]


def test_a_selector_matching_nothing_returns_an_empty_result_not_an_error() -> None:
    assert extract_selector(_fixture("clean.html"), "#nope") == ()


def test_an_unsupported_selector_fails_loudly_rather_than_matching_nothing() -> None:
    for unsupported in ("div p", "a, b", "div > p", "p:first-child", "*", ""):
        with pytest.raises(SelectorSyntaxError):
            extract_selector("<p>x</p>", unsupported)


def test_selector_extraction_is_deterministic() -> None:
    html = _fixture("agent_state.html")
    first = [m.to_dict() for m in extract_selector(html, "#agent-state")]
    second = [m.to_dict() for m in extract_selector(html, "#agent-state")]
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_selector_match_reports_the_selector_and_a_normalized_text() -> None:
    match = extract_selector("<p id='p'>  a\n  b  </p>", "#p")[0]
    assert match.selector == "#p"
    assert match.text == "a\n  b"
    assert match.normalized_text == "a b"


# --- tolerant recovery ------------------------------------------------------
#
# Real pages are malformed. Recovery must still be deterministic: flush what
# is pending, keep going, and never lose a block silently.


def test_unclosed_elements_still_yield_their_blocks() -> None:
    snapshot = _extract("<div><h1>Title<p>Body text")
    assert [(b.kind, b.text) for b in snapshot.blocks] == [
        (BlockKind.HEADING, "Title"),
        (BlockKind.PARAGRAPH, "Body text"),
    ]


def test_an_unclosed_list_is_still_emitted_as_a_list_block() -> None:
    snapshot = _extract("<ul><li>alpha<li>beta")
    assert [(b.kind, b.text) for b in snapshot.blocks] == [(BlockKind.LIST, "alpha\nbeta")]


def test_an_unclosed_table_is_still_emitted_as_a_table_block() -> None:
    snapshot = _extract("<table><tr><td>1<td>2")
    assert [(b.kind, b.text) for b in snapshot.blocks] == [(BlockKind.TABLE, "1 | 2")]


def test_nested_lists_and_tables_flatten_into_the_outer_block() -> None:
    nested_list = _extract("<ul><li>a<ul><li>a1</li></ul></li><li>b</li></ul>")
    assert [b.text for b in nested_list.blocks] == ["a\na1\nb"]
    nested_table = _extract("<table><tr><td>outer<table><tr><td>inner</td></tr></table></td></tr>")
    assert nested_table.blocks[0].kind is BlockKind.TABLE
    assert "inner" in nested_table.blocks[0].text


def test_an_anchor_without_an_href_is_not_a_link_but_its_text_survives() -> None:
    snapshot = _extract("<p>see <a>this</a> note</p>")
    assert snapshot.links == ()
    assert snapshot.blocks[0].text == "see this note"


def test_a_br_becomes_a_space_rather_than_gluing_words_together() -> None:
    assert _extract("<p>one<br>two</p>").blocks[0].text == "one two"


def test_a_submit_input_is_a_button_not_a_field() -> None:
    snapshot = _extract('<form><input type="submit" value="Send"></form>')
    assert snapshot.fields == ()
    assert [(b.text, b.button_type) for b in snapshot.buttons] == [("Send", "submit")]
    assert snapshot.forms[0].button_refs == (snapshot.buttons[0].ref,)


def test_a_textarea_is_a_field_and_its_default_text_stays_out_of_the_prose() -> None:
    snapshot = _extract("<p>before</p><textarea name='note'>draft</textarea>")
    assert [f.field_type for f in snapshot.fields] == ["textarea"]
    assert [b.text for b in snapshot.blocks] == ["before"]


def test_a_field_falls_back_to_aria_label_then_placeholder() -> None:
    html = (
        "<form>"
        "<input name='a' aria-label='Aria name'>"
        "<input name='b' placeholder='Type here'>"
        "<input name='c'>"
        "</form>"
    )
    assert [f.label for f in _extract(html).fields] == ["Aria name", "Type here", ""]


def test_controls_outside_a_form_have_no_form_ref() -> None:
    snapshot = _extract("<input name='loose'><button>Go</button>")
    assert snapshot.forms == ()
    assert snapshot.fields[0].form_ref is None
    assert snapshot.buttons[0].form_ref is None


def test_a_second_title_never_overwrites_the_first() -> None:
    snapshot = _extract("<title>First</title><title>Second</title><p>x</p>")
    assert snapshot.title == "First"


def test_caller_supplied_identity_wins_over_the_documents_own_claims() -> None:
    html = '<html lang="fr"><head><link rel="canonical" href="http://doc/canon"></head>'
    snapshot = _extract(html, language="en", canonical_url="http://caller/canon")
    assert snapshot.language == "en"
    assert snapshot.canonical_url == "http://caller/canon"


def test_caller_supplied_security_warnings_are_kept_alongside_extraction_findings() -> None:
    from webglass.pages import SecurityWarning

    supplied = SecurityWarning(code="tls-warning", message="certificate is self-signed")
    snapshot = _extract('<p><a href="data:text/html,x">x</a></p>', security_warnings=(supplied,))
    assert [w.code for w in snapshot.security_warnings] == ["tls-warning", "unsafe-link-scheme"]


def test_nested_non_content_elements_are_skipped_as_one_region() -> None:
    snapshot = _extract("<template><template>hidden</template></template><p>shown</p>")
    assert [b.text for b in snapshot.blocks] == ["shown"]
    assert [o.kind for o in snapshot.omissions] == [OmissionKind.NON_CONTENT]


def test_an_unclosed_anchor_is_finished_at_end_of_document() -> None:
    snapshot = _extract("<title>Late</title><p>prose <a href='/x'>link text")
    assert snapshot.title == "Late"
    assert [(ln.href, ln.text) for ln in snapshot.links] == [("/x", "link text")]
    assert snapshot.blocks[0].text == "prose link text"


def test_an_unclosed_label_still_labels_its_field() -> None:
    snapshot = _extract("<form><label for='q'>Query<input id='q' name='q'></form>")
    assert snapshot.fields[0].label == "Query"


def test_a_void_element_can_be_selected_and_reports_empty_content() -> None:
    matches = extract_selector("<p>x</p><img id='logo' alt='Logo'>", "#logo")
    assert [(m.tag, m.text, m.attributes["alt"]) for m in matches] == [("img", "", "Logo")]


def test_an_unclosed_matched_element_is_still_returned_at_end_of_document() -> None:
    assert [m.text for m in extract_selector("<div id='d'>tail text", "#d")] == ["tail text"]


def test_malformed_selector_fragments_each_fail_loudly() -> None:
    for unsupported in ("#", ".", "[data-x", "[]", "[=v]", "p#", "p.", "#id.", "p&"):
        with pytest.raises(SelectorSyntaxError):
            extract_selector("<p>x</p>", unsupported)


def test_a_selector_syntax_error_names_the_supported_forms() -> None:
    with pytest.raises(SelectorSyntaxError) as excinfo:
        extract_selector("<p>x</p>", "div p")
    assert "supported selector forms" in excinfo.value.remediation
