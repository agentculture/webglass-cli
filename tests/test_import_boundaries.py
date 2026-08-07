"""Import-boundary scan: webglass stays free of Playwright and Colleague coupling.

The policy core must never learn Colleague's own overlay-file format, and
Playwright must never spread past the one module that adapts it — see
CLAUDE.md "Target architecture" sections 8 and 17, and the M0-M2 build spec's
honesty conditions: "no playwright import exists outside the adapter module"
and "webglass code never imports Colleague and never reads .colleague files".

This test walks every ``.py`` file under ``webglass/`` (the shipped package,
not the test suite) and asserts, via the AST, that no ``import`` statement
names ``playwright`` (outside :data:`_PLAYWRIGHT_ADAPTER`) or ``colleague``
(anywhere); and via a literal-text scan that no ``.colleague`` path token
appears. The text scan deliberately excludes ``AGENTS.colleague.md`` (the
colleague-backend prompt-file name), which legitimately contains the
substring "colleague" as part of the backend-consistency mapping in
``doctor.py`` — that is a filename, not a read of Colleague's own
overlay-config file/directory.

Scope note: through M0 this banned playwright *everywhere* in the package,
because zero playwright imports existed anywhere yet. Task t11 landed
``webglass/adapters/playwright.py``, so the ban is now one module narrower —
and exactly one module narrower. Whether a Playwright *type* leaks through
that module's public signatures is a different question, checked by
``tests/test_playwright_adapter.py``.
"""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

import webglass

_PACKAGE_ROOT = Path(webglass.__file__).resolve().parent

# "AGENTS.colleague.md" legitimately contains the substring ".colleague" as
# part of the backend -> prompt-file mapping; a literal reference to
# Colleague's own overlay file/directory (".colleague", ".colleague/",
# ".colleague.yaml", ...) would not be preceded by "AGENTS" or followed by
# ".md".
_LITERAL_DOT_COLLEAGUE = re.compile(r"(?<!AGENTS)\.colleague(?!\.md)")

#: The single module allowed to import Playwright (build plan task t11).
#: CLAUDE.md section 8: "only ``adapters/playwright.py`` does, behind the
#: protocol seams". Widening this tuple is an architectural decision, not a
#: test fix.
_PLAYWRIGHT_ADAPTER = (_PACKAGE_ROOT / "adapters" / "playwright.py",)


def _package_source_files() -> list[Path]:
    return sorted(_PACKAGE_ROOT.rglob("*.py"))


def test_package_root_resolves_under_webglass() -> None:
    # Guards the scan itself: if this ever pointed outside the package (e.g.
    # a packaging change breaks webglass.__file__), every other assertion in
    # this file would silently pass over zero files.
    assert _PACKAGE_ROOT.name == "webglass"
    files = _package_source_files()
    assert files, "expected at least one .py file under webglass/"


def test_no_playwright_import_outside_the_adapter_module() -> None:
    offenders: list[str] = []
    for path in _package_source_files():
        if path in _PLAYWRIGHT_ADAPTER:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "playwright" or alias.name.startswith("playwright."):
                        offenders.append(f"{path}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                if node.module and (
                    node.module == "playwright" or node.module.startswith("playwright.")
                ):
                    offenders.append(f"{path}: from {node.module} import ...")
    assert (
        not offenders
    ), "playwright import outside webglass/adapters/playwright.py:\n" + "\n".join(offenders)


def test_the_playwright_adapter_module_exists_and_does_import_playwright() -> None:
    # Guards the exemption above: if the adapter were renamed or deleted, the
    # scan would keep passing while silently protecting nothing, and a real
    # leak elsewhere could be waved through by a stale allow-list.
    (adapter_path,) = _PLAYWRIGHT_ADAPTER
    assert adapter_path.exists(), f"expected the Playwright adapter at {adapter_path}"
    tree = ast.parse(adapter_path.read_text(encoding="utf-8"), filename=str(adapter_path))
    imports_playwright = any(
        (
            isinstance(node, ast.ImportFrom)
            and node.module
            and (node.module == "playwright" or node.module.startswith("playwright."))
        )
        or (
            isinstance(node, ast.Import)
            and any(
                alias.name == "playwright" or alias.name.startswith("playwright.")
                for alias in node.names
            )
        )
        for node in ast.walk(tree)
    )
    assert imports_playwright, f"{adapter_path} is the exempt module but imports no playwright"


def test_no_colleague_import_anywhere_in_the_package() -> None:
    offenders: list[str] = []
    for path in _package_source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "colleague" or alias.name.startswith("colleague."):
                        offenders.append(f"{path}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                if node.module and (
                    node.module == "colleague" or node.module.startswith("colleague.")
                ):
                    offenders.append(f"{path}: from {node.module} import ...")
    assert (
        not offenders
    ), "Colleague import found — WebGlass must never import Colleague:\n" + "\n".join(offenders)


def test_no_literal_dot_colleague_path_usage() -> None:
    offenders: list[str] = []
    for path in _package_source_files():
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _LITERAL_DOT_COLLEAGUE.search(line):
                offenders.append(f"{path}:{lineno}: {line.strip()}")
    assert not offenders, (
        "literal '.colleague' path usage found — the policy core must never "
        "read Colleague's own overlay files:\n" + "\n".join(offenders)
    )


def test_dot_colleague_regex_does_not_false_positive_on_the_prompt_file_name() -> None:
    # Guards the regex itself: a bug here could make the scan above vacuously
    # pass by matching nothing at all, including real hits.
    assert not _LITERAL_DOT_COLLEAGUE.search("backend 'colleague' requires AGENTS.colleague.md")
    assert _LITERAL_DOT_COLLEAGUE.search('Path(".colleague") / "config.yaml"')
    assert _LITERAL_DOT_COLLEAGUE.search("open('.colleague.yaml')")


def test_playwright_is_the_only_runtime_dependency() -> None:
    """Companion to the import-scan, from the packaging side.

    ``dependencies = []`` was true through M0 only. The 2026-08-07 user
    decision (spec claim c8) makes Playwright a *core* runtime dependency
    rather than the extra issue #1 section 12 recommended — so the honest
    assertion is no longer "no dependencies" but "Playwright and nothing
    else". Everything the operation model needs stays stdlib-first
    (CLAUDE.md section 8: SQLite plus a content-addressed artifact store),
    and adding a second runtime dependency should be as deliberate a decision
    as adding the first was: change this test only alongside that decision.
    """
    pyproject = _PACKAGE_ROOT.parent / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    dependencies = data["project"]["dependencies"]
    names = sorted(re.split(r"[<>=!~\[\s]", spec, maxsplit=1)[0] for spec in dependencies)
    assert names == ["playwright"], f"unexpected runtime dependencies: {dependencies}"
    # And it is pinned, not floating: an unbounded browser dependency would
    # silently change the Chromium revision every install (t14 reports the
    # pinned versions in `doctor`).
    assert any(bound in dependencies[0] for bound in (">=", "==", "~=")), dependencies[0]
