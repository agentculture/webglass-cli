# webglass-cli

**WebGlass — the guarded web operations and evidence plane for AI agents.**

WebGlass turns agent intent into normalized web operations: it applies
web-specific policy, drives search/fetch/browser backends, returns
token-efficient page state, records navigational provenance, and produces
durable, inspectable evidence.

```text
Colleague intent
  -> WebOperation
  -> capability + web policy
  -> search / fetch / browser backend
  -> PageSnapshot + Evidence + Effects
  -> Colleague interpretation and next decision
```

It is deliberately **not** a thin Playwright wrapper, not a generic scraper, and
not a second agent that decides what to believe. WebGlass records what it
observed; the calling agent draws the conclusions.

## Status: M0-M2 shipped, M3+ deferred

The authoritative build brief is
[issue #1](https://github.com/agentculture/webglass-cli/issues/1) — read it
before designing or contributing anything. This repo has shipped the first
three of its six milestones:

```text
webglass search  <query> ...
webglass page    open|read|inspect|extract|links|screenshot ...
webglass action  follow|press ...
webglass session create|list|show|close|clean ...
```

`page` and `action` drive a **real headless Chromium** by default (Playwright
is a **core** runtime dependency, not an optional extra — a 2026-08-07
decision, see `CLAUDE.md`); `session` records survive between separate
one-shot CLI invocations; `search` needs `$WEBGLASS_BRAVE_API_KEY`. Loopback
and private-network targets are **denied by default** — reaching a locally
served app under test needs an explicit `--policy-profile` (see
[`docs/ci-recipe.md`](docs/ci-recipe.md) for the full pattern, including a
GitHub Actions recipe). Every verb returns the same structured
`WebOperationResult` the library API returns, and every operation-model
module stays import-clean of Playwright behind a replaceable adapter seam
(`webglass/adapters/playwright.py` is the only module allowed to import it).

Not yet built — tracked as milestones M3-M6 in
[issue #8](https://github.com/agentculture/webglass-cli/issues/8):

```text
webglass exploration start|show|resume|mark ...
webglass evidence    show|export|verify|cite ...
webglass memory      find|show|forget|compact ...
webglass policy      check|explain ...
webglass operation   preview|show ...
```

There is also no remote action beyond `action press`'s preview-by-default (or
execute, under a declared test profile): no fill/select/submit/upload/
download, and nothing that *applies* rather than merely previews.

## Quickstart

The console script is **`webglass`** (the dist/PyPI name is `webglass-cli`).

```bash
uv sync                                   # create .venv and install (incl. dev group)
uv run playwright install --with-deps chromium   # one-time Chromium download

uv run webglass whoami             # identity from culture.yaml
uv run webglass learn              # self-teaching prompt (add --json)
uv run webglass explain webglass   # markdown docs for any noun/verb path

# Open a real page against a real browser — no policy profile needed for a
# public origin (the default policy already allows http/https):
uv run webglass page open https://example.com/ --json

uv run pytest -n auto              # run the test suite
uv run teken cli doctor . --strict # the agent-first rubric gate CI runs
```

Loopback and private-network targets are **denied by default** (issue #1
section 10) — that default does not relax for local development. Reaching a
locally served app under test needs an explicit, scoped policy profile:

```bash
cat > policy-profile.json <<'JSON'
{"name": "local-app", "declared_targets": ["127.0.0.1:8000"]}
JSON

uv run webglass page open http://127.0.0.1:8000/ --policy-profile policy-profile.json --json
```

See [`docs/ci-recipe.md`](docs/ci-recipe.md) for the full session-reuse +
console-evidence + screenshot pattern, and a copy-pasteable GitHub Actions
job that runs it.

## CLI (today)

| Verb | What it does |
|------|--------------|
| `whoami` | Report this agent's nick, version, backend, and model from `culture.yaml`. |
| `learn` | Print a structured self-teaching prompt. |
| `explain <path>` | Markdown docs for any noun/verb path. |
| `overview` | Read-only descriptive snapshot of the agent. |
| `doctor` | Check the agent-identity invariants (prompt-file-present, backend-consistency), plus browser-capability diagnostics. |
| `cli overview` | Describe the CLI surface itself. |
| `search <query>` | Run a search operation (needs `$WEBGLASS_BRAVE_API_KEY`). |
| `page open\|read\|inspect\|extract\|links\|screenshot` | One page's lifecycle, against a real headless Chromium. |
| `action follow\|press` | Follow a link reference, or dispatch a key sequence. |
| `session create\|list\|show\|close\|clean` | Browser session lifecycle, reusable across separate CLI invocations. |

Every command supports `--json`. Results go to stdout, errors and diagnostics to
stderr — never mixed. Failures carry `{code, message, remediation}`; no Python
traceback ever reaches stderr. Exit codes: `0` success, `1` user error, `2`
environment error, `3+` reserved.

## Design principles

These constrain every contribution — the full rationale is in issue #1.

- **The core abstraction is a web operation**, not a CLI handler. One operation
  lifecycle serves the Python API, the CLI, and the Colleague tool adapter; the
  library and CLI return the same semantic result, and text output is a
  *rendering* of it.
- **Four kinds of state stay separate** — browser session (volatile, sensitive,
  never emitted wholesale), exploration (a durable resumable graph of *why* the
  agent traversed), evidence (append-only observations), and Web-memory (a
  searchable index, never a credential store).
- **Three explicit effect classes** — `observe` executes when authorized;
  `local-state` needs an authorized state scope; `remote-action` previews by
  default and runs prepare → commit → verify. If classification is uncertain,
  classify upward. There is no rollback for the web.
- **Progressive disclosure** — `open` returns a compact page card, and
  `inspect` / `read` / `extract` / `evidence` are lenses over the same snapshot
  with stable block IDs, so an agent reaches exact source text without
  re-fetching or losing provenance.
- **Token efficiency without hidden distortion** — extraction is deterministic
  and declares every omission. WebGlass never silently summarizes with a model
  and presents it as page content.
- **Web content is adversarial** — untrusted source material stays structurally
  distinguishable from trusted control metadata, and remote text can never
  masquerade as a WebGlass warning or instruction.
- **The Playwright adapter is replaceable** — Chromium is the first engine, not
  the operation model. Playwright types never leak into the public API.

## Repository furniture

- **A mesh identity** — `culture.yaml` (`suffix` + `backend`) and the matching
  resident prompt file (`AGENTS.colleague.md`, since this agent runs
  `backend: colleague`).
- **The canonical guildmaster skill kit** under `.claude/skills/`, vendored
  cite-don't-import. See [`docs/skill-sources.md`](docs/skill-sources.md).
- **A build + deploy baseline** — pytest, lint, the agent-first rubric gate, and
  PyPI Trusted Publishing wired into GitHub Actions.

See [`CLAUDE.md`](CLAUDE.md) for the full architecture notes and conventions
(the CLI skeleton contracts, the rubric gate, version-bump-every-PR, the `cicd`
PR lane, deploy setup).

## License

Apache 2.0 — see [`LICENSE`](LICENSE).
