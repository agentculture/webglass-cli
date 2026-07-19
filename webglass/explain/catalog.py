"""Markdown catalog for ``webglass explain <path>``.

Each entry is verbatim markdown. Keys are command-path tuples. The empty tuple,
``("webglass-cli",)`` (the dist name), and ``("webglass",)`` (the import-package
name the agent-first rubric's ``explain_self`` check probes) all resolve to the
root entry.

Keep bodies self-contained: an agent reading one entry should get enough
context without chaining reads.
"""

from __future__ import annotations

_ROOT = """\
# webglass-cli

WebGlass — the guarded web operations and evidence plane for AI agents.

It turns agent intent into normalized web operations, applies web-specific
policy, drives search/fetch/browser backends, returns token-efficient page
state, records navigational provenance, and produces durable, inspectable
evidence. It is not a thin Playwright wrapper, not a generic scraper, and not a
second agent that decides what to believe: WebGlass records what it observed,
and the calling agent draws the conclusions.

## Status

**Pre-implementation.** The web operation surface (`search`, `page`, `action`,
`session`, `exploration`, `evidence`, `memory`, `policy`, `operation`) is
specified but not built — see the build brief at
<https://github.com/agentculture/webglass-cli/issues/1>. What ships today is the
agent-first introspection CLI below, plus the contracts every future verb
registers onto. The runtime has no third-party dependencies yet.

## Invocation

The console script is `webglass`. `webglass-cli` is the distribution/PyPI name
and is **not** an invocable binary; `webglass` is the import package too.

## Verbs

- `webglass whoami` — identity probe from `culture.yaml`.
- `webglass learn` — structured self-teaching prompt.
- `webglass explain <path>` — markdown docs for any noun/verb.
- `webglass overview` — descriptive snapshot of the agent.
- `webglass doctor` — check the agent-identity invariants.
- `webglass cli overview` — describe the CLI surface.

## Contracts

Every command supports `--json`. Results go to stdout; errors and diagnostics go
to stderr — never mixed. Failures carry `{code, message, remediation}`; no Python
traceback ever reaches stderr.

## Exit-code policy

- `0` success
- `1` user-input error
- `2` environment / setup error
- `3+` reserved

## See also

- `webglass explain whoami`
- `webglass explain doctor`
"""

_WHOAMI = """\
# webglass whoami

Reports the agent's identity from `culture.yaml`: nick (`suffix`), backend,
served model, and the package version. Read-only.

## Usage

    webglass whoami
    webglass whoami --json
"""

_LEARN = """\
# webglass learn

Prints a structured self-teaching prompt covering purpose, command map,
exit-code policy, `--json` support, and the `explain` pointer.

## Usage

    webglass learn
    webglass learn --json
"""

_EXPLAIN = """\
# webglass explain <path>

Prints markdown documentation for any noun/verb path. Unlike `--help` (terse,
positional), `explain` is global and addressable by path.

## Usage

    webglass explain webglass
    webglass explain whoami
    webglass explain --json <path>
"""

_OVERVIEW = """\
# webglass overview

Read-only descriptive snapshot of the agent: identity (from `culture.yaml`), the
verb surface, and the sibling-pattern artifacts this repo carries. Accepts an
ignored `target` so a stray path never hard-fails.

## Usage

    webglass overview
    webglass overview --json
"""

_DOCTOR = """\
# webglass doctor

Checks the agent-identity invariants `steward doctor` verifies:
prompt-file-present and backend-consistency (`colleague` → `AGENTS.colleague.md`), plus a
skills-present check. Exits 1 when unhealthy.

## Usage

    webglass doctor
    webglass doctor --json
"""

_CLI = """\
# webglass cli

Noun group for CLI-surface introspection. `cli overview` describes the CLI
itself (distinct from the global `overview`, which describes the agent).

## Usage

    webglass cli overview
    webglass cli overview --json
"""


ENTRIES: dict[tuple[str, ...], str] = {
    (): _ROOT,
    ("webglass-cli",): _ROOT,
    ("webglass",): _ROOT,
    ("whoami",): _WHOAMI,
    ("learn",): _LEARN,
    ("explain",): _EXPLAIN,
    ("overview",): _OVERVIEW,
    ("doctor",): _DOCTOR,
    ("cli",): _CLI,
    ("cli", "overview"): _CLI,
}
