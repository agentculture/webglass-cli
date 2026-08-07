"""Tests for the t14 browser-capability doctor checks.

Two tiers, mirroring ``tests/test_playwright_adapter.py``'s split:

**Unit tests** monkeypatch the small, individually-seamed probe functions in
``webglass.cli._browser_doctor`` (``_import_playwright_adapter``,
``_resolve_chromium_path``, ``_read_playwright_version``,
``_read_sysctl_int``, ``_default_state_dir``) to exercise every
finding/severity combination — found/missing chromium, restricted/available
sandbox, importable/broken playwright, writable/unwritable state dir —
without needing a real (or a deliberately absent) browser install.

**Integration tests** call ``webglass.cli._commands.doctor._diagnose()`` and
``webglass.cli.main(["doctor", ...])`` directly, both unmonkeypatched
(against this repo's real environment) and with the probes patched, to lock
the contract acceptance criteria care about most: a host that simply lacks a
browser or a usable sandbox stays ``healthy``, while a genuinely broken
``playwright_importable`` does not.

None of these tests require ``WEBGLASS_TEST_BROWSER=1`` — unlike
``tests/test_playwright_adapter.py``'s live-Chromium tier, every doctor
probe here is a static/read-only check (package-metadata read, file-exists
check, ``/proc/sys`` read, a throwaway file write). That is precisely what
lets this whole module run in the default, browser-free suite — the
acceptance criterion "a browserless run still passes the non-browser suite"
is what ``test_default_suite_never_needs_the_browser_test_env_var`` pins
directly.
"""

from __future__ import annotations

import logging

import pytest

from webglass.cli import _browser_doctor as bd
from webglass.cli import main
from webglass.cli._commands import doctor as doctor_cmd

# ---------------------------------------------------------------------------
# Unit: playwright_importable
# ---------------------------------------------------------------------------


def test_playwright_importable_passes_when_the_adapter_imports_cleanly() -> None:
    check = bd.check_playwright_importable()
    assert check["id"] == "playwright_importable"
    assert check["severity"] == "error"
    assert check["passed"] is True
    assert check["remediation"] == ""


def test_playwright_importable_is_an_error_when_the_import_breaks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _broken() -> None:
        raise ModuleNotFoundError("No module named 'playwright'")

    monkeypatch.setattr(bd, "_import_playwright_adapter", _broken)
    check = bd.check_playwright_importable()
    assert check["severity"] == "error"
    # playwright is a declared core dependency: unlike every other new
    # check, this is the one that is allowed to flip doctor unhealthy.
    assert check["passed"] is False
    assert "import playwright failed" in check["message"]
    assert "uv sync" in check["remediation"]


# ---------------------------------------------------------------------------
# Unit: chromium_installed
# ---------------------------------------------------------------------------


def test_chromium_installed_passes_and_names_the_path_when_found(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    fake_binary = tmp_path / "chrome"  # type: ignore[operator]
    fake_binary.write_text("", encoding="utf-8")
    monkeypatch.setattr(bd, "_resolve_chromium_path", lambda: str(fake_binary))
    check = bd.check_chromium_installed()
    assert check["id"] == "chromium_installed"
    assert check["severity"] == "warning"
    assert check["passed"] is True  # advisory: see module docstring
    assert str(fake_binary) in check["message"]
    assert check["remediation"] == ""


def test_chromium_installed_stays_passed_true_when_missing_but_names_the_remediation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    missing = tmp_path / "no-such-chrome"  # type: ignore[operator]
    monkeypatch.setattr(bd, "_resolve_chromium_path", lambda: str(missing))
    check = bd.check_chromium_installed()
    assert check["severity"] == "warning"
    # The load-bearing acceptance criterion: absence must not read as a
    # doctor failure, but the remediation is still there for a human/agent
    # who wants browser features.
    assert check["passed"] is True
    assert "not found" in check["message"]
    assert check["remediation"] == "run `uv run playwright install chromium`"


def test_chromium_installed_stays_passed_true_when_resolution_itself_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _broken() -> str:
        raise RuntimeError("driver did not start")

    monkeypatch.setattr(bd, "_resolve_chromium_path", _broken)
    check = bd.check_chromium_installed()
    assert check["passed"] is True
    assert "could not resolve" in check["message"]
    assert "playwright install chromium" in check["remediation"]


# ---------------------------------------------------------------------------
# Unit: playwright_version
# ---------------------------------------------------------------------------


def test_playwright_version_reports_the_version_and_the_pinned_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bd, "_read_playwright_version", lambda: "9.9.9")
    monkeypatch.setattr(bd, "_pinned_playwright_range", lambda: "playwright<2,>=1.55")
    check = bd.check_playwright_version()
    assert check["id"] == "playwright_version"
    assert check["severity"] == "info"
    assert check["passed"] is True
    assert "9.9.9" in check["message"]
    assert "playwright<2,>=1.55" in check["message"]


def test_playwright_version_stays_passed_true_when_the_version_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _broken() -> str:
        raise ModuleNotFoundError("playwright not installed")

    monkeypatch.setattr(bd, "_read_playwright_version", _broken)
    check = bd.check_playwright_version()
    assert check["severity"] == "info"
    assert check["passed"] is True
    assert "could not determine" in check["message"]


def test_pinned_playwright_range_reads_real_package_metadata() -> None:
    # Unmonkeypatched: this repo's own installed metadata names playwright
    # with a bounded (non-floating) range — see
    # tests/test_import_boundaries.py::test_playwright_is_the_only_runtime_dependency
    # for the packaging-side half of this same assertion.
    range_text = bd._pinned_playwright_range()
    assert "playwright" in range_text
    assert any(bound in range_text for bound in (">=", "==", "~="))


# ---------------------------------------------------------------------------
# Unit: usable_sandbox
# ---------------------------------------------------------------------------


def test_usable_sandbox_flags_the_apparmor_restriction(monkeypatch: pytest.MonkeyPatch) -> None:
    def _sysctl(path: object) -> int | None:
        if path == bd._APPARMOR_RESTRICT_PATH:
            return 1
        return None

    monkeypatch.setattr(bd, "_read_sysctl_int", _sysctl)
    check = bd.check_usable_sandbox()
    assert check["id"] == "usable_sandbox"
    assert check["severity"] == "warning"
    assert check["passed"] is True  # advisory — never blocks healthy
    assert "restricted" in check["message"]
    assert "apparmor_restrict_unprivileged_userns=1" in check["message"]
    assert "AppArmor" in check["remediation"]
    assert "unprivileged user namespaces" in check["remediation"]
    # The remediation names --no-sandbox only to warn callers away from it,
    # never as a suggested fix.
    assert "NEVER pass allow_unsandboxed=True / --no-sandbox" in check["remediation"]


def test_usable_sandbox_flags_userns_clone_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    def _sysctl(path: object) -> int | None:
        if path == bd._UNPRIVILEGED_USERNS_CLONE_PATH:
            return 0
        return None

    monkeypatch.setattr(bd, "_read_sysctl_int", _sysctl)
    check = bd.check_usable_sandbox()
    assert check["passed"] is True
    assert "unprivileged_userns_clone=0" in check["message"]


def test_usable_sandbox_passes_cleanly_when_unrestricted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        bd, "_read_sysctl_int", lambda path: 0 if path == bd._APPARMOR_RESTRICT_PATH else 1
    )
    check = bd.check_usable_sandbox()
    assert check["passed"] is True
    assert check["remediation"] == ""
    assert "should start" in check["message"]


def test_usable_sandbox_is_undetermined_on_a_platform_without_proc_sys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bd, "_read_sysctl_int", lambda path: None)
    check = bd.check_usable_sandbox()
    assert check["passed"] is True
    assert "undetermined" in check["message"]
    assert check["remediation"] == ""


def test_read_sysctl_int_handles_a_missing_or_malformed_file(tmp_path: object) -> None:
    missing = tmp_path / "does-not-exist"  # type: ignore[operator]
    assert bd._read_sysctl_int(missing) is None

    malformed = tmp_path / "malformed"  # type: ignore[operator]
    malformed.write_text("not-a-number\n", encoding="utf-8")
    assert bd._read_sysctl_int(malformed) is None

    real = tmp_path / "real"  # type: ignore[operator]
    real.write_text("1\n", encoding="utf-8")
    assert bd._read_sysctl_int(real) == 1


def test_usable_sandbox_never_probes_by_launching_a_browser() -> None:
    # Static guard against the one thing this check must never do: call
    # anything that could start a browser process. --no-sandbox/
    # allow_unsandboxed are named only inside remediation prose (warning
    # callers away from them), never passed as an argument here.
    import inspect

    source = inspect.getsource(bd.check_usable_sandbox)
    assert "subprocess" not in source
    assert "launch_detached(" not in source
    assert "allow_unsandboxed=" not in source
    assert "_import_playwright_adapter" not in source


# ---------------------------------------------------------------------------
# Unit: state_dir_writable
# ---------------------------------------------------------------------------


def test_state_dir_writable_passes_for_a_real_writable_directory(tmp_path: object) -> None:
    target = tmp_path / "state"  # type: ignore[operator]
    check = bd.check_state_dir_writable(target)  # type: ignore[arg-type]
    assert check["id"] == "state_dir_writable"
    assert check["severity"] == "info"
    assert check["passed"] is True
    assert str(target) in check["message"]
    assert target.is_dir()  # type: ignore[union-attr]
    # The write-probe file doesn't linger.
    assert list(target.iterdir()) == []  # type: ignore[union-attr]


def test_state_dir_writable_stays_passed_true_when_unwritable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    from pathlib import Path

    target = tmp_path / "state"  # type: ignore[operator]

    def _broken_mkdir(self: Path, *args: object, **kwargs: object) -> None:
        raise OSError("permission denied (simulated)")

    monkeypatch.setattr(Path, "mkdir", _broken_mkdir)
    check = bd.check_state_dir_writable(target)  # type: ignore[arg-type]
    assert check["severity"] == "info"
    assert check["passed"] is True
    assert "not writable" in check["message"]
    assert str(target) in check["remediation"]


def test_default_state_dir_honors_xdg_state_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert bd._default_state_dir() == tmp_path / "webglass"  # type: ignore[operator]


def test_default_state_dir_falls_back_to_dot_local_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    from pathlib import Path

    assert bd._default_state_dir() == Path.home() / ".local" / "state" / "webglass"


# ---------------------------------------------------------------------------
# browser_checks(): shape and ordering
# ---------------------------------------------------------------------------


def test_browser_checks_returns_the_five_checks_in_acceptance_order() -> None:
    checks = bd.browser_checks()
    assert [c["id"] for c in checks] == [
        "playwright_importable",
        "chromium_installed",
        "playwright_version",
        "usable_sandbox",
        "state_dir_writable",
    ]
    for check in checks:
        assert {"id", "passed", "severity", "message", "remediation"} <= set(check)
        assert isinstance(check["passed"], bool)


def test_browser_checks_severities_match_the_acceptance_criteria() -> None:
    severities = {c["id"]: c["severity"] for c in bd.browser_checks()}
    assert severities == {
        "playwright_importable": "error",
        "chromium_installed": "warning",
        "playwright_version": "info",
        "usable_sandbox": "warning",
        "state_dir_writable": "info",
    }


# ---------------------------------------------------------------------------
# Integration: webglass.cli._commands.doctor._diagnose()
# ---------------------------------------------------------------------------


def test_diagnose_includes_the_browser_checks_after_the_identity_checks() -> None:
    # Unmonkeypatched: runs against this repo's real culture.yaml, exactly
    # like test_cli_introspection.py::test_doctor_recognizes_declared_backend.
    report = doctor_cmd._diagnose()
    ids = [c["id"] for c in report["checks"]]
    assert ids[:2] == ["prompt_file_present", "skills_present"]
    assert ids[2:] == [
        "playwright_importable",
        "chromium_installed",
        "playwright_version",
        "usable_sandbox",
        "state_dir_writable",
    ]


def test_diagnose_stays_healthy_on_this_repo_even_with_apparmor_restricted() -> None:
    """The real-world case this task exists for.

    This dev host has kernel.apparmor_restrict_unprivileged_userns=1 (a
    genuinely sandbox-restricted host, per the module docstring and risk r4
    in docs/plans/2026-08-07-implement-webglass-issue-1.md) yet doctor must
    still report healthy — mirrors
    test_cli_introspection.py::test_doctor_recognizes_declared_backend but
    asserts the *reason* it stays healthy is understood, not just the fact.
    """
    report = doctor_cmd._diagnose()
    assert report["healthy"] is True
    by_id = {c["id"]: c for c in report["checks"]}
    # Whatever this host's real sandbox/chromium state is, it must not be
    # the thing that could make the report unhealthy.
    assert by_id["usable_sandbox"]["passed"] is True
    assert by_id["chromium_installed"]["passed"] is True


def test_diagnose_stays_healthy_when_chromium_and_sandbox_are_both_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A simulated fully-browserless, fully-sandbox-restricted host."""

    def _broken_path() -> str:
        raise RuntimeError("simulated: chromium not installed")

    monkeypatch.setattr(bd, "_resolve_chromium_path", _broken_path)
    monkeypatch.setattr(
        bd, "_read_sysctl_int", lambda path: 1 if path == bd._APPARMOR_RESTRICT_PATH else None
    )

    report = doctor_cmd._diagnose()
    assert report["healthy"] is True
    by_id = {c["id"]: c for c in report["checks"]}
    assert by_id["chromium_installed"]["passed"] is True
    assert "could not resolve" in by_id["chromium_installed"]["message"]
    assert by_id["usable_sandbox"]["passed"] is True
    assert "restricted" in by_id["usable_sandbox"]["message"]


def test_diagnose_goes_unhealthy_when_playwright_itself_is_not_importable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one new check that IS allowed to flip doctor unhealthy."""

    def _broken() -> None:
        raise ModuleNotFoundError("No module named 'playwright'")

    monkeypatch.setattr(bd, "_import_playwright_adapter", _broken)
    report = doctor_cmd._diagnose()
    assert report["healthy"] is False
    by_id = {c["id"]: c for c in report["checks"]}
    assert by_id["playwright_importable"]["passed"] is False
    assert by_id["playwright_importable"]["severity"] == "error"


def test_diagnose_with_no_culture_yaml_still_skips_browser_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pinned by test_characterization.py's strict-equality single-check
    assertion for this branch — browser checks must not sneak in here."""
    monkeypatch.setattr(doctor_cmd, "find_culture_yaml", lambda: None)
    report = doctor_cmd._diagnose()
    assert [c["id"] for c in report["checks"]] == ["source_checkout"]


# ---------------------------------------------------------------------------
# Integration: the CLI entry point (text and --json)
# ---------------------------------------------------------------------------


def test_cli_doctor_json_includes_browser_checks(capsys: pytest.CaptureFixture[str]) -> None:
    import json

    rc = main(["doctor", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    ids = [c["id"] for c in payload["checks"]]
    assert "playwright_importable" in ids
    assert "usable_sandbox" in ids


def test_cli_doctor_text_shows_a_hint_even_for_a_passed_advisory_check(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The renderer change: remediation shows whenever present, not only on FAIL."""

    def _broken_path() -> str:
        raise RuntimeError("simulated: chromium not installed")

    monkeypatch.setattr(bd, "_resolve_chromium_path", _broken_path)
    rc = main(["doctor"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[ok] chromium_installed" in out
    assert "hint: run `uv run playwright install chromium`" in out


def test_cli_doctor_exits_1_when_playwright_is_not_importable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _broken() -> None:
        raise ModuleNotFoundError("No module named 'playwright'")

    monkeypatch.setattr(bd, "_import_playwright_adapter", _broken)
    rc = main(["doctor"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "webglass doctor: unhealthy" in out
    assert "[FAIL] playwright_importable" in out


# ---------------------------------------------------------------------------
# The asyncio-teardown-chatter fix (chromium_executable_path starts a driver)
# ---------------------------------------------------------------------------


def test_resolving_the_chromium_path_quiets_the_asyncio_logger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """webglass/adapters/playwright.py's module docstring documents that
    Playwright 1.62's sync API prints asyncio teardown chatter at
    interpreter-exit time, and that "whichever layer wires these operations
    into webglass verbs has to quiet the asyncio logger for the process."
    doctor is that layer for chromium_executable_path(); this locks that the
    quieting actually happens once the real path is resolved.

    Resets the module's "already quieted" guard via monkeypatch so this test
    is order-independent — an earlier test in the same session may already
    have tripped it, which would otherwise make this assertion vacuous.
    """
    monkeypatch.setattr(bd, "_ASYNCIO_LOGGER_QUIETED", False)
    logging.getLogger("asyncio").setLevel(logging.NOTSET)
    bd.check_chromium_installed()
    assert logging.getLogger("asyncio").level == logging.CRITICAL


# ---------------------------------------------------------------------------
# "a browserless run still passes the non-browser suite" (acceptance item 3)
# ---------------------------------------------------------------------------


def test_default_suite_never_needs_the_browser_test_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every test in this module runs regardless of WEBGLASS_TEST_BROWSER.

    Unlike tests/test_playwright_adapter.py's live-Chromium tier (gated
    behind WEBGLASS_TEST_BROWSER=1 because it launches a real browser
    process), every doctor probe here is a static/read-only check: package
    metadata, a file-exists test, a /proc/sys read, a throwaway file write.
    That is what makes it safe for the *unmodified* `test` job in
    .github/workflows/tests.yml (no browser installed there at all) to run
    this whole module and still stay green — the new browser-test job in
    the same workflow is for the separately-gated live-Chromium tier only.
    Proven directly here: unsetting the env var entirely still produces a
    full, healthy report.
    """
    monkeypatch.delenv("WEBGLASS_TEST_BROWSER", raising=False)
    report = doctor_cmd._diagnose()
    assert report["healthy"] is True
    assert {c["id"] for c in bd.browser_checks()} == {
        "playwright_importable",
        "chromium_installed",
        "playwright_version",
        "usable_sandbox",
        "state_dir_writable",
    }
