# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is (and is not yet)

`webglass-cli` is **WebGlass** — *the guarded web operations and evidence plane
for AI agents.* The single authoritative build brief is
**[issue #1](https://github.com/agentculture/webglass-cli/issues/1)**; it
replaced the original scaffold brief and absorbed the earlier design spec in #2
(now closed). **Read issue #1 before designing anything.** Where this file and
issue #1 disagree, issue #1 wins — and fix this file.

WebGlass turns agent intent into normalized web operations, applies web-specific
policy, drives search/fetch/browser backends, returns token-efficient page
state, records navigational provenance, and produces durable inspectable
evidence:

```text
Colleague intent
  -> WebOperation
  -> capability + web policy
  -> search / fetch / browser backend
  -> PageSnapshot + Evidence + Effects
  -> Colleague interpretation and next decision
```

It is explicitly **not** a thin Playwright wrapper, not a generic scraper, and
not a second agent that decides what to believe. The ownership split is the
load-bearing idea:

- **Colleague owns** the research question, strategy, role/capability
  authorization, hooks, conclusions, and user communication.
- **WebGlass owns** operation normalization, URL/network policy, browser
  sessions, navigation mechanics, extraction, token budgets, evidence capture,
  exploration history, and web-specific memory.
- **shell-cli owns** local workspace operations. Files crossing between web and
  workspace go through an explicit bridge — never arbitrary browser filesystem
  access.
- **External providers own** ranking, page content, identity, site behavior.
  WebGlass records what they returned; it does not certify their truth.

Dependency direction stays one-way: `Colleague -> webglass-cli` and
`Colleague -> shell-cli`. WebGlass must never reach into Colleague internals,
and shell-cli must never learn WebGlass semantics. They are sibling capability
providers; neither becomes the other's god object.

**None of the product exists yet.** What ships today is the
`culture-agent-template` scaffold: the **agent-first introspection CLI** (cited
from [teken](https://github.com/agentculture/teken)'s `afi-cli` `python-cli`
reference) with verbs `whoami` / `learn` / `explain` / `overview` / `doctor`
plus the `cli` noun group. Runtime third-party dependencies are **zero**
(`dependencies = []`); `teken` is dev-only.

So keep two layers straight:

- **The CLI skeleton** (below) — registration / error / output / explain
  machinery. Real, tested, and the foundation every WebGlass noun registers onto.
- **The WebGlass product** (issue #1, summarized below) — the target
  architecture, which is an *operation contract*, not a verb list.

Much of the runtime *text* still calls this repo "a clonable template for
AgentCulture mesh agents" (the `learn` body, the `explain` root entry in
`webglass/explain/catalog.py`, the argparse `prog` description in
`webglass/cli/__init__.py:74`, `README.md`). Those strings are the CLI's own
documentation surface — update them as real WebGlass nouns land.

## Commands

**uv**-managed, Python 3.12+. The console script is **`webglass`** (see gotchas —
it is *not* `webglass-cli`).

```bash
uv sync                                    # create .venv and install (incl. dev group)
uv run webglass whoami                     # run the CLI; --json on every verb
python -m webglass whoami                  # equivalent entry point

uv run pytest -n auto                      # full test suite (xdist parallel)
uv run pytest tests/test_cli.py::test_whoami_text -v   # a single test
uv run pytest -n auto --cov=webglass --cov-report=term # coverage (fail_under=60)
```

Lint / format / security — these are the CI `lint` job and must all pass:

```bash
uv run black --check webglass tests        # line-length 100
uv run isort --check-only webglass tests   # black profile
uv run flake8 webglass tests
uv run bandit -c pyproject.toml -r webglass # B101/B404/B603 skipped (see pyproject)
markdownlint-cli2 "**/*.md" "#node_modules" "#.local" "#.claude/skills" "#.teken"
uv run teken cli doctor . --strict         # the agent-first rubric gate (see below)
```

(Drop `--check`/`--check-only` from black/isort to auto-format.)

## Architecture: the CLI skeleton (what exists)

Four contract-driven concerns; read them together.

**Command registration** (`webglass/cli/__init__.py`). `main()` →
`_build_parser()` → `_dispatch()`. Each command is a module under
`webglass/cli/_commands/` exposing `register(sub)` that adds its subparser and
calls `set_defaults(func=<handler>)` (plus `json=False` for bare noun groups).
To add a verb: write the module, then call its `register()` inside
`_build_parser` (there is a marked "Register your own noun groups here" spot).
Handlers return `None`/`0` or raise `CliError`; `_dispatch` translates that into
the exit code.

**Error contract** (`_errors.py` + `_output.py`). *No Python traceback ever
reaches stderr.* Every failure raises `CliError(code, message, remediation)`.
`_dispatch` routes `CliError` through `emit_error`; any *other* exception is
wrapped into a `CliError` so even bugs surface as the structured shape. Argparse
errors (unknown verb, missing arg) route through the same contract:
`_CliArgumentParser` overrides `.error()`, and because subparsers are built with
`parser_class=_CliArgumentParser`, the override propagates to every level —
including nested noun subparsers (see `_commands/cli.py`, which passes
`parser_class=type(p)` explicitly).

**Output contract** (`_output.py`). *Results to stdout, diagnostics and errors to
stderr — never mixed.* Every verb takes `--json`; in JSON mode the payload goes
to stdout and `{code, message, remediation}` to stderr. Text-mode errors render
as `error: <message>` + `hint: <remediation>` — the `hint:` prefix is
load-bearing (agents and the rubric look for it). Because argparse errors fire
*before* `args.json` exists, `main()` peeks at raw argv for `--json` and stashes
it on `_CliArgumentParser._json_hint`.

**Exit-code policy** (centralized in `_errors.py`): `0` success, `1` user-input
error, `2` environment/setup error, `3+` reserved. Documented in `learn` output
because the rubric checks for it.

**The explain catalog** (`webglass/explain/`). `explain` is a *global* verb (not
nested under a noun) resolving a command-path tuple to verbatim markdown.
Entries live in `catalog.py` as `dict[tuple[str, ...], str]`; `resolve()` raises
`CliError` on a miss. **Every new noun/verb must get a catalog entry**, keyed by
its path tuple — otherwise `explain <new-verb>` 404s and the rubric's per-verb
`explain` check fails. `tests/test_cli.py::test_every_catalog_path_resolves`
guards the catalog itself.

### The agent-first rubric gate is a hard CI gate

`uv run teken cli doctor . --strict` enforces teken's seven-bundle agent-first
rubric and runs in CI. A failure reds the build. Practical constraints when
extending:

- **Any noun with action-verbs must also expose `overview`.** The `cli` noun
  exists today purely to satisfy `overview_cli_noun_exists`. Every WebGlass noun
  (`page`, `action`, `session`, `exploration`, `evidence`, `memory`, `policy`,
  `operation`) needs one.
- **`learn`** must be ≥200 chars and mention purpose, command map, exit codes,
  `--json`, and `explain`.
- **Descriptive verbs** (`overview`) must never hard-fail on a stray target path
  — `overview` accepts an ignored positional `target` for exactly this.
- **`explain <self>` must resolve** for both the dist name (`webglass-cli`) *and*
  the **import-package** name (`webglass`), because the rubric's `explain_self`
  check probes the import name. `ENTRIES` carries both as root keys; keep them in
  sync if the package is ever renamed.

## Target architecture (WebGlass — issue #1)

The brief specifies an operation contract, not a feature list. These are the
parts that constrain how you write code.

### 1. The core abstraction is a web operation, not a CLI handler

Build **one operation lifecycle** used by the Python API, the CLI, and
Colleague's tool adapter. Do not architect around CLI handlers or direct
Playwright calls.

`WebOperation` carries: stable operation ID; kind and version; normalized args;
caller intent; caller/task/workspace metadata; capability + policy profile;
session/exploration ID; target URL / page / element / evidence reference;
cache-freshness mode; content and token budgets; effect classification;
preview/apply state; timeout and resource limits.

`WebOperationResult` reports: lifecycle state (`previewed`, `denied`,
`succeeded`, `failed`, `blocked`, `timed_out`, `cancelled`); structured output;
policy verdict and matched rule IDs; known browser/remote effects; evidence
references; redirect and navigation history; cache/freshness status; truncation
and extraction completeness; security warnings and degraded-evidence markers;
timings and backend identity; a stable machine-readable error on failure.

**Library and CLI must return the same semantic result.** Text output is a
*rendering* of the structured result, not a second contract. Operations also
charge a context budget — network requests, transferred bytes, browser time,
retained artifact bytes, estimated agent-visible tokens. Budget exhaustion is a
structured result, never an arbitrary backend failure.

### 2. Four kinds of state stay separate

Never collapse these into one object:

- **Browser session** — live/resumable context with tabs, cookies, storage,
  leases. Volatile and sensitive by default; isolated per caller/task; **never
  emitted wholesale** in JSON, logs, evidence, or diagnostics.
- **Exploration** — durable graph of *why and how* the agent traversed:
  query → result → page → followed link → evidence, with parent/child edges, the
  caller's reason for each branch, and status (useful / duplicate / dead-end /
  blocked / deferred). Resumable **without** the original live browser process.
- **Evidence** — append-only record of what was observed at a time: requested and
  final URL, redirect chain, retrieval time, selected blocks with stable block
  refs, content/artifact hashes, backend and policy context, extraction metadata.
  Evidence records observations; it does not make them true.
- **Web-memory** — searchable index over prior explorations/snapshots/
  extractions/evidence. Answers "have we seen this?" and "what changed?". Never
  silently injects old content into a new answer. **Never a credential store.**

A `WebContext` connects them for one task — caller/task/workspace scope,
effective policy and capabilities, budgets, optional live session reference,
exploration reference, evidence namespace, memory read/write scope. It holds
*references and policy*, not copies. This is what lets Colleague hand a child a
reduced capability set (e.g. read the shared evidence graph, fresh anonymous
session, no remote-action capability).

### 3. Three explicit effect classes

"Browsing is read-only" is false (GETs create logs, cookies, analytics), but
requiring `--apply` for every read would be unusable. So:

- **`observe`** — search, open/follow, read, inspect, extract, list links or
  controls, screenshot, revalidate. Executes when authorized; the result
  acknowledges that network and ordinary browser-state effects may occur.
- **`local-state`** — effects confined to WebGlass-owned state: create/close
  session, update its cookies, cache a snapshot, record an exploration edge, save
  evidence, compact memory. Needs an authorized state scope and retention policy,
  not remote-action approval.
- **`remote-action`** — submit, send, publish, purchase, delete, vote, confirm,
  upload, download, authenticate, or click an ambiguous control whose effect
  cannot be proven navigational. **Previews by default**; requires explicit apply
  authorization. **If classification is uncertain, classify upward.**

Remote actions use **prepare → commit → verify**, not a boolean on `click`.
*Prepare* resolves the exact target and page generation, classifies, applies
policy, and returns an expiring `ActionPlan`. *Commit* takes that exact plan ID
plus explicit authorization and refuses if page generation, target, session
owner, policy, or plan lifetime changed. *Verify* records the observed outcome as
evidence. **Commit is never inferred from prepare, and an uncertain commit is
never auto-retried** — report `outcome_unknown` and preserve the evidence.
There is no rollback for the web: a worktree can undo an edit; you cannot unsend
a message.

### 4. Semantic verbs over raw browser mechanics

Noun boundaries are stable even if spellings evolve:

```text
webglass search ...
webglass page open|read|inspect|extract|links|screenshot ...
webglass action follow|fill|select|press|submit|download|upload ...
webglass session create|list|show|close|clean ...
webglass exploration start|show|resume|mark ...
webglass evidence show|export|verify|cite ...
webglass memory find|show|forget|compact ...
webglass policy check|explain ...
webglass operation preview|show ...
```

Avoid a generic `click(selector)` as the central API. Prefer `follow(link_ref)`,
`fill(field_ref, value)`, `submit(form_ref)`, and `activate(control_ref)` only as
a conservative last resort.

### 5. PageSnapshot is the agent-facing unit; lenses disclose progressively

Agents never consume raw HTML or Playwright handles. A `PageSnapshot` carries
requested/final/canonical URLs, title, language, content type, status, retrieval
time, redirect chain, readable text blocks, outline and landmarks, structured
tables/lists, links/forms/fields/buttons, **stable snapshot-scoped references**
(`link:12`, `field:3`, `block:27`), frame and shadow-root boundaries, content
hash and previous-version relationship, truncation/omitted-region metadata, and
security warnings.

**Element references are valid only for the snapshot/session generation that
produced them — a stale reference must fail clearly rather than hit a different
element.** Raw HTML, full DOM, accessibility trees, network logs, and large
screenshots are opt-in artifacts with size limits.

```text
open      -> small PageCard: identity, status, outline, warnings, change state
inspect   -> selected lens: outline, controls, tables, metadata, structure
read      -> ordered readable blocks with a cursor and declared budget
extract   -> query-focused blocks with original source references
evidence  -> exact retained blocks/artifacts suitable for citation
```

All lenses project the same snapshot and **preserve stable block IDs**, so an
agent moves from compact outline to exact source text without re-fetching or
losing provenance. This is where WebGlass beats raw browser automation.

### 6. Token efficiency without hidden semantic distortion

Extraction is deterministic and inspectable: strip repeated nav/boilerplate,
preserve headings and source order, segment into stable blocks, dedup by hash,
outline-first with details on demand, explicit byte/token budgets, deterministic
chunks and cursors, query-focused ranking, and **identify every omitted,
collapsed, or truncated region**.

**WebGlass must not silently summarize with a model and present it as page
content.** Any model-assisted extraction added later must be a pluggable,
*labeled* transformation retaining source blocks and model provenance. Synthesis
and conclusions stay with Colleague.

### 7. Treat web content as adversarial, and preserve trust zones

Every structured result must keep these distinguishable:

- **trusted control metadata** — operation IDs, policy verdicts, backend
  diagnostics, limits, WebGlass-generated warnings;
- **untrusted source material** — titles, page text, attributes, URLs, download
  names, search snippets, site messages;
- **sensitive caller data** — credentials, form values, upload contents, cookies;
- **derived transformations** — cleaned text, ranked blocks, diffs.

The renderer must never let remote text masquerade as a WebGlass warning, policy
decision, or instruction. Baseline protections: deny `file:`, `javascript:`, and
browser-internal schemes; deny loopback, link-local, private-network, and cloud
metadata targets; validate DNS and **revalidate every redirect hop**; bound
redirects, response sizes, page lifetime, tabs, frames, downloads; preserve the
Chromium sandbox (never `--no-sandbox` as the normal path); quarantine downloads
and never execute them; uploads only from opaque artifact references; redact
credentials/tokens/cookies from logs and evidence; expose TLS, bot-wall,
login-wall, and blocked-content conditions explicitly.

### 8. Policy, persistence, and the replaceable adapter

**Policy** is evaluated by WebGlass (only it understands URLs, redirects,
methods, elements, sessions, downloads). The policy core consumes explicit data
and **must not know about `.colleague` files** — Colleague resolves overlays and
passes an effective profile. **Absent policy and malformed policy are different
states; malformed policy must never silently fail open.**

**Persistence** is a small explicit layer — SQLite plus a content-addressed
artifact store is the stdlib-first baseline. Cache modes are explicit and always
reported with age and freshness decision: `live`, `prefer-cache`, `refresh`,
`cache-only`, `no-store`. **No silent stale reads.** Ship `find` / `show` /
`export` / `forget` / compaction *from the beginning* so persistence never
becomes irreversible. Never persist by default: passwords, raw cookies or auth
headers, complete browser profiles, unbounded bodies, arbitrary form values, or
local file contents used for uploads.

**Playwright/Chromium is the first backend, not the operation model.** Keep
Playwright types out of the public API behind protocols: `SearchProvider`,
`FetchBackend`, `BrowserBackend`, `BrowserSessionStore`, `MemoryStore`,
`ArtifactStore`, `WebPolicyEvaluator`, plus injectable `Clock`/ID providers for
deterministic tests. Keep operation models, policy, extraction, evidence,
memory, and CLI plumbing dependency-light and expose Playwright as a **declared
runtime extra or adapter package** — this repo's `dependencies = []` is a real
property worth defending. Provide `doctor` / browser-install diagnostics, pin and
report compatible versions, fail with actionable capability diagnostics, and
**never silently fall back from a browser operation to a semantically different
fetch operation.**

### 9. Suggested package shape

Avoid a giant `Browser`/`WebGlass` god class:

```text
webglass/
  operations.py  results.py  effects.py  pages.py  extraction.py
  references.py  policy.py   evidence.py exploration.py memory.py
  artifacts.py   sessions.py service.py
  adapters/  search.py  fetch.py  browser.py  playwright.py
  cli/ ...
```

### 10. Delivery sequence — do not skip M0

- **M0 — contract and characterization.** Characterization-test the current CLI,
  JSON/error, explain, identity, and exit-code contracts. Decide operation and
  evidence schema versioning. Build deterministic test pages and hostile
  fixtures. Document the Colleague and shell-cli boundaries. **No Playwright
  implementation precedes this milestone.**
- **M1 — operation core with fake backends.** Models, service, and CLI dispatch
  over injectable fake search/fetch/browser adapters; budgets, cursors,
  cancellation, errors, event streaming. All existing introspection contracts
  preserved.
- **M2 — anonymous Chromium observation.** Playwright adapter, ephemeral
  sessions, open/read/inspect/links/follow/screenshot, deterministic page
  references, URL/redirect/network policy. No auth, uploads, or business actions.
- **M3 — evidence, exploration, Web-memory.** SQLite + content-addressed
  artifacts; capture/citation/verification; exploration graphs and resume; cache
  modes, canonicalization, hashing, change detection, retention, forget.
- **M4 — Colleague provider.** First read-oriented tool schemas by composition;
  contract tests; parallel child-session isolation.
- **M5 — guarded interaction and artifact bridge.** fill/select, preview/apply,
  quarantined downloads + shell-cli export, opaque upload artifacts, outcome
  validation. Send/publish/purchase/delete stay out pending separate review.
- **M6 — additional adapters and authenticated capability.** Only on demand;
  credential brokering is a separately threat-modelled design.

**Before writing product code**, produce the implementation plan answering the
twelve questions in issue #1 §22 and run it through **devague** (the
`/think` → `/spec-to-plan` chain). Challenge especially: generic click
semantics, authenticated session scope, prompt-injection boundaries,
SSRF/redirect enforcement, evidence immutability, stale-cache disclosure,
browser-profile and secret leakage, workspace crossings, parallel child
isolation, and any design that puts Playwright objects in the public contract.

### 11. Invariants to hold, and non-goals to refuse

Invariants: existing `whoami`/`learn`/`explain`/`overview`/`doctor` + JSON +
stderr + exit codes stay compatible; import API and CLI share one operation
implementation; every result identifies live vs cached; every omission is
declared; page content never mixes with trusted tool instructions; stale element
refs fail safely; redirects are policy-checked at every hop; child agents never
accidentally share cookies or mutable tabs; secrets never enter evidence or
memory; downloads never enter a worktree without an explicit shell-cli export;
uploads never accept an arbitrary host path; remote actions never execute from a
preview and never claim rollback; evidence is inspectable independently of
Colleague; malformed config cannot fail open.

Non-goals (refuse scope creep toward these): a general crawler framework;
stealth/anti-bot bypass; CAPTCHA circumvention; a truth engine or fact checker;
an LLM summarizer hidden inside retrieval; a credential vault; arbitrary host
filesystem access from the browser; automatic execution of downloads; automatic
sending/purchasing/publishing/deletion; mirroring every Playwright method;
replacing Colleague's reasoning or shell-cli's local plane.

### 12. Test ownership

WebGlass owns operation/result schema tests, extraction and stable-reference
tests, URL canonicalization and cache-mode tests, redirect/DNS/SSRF and
blocked-scheme tests, Playwright integration against **local deterministic
sites**, hostile-page and prompt-injection labeling tests, download-quarantine
and upload-reference tests, session lease/isolation/cleanup tests,
evidence/hash/truncation/freshness tests, adapter conformance tests, and CLI
JSON/stderr/exit-code/introspection tests. **No default test may depend on a
live public website** — public-web smoke tests are optional and
non-authoritative. Colleague owns role curation, hook ordering, context
propagation, telemetry, citation validation, and end-to-end research flows;
shell-cli owns workspace path resolution and artifact import/export.

## Identity and mesh membership

This repo is a node in the AgentCulture IRC mesh. Identity is declared in
`culture.yaml`: `suffix: webglass-cli`, **`backend: colleague`**, and a pinned
served model. The backend determines the **resident prompt file**: `colleague` →
**`AGENTS.colleague.md`** (not `CLAUDE.md`). So `CLAUDE.md` is *your* operating
guide while `AGENTS.colleague.md` is what the colleague backend residents on —
keep both coherent; the mesh runtime reads `AGENTS.colleague.md`.

`doctor` checks the invariants `steward doctor` enforces: **backend-consistency**
(`claude`→`CLAUDE.md`, `colleague`→`AGENTS.colleague.md`, `acp`→`AGENTS.md`,
`gemini`→`GEMINI.md`) and **skills-present** (`.claude/skills/` non-empty).

Note: `whoami`/`doctor` parse `culture.yaml` with a **hand-rolled line parser**
(`read_agent_fields` in `whoami.py`), deliberately *not* PyYAML, to keep the
runtime dependency-free. It reads only the first agent block's top-level
`suffix`/`backend`/`model`. If WebGlass needs richer config, revisit that — but
adding a YAML dep is a real architectural decision, same as adding Playwright.

## Conventions and workflow

**Memory discipline — recall before, remember after.** This repo shares an
eidetic memory store with its mesh peers (the `claude` and `colleague` backends
both resolve the same `webglass-cli` scope, so they read each other's notes).
Per-task habit:

- **`/recall` before you start.** Search the store for the area you're about to
  touch — prior decisions, gotchas, "have we done this before?" — before
  non-trivial tasks, not just when asked.
- **`/remember` when something worth keeping surfaces.** A non-obvious decision
  and its rationale, a constraint, a fix and *why*, a gotcha that cost time.
  Capture it as it happens.

Default scope is this agent's *private* memory plus the shared *public* pool;
pass `--visibility public` when the fact should reach mesh peers. Don't store
what the repo already records. These are the `recall`/`remember` skills, backed
by the `eidetic` store — distinct from WebGlass's own Web-memory module.

**Version-bump-every-PR (hard CI gate).** The `version-check` job fails any PR
whose `pyproject.toml` version equals `main`'s — *every* PR bumps, even
docs/config/CI-only ones. Use the `version-bump` skill (`major|minor|patch`),
which also prepends a Keep-a-Changelog entry. The version flows into
`webglass.__version__` at runtime via `importlib.metadata`.

**PR lifecycle** runs through the `cicd` skill (layered on the `devex pr` CLI):
lint/open/read/reply plus `status` (SonarCloud quality gate) and `await` (block
until CI + Sonar settle). The Sonar gate is wired in
(`sonar-project.properties`, `sonar.qualitygate.wait=true`) and gates the `test`
job when `SONAR_TOKEN` is set; token-less/fork PRs stay green.

**Skills.** `.claude/skills/` is vendored **cite-don't-import** from
`guildmaster`; `docs/skill-sources.md` is the provenance ledger and re-sync
procedure — read it before touching anything there, and never edit a vendored
script body in place (lift the change upstream and re-vendor). Beyond
`cicd`/`version-bump`: `communicate` (cross-repo issues + mesh messages),
`ask-colleague` (hand a scoped task to a *different* model — reach for `review`
before opening a PR), `think`/`spec-to-plan`/`assign-to-workforce` (the devague
idea→spec→plan→build chain, which issue #1 §22 explicitly requires before
product code), `run-tests`, `sonarclaude`, and `recall`/`remember`.

**Deploy.** PyPI Trusted Publishing (`.github/workflows/publish.yml`). A push to
`main` touching `pyproject.toml` or `webglass/**` publishes to PyPI; a PR
touching them publishes a `.devN` build to TestPyPI. Dist name is `webglass-cli`.

## Known gotchas

- **Console script is `webglass`, not `webglass-cli`.** `[project.scripts]` binds
  `webglass = "webglass.cli:main"`. The README's `uv run webglass-cli ...`
  examples are wrong — that binary does not exist. Naming map: dist/PyPI name
  `webglass-cli`, import package `webglass`, console script `webglass`, argparse
  `prog` `webglass-cli`. This divergence is why the explain catalog keys the root
  entry under *both* names (see the rubric section). These should converge as the
  product matures.
- **Issue #2 is closed and superseded.** Any doc or comment pointing at #2 for
  the design spec is stale — the brief lives in #1.
