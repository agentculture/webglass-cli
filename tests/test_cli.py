"""Smoke tests for the webglass-cli CLI entry point and its verbs."""

from __future__ import annotations

import json

import pytest

from webglass import __version__
from webglass.cli import main
from webglass.cli._commands.learn import _as_json_payload
from webglass.explain import known_paths


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_no_args_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main([])
    assert rc == 0
    assert "usage: webglass-cli" in capsys.readouterr().out


def test_unknown_command_errors(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["bogus"])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err


# --- whoami ---------------------------------------------------------------


def test_whoami_text(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["whoami"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "nick: webglass-cli" in out
    assert "backend: colleague" in out
    assert "model:" in out


def test_whoami_json(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["whoami", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["nick"] == "webglass-cli"
    assert payload["version"] == __version__
    assert payload["backend"] == "colleague"


# --- learn ----------------------------------------------------------------


def test_learn_text(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["learn"])
    assert rc == 0
    out = capsys.readouterr().out
    assert len(out) >= 200
    assert "webglass-cli" in out
    assert "Exit-code policy" in out
    assert "--json" in out
    assert "explain" in out


def test_learn_json(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["learn", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["tool"] == "webglass-cli"
    assert payload["version"] == __version__
    assert payload["json_support"] is True


def test_learn_json_declares_the_shipped_and_deferred_status_honestly() -> None:
    """A JSON consumer must not infer capabilities that are not built yet —
    nor be told a capability is missing once it ships.

    The text body carries a Status section; the JSON payload has to say the
    same thing in a machine-readable way. As of the M0-M2 status pass,
    search/page/action/session are real, so `status` must not claim
    "pre-implementation" (that would tell an agent to distrust a live
    capability); it must still name what genuinely is not built yet
    (evidence/exploration/memory/policy/operation, tracked in issue #8).
    """
    payload = _as_json_payload()
    assert payload["status"] != "pre-implementation"
    assert "not built" in payload["status_detail"]
    assert "issues/8" in payload["status_detail"]
    assert "pre-implementation" not in payload["purpose"]


def test_learn_examples_use_the_real_console_script(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`learn` must print copy-pasteable commands.

    `[project.scripts]` binds `webglass`; `webglass-cli` is the distribution
    name and is not an invocable binary, so a command map using it sends an
    agent straight to "command not found".
    """
    rc = main(["learn"])
    assert rc == 0
    out = capsys.readouterr().out
    for verb in ("whoami", "learn", "explain", "overview", "doctor"):
        assert f"webglass-cli {verb}" not in out, f"learn prints a non-existent binary for {verb}"
    assert "webglass whoami" in out


# --- explain --------------------------------------------------------------


def test_explain_root(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["explain"])
    assert rc == 0
    assert "# webglass-cli" in capsys.readouterr().out


def test_explain_self(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["explain", "webglass-cli"])
    assert rc == 0
    assert capsys.readouterr().out.startswith("#")


def test_explain_import_package_name(capsys: pytest.CaptureFixture[str]) -> None:
    # The agent-first rubric's `explain_self` probes the import-package name
    # (`webglass`), not the dist name (`webglass-cli`). Both must resolve.
    rc = main(["explain", "webglass"])
    assert rc == 0
    assert capsys.readouterr().out.startswith("#")


def test_explain_json(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["explain", "whoami", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["path"] == ["whoami"]
    assert "webglass whoami" in payload["markdown"]


def test_explain_unknown_path_errors(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["explain", "nonexistent"])
    assert rc == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("error:")
    assert "hint:" in captured.err


def test_catalog_examples_use_the_real_console_script() -> None:
    """No explain entry may print `webglass-cli <verb>` as a runnable command.

    Guards the whole catalog, not just the root: the wrong executable name was
    originally present in every entry's Usage block.
    """
    from webglass.explain.catalog import ENTRIES

    for path, body in ENTRIES.items():
        for verb in ("whoami", "learn", "explain", "overview", "doctor", "cli"):
            assert f"webglass-cli {verb}" not in body, (
                f"explain entry {path or '<root>'} prints a non-existent binary "
                f"'webglass-cli {verb}' — the console script is 'webglass'"
            )


def test_every_catalog_path_resolves(capsys: pytest.CaptureFixture[str]) -> None:
    for path in known_paths():
        rc = main(["explain", *path])
        assert rc == 0, f"explain {' '.join(path)} failed"
        capsys.readouterr()


def test_session_contract_wording_is_consistent(capsys: pytest.CaptureFixture[str]) -> None:
    """The throwaway/reuse session contract must read the same everywhere.

    Four user-facing surfaces describe it in prose: ``page``'s module
    docstring, ``page overview``'s "Naming the page" section, ``explain page
    open``, and ``session overview``'s Persistence section (build plan t15,
    issue #14). Before t15 they had drifted — one surface described
    flow-scoped reuse (build plan t13) while the other three still described
    only the pre-t13 throwaway-only posture, and even the surfaces that did
    agree on the default case used different wording for it.

    Rather than re-reading four hand-written paragraphs for agreement every
    time one changes, all four render the same three canonical fragments
    from :mod:`webglass.cli._session_wording`. This test checks all three
    fragments survive in all four — the same mechanical-guard shape as
    ``test_every_catalog_path_resolves`` above, which guards the catalog
    itself rather than trusting that every entry was kept up to date by eye.
    """
    import webglass.cli._commands.page as page_module
    from webglass.cli._session_wording import (
        DEFAULT_EPHEMERAL_CLAIM,
        FLOW_REUSE_CLAIM,
        FRESH_SESSION_OPT_OUT_CLAIM,
        SWEEP_DISCLOSURE_CLAIM,
    )

    claims = (
        DEFAULT_EPHEMERAL_CLAIM,
        FLOW_REUSE_CLAIM,
        FRESH_SESSION_OPT_OUT_CLAIM,
        SWEEP_DISCLOSURE_CLAIM,
    )

    def _normalized(text: str) -> str:
        # Each surface wraps the same plain-text claims in whatever inline
        # styling its own format uses: RST double-backticks and *emphasis*
        # in the page module's docstring, Markdown single backticks in the
        # explain catalog, and no markup at all in CLI overview text. Strip
        # backtick/asterisk markup before comparing, so this checks the
        # words rather than the formatting.
        #
        # Whitespace is collapsed for the same reason: every surface hard-wraps
        # to its own width, so a claim longer than one line arrives split by a
        # newline and indentation in one surface and not in another. Without
        # this, the guard would silently depend on each claim being short
        # enough never to wrap — which is a property of the line width, not of
        # the wording it is supposed to be checking.
        return " ".join(text.replace("`", "").replace("*", "").split())

    assert page_module.__doc__ is not None
    surfaces: dict[str, str] = {"page module docstring": _normalized(page_module.__doc__)}

    rc = main(["page", "overview"])
    assert rc == 0
    surfaces["page overview"] = _normalized(capsys.readouterr().out)

    rc = main(["explain", "page", "open"])
    assert rc == 0
    surfaces["explain page open"] = _normalized(capsys.readouterr().out)

    rc = main(["session", "overview"])
    assert rc == 0
    surfaces["session overview"] = _normalized(capsys.readouterr().out)

    assert len(surfaces) == 4
    for name, text in surfaces.items():
        for claim in claims:
            assert claim in text, f"{name!r} is missing the shared claim {claim!r}"


def test_session_owner_env_name_matches_the_factory() -> None:
    """The duplicated ``SESSION_OWNER_ENV`` names cannot drift apart.

    ``webglass.cli._session_wording`` deliberately re-declares this constant
    rather than importing it, so that ``webglass.explain.catalog`` — loaded on
    every ``explain`` invocation — does not pull in ``_factory``'s adapter and
    service import graph. That duplication is the right trade, but its own
    docstring notes nothing checked the two agreed. This is that check: a
    renamed environment variable would otherwise leave ``explain`` telling
    callers to set a name the code no longer reads.
    """
    from webglass.cli import _factory, _session_wording

    assert _session_wording.SESSION_OWNER_ENV == _factory.SESSION_OWNER_ENV
