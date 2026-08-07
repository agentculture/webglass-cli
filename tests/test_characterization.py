"""Characterization tests locking the CLI skeleton's cross-cutting contracts.

These tests *extend* the existing coverage in ``tests/test_cli.py`` and
``tests/test_cli_introspection.py`` — they must not duplicate assertions those
files already make. New ground covered here: stdout/stderr never mix (checked
on both streams, not just the one under test) across success and error paths
in text and ``--json`` mode; the exit-code policy including the argparse
SystemExit-vs-handler-return-value asymmetry; the exact ``--json`` error shape
``{code, message, remediation}`` on both the argparse-error path and the
handler-raised ``CliError`` path; that no Python traceback ever reaches stderr
even when a handler raises an unexpected exception; the explain catalog's
module-level contract (not just CLI-level resolution); ``_output.py`` and
``_errors.py`` unit-level behavior; and the ``whoami``/``doctor`` identity
parsing failure branches the happy-path CLI tests never exercise (this repo's
own ``culture.yaml`` is always well-formed, so branches like "unknown
backend" or "missing prompt file" are otherwise dead code as far as tests go).

Every test here characterizes what *already exists* — it must pass unmodified
against the current, unmodified code.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from importlib import metadata as importlib_metadata

import pytest

from webglass import __version__
from webglass.cli import main
from webglass.cli._commands import doctor as doctor_cmd
from webglass.cli._commands import whoami as whoami_cmd
from webglass.cli._errors import EXIT_ENV_ERROR, EXIT_SUCCESS, EXIT_USER_ERROR, CliError
from webglass.cli._output import emit_diagnostic, emit_error, emit_result
from webglass.explain import known_paths, resolve
from webglass.explain.catalog import ENTRIES


def _run(argv: list[str]) -> int:
    """Run ``main(argv)``, normalizing the SystemExit/return-value split to an int.

    See ``test_argparse_level_errors_raise_systemexit_but_handler_errors_return``
    for why the two failure origins behave differently at the Python level.
    """
    try:
        return main(argv)
    except SystemExit as exc:
        code = exc.code
        return code if isinstance(code, int) else 1


# --- stdout / stderr split ---------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["whoami"],
        ["whoami", "--json"],
        ["learn"],
        ["learn", "--json"],
        ["overview"],
        ["overview", "--json"],
        ["explain"],
        ["explain", "--json"],
        ["cli"],
        ["cli", "overview"],
        ["cli", "overview", "--json"],
    ],
)
def test_success_paths_write_only_to_stdout(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(argv)
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out != ""


@pytest.mark.parametrize(
    "argv",
    [
        ["explain", "nonexistent"],
        ["explain", "nonexistent", "--json"],
    ],
)
def test_handler_raised_errors_write_only_to_stderr(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(argv)
    assert rc == EXIT_USER_ERROR
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err != ""


@pytest.mark.parametrize(
    "argv",
    [
        ["bogus"],
        ["bogus", "--json"],
        ["cli", "overview", "--bogus"],
        ["cli", "overview", "--bogus", "--json"],
    ],
)
def test_argparse_level_errors_write_only_to_stderr(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == EXIT_USER_ERROR
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err != ""


# --- exit code policy, including the SystemExit-vs-return asymmetry ---------


def test_argparse_level_errors_raise_systemexit_but_handler_errors_return(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Two different failure origins use two different Python-level mechanisms.

    A parse-time failure (unrecognized subcommand) fires *before* any handler
    runs: ``_CliArgumentParser.error()`` calls ``emit_error`` and then raises
    ``SystemExit`` directly, bypassing ``_dispatch``. A handler-raised
    ``CliError`` (e.g. an unknown ``explain`` path) instead flows through
    ``_dispatch``, which *catches* it and returns a plain ``int`` — no
    ``SystemExit`` involved. Both end up as process exit code 1 once wrapped
    by ``sys.exit(main())`` at the console-script / ``__main__`` entry points,
    but a caller invoking ``main()`` directly as a library function (as these
    tests do) must handle both shapes.
    """
    with pytest.raises(SystemExit) as exc:
        main(["bogus"])
    assert exc.value.code == EXIT_USER_ERROR
    capsys.readouterr()

    rc = main(["explain", "nonexistent"])
    assert rc == EXIT_USER_ERROR
    capsys.readouterr()


def test_exit_code_constants_match_the_documented_policy() -> None:
    assert EXIT_SUCCESS == 0
    assert EXIT_USER_ERROR == 1
    assert EXIT_ENV_ERROR == 2


# --- --json error shape: exactly {code, message, remediation} ---------------


@pytest.mark.parametrize(
    "argv",
    [
        ["explain", "nonexistent", "--json"],
        ["bogus", "--json"],
        ["cli", "overview", "--bogus", "--json"],
    ],
)
def test_json_error_shape_is_exactly_code_message_remediation(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    rc = _run(argv)
    assert rc == EXIT_USER_ERROR
    payload = json.loads(capsys.readouterr().err)
    assert set(payload) == {"code", "message", "remediation"}
    assert payload["code"] == EXIT_USER_ERROR
    assert isinstance(payload["message"], str)
    assert payload["message"]
    assert isinstance(payload["remediation"], str)


def test_json_hint_does_not_leak_between_main_invocations(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``_CliArgumentParser._json_hint`` is a class attribute set fresh by every
    ``main()`` call from a raw-argv scan (``_argv_has_json``). If a regression
    made it sticky (set but never reset), a later --json-less call would
    wrongly render its argparse error as JSON — or vice versa.
    """
    with pytest.raises(SystemExit):
        main(["bogus", "--json"])
    first_err = capsys.readouterr().err
    json.loads(first_err)  # must be valid JSON

    with pytest.raises(SystemExit):
        main(["bogus"])
    second_err = capsys.readouterr().err
    assert second_err.startswith("error:")
    with pytest.raises(json.JSONDecodeError):
        json.loads(second_err)


# --- no Python traceback ever reaches stderr ---------------------------------


def test_unexpected_exception_in_a_handler_never_leaks_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _boom(_args: object) -> None:
        raise RuntimeError("boom from a bug, not a CliError")

    monkeypatch.setattr(whoami_cmd, "cmd_whoami", _boom)
    rc = main(["whoami"])
    assert rc == EXIT_USER_ERROR
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Traceback" not in captured.err
    lines = captured.err.splitlines()
    assert lines[0] == "error: unexpected: RuntimeError: boom from a bug, not a CliError"
    assert lines[1].startswith("hint: file a bug at")


def test_unexpected_exception_in_json_mode_stays_structured(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _boom(_args: object) -> None:
        raise ValueError("nope")

    monkeypatch.setattr(whoami_cmd, "cmd_whoami", _boom)
    rc = main(["whoami", "--json"])
    assert rc == EXIT_USER_ERROR
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Traceback" not in captured.err
    payload = json.loads(captured.err)
    assert payload["code"] == EXIT_USER_ERROR
    assert payload["message"] == "unexpected: ValueError: nope"
    assert "file a bug at" in payload["remediation"]


# --- explain catalog, module-level (not just through the CLI) ---------------


def test_known_paths_matches_catalog_entries_exactly() -> None:
    assert set(known_paths()) == set(ENTRIES.keys())
    assert len(known_paths()) == len(ENTRIES)


def test_root_resolves_identically_for_empty_dist_and_import_names() -> None:
    assert resolve(()) == resolve(("webglass-cli",)) == resolve(("webglass",))


def test_resolve_unknown_path_raises_cli_error_with_remediation() -> None:
    with pytest.raises(CliError) as exc:
        resolve(("nope", "not-a-verb"))
    err = exc.value
    assert err.code == EXIT_USER_ERROR
    assert "nope not-a-verb" in err.message
    assert err.remediation == "list entries with: webglass-cli explain webglass-cli"


def test_every_catalog_entry_is_nonempty_markdown() -> None:
    for path, body in ENTRIES.items():
        assert body.startswith("#"), f"entry {path} does not start with a markdown heading"
        assert body.strip(), f"entry {path} is empty"


# --- _output.py, unit-level ---------------------------------------------------


def test_emit_result_text_mode_appends_exactly_one_trailing_newline() -> None:
    stream = io.StringIO()
    emit_result("no newline", json_mode=False, stream=stream)
    assert stream.getvalue() == "no newline\n"

    stream = io.StringIO()
    emit_result("already has one\n", json_mode=False, stream=stream)
    assert stream.getvalue() == "already has one\n"


def test_emit_result_text_mode_casts_non_string_data() -> None:
    stream = io.StringIO()
    emit_result({"a": 1}, json_mode=False, stream=stream)
    assert stream.getvalue() == str({"a": 1}) + "\n"


def test_emit_result_json_mode_writes_exact_payload_plus_newline() -> None:
    stream = io.StringIO()
    emit_result({"a": 1, "b": [1, 2]}, json_mode=True, stream=stream)
    value = stream.getvalue()
    assert value.endswith("\n")
    assert json.loads(value) == {"a": 1, "b": [1, 2]}


def test_emit_error_text_mode_omits_hint_line_when_remediation_empty() -> None:
    stream = io.StringIO()
    emit_error(CliError(code=1, message="broke", remediation=""), json_mode=False, stream=stream)
    assert stream.getvalue() == "error: broke\n"


def test_emit_error_text_mode_includes_hint_line_when_remediation_present() -> None:
    stream = io.StringIO()
    emit_error(
        CliError(code=1, message="broke", remediation="fix it"),
        json_mode=False,
        stream=stream,
    )
    assert stream.getvalue() == "error: broke\nhint: fix it\n"


def test_emit_error_json_mode_writes_full_dict_even_with_empty_remediation() -> None:
    stream = io.StringIO()
    emit_error(CliError(code=2, message="broke", remediation=""), json_mode=True, stream=stream)
    assert json.loads(stream.getvalue()) == {"code": 2, "message": "broke", "remediation": ""}


def test_emit_diagnostic_appends_newline_exactly_once() -> None:
    stream = io.StringIO()
    emit_diagnostic("progress update", stream=stream)
    assert stream.getvalue() == "progress update\n"

    stream = io.StringIO()
    emit_diagnostic("already newline\n", stream=stream)
    assert stream.getvalue() == "already newline\n"


# --- _errors.py, unit-level ---------------------------------------------------


def test_cli_error_is_a_real_exception_and_carries_its_fields() -> None:
    err = CliError(code=EXIT_ENV_ERROR, message="no chromium", remediation="install it")
    assert isinstance(err, Exception)
    assert str(err) == "no chromium"
    assert err.to_dict() == {"code": 2, "message": "no chromium", "remediation": "install it"}


def test_cli_error_default_remediation_is_empty_string() -> None:
    err = CliError(code=EXIT_USER_ERROR, message="bad input")
    assert err.remediation == ""
    assert err.to_dict()["remediation"] == ""


# --- whoami / doctor identity behavior: failure branches --------------------
#
# This repo's own culture.yaml is always well-formed, so the happy-path CLI
# tests in test_cli.py / test_cli_introspection.py never exercise these
# branches: no culture.yaml at all, an unknown backend, a missing prompt
# file, a missing skills dir, an unreadable culture.yaml, or a multi-agent
# file where only the first block should be read.


def test_read_agent_fields_falls_back_when_no_culture_yaml(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(whoami_cmd, "find_culture_yaml", lambda: None)
    fields = whoami_cmd.read_agent_fields()
    assert fields == {"nick": "webglass-cli", "backend": "unknown", "model": "unknown"}


def test_read_agent_fields_falls_back_on_unreadable_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Unreadable:
        def read_text(self, encoding: str = "utf-8") -> str:
            raise OSError("permission denied (simulated)")

    monkeypatch.setattr(whoami_cmd, "find_culture_yaml", lambda: _Unreadable())
    fields = whoami_cmd.read_agent_fields()
    assert fields == {"nick": "webglass-cli", "backend": "unknown", "model": "unknown"}


def test_read_agent_fields_reads_only_the_first_agent_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    culture_yaml = tmp_path / "culture.yaml"  # type: ignore[attr-defined]
    culture_yaml.write_text(
        "agents:\n"
        "  - suffix: first-agent\n"
        "    backend: claude\n"
        "    model: model-one\n"
        "  - suffix: second-agent\n"
        "    backend: colleague\n"
        "    model: model-two\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(whoami_cmd, "find_culture_yaml", lambda: culture_yaml)
    fields = whoami_cmd.read_agent_fields()
    assert fields == {"nick": "first-agent", "backend": "claude", "model": "model-one"}


def test_diagnose_reports_source_checkout_info_when_no_culture_yaml(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(doctor_cmd, "find_culture_yaml", lambda: None)
    report = doctor_cmd._diagnose()
    assert report == {
        "healthy": True,
        "checks": [
            {
                "id": "source_checkout",
                "passed": True,
                "severity": "info",
                "message": "no culture.yaml found alongside the package; identity checks skipped",
                "remediation": "",
            }
        ],
    }


def test_diagnose_flags_unknown_backend(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> None:
    culture_yaml = tmp_path / "culture.yaml"  # type: ignore[attr-defined]
    culture_yaml.write_text("agents:\n  - suffix: x\n    backend: made-up\n", encoding="utf-8")
    monkeypatch.setattr(doctor_cmd, "find_culture_yaml", lambda: culture_yaml)
    monkeypatch.setattr(
        doctor_cmd,
        "read_agent_fields",
        lambda: {"nick": "x", "backend": "made-up", "model": "unknown"},
    )
    report = doctor_cmd._diagnose()
    assert report["healthy"] is False
    backend_check = next(c for c in report["checks"] if c["id"] == "backend_consistency")
    assert backend_check["passed"] is False
    assert "made-up" in backend_check["message"]
    assert "claude" in backend_check["remediation"]


def test_diagnose_flags_missing_prompt_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    culture_yaml = tmp_path / "culture.yaml"  # type: ignore[attr-defined]
    culture_yaml.write_text("agents:\n  - suffix: x\n    backend: acp\n", encoding="utf-8")
    monkeypatch.setattr(doctor_cmd, "find_culture_yaml", lambda: culture_yaml)
    monkeypatch.setattr(
        doctor_cmd,
        "read_agent_fields",
        lambda: {"nick": "x", "backend": "acp", "model": "unknown"},
    )
    report = doctor_cmd._diagnose()
    assert report["healthy"] is False
    prompt_check = next(c for c in report["checks"] if c["id"] == "prompt_file_present")
    assert prompt_check["passed"] is False
    assert "AGENTS.md" in prompt_check["message"]


def test_diagnose_flags_missing_skills_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    culture_yaml = tmp_path / "culture.yaml"  # type: ignore[attr-defined]
    culture_yaml.write_text("agents:\n  - suffix: x\n    backend: acp\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("stub", encoding="utf-8")  # type: ignore[operator]
    monkeypatch.setattr(doctor_cmd, "find_culture_yaml", lambda: culture_yaml)
    monkeypatch.setattr(
        doctor_cmd,
        "read_agent_fields",
        lambda: {"nick": "x", "backend": "acp", "model": "unknown"},
    )
    report = doctor_cmd._diagnose()
    assert report["healthy"] is False
    skills_check = next(c for c in report["checks"] if c["id"] == "skills_present")
    assert skills_check["passed"] is False


# --- entry points -------------------------------------------------------------


def test_console_script_entry_point_is_webglass_not_webglass_cli() -> None:
    eps = importlib_metadata.entry_points()
    try:
        scripts = eps.select(group="console_scripts")
    except AttributeError:  # pragma: no cover - py<3.10 EntryPoints API fallback
        scripts = eps.get("console_scripts", [])
    matches = {ep.name: ep.value for ep in scripts if "webglass" in ep.name}
    assert matches == {"webglass": "webglass.cli:main"}


def test_python_dash_m_webglass_is_an_equivalent_entry_point() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "webglass", "whoami", "--json"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["nick"] == "webglass-cli"
    assert payload["version"] == __version__


# --- learn text/JSON consistency + bare-noun-group JSON default -------------


def test_learn_text_and_json_list_the_same_verb_set(capsys: pytest.CaptureFixture[str]) -> None:
    from webglass.cli._commands.learn import _as_json_payload

    payload = _as_json_payload()
    json_paths = {" ".join(cmd["path"]) for cmd in payload["commands"]}

    rc = main(["learn"])
    assert rc == 0
    text = capsys.readouterr().out
    for path in json_paths:
        assert f"webglass {path}" in text, f"learn text omits documented command {path!r}"


def test_bare_cli_noun_defaults_to_text_not_json(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["cli"])
    assert rc == 0
    out = capsys.readouterr().out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)
    assert out.startswith("# webglass cli")
