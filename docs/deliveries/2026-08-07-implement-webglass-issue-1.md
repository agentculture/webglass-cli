# Delivery Summary — implement WebGlass (issue #1)

plan: `implement-webglass-issue-1` · run: `complete` · date: `2026-08-07`
baseline: `devague summary skeleton`

## Intent

Ship the WebGlass M0–M2 slice of the issue #1 brief — the guarded web
operations and evidence plane's contract/characterization milestone, the
operation core over fake backends, and anonymous Chromium observation
(including the issue #9 consumer criteria and the CI web-app-testing use
case) — by fanning the converged 16-task plan out to parallel worktree agents
in 7 dependency waves, TDD-gating every merge into `feat/webglass-m0-m2`.

## Planned Work

Quoted verbatim from the `devague summary` skeleton:

- `t1` — M0: extend characterization tests locking every existing contract
- `t2` — M0: deterministic + hostile fixture site harness
- `t3` — M0: schema versioning decision, boundary docs, stale-text cleanup
- `t4` — M1: operation, result, and effects models (operations.py,
  results.py, effects.py)
- `t5` — M1: policy core with default denylists and test-profile allows
  (policy.py)
- `t6` — M1: adapter protocols, fake backends, conformance tests (adapters/)
- `t7` — M1: page snapshot, extraction, and stable references (pages.py,
  extraction.py, references.py)
- `t8` — M1: WebContext and four-store separation, sessions store first
  (sessions.py, context)
- `t9` — M1: operation service with budgets, timeouts, cancellation
  (service.py)
- `t10` — M1: CLI noun registration over fakes + introspection updates
  (cli/`_commands`, catalog, learn)
- `t11` — M2: Playwright/Chromium adapter with detached-session reattach
  (adapters/playwright.py)
- `t12` — M2: session lifecycle verbs, endpoint secrecy, leases, cleanup
  (session create/list/show/close/clean)
- `t13` — M2: observation verbs live against fixtures (page
  open/read/inspect/extract/links/screenshot, action follow/press)
- `t14` — M2: doctor browser diagnostics + CI browser provisioning
  (doctor.py, workflows)
- `t15` — M2: real API search provider + key redaction (adapters/, proposed
  vendor: Brave Search API)
- `t16` — M2: CI recipe, definition-of-done sweep, audience entry paths
  (docs, example workflow, status text)

## Actual Delivery

| Plan task | Status | What actually landed |
|-----------|--------|----------------------|
| `t1` | delivered | 55 characterization + import-boundary tests (merge `ce5e756`): stdout/stderr split, exit codes, JSON shapes, no-traceback, catalog resolution, culture.yaml failure branches; AST scan banning playwright/colleague imports |
| `t2` | delivered | Local fixture-site harness (merge `3e6ffa2`): clean/throwing/keydown/agent-state/spoofed-console/boilerplate pages + redirect chains incl. redirect-to-private; network-free proof via socket guard |
| `t3` | delivered | `docs/schema-versioning.md`, `docs/boundaries.md`, whoami docstring + CLAUDE.md gotcha fixes, Playwright-in-core decision recorded (merge `e7cf0b8`) |
| `t4` | delivered | `effects.py`/`operations.py`/`results.py` (merge `34d7f8f`): 3 effect classes with classify-upward, 7 lifecycle states, four trust zones, schema_version=1, 100% module coverage |
| `t5` | delivered | `policy.py` (merge `7a2ea70`): 32 stable rule ids, default scheme/target denylists, per-hop + resolved-IP evaluation, declared-target allows, fail-closed malformed policy; SSRF bypasses closed (inet_aton encodings, IPv4-in-IPv6, host normalization, userinfo URLs) |
| `t6` | delivered | `adapters/` protocol seams + fakes + reusable conformance mixins (merge `43834b3`) that t11/t15 later inherited verbatim |
| `t7` | delivered | `pages.py`/`extraction.py`/`references.py` (merge `2062112`): PageSnapshot, lenses preserving block ids, declared omissions (4 kinds), selector extraction, stale refs fail clearly, deterministic output |
| `t8` | delivered | `sessions.py`/`context.py` + exploration/memory M3 interface stubs (merge `763892f`): lease semantics proven under a real two-thread race, endpoint redaction, four-store AST-verified separation |
| `t9` | delivered | `service.py` (merge `d59e59c`): single exception boundary, 5 budget dimensions reserve-before/charge-after, timeout/cancel as structured results, no-silent-fallback proven, per-hop re-check of adapter-returned redirects |
| `t10` | delivered | CLI nouns search/page/action/session (merge `9cc8d84`): 19 explain-catalog paths, per-verb CLI-JSON≡library contract tests, exit-code matrix, teken rubric 26/26 |
| `t11` | delivered | `adapters/playwright.py` (merge `4b3beba`): playwright 1.62.0 pinned as core dep, detached-browser CDP reattach proven cross-process, sandbox refusal structured (never silent `--no-sandbox`), console/page-error capture, conformance suite green against real Chromium |
| `t12` | delivered | `adapters/session_store.py` (merge `8f28ac6`): 0600 records via mkstemp+replace, flock leases with crash recovery, reaping (SIGTERM→SIGKILL + profile cleanup), three-process CLI e2e with JS state surviving |
| `t13` | delivered | Live observation verbs (merge `5268b47`): console lens with explicit empty lists, spoofed-console containment, c37 press-observe-under-profile, selector extract, `screenshot --out` as the sole caller-path write (AST-verified), unreachable-target framing, no stderr chatter |
| `t14` | delivered | Doctor browser checks (playwright/chromium/version/sandbox/state-dir) + CI `browser-test` job with pinned cached Chromium and the AppArmor userns sysctl fix (merge `d91a8eb`) |
| `t15` | delivered | `adapters/brave.py` (merge `907cffb`): Brave Search API behind the seam, key from `WEBGLASS_BRAVE_API_KEY` only, planted-key redaction proven across repr/dicts/errors/logs, no live API in tests |
| `t16` | delivered | `docs/ci-recipe.md` + `.github/workflows/example-webapp-test.yml` + `tests/test_definition_of_done.py` (13 §20 items mapped, 4 declared M3/M4 deferrals) + status-text truth pass everywhere (merge `cb5af45`) |

## Mid-work Decisions

- `d1` — M2 per-hop redirect policy enforcement is post-hoc, not in-line
  (adapter checks pre-navigation; service re-evaluates every returned hop and
  denies results that traversed a denied hop, with a degraded-evidence
  warning) — probed during t11: Playwright route interception is not
  re-invoked for auto-followed redirects, so interception-based in-line
  enforcement would have silently failed open (recorded via `/deviate`,
  approved; issue #10).
- Operator scheduling: t6 (adapter protocols) was run **before** t9 (service)
  inside wave 3, although the plan graph put them in one wave — the service
  consumes t6's protocol seams, and running them in parallel would have
  forced duplicate protocol definitions. Waves are scheduling metadata; this
  only added an ordering constraint. No task content changed.
- t13 additively extended `webglass/service.py` beyond its briefed file list:
  its acceptance criteria (live-DOM lens, selector routing, `--out` write,
  connection-error classification) are unreachable from the CLI layer, and
  the spec's h2 (library and CLI return the same semantic result) requires
  them in the service. Disclosed by the agent, no plan acceptance criterion
  affected.
- t14 kept doctor's `healthy = all(passed)` formula (pinned by
  characterization tests) and encoded warning-severity browser findings as
  `passed: true` with the finding in message/remediation — the documented
  resolution of a tension between the new acceptance text and the frozen M0
  contract.
- `action.press` is REMOTE_ACTION at the model layer (classify-upward); the
  c37 observe-under-test-profile decision is implemented as an
  `EffectClassResolver` seam wired by the CLI factory, scoped to
  `action.press` with `declared_targets` present — its docstring names the
  residual (a press on a public page within a test-profile session also
  classifies observe).
- Integration fix by the operator: `.venv/**` excluded from markdownlint
  (config + CI command) — Playwright, a core dep since t11, ships markdown
  inside the venv that failed the lint job's glob (commit `6a74f0f`).
- t12 fixed a real defect beyond its brief: per-process browser-backend
  caching (a second web operation in one process previously hit "Playwright
  Sync API inside asyncio loop"), and left the browser default behind a
  `WEBGLASS_BROWSER_BACKEND` staging switch that t13 flipped — both
  documented handoffs, both disclosed.
- Browser-dependent tests are opt-in via `WEBGLASS_TEST_BROWSER=1`; on this
  AppArmor-userns-restricted host the harness additionally sets
  `WEBGLASS_TEST_ALLOW_NO_SANDBOX=1` (explicit, test-only). The product path
  never downgrades: an unsandboxed launch requires the loud
  `WEBGLASS_ALLOW_UNSANDBOXED=1` opt-in and marks results.

## Drift From Plan

| Plan item | Reason for divergence | Classification |
|-----------|-----------------------|----------------|
| `t11` (`d1`) | Playwright route interception is not re-invoked for auto-followed redirects, so in-line blocking would have silently failed open; shipped post-hoc containment (pre-navigation check + service-side per-hop re-evaluation + denial + degraded-evidence warning) instead; true in-line enforcement needs a policy-enforcing local proxy | needs-follow-up |
| `t13` | Implementation additively crossed into `service.py` beyond the briefed file ownership because the spec's library/CLI-parity requirement (h2) demands the behavior live in the shared service, not the CLI layer; all acceptance criteria met as written | acceptable |

All other 14 tasks: no drift — delivered to their confirmed acceptance
criteria (see Actual Delivery, task by task).

## Evidence

- tests (browser-free): `uv run pytest -n auto` — **2008 passed, 34 skipped**
  (skips: browser-gated tests + 4 declared M3/M4 deferrals in
  `tests/test_definition_of_done.py`)
- tests (browser-enabled): `WEBGLASS_TEST_BROWSER=1
  WEBGLASS_TEST_ALLOW_NO_SANDBOX=1 uv run pytest -n auto` — **2037 passed,
  5 skipped**, coverage ≈96% (gate: 60)
- lint: `black --check` / `isort --check-only` / `flake8` / `bandit -c
  pyproject.toml -r webglass` — all clean
- rubric: `uv run teken cli doctor . --strict` — pass
- markdown: `markdownlint-cli2 "**/*.md" …` — 0 errors
- commits: `cad0a03..983e8a5` on `feat/webglass-m0-m2` (16 task merges + spec/plan
  baseline + lint fix + version bump 0.6.0 + this ledger)
- live CLI demo on this host (transcript in the run session; repeatable via
  `tests/test_session_persistence.py` e2e + `docs/ci-recipe.md`): session
  create → page open under `profile-allow-declared-target` → `action press
  t e s t` (observe under profile) → `page extract --selector "#keylog"` →
  `"t,e,s,t"` → throw-page error with source+line / clean-page explicit `[]`
  console lens → `screenshot --out` decodable PNG → `session close` with
  `browser_reaped: true`
- issues: #8 (deferred M3–M6), #9 (consumer criteria), #10 (d1 follow-up)

## Delivery Claims

| Claim | Confidence | Evidence |
|-------|------------|----------|
| The M0–M2 operation surface (search, page open/read/inspect/extract/links/screenshot, action follow/press, session lifecycle) works end to end over real Chromium | high | merge `5268b47` · `tests/test_verbs_live.py` (browser-enabled run 2037 passed) |
| Library and CLI return the same semantic result per verb | high | `tests/test_cli_webverbs.py::test_cli_json_matches_library_result` (parametrized) |
| Policy denies loopback/private/metadata targets by default and admits only declared apps-under-test; malformed policy fails closed | high | `tests/test_policy.py` + `tests/test_policy_properties.py` (1,270 tests) · live demo verdict `profile-allow-declared-target` |
| Sessions reattach across one-shot CLI invocations with in-memory JS state intact | high | `tests/test_session_persistence.py` three-process e2e · challenge-pass probe |
| The session endpoint (secret-equivalent) never appears in output, logs, or evidence; records are 0600 | high | planted-secret tests in `tests/test_session_persistence.py` |
| A throwing page is distinguishable from a clean page by CLI evidence alone (error text + source location; explicit empty lists) | high | `tests/test_verbs_live.py` h24 tests · live demo |
| Search API keys are redacted from every output and store | high | `tests/test_brave_search.py::TestPlantedKeyRedaction` |
| All seven issue #9 acceptance criteria hold | high | `tests/test_definition_of_done.py` + `tests/test_verbs_live.py` mappings |
| The issue #1 §20 definition-of-done holds for the M0–M2 subset, with M3+ items declared deferred | high | `tests/test_definition_of_done.py` (13 items: 9 tested, 4 declared skips naming #8) |
| The CI `browser-test` job and `example-webapp-test.yml` recipe pass on real GitHub Actions runners | unverified | not yet run on GH infrastructure — first PR CI run will verify (risk r4) |
| Publishing to (Test)PyPI succeeds with the Playwright core dep | unverified | publish workflow untested with the new dependency set — verified at PR time |

## Remaining Work / Follow-up

- In-line per-hop redirect enforcement via a policy-enforcing local proxy —
  issue #10 (`d1`, needs-follow-up); M3-adjacent design work.
- M3–M6: evidence store, exploration graphs, Web-memory, Colleague provider,
  guarded interaction/artifact bridge, credential brokering — issue #8.
- Verify the `browser-test` job's AppArmor sysctl fix and the
  `example-webapp-test.yml` recipe on real GitHub Actions runners (risk r4) —
  happens on this branch's PR.
- Sonar quality gate + Qodo review comments on the PR — handled in the cicd
  leg following this summary.
- Notify colleague on issue #9 once the PR merges (their #387 proof is
  timeline-coupled to M2).
- Plan risks r1 (NDJSON/event streaming deferred) and r2 (non-Linux platform
  support unexamined) remain open by design.
