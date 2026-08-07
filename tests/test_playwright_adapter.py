"""Tests for the Playwright/Chromium adapter (build plan task t11).

Two tiers, deliberately separated:

**Browser-free tests** run everywhere, always. They cover the parts of the
adapter that are pure logic — the launch argv (does ``--no-sandbox`` appear
when it must not?), failure classification, stack-location parsing, endpoint
redaction, and the "no Playwright type crosses a public signature" scan.
``launch_detached`` is exercised against a *fake* Chromium script that
records its argv and either writes a ``DevToolsActivePort`` file, prints
Chromium's real sandbox-failure banner, or hangs — so the launch contract is
tested without a browser being installed at all.

**Browser tests** launch real Chromium and are gated behind
``WEBGLASS_TEST_BROWSER=1``; without it they skip with a reason, which is what
keeps the default suite (and any browserless CI job) green. On a host whose
kernel/AppArmor configuration blocks the Chromium sandbox — Ubuntu
23.10+/24.04-class, including the machine this task was developed on — also
set ``WEBGLASS_TEST_ALLOW_NO_SANDBOX=1``. That variable exists so the opt-out
is made by a *human configuring a test harness*, never by the adapter itself:

    WEBGLASS_TEST_BROWSER=1 WEBGLASS_TEST_ALLOW_NO_SANDBOX=1 uv run pytest \\
        tests/test_playwright_adapter.py -v

When ``WEBGLASS_TEST_ALLOW_NO_SANDBOX=1`` is set the harness is asserting
"this host has no usable sandbox", so
``test_sandboxed_launch_is_refused_not_downgraded`` turns that into a
positive test: a default (sandboxed) launch must raise a structured
``BrowserLaunchError``, never quietly succeed by dropping the sandbox.
"""

from __future__ import annotations

import inspect
import os
import re
import subprocess
import sys
import textwrap
import typing
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.test_adapter_conformance import BrowserBackendConformance
from webglass.adapters import playwright as adapter
from webglass.adapters.browser import BrowserBackend, BrowserOpenResult, PressResult
from webglass.policy import PolicyDecision, PolicyVerdict

BROWSER_ENV = "WEBGLASS_TEST_BROWSER"
NO_SANDBOX_ENV = "WEBGLASS_TEST_ALLOW_NO_SANDBOX"

_BROWSER_ENABLED = os.environ.get(BROWSER_ENV) == "1"
_NO_SANDBOX_ALLOWED = os.environ.get(NO_SANDBOX_ENV) == "1"

requires_browser = pytest.mark.skipif(
    not _BROWSER_ENABLED,
    reason=f"browser tests are opt-in: set {BROWSER_ENV}=1 (see this module's docstring)",
)
requires_unsandboxed_host = pytest.mark.skipif(
    not (_BROWSER_ENABLED and _NO_SANDBOX_ALLOWED),
    reason=(
        f"needs {BROWSER_ENV}=1 and {NO_SANDBOX_ENV}=1 — the harness declaring "
        "that this host cannot provide a usable Chromium sandbox"
    ),
)

# A real Chromium stack, copied verbatim from this host running the
# tests/fixtures/pages/throw.html fixture.
_REAL_STACK = (
    "Error: WebGlass fixture: deliberate synchronous throw on load\n"
    "    at http://127.0.0.1:36767/throw:16:13"
)


# ---------------------------------------------------------------------------
# Browser-free: the seam itself.
# ---------------------------------------------------------------------------


def _public_members() -> list[tuple[str, object]]:
    return [(name, getattr(adapter, name)) for name in adapter.__all__]


def _annotation_types(annotation: object) -> Iterator[object]:
    """Yield ``annotation`` and every type nested inside it."""
    yield annotation
    for argument in typing.get_args(annotation):
        yield from _annotation_types(argument)


def _signature_annotations(target: object) -> Iterator[tuple[str, object]]:
    try:
        signature = inspect.signature(target, eval_str=True)
    except (TypeError, ValueError, NameError):  # pragma: no cover - not introspectable
        return
    for parameter in signature.parameters.values():
        if parameter.annotation is not inspect.Signature.empty:
            yield parameter.name, parameter.annotation
    if signature.return_annotation is not inspect.Signature.empty:
        yield "return", signature.return_annotation


def test_no_playwright_type_crosses_a_public_signature() -> None:
    """CLAUDE.md section 8: Playwright types stay out of the public API.

    Resolves (not merely string-matches) every annotation on every public
    function, class, and public method in the module, walks into generic
    arguments, and asserts nothing resolves to a type defined in the
    ``playwright`` package. The module is free to *import* Playwright — it is
    the one module that may — but a caller must never have to hold one of its
    objects to satisfy a type.
    """
    offenders: list[str] = []
    targets: list[tuple[str, object]] = []
    for name, member in _public_members():
        if inspect.isclass(member):
            targets.append((name, member))
            for attr_name, attr in vars(member).items():
                if not attr_name.startswith("_") and callable(attr):
                    targets.append((f"{name}.{attr_name}", attr))
        elif callable(member):
            targets.append((name, member))

    for label, target in targets:
        for parameter_name, annotation in _signature_annotations(target):
            for resolved in _annotation_types(annotation):
                module = getattr(resolved, "__module__", "") or ""
                if module == "playwright" or module.startswith("playwright."):
                    offenders.append(f"{label}({parameter_name}): {resolved!r}")
    assert not offenders, "Playwright type in a public signature:\n" + "\n".join(offenders)


def test_public_annotation_strings_never_name_the_playwright_package() -> None:
    # Belt-and-braces companion to the resolved-type scan above: catches a
    # string annotation that fails to resolve (and would silently skip).
    offenders: list[str] = []
    for name, member in _public_members():
        for holder in (member, *(vars(member).values() if inspect.isclass(member) else ())):
            annotations = getattr(holder, "__annotations__", {}) or {}
            for field_name, annotation in annotations.items():
                if isinstance(annotation, str) and re.search(r"\bplaywright\.", annotation):
                    offenders.append(f"{name}.{field_name}: {annotation}")
    assert not offenders, "\n".join(offenders)


def test_backend_satisfies_the_browser_backend_protocol_without_connecting() -> None:
    backend = adapter.PlaywrightBrowserBackend("http://127.0.0.1:1")
    assert isinstance(backend, BrowserBackend)


def test_results_are_browser_backend_results_plus_trusted_diagnostics() -> None:
    """Diagnostics are a *separate* field from page-authored text.

    Issue #1 section 7: a page must never be able to put text where a
    WebGlass warning goes. Subclassing keeps every ``BrowserOpenResult``
    assertion valid while giving trusted control metadata its own home.
    """
    result = adapter.PlaywrightOpenResult(
        requested_url="https://example.test/",
        final_url="https://example.test/",
        status=200,
        diagnostics=(adapter.SANDBOX_DISABLED_WARNING,),
    )
    assert isinstance(result, BrowserOpenResult)
    payload = result.to_dict()
    assert payload["diagnostics"] == [adapter.SANDBOX_DISABLED_WARNING]
    assert payload["console_messages"] == []
    assert payload["page_errors"] == []

    press = adapter.PlaywrightPressResult(
        session_id="s", pressed=("a",), key_log=("a",), diagnostics=("x",)
    )
    assert isinstance(press, PressResult)
    assert press.to_dict()["diagnostics"] == ["x"]


def test_endpoint_for_accepts_a_string_a_mapping_or_a_callable() -> None:
    assert adapter.PlaywrightBrowserBackend("http://a").endpoint_for("any") == "http://a"
    mapping = adapter.PlaywrightBrowserBackend({"s1": "http://b"})
    assert mapping.endpoint_for("s1") == "http://b"
    with pytest.raises(KeyError):
        mapping.endpoint_for("missing")
    # The callable form is the seam t12 uses: look the session up in the
    # on-disk store and hand back its endpoint_ref.
    resolver = adapter.PlaywrightBrowserBackend(lambda session_id: f"http://c/{session_id}")
    assert resolver.endpoint_for("s2") == "http://c/s2"


def test_detached_browser_keeps_its_endpoint_out_of_repr_and_public_dict() -> None:
    """The CDP endpoint is secret-equivalent (spec claim c33 / honesty h30)."""
    browser = adapter.DetachedBrowser(
        endpoint="http://127.0.0.1:31337",
        pid=4242,
        user_data_dir="/tmp/webglass-session",  # nosec B108 - a literal in an assertion
        sandboxed=False,
        diagnostics=(adapter.SANDBOX_DISABLED_WARNING,),
    )
    assert "31337" not in repr(browser)
    assert "<redacted>" in repr(browser)
    payload = browser.to_public_dict()
    assert "endpoint" not in payload
    assert "31337" not in str(payload)
    assert payload["pid"] == 4242
    assert payload["sandboxed"] is False


def test_parse_stack_location_reads_a_real_chromium_stack() -> None:
    url, line = adapter._parse_stack_location(_REAL_STACK)
    assert url == "http://127.0.0.1:36767/throw"
    assert line == 16


@pytest.mark.parametrize(
    "stack, expected_url",
    [
        (
            "Error: boom\n    at handler (https://example.test/app.js:5:9)",
            "https://example.test/app.js",
        ),
        ("Error: boom\n    at file:///tmp/x.html:2:1", "file:///tmp/x.html"),
        ("", None),
        ("Error: boom with no frames", None),
    ],
)
def test_parse_stack_location_handles_other_shapes(stack: str, expected_url: str | None) -> None:
    assert adapter._parse_stack_location(stack)[0] == expected_url


def test_browser_launch_error_renders_as_structured_data() -> None:
    error = adapter.BrowserLaunchError("browser_launch_failed", "nope", "try this")
    assert error.to_dict() == {
        "code": "browser_launch_failed",
        "message": "nope",
        "remediation": "try this",
    }
    assert isinstance(error, RuntimeError)


# ---------------------------------------------------------------------------
# Browser-free: the launch contract, against a fake Chromium.
# ---------------------------------------------------------------------------

_FAKE_CHROMIUM = """\
#!{python}
import pathlib, sys, time

argv = sys.argv[1:]
mode = {mode!r}
user_data_dir = None
for arg in argv:
    if arg.startswith("--user-data-dir="):
        user_data_dir = pathlib.Path(arg.split("=", 1)[1])
pathlib.Path({argv_log!r}).write_text("\\n".join(argv), encoding="utf-8")

if mode == "sandbox-failure":
    sys.stderr.write(
        "[1:1:0807/222600.605240:FATAL:content/browser/zygote_host/"
        "zygote_host_impl_linux.cc:128] No usable sandbox! If you are running on "
        "Ubuntu 23.10+ ... see apparmor-userns-restrictions.md\\n"
    )
    sys.exit(133)
if mode == "other-failure":
    sys.stderr.write("something else went wrong\\n")
    sys.exit(7)
if mode == "hang":
    time.sleep(60)
    sys.exit(0)

(user_data_dir / "DevToolsActivePort").write_text(
    "45671\\n/devtools/browser/fake-browser-id\\n", encoding="utf-8"
)
time.sleep(60)
"""


def _fake_chromium(tmp_path: Path, mode: str) -> tuple[str, Path]:
    """Write an executable stand-in for Chromium; return (path, argv-log path)."""
    argv_log = tmp_path / f"argv-{mode}.txt"
    script = tmp_path / f"fake-chromium-{mode}"
    script.write_text(
        _FAKE_CHROMIUM.format(python=sys.executable, mode=mode, argv_log=str(argv_log)),
        encoding="utf-8",
    )
    script.chmod(0o755)
    return str(script), argv_log


def test_launch_detached_returns_the_endpoint_from_devtoolsactiveport(tmp_path: Path) -> None:
    executable, argv_log = _fake_chromium(tmp_path, "ok")
    browser = adapter.launch_detached(tmp_path / "profile", executable_path=executable)
    try:
        assert browser.endpoint == "http://127.0.0.1:45671"
        assert browser.sandboxed is True
        assert browser.diagnostics == ()
        assert browser.pid > 0
        argv = argv_log.read_text(encoding="utf-8").splitlines()
        assert f"--user-data-dir={tmp_path / 'profile'}" in argv
        assert "--remote-debugging-port=0" in argv
        # --no-startup-window is what makes reattach unambiguous: the default
        # context starts empty, so the session's page is the only page.
        assert "--no-startup-window" in argv
    finally:
        browser.terminate()


def test_a_stale_devtools_port_file_is_never_reported_as_a_live_endpoint(tmp_path: Path) -> None:
    """A killed browser leaves its port file behind; a reused profile must not
    hand that dead endpoint to a caller who would then "connect" to nothing."""
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "DevToolsActivePort").write_text("9999\n/devtools/browser/stale\n", encoding="utf-8")
    executable, _ = _fake_chromium(tmp_path, "ok")
    browser = adapter.launch_detached(profile, executable_path=executable)
    try:
        assert browser.endpoint == "http://127.0.0.1:45671"
    finally:
        browser.terminate()


def test_launch_detached_never_passes_no_sandbox_by_default(tmp_path: Path) -> None:
    executable, argv_log = _fake_chromium(tmp_path, "ok")
    browser = adapter.launch_detached(tmp_path / "profile", executable_path=executable)
    try:
        assert "--no-sandbox" not in argv_log.read_text(encoding="utf-8").splitlines()
    finally:
        browser.terminate()


def test_allow_unsandboxed_is_explicit_and_marks_every_result(tmp_path: Path) -> None:
    """The opt-in is loud: the flag is added *and* the choice is recorded."""
    executable, argv_log = _fake_chromium(tmp_path, "ok")
    browser = adapter.launch_detached(
        tmp_path / "profile", executable_path=executable, allow_unsandboxed=True
    )
    try:
        assert "--no-sandbox" in argv_log.read_text(encoding="utf-8").splitlines()
        assert browser.sandboxed is False
        assert adapter.SANDBOX_DISABLED_WARNING in browser.diagnostics
        # connect() propagates the marker into every result the backend makes.
        backend = adapter.connect(browser.endpoint, diagnostics=browser.diagnostics)
        assert adapter.SANDBOX_DISABLED_WARNING in backend.diagnostics
    finally:
        browser.terminate()


def test_sandbox_failure_is_classified_with_apparmor_remediation(tmp_path: Path) -> None:
    executable, _ = _fake_chromium(tmp_path, "sandbox-failure")
    with pytest.raises(adapter.BrowserLaunchError) as excinfo:
        adapter.launch_detached(tmp_path / "profile", executable_path=executable)
    error = excinfo.value
    assert error.code == "browser_sandbox_unavailable"
    assert "AppArmor" in error.remediation
    assert "unprivileged user namespaces" in error.remediation
    assert "allow_unsandboxed=True" in error.remediation
    assert "No usable sandbox" in error.log_tail


def test_a_non_sandbox_launch_failure_is_not_mislabelled(tmp_path: Path) -> None:
    executable, _ = _fake_chromium(tmp_path, "other-failure")
    with pytest.raises(adapter.BrowserLaunchError) as excinfo:
        adapter.launch_detached(tmp_path / "profile", executable_path=executable)
    assert excinfo.value.code == "browser_launch_failed"


def test_launch_timeout_is_structured_and_kills_the_process(tmp_path: Path) -> None:
    executable, _ = _fake_chromium(tmp_path, "hang")
    with pytest.raises(adapter.BrowserLaunchError) as excinfo:
        adapter.launch_detached(
            tmp_path / "profile", executable_path=executable, timeout_seconds=0.5
        )
    assert excinfo.value.code == "browser_launch_timeout"


def test_open_never_contacts_a_policy_denied_target() -> None:
    """A denial is enforced *before* navigating, so no page is even needed."""

    class DenyEverything:
        def evaluate(self, url: str, *, hop_index: int | None = None) -> PolicyVerdict:
            return PolicyVerdict(
                decision=PolicyDecision.DENIED,
                url=url,
                matched_rule_ids=("test-deny-all",),
                reason="denied by test policy",
                hop_index=hop_index,
            )

    backend = adapter.PlaywrightBrowserBackend("http://127.0.0.1:1", policy=DenyEverything())
    result = backend.open("s1", "https://example.test/secret")
    assert result.blocked is True
    assert result.html == ""
    assert result.policy_verdict is not None
    assert result.policy_verdict.matched_rule_ids == ("test-deny-all",)


# ---------------------------------------------------------------------------
# Browser tests: real Chromium.
# ---------------------------------------------------------------------------


@pytest.fixture
def detached_browser(tmp_path: Path) -> Iterator[adapter.DetachedBrowser]:
    """A real detached Chromium, one per test.

    Per-test rather than shared, because the adapter's model is one detached
    browser per session (see its module docstring): a browser shared between
    session ids would have them all adopt the same page, and a suite that
    quietly violates the model it is testing proves less than it appears to.
    """
    browser = adapter.launch_detached(
        tmp_path / "browser-profile", allow_unsandboxed=_NO_SANDBOX_ALLOWED
    )
    try:
        yield browser
    finally:
        browser.terminate()


@pytest.fixture
def live_backend(
    detached_browser: adapter.DetachedBrowser,
) -> Iterator[adapter.PlaywrightBrowserBackend]:
    backend = adapter.connect(detached_browser.endpoint, diagnostics=detached_browser.diagnostics)
    try:
        yield backend
    finally:
        backend.disconnect()


@requires_unsandboxed_host
def test_sandboxed_launch_is_refused_not_downgraded(tmp_path: Path) -> None:
    """The positive test of "never a silent --no-sandbox fallback".

    Reaching this test means the harness has declared this host cannot
    sandbox Chromium. A default launch must therefore fail loudly with the
    structured sandbox error — the one outcome that is unacceptable is a
    launch that quietly succeeds by dropping the sandbox.
    """
    with pytest.raises(adapter.BrowserLaunchError) as excinfo:
        adapter.launch_detached(tmp_path / "sandboxed-profile", timeout_seconds=20)
    error = excinfo.value
    assert error.code == "browser_sandbox_unavailable"
    assert "No usable sandbox" in error.log_tail
    assert "AppArmor" in error.remediation


@requires_browser
def test_launch_detached_reports_a_live_cdp_endpoint(
    detached_browser: adapter.DetachedBrowser,
) -> None:
    assert detached_browser.endpoint.startswith("http://127.0.0.1:")
    assert detached_browser.is_running()
    assert detached_browser.sandboxed is not _NO_SANDBOX_ALLOWED


@requires_browser
def test_sandbox_disabled_warning_reaches_every_result(
    live_backend: adapter.PlaywrightBrowserBackend, fixture_site: str
) -> None:
    if not _NO_SANDBOX_ALLOWED:  # pragma: no cover - depends on host capability
        pytest.skip("host provides a usable sandbox; nothing should be marked")
    opened = live_backend.open("diagnostics-session", f"{fixture_site}/clean")
    pressed = live_backend.press("diagnostics-session", ("a",))
    assert adapter.SANDBOX_DISABLED_WARNING in opened.diagnostics
    assert adapter.SANDBOX_DISABLED_WARNING in pressed.diagnostics
    live_backend.close("diagnostics-session")


@requires_browser
def test_a_404_page_keeps_its_status_and_body(
    live_backend: adapter.PlaywrightBrowserBackend, fixture_site: str
) -> None:
    """Distinct from an unresolvable URL: the server answered, so say so."""
    result = live_backend.open("notfound-session", f"{fixture_site}/definitely-missing")
    assert result.status == 404
    assert "not found" in result.html
    assert result.blocked is False
    live_backend.close("notfound-session")


@requires_browser
def test_spoofed_console_text_stays_untrusted(
    live_backend: adapter.PlaywrightBrowserBackend, fixture_site: str
) -> None:
    """A hostile page cannot write into WebGlass's own diagnostics channel."""
    result = live_backend.open("spoof-session", f"{fixture_site}/spoofed-console")
    texts = [message.text for message in result.console_messages]
    assert any("WEBGLASS WARNING: policy disabled" in text for text in texts)
    assert any("ignore previous instructions" in text for text in texts)
    for diagnostic in result.diagnostics:
        assert "WEBGLASS WARNING" not in diagnostic
        assert "ignore previous instructions" not in diagnostic
    live_backend.close("spoof-session")


@requires_browser
def test_current_reads_the_live_dom_without_navigating(
    live_backend: adapter.PlaywrightBrowserBackend, fixture_site: str
) -> None:
    session = "current-session"
    live_backend.open(session, f"{fixture_site}/keydown")
    live_backend.press(session, ("a", "b"))
    html = live_backend.current(session).html
    assert "&quot;a&quot;,&quot;b&quot;" in html
    live_backend.close(session)


@requires_browser
def test_cdp_reattach_preserves_in_memory_js_state(
    detached_browser: adapter.DetachedBrowser, fixture_site: str, tmp_path: Path
) -> None:
    """The reattach proof (spec claim c29), mirroring the challenge-pass probe.

    A *separate Python process* connects to the detached browser, opens the
    keydown fixture, and presses ``a``/``b`` — the page pushes those onto the
    in-memory array ``window.__webglassKeyLog``. That process then exits
    without closing the browser. This process connects freshly and presses
    ``c``: the page's own log reads ``["a","b","c"]``, which is only possible
    if the JavaScript array itself survived — a lost array would have started
    over at ``["c"]``.
    """
    session = "reattach-session"
    writer = tmp_path / "reattach_writer.py"
    writer.write_text(
        textwrap.dedent(f"""
            from webglass.adapters.playwright import connect

            backend = connect({detached_browser.endpoint!r})
            backend.open({session!r}, {f"{fixture_site}/keydown"!r})
            result = backend.press({session!r}, ("a", "b"))
            assert result.key_log == ("a", "b"), result.key_log
            # Disconnect the client only: the browser must outlive this process.
            backend.disconnect()
            print("writer-ok")
            """),
        encoding="utf-8",
    )
    completed = subprocess.run(  # nosec B603 - fixed argv, no shell
        [sys.executable, str(writer)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        cwd=str(Path(__file__).resolve().parent.parent),
    )
    assert completed.returncode == 0, completed.stderr
    assert "writer-ok" in completed.stdout
    assert detached_browser.is_running(), "the writer process killed the detached browser"

    backend = adapter.connect(detached_browser.endpoint, diagnostics=detached_browser.diagnostics)
    try:
        # A fresh connection in a process that never launched this browser.
        reattached = backend.current(session)
        assert "/keydown" in reattached.final_url
        assert "&quot;a&quot;,&quot;b&quot;" in reattached.html

        pressed = backend.press(session, ("c",))
        # This backend's own key log starts empty after a reattach — it
        # records what *it* dispatched, not what the page remembers.
        assert pressed.key_log == ("c",)
        html = backend.current(session).html
        assert "&quot;a&quot;,&quot;b&quot;,&quot;c&quot;" in html
    finally:
        backend.close(session)
        backend.disconnect()


@requires_browser
class TestPlaywrightBrowserBackendConformance(BrowserBackendConformance):
    """The real backend through the shared suite the fake already passes.

    ``unknown_url`` points at a closed loopback port rather than at the
    fixture site's 404 route, because for a live browser "a URL the backend
    cannot resolve" is a host that does not answer — that is the case whose
    result must be structured rather than an exception. The suite anticipates
    exactly this (``status is None or status >= 400``). A served 404, where
    the server *did* answer, keeps its status and body and is covered
    separately by ``test_a_404_page_keeps_its_status_and_body``.
    """

    @pytest.fixture
    def backend(self, live_backend: adapter.PlaywrightBrowserBackend) -> BrowserBackend:
        return live_backend

    @pytest.fixture
    def session_id(self, request: pytest.FixtureRequest) -> str:
        # One page per test: conformance assertions must not observe another
        # test's navigation or key presses.
        return f"conformance-{request.node.name}"

    @pytest.fixture
    def clean_url(self, fixture_site: str) -> str:
        return f"{fixture_site}/clean"

    @pytest.fixture
    def throw_url(self, fixture_site: str) -> str:
        return f"{fixture_site}/throw"

    @pytest.fixture
    def keydown_url(self, fixture_site: str) -> str:
        return f"{fixture_site}/keydown"

    @pytest.fixture
    def redirect_chain_urls(self, fixture_site: str) -> tuple[str, ...]:
        return (
            f"{fixture_site}/redirect1",
            f"{fixture_site}/redirect2",
            f"{fixture_site}/final",
        )

    @pytest.fixture
    def unknown_url(self) -> str:
        import socket

        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        return f"http://127.0.0.1:{port}/never-served"
