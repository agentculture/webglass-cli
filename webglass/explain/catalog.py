"""Markdown catalog for ``webglass-cli explain <path>``.

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

A clonable template for AgentCulture mesh agents. It carries an agent-first CLI
(cited from the teken `python-cli` reference), a mesh identity (`culture.yaml` +
`CLAUDE.md`), the canonical guildmaster skill kit under `.claude/skills/`, and a
buildable/deployable package baseline. Clone it, rename the package, edit
`culture.yaml`, and you have a new agent.

## Verbs

- `webglass-cli whoami` — identity probe from `culture.yaml`.
- `webglass-cli learn` — structured self-teaching prompt.
- `webglass-cli explain <path>` — markdown docs for any noun/verb.
- `webglass-cli overview` — descriptive snapshot of the agent.
- `webglass-cli doctor` — check the agent-identity invariants.
- `webglass-cli cli overview` — describe the CLI surface.

## Exit-code policy

- `0` success
- `1` user-input error
- `2` environment / setup error
- `3+` reserved

## See also

- `webglass-cli explain whoami`
- `webglass-cli explain doctor`
"""

_WHOAMI = """\
# webglass-cli whoami

Reports the agent's identity from `culture.yaml`: nick (`suffix`), backend,
served model, and the package version. Read-only.

## Usage

    webglass-cli whoami
    webglass-cli whoami --json
"""

_LEARN = """\
# webglass-cli learn

Prints a structured self-teaching prompt covering purpose, command map,
exit-code policy, `--json` support, and the `explain` pointer.

## Usage

    webglass-cli learn
    webglass-cli learn --json
"""

_EXPLAIN = """\
# webglass-cli explain <path>

Prints markdown documentation for any noun/verb path. Unlike `--help` (terse,
positional), `explain` is global and addressable by path.

## Usage

    webglass-cli explain webglass-cli
    webglass-cli explain whoami
    webglass-cli explain --json <path>
"""

_OVERVIEW = """\
# webglass-cli overview

Read-only descriptive snapshot of the agent: identity (from `culture.yaml`), the
verb surface, and the sibling-pattern artifacts the template carries. Accepts an
ignored `target` so a stray path never hard-fails.

## Usage

    webglass-cli overview
    webglass-cli overview --json
"""

_DOCTOR = """\
# webglass-cli doctor

Checks the agent-identity invariants `steward doctor` verifies:
prompt-file-present and backend-consistency (`colleague` → `AGENTS.colleague.md`), plus a
skills-present check. Exits 1 when unhealthy.

## Usage

    webglass-cli doctor
    webglass-cli doctor --json
"""

_CLI = """\
# webglass-cli cli

Noun group for CLI-surface introspection. `cli overview` describes the CLI
itself (distinct from the global `overview`, which describes the agent).

## Usage

    webglass-cli cli overview
    webglass-cli cli overview --json
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
