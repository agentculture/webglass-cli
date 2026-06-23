# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is (and is not yet)

`webglass-cli` is **WebGlass** — an agent-facing web exploration CLI. The product
goal (the design spec is [issue #2](https://github.com/agentculture/webglass-cli/issues/2))
is a *web lens for agents*: search, open, read, inspect, follow link paths,
capture evidence, and remember it all so an agent can explore the web cheaply
(in tokens) and resume a session later — built on **Playwright** automation with
**Chromium** as the first browser backend.

**None of that exists yet.** The repo was scaffolded from `culture-agent-template`
and is at the *seed* stage. What ships today is the template's **agent-first
introspection CLI** (cited from [teken](https://github.com/agentculture/teken)'s
`afi-cli` `python-cli` reference) with verbs `whoami` / `learn` / `explain` /
`overview` / `doctor` and the `cli` noun group. The runtime has **zero
third-party dependencies** (`dependencies = []` in `pyproject.toml`); `teken` is
a dev-only dependency.

So there are two layers to keep straight:

- **The CLI skeleton** (below) — the registration / error / output / explain
  machinery you extend. It is real, tested, and the foundation every WebGlass
  verb will register onto.
- **The WebGlass product** (issue #2, summarized at the end) — the target
  architecture. Building it means adding `search` / `open` / `read` / `inspect`
  / `explore` / `screenshot` / `path` / `memory` / `cite` verbs onto the
  skeleton, plus the browser, extraction, evidence, and Web-memory modules.

Much of the runtime *text* still describes the repo as "a clonable template for
AgentCulture mesh agents" (the `learn` body, the `explain` catalog, the argparse
`prog` description, the README). As real WebGlass verbs land, update those
self-description strings to describe WebGlass — they are the CLI's own
documentation surface, not just prose.

## Commands

This is a **uv**-managed Python 3.12+ package. The console script is **`webglass`**
(see the gotcha below — it is *not* `webglass-cli`).

```bash
uv sync                                    # create .venv and install (incl. dev group)
uv run webglass whoami                     # run the CLI; --json on every verb
python -m webglass whoami                  # equivalent entry point

uv run pytest -n auto                      # full test suite (xdist parallel)
uv run pytest tests/test_cli.py::test_whoami_text -v   # a single test
uv run pytest -n auto --cov=webglass --cov-report=term # coverage (fail_under=60)
```

Lint / format / security — these run as the CI `lint` job and must all pass:

```bash
uv run black --check webglass tests        # line-length 100
uv run isort --check-only webglass tests   # black profile
uv run flake8 webglass tests
uv run bandit -c pyproject.toml -r webglass # B101/B404/B603 skipped (see pyproject)
markdownlint-cli2 "**/*.md" "#node_modules" "#.local" "#.claude/skills" "#.teken"
uv run teken cli doctor . --strict         # the agent-first rubric gate (see below)
```

(Drop `--check`/`--check-only` from black/isort to auto-format.)

## Architecture: the CLI skeleton

Reading these four concerns together is how you understand the CLI — they are
deliberately small and contract-driven so an agent consumer can rely on them.

**Command registration** (`webglass/cli/__init__.py`). `main()` → `_build_parser()`
→ `_dispatch()`. Every command lives in its own module under
`webglass/cli/_commands/` and exposes a `register(sub)` function that adds its
subparser and sets `func=<handler>` (and `json=False`) via `set_defaults`. To
add a verb: write the module, then call its `register()` inside `_build_parser`
(there is a marked "Register your own noun groups here" spot). Handlers return
`None`/`0` for success or raise `CliError`; `_dispatch` translates the return or
exception into the process exit code.

**Error contract** (`webglass/cli/_errors.py` + `_output.py`). *No Python
traceback ever reaches stderr.* Every failure raises `CliError(code, message,
remediation)`. `_dispatch` catches `CliError` and routes it through
`emit_error`; any *other* exception is wrapped into a `CliError` so even bugs
surface as the structured shape. Argparse's own errors (unknown verb, missing
arg) are also routed through this contract: `_CliArgumentParser` overrides
`.error()`, and because the subparsers are built with
`parser_class=_CliArgumentParser`, the override propagates to every level.

**Output contract** (`webglass/cli/_output.py`). *Results to stdout, diagnostics
and errors to stderr — never mixed.* Every verb takes `--json`; in JSON mode the
result payload goes to stdout and `{code, message, remediation}` errors go to
stderr. Text-mode errors render as `error: <message>` + `hint: <remediation>`
(the `hint:` prefix is load-bearing — agents and the rubric look for it).
Because argparse errors fire *before* `args.json` is parsed, `main()` peeks at
raw argv for `--json` and stashes it on `_CliArgumentParser._json_hint` so
parse-time errors still honor JSON mode.

**Exit-code policy** (centralized in `_errors.py`): `0` success, `1` user-input
error, `2` environment/setup error, `3+` reserved. Documented in `learn` output
because the rubric checks for it.

**The explain catalog** (`webglass/explain/`). `explain` is a *global* verb
(not nested under a noun) that resolves a command-path tuple to verbatim
markdown. Entries live in `catalog.py` as a `dict[tuple[str, ...], str]`;
`resolve()` raises `CliError` on a miss. **Every new noun/verb must get a
catalog entry**, keyed by its path tuple — otherwise `explain <new-verb>` 404s
and the rubric's per-verb `explain` check fails.

### The agent-first rubric gate is a hard CI gate

`uv run teken cli doctor . --strict` enforces teken's seven-bundle agent-first
rubric and runs in CI. It is not advisory — a failure reds the build. Practical
constraints it imposes when you extend the CLI:

- **Any noun that has action-verbs must also expose `overview`.** The `cli` noun
  exists today purely to satisfy `overview_cli_noun_exists` (it has no
  action-verbs yet); follow that pattern for new nouns like `path` and `memory`.
- **`learn`** must be ≥200 chars and mention purpose, the command map, exit
  codes, `--json`, and `explain`.
- **Descriptive verbs** (`overview`) must never hard-fail on a stray/bad target
  path — `overview` accepts an ignored positional `target` for exactly this.
- **`explain <self>` must resolve** — for both the dist name (`webglass-cli`)
  *and* the **import-package** name (`webglass`), because the rubric's
  `explain_self` check probes the import name. The catalog (`ENTRIES` in
  `webglass/explain/catalog.py`) carries both as root keys; if you ever rename
  the package, keep both aliases in sync or this check goes red.

## Identity and mesh membership

This repo is a node in the AgentCulture IRC mesh. Its identity is declared in
`culture.yaml`: `suffix: webglass-cli`, **`backend: colleague`**, and a pinned
served model. The backend determines the **resident prompt file**: `colleague` →
**`AGENTS.colleague.md`** (not `CLAUDE.md`). So `CLAUDE.md` is *your* operating
guide, while `AGENTS.colleague.md` is the prompt the colleague backend actually
residents on — keep both coherent, but the mesh runtime reads
`AGENTS.colleague.md`.

`doctor` checks the same invariants `steward doctor` enforces:
**backend-consistency** (the prompt file for the declared backend exists —
`claude`→`CLAUDE.md`, `colleague`→`AGENTS.colleague.md`, `acp`→`AGENTS.md`,
`gemini`→`GEMINI.md`) and **skills-present** (`.claude/skills/` is non-empty).

Note: `whoami`/`doctor` parse `culture.yaml` with a **hand-rolled line parser**
(`read_agent_fields` in `whoami.py`), deliberately *not* PyYAML, to keep the
runtime dependency-free. It reads only the first agent block's top-level
`suffix`/`backend`/`model`. If WebGlass ever needs richer config, that's the
constraint to revisit — adding a YAML dep is a real architectural decision, same
as adding Playwright.

## Conventions and workflow

**Version-bump-every-PR (hard CI gate).** The `version-check` job fails any PR
whose `pyproject.toml` version equals `main`'s — *every* PR bumps the version,
even docs/config/CI-only ones. Use the `version-bump` skill (`major|minor|patch`)
which also prepends a Keep-a-Changelog entry to `CHANGELOG.md`. The version
flows from package metadata into `webglass.__version__` at runtime via
`importlib.metadata`.

**PR lifecycle** runs through the `cicd` skill (layered on the `devex pr` CLI):
it handles lint/open/read/reply and adds `status` (SonarCloud quality gate) and
`await` (block until CI + Sonar settle). The Sonar quality gate is wired in
(`sonar-project.properties`, `sonar.qualitygate.wait=true`) and gates the `test`
job when `SONAR_TOKEN` is set; token-less/fork PRs stay green.

**Skills.** `.claude/skills/` is vendored **cite-don't-import** from
`guildmaster` (the AgentCulture skills supplier); `docs/skill-sources.md` is the
provenance ledger and re-sync procedure — read it before touching anything under
`.claude/skills/`, and never edit a vendored script body in place (lift the
change upstream and re-vendor). Beyond `cicd`/`version-bump`, the kit includes
`communicate` (cross-repo issues + mesh messages), `ask-colleague` (hand a
scoped task to a *different* model for a second opinion — reach for `review`
before opening a PR), `think`/`spec-to-plan`/`assign-to-workforce` (the devague
idea→spec→plan→build chain), `run-tests`, `sonarclaude`, and `recall`/`remember`
(shared eidetic memory).

**Deploy.** PyPI Trusted Publishing (`.github/workflows/publish.yml`). A push to
`main` that touches `pyproject.toml` or `webglass/**` publishes to PyPI; a PR
touching them publishes a `.devN` build to TestPyPI. The dist name is
`webglass-cli`.

## Target architecture (WebGlass — issue #2)

When building the actual product, the spec calls for **strict module
separation** — keep browser automation, extraction, evidence, and Web-memory as
distinct modules. The intended shape:

- **Browser automation** — a Playwright-backed adapter. Keep it *adapter-shaped*:
  Chromium is the first backend, but the interface must accommodate
  Firefox/WebKit/plain-HTTP-fetch/search-provider-API/local-cache backends
  later. Operations: open URL, navigate links, extract page/visible text, DOM
  snapshots, screenshots, wait-for-selector, page metadata, and detection of
  navigation failures / redirects / login walls / bot walls / blocked content.
  *This adds the first runtime dependency (Playwright) — currently `[]`.*
- **Token-budgeted extraction (the whole point).** Reads return cleaned,
  budgeted content *by default* — strip boilerplate, outline-first then detail
  on demand, deterministic chunking, dedup across pages, query-focused
  extraction, agent-selectable verbosity (e.g. `read <url> --budget 4000`,
  `inspect <url> --outline`, `extract <url> --query "..."`).
- **Web-memory** — persistent, reusable exploration memory so an agent can ask
  "have I seen this URL?", "did it change?", "what did I extract from this
  domain?", "can I resume that session?". Built on URL canonicalization, content
  hashing, snapshot IDs, and a Session→Query→Result→Page→{Extraction, Evidence,
  Screenshot, LinkEdges, Notes} record model. Repeated reads must be
  cache/memory-aware.
- **Exploration paths** — represent exploration as a graph, not isolated
  fetches: source query → result page → clicked page → evidence, with link
  edges, the reason each link was followed, and per-node branch status
  (useful / dead-end / blocked / duplicate / deferred). Paths are persisted,
  inspectable, and resumable (`path list|show|resume|mark`, `map <url> --depth`).
- **Evidence & citation** — useful findings are saved and citeable (`cite <id>`).

Acceptance criteria worth internalizing: agents use WebGlass without ever
touching Playwright directly; extraction is token-budgeted by default; repeated
reads are cache-aware; every session is resumable; paths and evidence are
persisted and inspectable. Position it as an *agent web exploration layer*, not
a generic scraper/crawler.

## Known gotchas

- **Console script is `webglass`, not `webglass-cli`.** `[project.scripts]` binds
  `webglass = "webglass.cli:main"`. The README's `uv run webglass-cli ...`
  examples are wrong (that binary does not exist). Naming map: dist/PyPI name
  `webglass-cli`, import package `webglass`, console script `webglass`, argparse
  `prog` (shown in help/errors) `webglass-cli`. Several of these should converge
  as the product matures. This divergence is also why the explain catalog keys
  the root entry under *both* `webglass-cli` and `webglass` (see the rubric
  section).
