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


def test_learn_json_declares_pre_implementation_status() -> None:
    """A JSON consumer must not infer capabilities that are not built yet.

    The text body carries a Status section; the JSON payload has to say the same
    thing in a machine-readable way, or an agent reading only `purpose` would
    assume the web operation surface exists.
    """
    payload = _as_json_payload()
    assert payload["status"] == "pre-implementation"
    assert "not built" in payload["status_detail"]
    assert "pre-implementation" in payload["purpose"]


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
