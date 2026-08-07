"""The M0-M2 definition-of-done sweep (build plan task t16).

Issue #1 section 20 ("Definition of done for the first useful release") lists
thirteen bullets. This file walks that checklist **restricted to the M0-M2
slice this repo actually ships** and points each item at a passing,
mechanical check — never a narrative claim. Four bullets name capabilities
this slice explicitly does not build (the durable evidence store, exploration
resume, web-memory reuse, and a Colleague library provider); each of those
gets a real, collected ``@pytest.mark.skip`` naming issue #8 as the declared
deferral, so the gap is visible in every test run rather than silent.

Two ways a check "delegates", both used below (spec honesty h18/c20's ban on
a WebGlass-side assertion DSL applies to the *product*, not to this
meta-test file, which is allowed to use pytest and subprocess like any other
test):

* **Direct exercise via fakes** — import an existing, self-contained test
  function (no pytest fixture parameters) from its home module and call it
  directly. This both proves the function still exists (an import error
  fails loudly) and proves it still passes (calling it re-runs its
  assertions) in the same statement.
* **Delegate to a dedicated test file/node** — spawn a fresh, isolated
  ``pytest`` subprocess against a specific file or node id and assert it
  exits ``0``. Used where the dedicated coverage needs pytest fixtures
  (parametrization, ``tmp_path``, ``capsys``) this file has no business
  reimplementing. Browser-dependent delegates target files that already gate
  themselves on ``WEBGLASS_TEST_BROWSER``/``WEBGLASS_TEST_ALLOW_NO_SANDBOX``
  (see ``tests/test_playwright_adapter.py``'s module docstring); this file's
  own browser-dependent items additionally skip *themselves* under the same
  two variables, so a run with no browser opt-in reports them as SKIPPED —
  not silently folded into a green PASS — exactly mirroring the rest of the
  suite's gating story::

      WEBGLASS_TEST_BROWSER=1 WEBGLASS_TEST_ALLOW_NO_SANDBOX=1 \\
          uv run pytest tests/test_definition_of_done.py -v

DoD checklist item -> mechanical check
=======================================

Each line is "issue #1 section 20 item -> this file's test(s)"::

    1.  search/open/read/inspect/follow/screenshot, stable structured
        operations -> test_dod_01_*
    2.  compact page card -> progressive disclosure w/o re-fetch
        -> test_dod_02_*
    3.  Chromium via a replaceable Playwright adapter -> test_dod_03_*
        (import-boundary half always runs; live-Chromium half is
        browser-gated)
    4.  content is token-budgeted, chunked, omissions declared
        -> test_dod_04_*
    5.  URL/redirect/network/resource policy enforced -> test_dod_05_*
    6.  sessions isolated and cleaned up safely -> test_dod_06_*
        (fakes half always runs; cross-process half is browser-gated)
    7.  observations produce inspectable evidence -> DEFERRED, issue #8
        (the durable Evidence store); test_dod_07_evidence_store_*
    8.  context reports every budget dimension -> test_dod_08_*
    9.  prior pages found/compared/reused (memory reuse) -> DEFERRED,
        issue #8; test_dod_09_memory_reuse_*
    10. explorations resume without a live browser -> DEFERRED, issue #8;
        test_dod_10_exploration_*
    11. Colleague imports the provider w/o subprocess glue -> DEFERRED,
        issue #8 (M4); test_dod_11_colleague_*
    12. CLI and library behavior are contract-tested -> test_dod_12_*
    13. auth browsing / remote business actions stay unavailable unless
        explicitly enabled -> test_dod_13_*

Success signals -> mechanical check
====================================

The spec's "Success signals" section names six things (plus the seven issue
#9 acceptance criteria folded into one of them); each maps to a concrete,
CI-visible mechanism — none of them requires a human to eyeball anything::

    1. characterization + product suites green in CI, no live public
       website -> tests.yml's `test` job (`uv run pytest`), backed by
       tests/conftest.py's local-only fixture server
    2. the teken rubric gate passes with every new noun -> tests.yml's
       `lint` job running `uv run teken cli doctor . --strict`
    3. a CI job drives a local app through webglass, asserts on JSON and
       exit codes -> .github/workflows/example-webapp-test.yml, this
       repo's own CI running docs/ci-recipe.md's recipe for real;
       test_dod_audience_ci_recipe_* asserts both artifacts exist
    4. the seven issue #9 acceptance criteria pass as fixture-based M2
       tests -> the acceptance table in docs/ci-recipe.md, each row
       citing its live test in tests/test_verbs_live.py /
       tests/test_session_persistence.py
    5. every result declares live-vs-cached and omissions ->
       test_dod_04_* (omissions) + tests/test_service.py's cache-mode
       tests
    6. policy denies loopback by default yet allows the declared app
       under test -> test_dod_05_*

Audience entry paths (spec honesty h19/c22)
============================================

``test_dod_audience_*`` below assert all three of: a test composing
``WebGlassService`` directly (library import), the ``webglass`` console
script (``[project.scripts]``), and the CI recipe (``docs/ci-recipe.md`` +
``.github/workflows/example-webapp-test.yml``) all exist as real artifacts
in this repo.
"""

from __future__ import annotations

import os
import subprocess  # nosec B404 - fixed argv, no shell; mirrors tests/test_verbs_live.py
import sys
from pathlib import Path

import pytest

from webglass.effects import EFFECT_CLASS_BY_KIND, EffectClass, OperationKind
from webglass.service import WebGlassService

_REPO_ROOT = Path(__file__).resolve().parent.parent

BROWSER_ENV = "WEBGLASS_TEST_BROWSER"
NO_SANDBOX_ENV = "WEBGLASS_TEST_ALLOW_NO_SANDBOX"
_BROWSER_ENABLED = os.environ.get(BROWSER_ENV) == "1"
_NO_SANDBOX_ALLOWED = os.environ.get(NO_SANDBOX_ENV) == "1"

requires_browser_tier = pytest.mark.skipif(
    not (_BROWSER_ENABLED and _NO_SANDBOX_ALLOWED),
    reason=(
        f"this DoD item's live-Chromium confirmation is opt-in: set {BROWSER_ENV}=1 and "
        f"{NO_SANDBOX_ENV}=1 (see tests/test_playwright_adapter.py's module docstring) — "
        "the item's non-browser half (a separate test, always collected) already covers "
        "the claim's fakes-backed half"
    ),
)


def _run_pytest(*node_ids: str, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    """Run a subset of the suite as a fresh, isolated pytest process.

    A nested interpreter, not ``pytest.main()`` in-process — so this file's
    own capture/plugin state can never leak into (or be corrupted by) the
    thing it is checking. The same idea, one level up, as the CLI-subprocess
    pattern ``tests/test_verbs_live.py``/``tests/test_session_persistence.py``
    already use. Inherits this process's environment unmodified, so a
    browser-dependent node id sees the same ``WEBGLASS_TEST_BROWSER``/
    ``WEBGLASS_TEST_ALLOW_NO_SANDBOX`` this file itself was invoked with.
    """
    return subprocess.run(  # nosec B603 - fixed argv, no shell
        [sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider", *node_ids],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _assert_delegate_passes(*node_ids: str, timeout: int = 600) -> None:
    result = _run_pytest(*node_ids, timeout=timeout)
    assert result.returncode == 0, (
        f"DoD delegate check failed for {node_ids} (exit {result.returncode}):\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


# ===========================================================================
# 1. search / open / read / inspect / follow / screenshot as stable
#    structured operations
# ===========================================================================


def test_dod_01_search_open_read_inspect_follow_screenshot() -> None:
    from tests.test_service import (
        test_action_follow_resolves_a_link_ref_and_navigates_to_it,
        test_lenses_project_the_retained_snapshot_without_re_fetching,
        test_page_open_returns_a_page_card_and_retains_the_snapshot,
        test_page_screenshot_stores_a_decodable_artifact,
        test_search_returns_a_fully_populated_structured_result,
    )

    test_search_returns_a_fully_populated_structured_result()
    test_page_open_returns_a_page_card_and_retains_the_snapshot()
    # read/inspect/links are all lenses over one retained snapshot.
    test_lenses_project_the_retained_snapshot_without_re_fetching()
    test_action_follow_resolves_a_link_ref_and_navigates_to_it()
    test_page_screenshot_stores_a_decodable_artifact()


# ===========================================================================
# 2. compact page card -> progressive disclosure without re-fetch
# ===========================================================================


def test_dod_02_progressive_disclosure_without_refetch() -> None:
    from tests.test_references import (
        test_a_ref_from_another_snapshot_is_stale_never_silently_resolved,
    )
    from tests.test_service import (
        test_lenses_project_the_retained_snapshot_without_re_fetching,
        test_page_open_returns_a_page_card_and_retains_the_snapshot,
        test_page_read_declares_omissions_and_resumes_from_its_cursor,
    )

    test_page_open_returns_a_page_card_and_retains_the_snapshot()
    test_lenses_project_the_retained_snapshot_without_re_fetching()
    test_page_read_declares_omissions_and_resumes_from_its_cursor()
    # Stable references are only meaningful if a stale one fails clearly
    # rather than silently resolving against the wrong snapshot generation.
    test_a_ref_from_another_snapshot_is_stale_never_silently_resolved()


# ===========================================================================
# 3. Chromium runs through a replaceable Playwright adapter
# ===========================================================================


def test_dod_03_playwright_is_confined_to_its_own_adapter_module() -> None:
    """ "Replaceable" half: Playwright never leaks past its protocol seam."""
    from tests.test_import_boundaries import (
        test_no_playwright_import_outside_the_adapter_module,
        test_playwright_is_the_only_runtime_dependency,
        test_the_playwright_adapter_module_exists_and_does_import_playwright,
    )

    test_no_playwright_import_outside_the_adapter_module()
    test_the_playwright_adapter_module_exists_and_does_import_playwright()
    test_playwright_is_the_only_runtime_dependency()


@requires_browser_tier
def test_dod_03_live_chromium_confirms_the_adapter_actually_drives_a_browser() -> None:
    """ "Runs" half: delegate to the adapter's own live-Chromium suite."""
    _assert_delegate_passes("tests/test_playwright_adapter.py")


# ===========================================================================
# 4. content is token-budgeted, chunked, and explicit about omissions
# ===========================================================================


def test_dod_04_token_budgets_chunking_and_declared_omissions() -> None:
    from tests.test_service import (
        test_a_response_over_the_operation_limit_is_blocked,
        test_non_content_omissions_are_counted_and_declared,
        test_page_read_declares_omissions_and_resumes_from_its_cursor,
    )
    from tests.test_service_budgets import (
        test_every_result_carries_the_budget_state,
        test_the_token_dimension_is_always_reported_as_a_labeled_estimate,
    )

    test_page_read_declares_omissions_and_resumes_from_its_cursor()
    test_non_content_omissions_are_counted_and_declared()
    test_a_response_over_the_operation_limit_is_blocked()
    test_the_token_dimension_is_always_reported_as_a_labeled_estimate()
    test_every_result_carries_the_budget_state()
    # The full budget-dimension matrix (requests/bytes/browser-time/artifact
    # bytes/tokens x reserve/charge/exhaust/timeout/cancel) needs pytest
    # fixtures this file has no business reimplementing.
    _assert_delegate_passes("tests/test_service_budgets.py", timeout=120)


# ===========================================================================
# 5. URL, redirect, network, and resource policy is enforced
# ===========================================================================


def test_dod_05_url_redirect_network_and_resource_policy_enforced() -> None:
    from tests.test_service import (
        test_a_redirect_chain_over_the_operation_limit_is_blocked,
        test_a_response_over_the_operation_limit_is_blocked,
        test_page_open_denies_a_loopback_target_without_touching_the_backend,
        test_service_revalidates_every_redirect_hop_even_when_the_adapter_did_not,
    )

    test_page_open_denies_a_loopback_target_without_touching_the_backend()
    test_service_revalidates_every_redirect_hop_even_when_the_adapter_did_not()
    test_a_redirect_chain_over_the_operation_limit_is_blocked()
    test_a_response_over_the_operation_limit_is_blocked()
    # The scheme/target denylist property suite (hypothesis-driven, many
    # fixture parametrizations) lives in tests/test_policy.py +
    # tests/test_policy_properties.py; delegate rather than reimplement it.
    _assert_delegate_passes("tests/test_policy.py", "tests/test_policy_properties.py", timeout=120)


# ===========================================================================
# 6. sessions are isolated and cleaned up safely
# ===========================================================================


def test_dod_06_sessions_isolated_and_cleaned_up_via_fakes() -> None:
    from tests.test_service import (
        test_a_session_belonging_to_another_caller_is_denied_without_naming_them,
        test_a_session_endpoint_ref_never_reaches_a_result,
        test_session_lifecycle_create_list_show_close_clean,
        test_two_tasks_of_one_caller_cannot_share_a_live_session,
    )

    test_session_lifecycle_create_list_show_close_clean()
    test_two_tasks_of_one_caller_cannot_share_a_live_session()
    test_a_session_belonging_to_another_caller_is_denied_without_naming_them()
    test_a_session_endpoint_ref_never_reaches_a_result()


@requires_browser_tier
def test_dod_06_live_cross_process_session_isolation_and_cleanup() -> None:
    """Real leases, real reaped browser processes, across separate CLI
    invocations — spec claim c33/honesty h30. Delegate: process-level
    lease/reap assertions need real pids and real subprocess boundaries."""
    _assert_delegate_passes("tests/test_session_persistence.py")


# ===========================================================================
# 7. observations produce inspectable evidence -- DEFERRED (issue #8)
# ===========================================================================


@pytest.mark.skip(
    reason=(
        "deferred to M3 per issue #8: the durable, append-only Evidence store "
        "(CLAUDE.md 'Target architecture' section 2 — requested/final URL, redirect "
        "chain, retrieval time, block refs, content/artifact hashes, backend/policy "
        "context) does not exist yet. webglass/evidence.py is not even a stub module "
        "today (contrast webglass/exploration.py and webglass/memory.py, which are). "
        "The M0-M2 slice does surface *observation-level* evidence — console/page-error "
        "text (test_dod_01/tests/test_verbs_live.py), policy verdicts, and screenshot "
        "artifacts — but not a citable, hash-addressed, append-only record of them."
    )
)
def test_dod_07_evidence_store_deferred_to_m3() -> None:
    pass


# ===========================================================================
# 8. each context reports request/byte/browser-time/artifact/token budgets
# ===========================================================================


def test_dod_08_context_reports_every_budget_dimension() -> None:
    from tests.test_service import test_result_always_carries_budget_policy_and_cache_blocks
    from tests.test_service_budgets import (
        test_limits_cover_every_declared_dimension,
        test_limits_read_the_four_dimensions_a_web_context_carries,
    )

    test_result_always_carries_budget_policy_and_cache_blocks()
    test_limits_read_the_four_dimensions_a_web_context_carries()
    test_limits_cover_every_declared_dimension()


# ===========================================================================
# 9. prior pages can be found, compared, and intentionally reused
#    (web-memory reuse) -- DEFERRED (issue #8)
# ===========================================================================


@pytest.mark.skip(
    reason=(
        "deferred to M3 per issue #8: webglass/memory.py is a typed MemoryStore "
        "*interface stub only* (see that module's own docstring) — find/show/forget/"
        "compact raise nothing because nothing is implemented behind them. No page "
        "is 'found, compared, or reused' from a prior observation at M0-M2; every "
        "page open re-fetches from the backend."
    )
)
def test_dod_09_memory_reuse_deferred_to_m3() -> None:
    pass


# ===========================================================================
# 10. explorations can be resumed without preserving a live browser forever
#     -- DEFERRED (issue #8)
# ===========================================================================


@pytest.mark.skip(
    reason=(
        "deferred to M3 per issue #8: webglass/exploration.py is a typed "
        "ExplorationStore *interface stub only* (see that module's own docstring) — "
        "record_edge/get_graph/resume raise nothing because nothing is implemented "
        "behind them. WebContext carries an exploration_id reference today, but "
        "nothing populates or resumes a traversal graph from it."
    )
)
def test_dod_10_exploration_resume_deferred_to_m3() -> None:
    pass


# ===========================================================================
# 11. Colleague can import the provider without subprocess glue
#     -- DEFERRED (issue #8)
# ===========================================================================


@pytest.mark.skip(
    reason=(
        "deferred to M4 per issue #8: no colleague-facing library provider exists in "
        "this repo (it lands in the colleague repo by composition, CLAUDE.md 'Target "
        "architecture' section 10). This is a confirmed, scope-relieved deferral, not "
        "an oversight: issue #9's own colleague brief states colleague consumes "
        "webglass strictly as an operator-installed CLI subprocess for the #387 proof "
        "and explicitly needs 'No Python API' at M2 (issue #9, 'What colleague does "
        "NOT need') — so subprocess glue is exactly what the first real consumer "
        "asked for, not a gap against its own request."
    )
)
def test_dod_11_colleague_library_provider_deferred_to_m4() -> None:
    pass


# ===========================================================================
# 12. CLI and library behavior are contract-tested
# ===========================================================================


def test_dod_12_cli_and_library_behavior_are_contract_tested() -> None:
    """The parametrized suite: CLI --json payload == library result.to_dict()
    for every M0-M2 verb kind, byte for byte after JSON round-tripping."""
    _assert_delegate_passes(
        "tests/test_cli_webverbs.py::test_cli_json_matches_library_result", timeout=60
    )


# ===========================================================================
# 13. authenticated browsing and remote business actions stay unavailable
#     unless explicitly designed and enabled
# ===========================================================================


def test_dod_13_no_auth_and_no_unbounded_remote_actions() -> None:
    from tests.test_service import (
        test_action_press_previews_by_default_and_never_reaches_the_backend,
        test_apply_on_a_remote_action_is_denied_until_the_m5_protocol_exists,
    )

    test_action_press_previews_by_default_and_never_reaches_the_backend()
    test_apply_on_a_remote_action_is_denied_until_the_m5_protocol_exists()

    # The operation surface itself is the other half of the claim: it is not
    # merely that auth/fill/submit/upload/download verbs are policy-gated —
    # they are structurally absent from OperationKind, so there is nothing
    # for a permissive policy to even authorize. Every kind that does exist
    # declares exactly one effect class, and action.press (the one verb that
    # can execute on the open web at all beyond pure observation) classifies
    # remote-action, previewing by default (spec decision 2026-08-07,
    # resolving q4 — a declared test profile is the sole, explicit override).
    m0_m2_kinds = {
        "search",
        "page.open",
        "page.read",
        "page.inspect",
        "page.extract",
        "page.links",
        "page.screenshot",
        "action.follow",
        "action.press",
        "session.create",
        "session.list",
        "session.show",
        "session.close",
        "session.clean",
    }
    assert {kind.value for kind in OperationKind} == m0_m2_kinds, (
        "an auth, fill, submit, upload, or download kind appeared in the M0-M2 "
        "operation surface — that capability is explicitly out of scope until a "
        "separately reviewed milestone (M5/M6)"
    )
    assert EFFECT_CLASS_BY_KIND[OperationKind.ACTION_PRESS] is EffectClass.REMOTE_ACTION


# ===========================================================================
# Audience entry paths (spec honesty h19/c22): library import, console
# script, CI recipe -- all three demonstrated as real artifacts in this repo.
# ===========================================================================


def test_dod_audience_library_import_entry_path_exists() -> None:
    """A test composing WebGlassService directly, with no CLI involved."""
    from tests.test_service import make_context, make_service

    service = make_service()
    assert isinstance(service, WebGlassService)
    # And it is a real library call, not a stub: it executes an operation.
    from webglass.effects import OperationKind as _Kind
    from webglass.operations import WebOperation

    result = service.execute(
        WebOperation(operation_id="dod-lib-import", kind=_Kind.SEARCH), make_context()
    )
    assert result.to_dict()["operation_id"] == "dod-lib-import"

    source = (_REPO_ROOT / "tests" / "test_service.py").read_text(encoding="utf-8")
    assert "WebGlassService(" in source, (
        "tests/test_service.py should compose WebGlassService directly — the "
        "library-import audience entry path this DoD item cites"
    )


def test_dod_audience_console_script_entry_path_exists() -> None:
    """``[project.scripts]`` binds ``webglass`` — the agent/human CLI path."""
    from tests.test_characterization import (
        test_console_script_entry_point_is_webglass_not_webglass_cli,
    )

    test_console_script_entry_point_is_webglass_not_webglass_cli()


def test_dod_audience_ci_recipe_entry_path_exists() -> None:
    """The documented recipe and the real workflow that runs it."""
    recipe = _REPO_ROOT / "docs" / "ci-recipe.md"
    workflow = _REPO_ROOT / ".github" / "workflows" / "example-webapp-test.yml"
    assert recipe.is_file(), "docs/ci-recipe.md is missing"
    assert workflow.is_file(), ".github/workflows/example-webapp-test.yml is missing"

    recipe_text = recipe.read_text(encoding="utf-8")
    workflow_text = workflow.read_text(encoding="utf-8")

    # Not stub content: the recipe covers every verb issue #9's acceptance
    # criteria named, and the workflow actually runs webglass, not just
    # describes it.
    for marker in ("session create", "page open", "page extract", "page screenshot", "jq -e"):
        assert marker in recipe_text, f"docs/ci-recipe.md is missing {marker!r}"
    for marker in ("webglass session create", "webglass page open", "jq -e", "policy-profile"):
        assert (
            marker in workflow_text
        ), f".github/workflows/example-webapp-test.yml is missing {marker!r}"

    fixture_dir = _REPO_ROOT / "docs" / "examples" / "ci-recipe-app"
    assert (fixture_dir / "index.html").is_file()
    assert "agent-state" in (fixture_dir / "index.html").read_text(encoding="utf-8")
