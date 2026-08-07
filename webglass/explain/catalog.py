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
skills-present check. Also runs five browser-capability checks:
`playwright_importable` (error severity — Playwright is a core runtime
dependency), `chromium_installed`, `playwright_version` (installed version
plus the pinned range), `usable_sandbox` (probes
`/proc/sys/kernel/apparmor_restrict_unprivileged_userns` and
`unprivileged_userns_clone` — never attempts `--no-sandbox`), and
`state_dir_writable`. The last four are advisory (`warning`/`info`
severity): a host that simply lacks a browser or a usable sandbox is reported
with actionable remediation but stays healthy. Exits 1 only when an identity
check or `playwright_importable` fails.

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

# ---------------------------------------------------------------------------
# The M1 web-operation surface (build plan task t10). Every verb below builds
# a webglass.operations.WebOperation, executes it through the one
# WebGlassService (webglass/service.py), and renders the same structured
# WebOperationResult in text or --json — never a second contract. M1 ships
# with no search/browser backend wired in by default (see
# webglass/cli/_factory.py), so every verb that needs one reports a
# structured `backend_unavailable` result (exit 1) rather than failing
# silently or falling back to a different kind of operation.
# ---------------------------------------------------------------------------

_SEARCH = """\
# webglass search <query>

Run a search operation: normalizes `query` into a `WebOperation`, executes it
through `WebGlassService`, and renders the structured result. Search results
(`title`/`url`/`snippet`) are untrusted source material — an external
provider authored them, not WebGlass.

## Status

M1 ships with no search backend wired in, so this verb reports a structured
`backend_unavailable` result (exit 1) until build plan t15 lands a real
provider behind the `SearchProvider` seam.

## Usage

    webglass search "widgets"
    webglass search "widgets" --limit 5 --json
"""

_PAGE = """\
# webglass page

Noun group for one page's lifecycle: `open`, `read`, `inspect`, `extract`,
`links`, `screenshot`. Every verb executes through the same `WebGlassService`
and renders the same `WebOperationResult` shape as the library API — CLI text
output is only a rendering of it, never a second contract.

Bare `webglass page` (no sub-verb) prints `page overview`.

## Status

M1 ships with no browser backend wired in, so every verb below reports a
structured `backend_unavailable` result (exit 1) until build plan t11/t13
land the Playwright adapter.

## Usage

    webglass page overview
    webglass page open <url>
    webglass page read --page-ref <ref>
    webglass page inspect --page-ref <ref> --lens outline
    webglass page extract "query" --page-ref <ref>
    webglass page links --page-ref <ref>
    webglass page screenshot --page-ref <ref>

## See also

  - `webglass explain page open`
  - `webglass explain action`
"""

_PAGE_OPEN = """\
# webglass page open <url>

Navigate a session to `url` and retain the resulting `PageSnapshot` for later
lens operations (`read`/`inspect`/`extract`/`links`/`screenshot`). Every
navigation is policy-checked (URL and every redirect hop) before it happens.

## Usage

    webglass page open https://example.com/
    webglass page open https://example.com/ --session-id <id> --json
"""

_PAGE_READ = """\
# webglass page read

Ordered readable blocks from a retained snapshot, with a resumable cursor and
a declared content budget. Pass `--page-ref` to project a snapshot already
retained by an earlier `page open`, or `--url` to open-then-read in one call.
A stale or unknown `--page-ref` fails clearly rather than silently re-fetching.

## Usage

    webglass page read --page-ref <ref>
    webglass page read --page-ref <ref> --cursor block:3 --json
"""

_PAGE_INSPECT = """\
# webglass page inspect

Project one lens (`outline`, `controls`, `metadata`, `console`, `structure`)
over a retained snapshot — a view of the same snapshot `page read` and `page
extract` share, preserving stable block/link/field references.

## Usage

    webglass page inspect --page-ref <ref> --lens controls
    webglass page inspect --page-ref <ref> --lens console --json
"""

_PAGE_EXTRACT = """\
# webglass page extract <query>

Query-focused block selection: blocks are ranked by distinct query-term
overlap, ties broken by source order. Deterministic — never a model-generated
summary; every returned block keeps its original `block:<n>` reference.

## Usage

    webglass page extract "pricing" --page-ref <ref>
    webglass page extract "pricing" --page-ref <ref> --json
"""

_PAGE_LINKS = """\
# webglass page links

Every link on a retained snapshot, with its stable `link:<n>` reference —
the reference `action follow` consumes.

## Usage

    webglass page links --page-ref <ref>
    webglass page links --page-ref <ref> --json
"""

_PAGE_SCREENSHOT = """\
# webglass page screenshot

Capture the current page as a PNG stored in the injected artifact store; the
result carries a content-addressed reference (hash + size), never the image
bytes inline. Needs either `--session-id` or `--page-ref` (a retained
snapshot names its own session).

## Usage

    webglass page screenshot --page-ref <ref>
    webglass page screenshot --session-id <id> --json
"""

_PAGE_OVERVIEW = """\
# webglass page overview

Read-only descriptive snapshot of the `page` noun: its verbs and current
backend status. Satisfies the agent-first rubric's "any noun with
action-verbs must also expose overview" rule.

## Usage

    webglass page overview
    webglass page overview --json
"""

_ACTION = """\
# webglass action

Noun group for `follow` and `press` — the conservative verbs issue #1
prefers over a generic `click(selector)`. Every verb executes through
`WebGlassService` and renders the same `WebOperationResult` shape as the
library API.

Bare `webglass action` (no sub-verb) prints `action overview`.

`action press` classifies as `remote-action` (it cannot be proven
navigational) and previews by default; `--apply` is always denied at M1 —
the prepare -> commit -> verify protocol lands at M5.

## Usage

    webglass action overview
    webglass action follow <page-ref> <link-ref>
    webglass action press Enter --session-id <id>

## See also

  - `webglass explain page links`
"""

_ACTION_FOLLOW = """\
# webglass action follow <page-ref> <link-ref>

Follow a link *reference* (`link:<n>`) from a retained snapshot — never a raw
selector or URL. The href is resolved relative to the snapshot's final URL
and policy-checked exactly like any caller-supplied URL: a page cannot widen
policy by putting a private-network href in an anchor.

## Status

M1 ships with no browser backend wired in, and no snapshot to follow a link
from without one, so this verb always reports a structured failure (exit 1)
— `unknown_snapshot` for a page-ref naming nothing retained, or
`backend_unavailable` once a real one does — until build plan t11/t13 land
the Playwright adapter.

## Usage

    webglass action follow snap-1 link:3
    webglass action follow snap-1 link:3 --json
"""

_ACTION_PRESS = """\
# webglass action press <keys...>

Dispatch a key sequence on an open session. Classifies as `remote-action`
(CLAUDE.md "Target architecture" section 3: a key press cannot be proven
navigational outside a declared test profile) and previews by default;
passing `--apply` requests apply and is always denied at M1 — the
prepare -> commit -> verify protocol lands at M5.

## Usage

    webglass action press Enter --session-id <id>
    webglass action press a b c --session-id <id> --delay-ms 50 --json
    webglass action press Enter --session-id <id> --apply   # denied at M1
"""

_ACTION_OVERVIEW = """\
# webglass action overview

Read-only descriptive snapshot of the `action` noun: its verbs and current
backend status. Satisfies the agent-first rubric's "any noun with
action-verbs must also expose overview" rule.

## Usage

    webglass action overview
    webglass action overview --json
"""

_SESSION = """\
# webglass session

Noun group for browser session lifecycle: `create`, `list`, `show`, `close`,
`clean`. Every verb executes through `WebGlassService` and renders the same
`WebOperationResult` shape as the library API.

Bare `webglass session` (no sub-verb) prints `session overview`.

## Status

M1 sessions live in an in-memory, per-process store (see `webglass explain
session overview`): they survive across `webglass` invocations within the
same process, but not across separate CLI subprocesses yet — cross-invocation
persistence lands at build plan task t12.

## Usage

    webglass session overview
    webglass session create
    webglass session list
    webglass session show <session-id>
    webglass session close <session-id>
    webglass session clean
"""

_SESSION_CREATE = """\
# webglass session create

Create a new browser session record. The record's `endpoint_ref` (the
connect endpoint) is secret-equivalent and never appears in JSON output, logs,
or evidence — only `to_public_dict()`'s redacted shape is ever rendered.

## Usage

    webglass session create
    webglass session create --ttl-seconds 60 --json
"""

_SESSION_LIST = """\
# webglass session list

List this caller's own sessions — never another caller's, even in a shared
store.

## Usage

    webglass session list
    webglass session list --json
"""

_SESSION_SHOW = """\
# webglass session show <session-id>

Show one session's public record (its redacted shape — no connect endpoint).

## Usage

    webglass session show <session-id>
    webglass session show <session-id> --json
"""

_SESSION_CLOSE = """\
# webglass session close <session-id>

Close a session. Touches no evidence record and no exploration edge — the
four state kinds (CLAUDE.md "Target architecture" section 2) stay separate.

## Usage

    webglass session close <session-id>
    webglass session close <session-id> --json
"""

_SESSION_CLEAN = """\
# webglass session clean

Reap this store's expired sessions (past their `expires_at`). Only this
caller's reaped sessions are listed in the result; a warning notes if other
callers' sessions were also reaped from a shared store.

## Usage

    webglass session clean
    webglass session clean --json
"""

_SESSION_OVERVIEW = """\
# webglass session overview

Read-only descriptive snapshot of the `session` noun: its verbs and current
persistence status. Satisfies the agent-first rubric's "any noun with
action-verbs must also expose overview" rule.

## Usage

    webglass session overview
    webglass session overview --json
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
    ("search",): _SEARCH,
    ("page",): _PAGE,
    ("page", "open"): _PAGE_OPEN,
    ("page", "read"): _PAGE_READ,
    ("page", "inspect"): _PAGE_INSPECT,
    ("page", "extract"): _PAGE_EXTRACT,
    ("page", "links"): _PAGE_LINKS,
    ("page", "screenshot"): _PAGE_SCREENSHOT,
    ("page", "overview"): _PAGE_OVERVIEW,
    ("action",): _ACTION,
    ("action", "follow"): _ACTION_FOLLOW,
    ("action", "press"): _ACTION_PRESS,
    ("action", "overview"): _ACTION_OVERVIEW,
    ("session",): _SESSION,
    ("session", "create"): _SESSION_CREATE,
    ("session", "list"): _SESSION_LIST,
    ("session", "show"): _SESSION_SHOW,
    ("session", "close"): _SESSION_CLOSE,
    ("session", "clean"): _SESSION_CLEAN,
    ("session", "overview"): _SESSION_OVERVIEW,
}
