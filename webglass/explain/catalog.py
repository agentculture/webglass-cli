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

**The M0-M2 surface is real and shipped.** `search`, `page`, `action`, and
`session` all execute through one operation service against a real headless
Chromium (Playwright, a core runtime dependency since M2) — see
`webglass explain page`, `webglass explain action`, and `webglass explain
session`. `search` needs `$WEBGLASS_BRAVE_API_KEY`; without one, and for any
web verb under `WEBGLASS_BROWSER_BACKEND=none`, the result is a structured
`backend_unavailable` outcome, never a crash or a silent no-op. Loopback and
private-network targets stay denied unless an explicit `--policy-profile`
declares them.

**`exploration`, `evidence`, `memory`, `policy`, and `operation` are not
built yet** — tracked as milestones M3-M6 in
<https://github.com/agentculture/webglass-cli/issues/8>. Remote actions
beyond `action press`'s preview-by-default (or execute, under a declared test
profile) do not exist: no fill/select/submit/upload/download, and no applied
— only previewed — remote action anywhere in the surface. See the build
brief at <https://github.com/agentculture/webglass-cli/issues/1> for the full
target architecture.

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
- `webglass search <query>` — run a search operation (needs a search backend).
- `webglass page open|read|inspect|extract|links|screenshot` — one page's lifecycle.
- `webglass action follow|press` — follow a link reference or dispatch keys.
- `webglass session create|list|show|close|clean` — browser session lifecycle.

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
- `webglass explain page`
- `webglass explain session`
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
# The web-operation surface (build plan tasks t10 + t13). Every verb below
# builds a webglass.operations.WebOperation, executes it through the one
# WebGlassService (webglass/service.py), and renders the same structured
# WebOperationResult in text or --json — never a second contract. The browser
# backend is wired in by default (see webglass/cli/_factory.py); the search
# provider needs an API key. A verb whose backend is absent reports a
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

The provider is the Brave Search API, wired in when
`$WEBGLASS_BRAVE_API_KEY` is set. Without it no provider is injected and this
verb reports a structured `backend_unavailable` result (exit 1) — a
configuration answer, not a crash. The key is read at call time and never
written to a log, a session record, a result, or any WebGlass store.

## Usage

    export WEBGLASS_BRAVE_API_KEY=...
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

## Naming the page

Three ways, checked in this order:

  - `--page-ref <snapshot-id>` — project a snapshot this process retained.
    No network access at all.
  - `--url <url>` — open the URL, then apply the verb, in one operation.
  - `--session-id <id>` — re-read that session's *live* page without
    navigating it. This is how a later one-shot invocation observes what an
    earlier one's `page open` or `action press` did: navigating again would
    destroy the in-memory page state being asked about.

`page open` given no `--session-id` runs in a throwaway session created and
closed inside the invocation, so it leaves no browser behind.

## Status

A real Chromium is the default backend. Set `WEBGLASS_BROWSER_BACKEND=none`
for the explicit browser-less posture, in which every verb below reports a
structured `backend_unavailable` result (exit 1).

Loopback and private-network targets stay denied by default: reaching a local
app under test requires naming its origin in a `--policy-profile` file's
`declared_targets`. A malformed profile fails closed (exit 2), never open.

## Usage

    webglass page overview
    webglass page open <url>
    webglass page read --page-ref <ref>
    webglass page inspect --url <url> --lens console --json
    webglass page extract --selector '#agent-state' --url <url> --json
    webglass page links --page-ref <ref>
    webglass page screenshot --url <url> --out shot.png

## See also

  - `webglass explain page open`
  - `webglass explain action`
"""

_PAGE_OPEN = """\
# webglass page open <url>

Navigate a session to `url` and retain the resulting `PageSnapshot` for later
lens operations (`read`/`inspect`/`extract`/`links`/`screenshot`). Every
navigation is policy-checked (URL and every redirect hop) before it happens.

Console messages and uncaught page errors observed during the navigation are
reported under `content.untrusted` — page-authored text, never rendered as a
WebGlass diagnostic. Use `page inspect --lens console` to ask for them
explicitly, including on a page that produced none.

An unreachable target (connection refused, DNS failure, navigation timeout)
is a structured `navigation_failed` result telling you to check your server:
WebGlass never starts, stops, or supervises the app under test.

## Usage

    webglass page open https://example.com/
    webglass page open https://example.com/ --session-id <id> --json
    webglass page open http://127.0.0.1:8000/ --policy-profile test-profile.json
"""

_PAGE_READ = """\
# webglass page read

Ordered readable blocks from a retained snapshot, with a resumable cursor and
a declared content budget. Pass `--page-ref` to project a snapshot already
retained by an earlier `page open`, `--url` to open-then-read in one call, or
`--session-id` to read a session's live page without navigating it. A stale
or unknown `--page-ref` fails clearly rather than silently re-fetching.

## Usage

    webglass page read --page-ref <ref>
    webglass page read --session-id <id> --json
    webglass page read --page-ref <ref> --cursor block:3 --json
"""

_PAGE_INSPECT = """\
# webglass page inspect

Project one lens (`outline`, `controls`, `metadata`, `console`, `structure`)
over a snapshot — a view of the same snapshot `page read` and `page extract`
share, preserving stable block/link/field references.

## The console lens

`--lens console` reports the page's console messages and uncaught page errors
(each with its source URL and line, where the browser provided one). Both are
**always** present as lists, so a page that produced nothing yields two empty
lists rather than silence: "observed, and there was nothing" and "nobody
looked" are different answers, and only the first one lets you tell a dead
canvas from a working one by evidence alone.

That text is untrusted source material. A page logging
`WEBGLASS WARNING: ...` gets it rendered under `content.untrusted` and
nowhere else — never among the result's own warnings.

## Usage

    webglass page inspect --page-ref <ref> --lens controls
    webglass page inspect --url <url> --lens console --json
    webglass page inspect --session-id <id> --lens console --json
"""

_PAGE_EXTRACT = """\
# webglass page extract [query]

Two deterministic modes — never a model-generated summary in either.

**Query mode** (the positional argument): readable blocks ranked by distinct
query-term overlap, ties broken by source order. Every returned block keeps
its original `block:<n>` reference, so a citation still points at its source.

**Selector mode** (`--selector`): exactly the matching elements' own content
and nothing else — including elements the readable pipeline deliberately
drops, such as a `<script type="application/json">` state node an app under
test exposes for machine reading. The match text is returned verbatim, so it
parses. Supported forms: `tag`, `#id`, `.class`, `[attr]`, `[attr=value]`,
and combinations such as `script#agent-state`.

A selector that matches nothing is a success with zero matches; a selector
that is not understood is a structured `invalid_argument` naming the
supported forms. Those are different states and stay different.

## Usage

    webglass page extract "pricing" --page-ref <ref>
    webglass page extract --selector '#agent-state' --url <url> --json
    webglass page extract --selector '#keylog' --session-id <id> --json
"""

_PAGE_LINKS = """\
# webglass page links

Every link on a retained snapshot, with its stable `link:<n>` reference —
the reference `action follow` consumes.

## Usage

    webglass page links --page-ref <ref>
    webglass page links --url <url> --json
"""

_PAGE_SCREENSHOT = """\
# webglass page screenshot

Capture the current page as a PNG stored in the injected artifact store; the
result carries a content-addressed reference (hash + size), never the image
bytes inline. Names the page with `--session-id`, `--url` (open then
capture), or `--page-ref` (a retained snapshot names its own session).

## --out

`--out PATH` also writes the PNG to a path you choose. This is the *only*
place WebGlass writes bytes to a caller-supplied path, and the reason it is
allowed here is that these are WebGlass-rendered bytes: the browser's own
screenshotter produced them, with no attacker-chosen filename, content type,
or payload involved. Remote-origin download bytes are a different thing and
stay quarantined behind the shell-cli export bridge (milestone M5).

A write failure is a structured `artifact_write_failed` result; the artifact
itself is still stored and reachable by hash.

## Usage

    webglass page screenshot --page-ref <ref>
    webglass page screenshot --session-id <id> --json
    webglass page screenshot --url <url> --out shot.png --json
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

`action press` classifies as `remote-action` (on the open web a key press
cannot be proven navigational — `Enter` submits forms) and previews by
default. Under a `--policy-profile` whose `declared_targets` name your app
under test it classifies as `observe` and executes instead. `--apply` is
denied either way; the prepare -> commit -> verify protocol lands at M5.

## Usage

    webglass action overview
    webglass action follow <page-ref> <link-ref>
    webglass action press Enter --session-id <id>
    webglass action press a b c --session-id <id> --policy-profile test.json

## See also

  - `webglass explain page links`
  - `webglass explain action press`
"""

_ACTION_FOLLOW = """\
# webglass action follow <page-ref> <link-ref>

Follow a link *reference* (`link:<n>`) from a retained snapshot — never a raw
selector or URL. The href is resolved relative to the snapshot's final URL
and policy-checked exactly like any caller-supplied URL: a page cannot widen
policy by putting a private-network href in an anchor.

## Status

Snapshots are retained per process, so pair this with a `page open` in the
same invocation (or pass a `--page-ref` from one). A page-ref naming nothing
retained is a structured `unknown_snapshot` failure (exit 1); durable,
cross-invocation snapshot retention is milestone M3.

## Usage

    webglass action follow snap-1 link:3
    webglass action follow snap-1 link:3 --json
"""

_ACTION_PRESS = """\
# webglass action press <keys...>

Dispatch a key sequence to a session's focused page, in order, optionally
with `--delay-ms` between consecutive keys. Key names are the browser's own
`KeyboardEvent.key` values (`a`, `ArrowRight`, `Enter`).

## Effect class

By default this classifies as `remote-action` and **previews**: nothing is
dispatched. That is classify-upward doing its job — on the open web a key
press cannot be proven navigational, because `Enter` submits forms
(CLAUDE.md "Target architecture" section 3).

Under a `--policy-profile` whose `declared_targets` name your app under test,
it classifies as `observe` and executes, all keys included. The profile is
the authorization, and it is data in a file — not a flag that widens policy,
and not a code path that skips it. The same profile is what lets the session
reach a loopback app in the first place.

`--apply` is denied in both cases: the prepare -> commit -> verify protocol
for real remote actions lands at M5.

## Reading back what the keys did

`content.trusted.press.pressed` echoes what you asked for. To see what the
*page* did with it, re-read the live page:
`webglass page extract --selector '#keylog' --session-id <id> --json`.

## Usage

    webglass action press Enter --session-id <id>              # previews
    webglass action press a b c --session-id <id> --delay-ms 50 \\
        --policy-profile test.json --json                      # executes
    webglass action press Enter --session-id <id> --apply       # denied
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

Sessions persist on disk (one 0600 record per session in a 0700 directory
under `$WEBGLASS_STATE_DIR`, else `$XDG_STATE_HOME/webglass`, else
`~/.local/state/webglass`), so a session created by one CLI process is usable
by the next. `session create` launches a real detached browser only when a
browser backend is configured (`WEBGLASS_BROWSER_BACKEND=playwright`);
making that the default belongs to the page/action verbs (build plan t13).

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

Create a new browser session record, and — when a browser backend is
configured — launch the detached browser it names. The record's
`endpoint_ref` (the connect endpoint) is secret-equivalent: it is stored in a
0600 file and never appears in JSON output, logs, or evidence — only
`to_public_dict()`'s redacted shape is ever rendered.

The sandbox posture is on the record: a session whose browser was launched
with the explicit `WEBGLASS_ALLOW_UNSANDBOXED=1` opt-in reports
`sandboxed: false` and a sandbox-disabled diagnostic on every render.

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

Close a session and stop its browser: the process is terminated and its
profile directory removed, so a closed session leaves nothing running.
Touches no evidence record and no exploration edge — the four state kinds
(CLAUDE.md "Target architecture" section 2) stay separate.

## Usage

    webglass session close <session-id>
    webglass session close <session-id> --json
"""

_SESSION_CLEAN = """\
# webglass session clean

Reap this store's expired sessions (past their `expires_at`) *and* their
browser processes: each reaped session's browser is terminated by its stored
pid and its profile directory removed, so a crashed caller leaves no orphan
browser past expiry. An already-dead process is not an error — the result
reports `browser_reaped` and `browser_was_running` per session. Expired
leases on still-live sessions are released, and long-dead or unreadable
record files are purged.

Only this caller's reaped sessions are listed in the result; a warning notes
if other callers' sessions were also reaped from a shared store.

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
