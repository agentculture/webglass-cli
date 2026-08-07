# implement WebGlass (issue #1)

> WebGlass ships its first useful release: agents search, open, read, inspect, follow, and screenshot the web through guarded, token-efficient, evidence-producing operations, per the issue 1 brief
> instruction: before claiming the release, walk the section 20 checklist restricted to M0-M2 items and point each to a passing test

## Audience

- AI agents and their operators: Colleague residents composing WebGlass as a Python library, mesh agents and humans driving the webglass CLI directly, and CI pipelines using the same verbs to test locally served web apps

## Before → After

- Before: Today only the introspection scaffold exists (whoami/learn/explain/overview/doctor); there is no web operation surface — an agent needing the web falls back to raw browser automation or ad-hoc fetch with no policy, no budgets, no stable references, and no evidence trail
- After: Through M2, an agent can search, open, read, inspect, follow, press keys into, and screenshot the web via stable structured operations — policy-enforced, token-budgeted, omission-declaring, with console/page-error evidence and sessions reusable across one-shot CLI invocations, over a replaceable Playwright/Chromium adapter (a core dependency) — and a CI pipeline or colleague drives a locally served app with the same verbs under an explicit test-profile allow

## Why it matters

- Unguarded browsing wastes agent tokens on raw HTML and invites SSRF and prompt-injection risk; without durable evidence records, agent web claims are unauditable — and web-app testing in CI currently needs a whole separate toolchain instead of the same guarded operations

## Requirements

- One operation lifecycle (WebOperation -> WebOperationResult) is shared by the Python API, the CLI, and the Colleague tool adapter; the library and CLI return the same semantic result, text output is only a rendering — never architected around CLI handlers or direct Playwright calls (issue 1 sections 1 and 14)
  - instruction: add a per-verb contract test asserting the CLI --json payload equals the library WebOperationResult rendering
  - honesty: no CLI handler for a web verb contains operation logic — it builds a WebOperation, calls the one service, renders the result; a library caller invoking the same operation gets a semantically identical result
- Four state kinds stay separate objects — browser session (volatile, sensitive, never emitted wholesale), exploration graph (durable, resumable without a live browser), evidence (append-only observations), and web-memory (searchable index, never a credential store) — connected per task by a WebContext that holds references and policy, not copies (issue 1 section 2)
  - instruction: model sessions.py, exploration.py, evidence.py, memory.py as separate stores joined only by WebContext references; test resume-without-browser and independent lifecycle
  - honesty: no object holds two state kinds and WebContext holds only references and policy: closing a session never touches evidence or exploration, and an exploration resumes with no live browser process
- Three explicit effect classes: observe executes when authorized, local-state needs an authorized state scope, remote-action previews by default and runs prepare -> commit -> verify with an expiring ActionPlan; classification uncertainty classifies upward; commit is never inferred from prepare and an uncertain commit is never auto-retried — it reports `outcome_unknown` (issue 1 section 3)
  - instruction: implement effects.py plus prepare/commit/verify service methods; add stale-plan-refusal and `outcome_unknown` tests against hostile fixtures
  - honesty: every operation kind declares exactly one effect class; ambiguous controls classify as remote-action; commit demands a valid unexpired plan id and re-checks page generation, target, session owner, and policy; an uncertain outcome reports `outcome_unknown` and is never auto-retried
- Delivery follows the staged sequence M0 -> M5 in order: M0 (characterization tests, schema versioning decision, deterministic test pages plus hostile fixtures, boundary docs) completes before any Playwright implementation; M6 items are on-demand only (issue 1 section 17)
  - instruction: sequence the work as one PR per milestone; the M0 PR contains only tests, fixtures, docs, and schema-version decisions — no product code
  - honesty: no playwright import exists anywhere in the repo until the M0 characterization suite and the schema-versioning decision are merged; milestone PRs land in M0 -> M1 -> M2 order
- Every WebGlass noun registers onto the existing CLI skeleton: a module under webglass/cli/`_commands` exposing register(sub) wired at the marked spot in `_build_parser`; handlers raise CliError, results go to stdout and errors to stderr with the hint: line, exit codes stay 0/1/2 — the skeleton is the substrate, not replaced (webglass/cli/`__init__.py`, `_errors.py`, `_output.py`)
  - instruction: extend the existing registration pattern at the marked spot in `_build_parser` and keep `_dispatch` as the sole exception boundary
  - honesty: every new verb registers through a `_commands` module register(sub); no handler writes directly to a stream or lets a non-CliError escape; results never appear on stderr; exit codes stay 0/1/2
- Every new noun/verb ships an explain catalog entry keyed by its path tuple, every noun with action-verbs also exposes overview, and the learn command map plus its JSON status field are updated as verbs land — the teken rubric gate (uv run teken cli doctor . --strict) and `test_every_catalog_path_resolves` enforce this in CI (webglass/explain/catalog.py, `_commands`/cli.py, `_commands`/learn.py, .github/workflows/tests.yml lint job)
  - instruction: update catalog.py, the noun overview, and learn in the same PR as each new noun — never as a follow-up
  - honesty: uv run teken cli doctor . --strict passes on every PR; every new path tuple resolves in the explain catalog; learn lists every implemented verb and its status text matches what actually ships
- Web content is adversarial input: every result preserves the four trust zones (trusted control metadata, untrusted source material, sensitive caller data, derived transformations); deny file:/javascript:/browser-internal schemes and loopback/link-local/private/cloud-metadata targets; revalidate every redirect hop; keep the Chromium sandbox; quarantine downloads; malformed policy never silently fails open (issue 1 sections 10 and 11)
  - instruction: implement policy.py over explicit data with per-hop evaluation; give result models structurally separate trusted/untrusted/sensitive/derived fields; test with hostile fixtures
  - honesty: policy denies file:/javascript:/browser-internal schemes and loopback/link-local/private/metadata targets by default; every redirect hop is re-evaluated; malformed policy yields a structured error and never fails open; trust zones are distinct fields in every result schema
- WebGlass owns its test surface per issue 1 section 18 — schema, extraction/stable-reference, SSRF/redirect, session isolation, evidence, adapter conformance, and CLI contract tests; no default test depends on a live public website (deterministic local sites and hostile fixtures only); the coverage gate `fail_under`=60 in pyproject stays enforced
  - instruction: serve deterministic and hostile fixture sites from the test harness itself and keep any public-web smoke test optional and non-authoritative
  - honesty: the default test suite passes with external network access unavailable (localhost fixtures only); coverage stays at or above `fail_under`=60; Playwright integration tests target only local deterministic sites
- Stale self-description text is cleaned up as nouns land: whoami.py line 8 still says "When you clone this template"; CLAUDE.md Known gotchas still claims the README uses the non-existent webglass-cli binary but README.md already uses webglass — fix the docs that lag the code (README.md, CLAUDE.md, whoami.py)
  - instruction: fix the whoami.py docstring and the stale README gotcha in CLAUDE.md as part of the M0 PR
  - honesty: once product nouns land, no runtime or doc text describes this repo as a clonable template, and every CLAUDE.md gotcha matches observable reality
- webglass-cli is also a first-class tool for testing web-based solutions, including on CI: an agent or pipeline drives a locally served app headlessly through the same guarded operations (open/read/inspect/screenshot), asserts against structured JSON results and exit codes, and keeps evidence records (snapshots, screenshots) as CI artifacts (user direction, 2026-08-07; extends the research-oriented framing of issue 1)
  - instruction: ship a documented CI recipe (example GitHub Actions workflow driving a local fixture app) as part of M2 acceptance
  - honesty: a CI job can start a local web app, drive it with webglass open/read/inspect/screenshot headlessly, and assert on JSON output and exit codes — with no WebGlass-side test framework or assertion DSL involved
- The web policy supports an explicit test/CI profile that allows loopback or declared private targets for an app under test without weakening the default: issue 1 section 10 denies loopback/link-local/private-network/metadata targets by default, so the CI-testing use case is enabled by an explicit allow in the effective policy profile — never by relaxing the default denylist, and malformed policy still never fails open
  - instruction: express the test/CI allowance as policy profile data evaluated by the same policy core — never a code-path bypass or environment flag
  - honesty: loopback and private targets stay denied by default; enabling an app under test requires an explicit allow in the effective policy profile scoped to declared targets; a malformed profile still fails closed
- Search API keys are runtime configuration, never product state: supplied via environment/config, redacted from logs and evidence, never persisted to web-memory or artifacts — consistent with the section 8 do-not-persist list and the never-a-credential-store invariant, and now needed at M1/M2 because the first real provider is API-based (q3 decision)
  - instruction: add a redaction test that plants a known key and asserts its absence from every output and store
  - honesty: no search API key ever appears in logs, evidence, memory, artifacts, or JSON results; keys are read from environment or config at call time only
- Console and page-error evidence is part of the observation surface: WebGlass captures the running pages console messages and uncaught errors (with source locations) and exposes them via a documented verb in --json — a page that throws on load is distinguishable from a working page by CLI evidence alone, and a clean page yields an explicitly empty error list (issue 9 item 2; the NEBULA field lesson: node --check passed on a page that was dead in the browser)
  - instruction: capture console messages and uncaught page errors into the snapshot/evidence surface, expose them via a documented verb (page inspect lens or evidence show), and test with throwing and clean fixture pages
  - honesty: a fixture page that throws on load yields the error text and source location in --json output, and a clean page yields an explicitly empty error list — a dead canvas is distinguishable from a working game by CLI evidence alone
  - honesty: console and page-error text is untrusted source material: labeled as such in results and evidence, and never rendered as a WebGlass diagnostic, warning, or instruction — a hostile page logging WEBGLASS WARNING to console cannot spoof tool output
- action press ships inside the M0-M2 slice: single keys and key sequences with configurable inter-key delay delivered to the focused page, so a caller can drive a declared app under test (platformer controls); under a general non-test policy profile the verbs classification stays conservative per classify-upward (issue 9 item 4 — pulled forward from the M5 staging for this one verb)
  - instruction: implement action press (single keys, sequences, configurable inter-key delay) gated by the effective policy profile; keep general-web classification at classify-upward
  - honesty: a fixture page that logs keydown events shows the exact pressed sequence in its state after action press; under a general non-test profile the same verb still classifies conservatively
- Sessions are reusable across separate one-shot CLI invocations: session create returns an id that later subprocess calls attach to, page state persists across those calls until session close, and callers never hold a daemon connection — the live-process lease machinery of brief section 9 makes this work (issue 9 item 6)
  - instruction: back sessions with a store and live-process leases (brief section 9) so separate one-shot CLI calls attach by session id; test create -> act -> read -> close across real subprocess boundaries
  - honesty: state set by an action in one CLI invocation is visible to a later read in the same session across separate subprocesses until session close; callers never hold a daemon connection
- Selector-scoped extraction and screenshot-to-file: page extract can return a single selectors content in --json without the whole page (the app under test exposes an agent-readable state node), and page screenshot --out PATH writes a decodable PNG at the caller-given path (issue 9 items 3 and 5)
  - instruction: support selector-scoped extract (an agent-readable state node) and screenshot-to-file; test both against local fixtures
  - honesty: extracting a single selector returns exactly that content in --json without the rest of the page, and page screenshot --out writes a decodable PNG at the caller-given path
- Browser provisioning is explicit and diagnosable: M2 CI installs a pinned Chromium (playwright install, cached); the non-browser test suite passes with no browser present; webglass doctor gains browser-capability checks — playwright importable, chromium installed, pinned versions reported, and a usable-sandbox check that fails actionably when the host blocks unprivileged user namespaces (probed on this host: chrome aborts with No usable sandbox on AppArmor-restricted Ubuntu) — never silently degrading to --no-sandbox (challenge pass: tests.yml and publish.yml have no browser step today; brief section 12)
  - instruction: add a pinned, cached browser-provisioning step scoped to the M2 integration job; extend doctor with playwright/chromium/usable-sandbox checks in the existing rubric check shape
  - honesty: a CI run with no browser installed still passes the non-browser suite, and doctor on a browserless or sandbox-restricted host reports the specific failed capability with remediation — never a crash and never a silent --no-sandbox fallback
- The cross-invocation session mechanism treats its connect endpoint as secret-equivalent: the CDP/ws endpoint grants full browser control, so it gets restrictive file permissions and never appears in JSON output, logs, or evidence; concurrent attaches to one session are serialized or refused via the lease; a crashed caller leaves no orphaned browser process past lease expiry and session clean reaps deterministically (challenge probe validated CDP reattach with in-memory JS state surviving across processes; brief section 9)
  - instruction: store session records with the endpoint under restrictive permissions; take a lease on attach, release on exit; session clean reaps expired leases and their processes; test all three properties
  - honesty: the stored endpoint is unreadable by other users; no JSON result, log line, or evidence record contains it; two concurrent attaches to one session never interleave — one proceeds, the other gets a structured lease refusal; after lease expiry the browser process is gone
- Budgets, timeouts, and cancellation are structured results, not backend failures: every operation charges the context budget (requests, transferred bytes, browser time, artifact bytes, and agent-visible tokens as labeled heuristic estimates — no tokenizer dependency) and enforces its timeout; exhaustion, timeout, and cancellation return budget-exhausted/`timed_out`/cancelled lifecycle results bounded for subprocess callers with 120-300s budgets (challenge pass: the exported spec carried budgets only in the `after_state`, not as a requirement; brief sections 1 and 14; issue 9 item 7)
  - instruction: implement per-dimension budget counters and a deterministic token estimator in the service layer; surface budget state in every result; test exhaustion mid-read
  - honesty: exhausting any budget dimension or hitting a timeout yields the structured lifecycle result with partial evidence preserved — never a raw backend exception — and token figures are labeled as estimates

## Honesty conditions

- anything the announcement presents as shipped is covered by the M0-M2 test suites, not narrative — the issue 1 section 20 definition-of-done subset is demonstrable end to end
- no playwright import exists outside the adapter module; public API type hints contain no Playwright types; an import-boundary test enforces both
- the existing characterization tests keep passing unmodified through M0-M2 — extended, never rewritten to accommodate a regression
- webglass code never imports Colleague and never reads .colleague files; workspace paths never appear in operation arguments except as opaque artifact references
- whoami/doctor behavior and `read_agent_fields` stay byte-identical through the M0-M2 PRs and no YAML dependency appears
- the public surface gains no assert/expect/test-runner verbs; the CI recipe consumes ordinary JSON results with caller-side assertions only
- each named audience has a working entry path at M2: library import for Colleague, the webglass console script for agents and humans, a documented CI recipe for pipelines
- every listed verb is demonstrable against local fixture sites at M2 with policy verdicts, budgets, and omission declarations visible in the structured result
- accurate as of 2026-08-07: the repo ships only the introspection verbs, verified against webglass/cli/`_commands`
- the shipped design addresses each named risk concretely: token budgets and lenses for cost, target denylists for SSRF, trust zones for injection, evidence records for auditability
- every listed signal is mechanically checkable in CI — none requires human judgment to evaluate
- no code path writes remote-origin response bytes to a caller-supplied path; screenshot --out writes only WebGlass-rendered PNG output; download verbs keep quarantine when they arrive in M5
- webglass never spawns or kills the app-under-test process; an unreachable target yields a structured connection error telling the caller to check their server

## Success signals

- The M0-M2 definition-of-done subset holds: characterization plus product suites green in CI with no live public website; the teken rubric gate passes with every new noun; a CI job drives a locally served app through webglass and asserts on JSON results and exit codes; the seven issue 9 acceptance criteria pass as fixture-based M2 tests (localhost open, console/page-error evidence, selector extract, key sequences, screenshot to file, cross-invocation session reuse, agent-first contract); every result declares live-vs-cached and omissions; policy denies loopback by default yet allows the declared app under test

## Scope / boundaries

- The operation-model modules stay dependency-light and Playwright types never enter the public API, enforced behind protocol seams (SearchProvider, FetchBackend, BrowserBackend, BrowserSessionStore, MemoryStore, ArtifactStore, WebPolicyEvaluator, Clock/ID providers) — but Playwright itself is a CORE runtime dependency (user decision 2026-08-07, q2, overriding the issue 1 section 12 recommended extra/adapter-package direction); pyproject dependencies gains playwright, and the stale dependencies-are-empty language in CLAUDE.md and the explain/learn text must be updated when this lands
- The existing introspection surface is frozen compatibility contract: whoami/learn/explain/overview/doctor behavior, --json, the stdout/stderr split, and exit codes stay compatible; tests/`test_cli.py` and tests/`test_cli_introspection.py` already characterize much of it and M0 extends that characterization before anything changes (issue 1 section 19)
- Dependency direction stays one-way: WebGlass never reaches Colleague internals (the M4 provider lands in the colleague repo by composition), workspace crossings go only through the shell-cli artifact bridge (agentculture/shell-cli exists: the guarded local operations plane for AI agents), and the policy core never reads .colleague files — Colleague passes an effective profile (issue 1 vision, sections 13 and 15)
- Identity and mesh plumbing stay untouched by product work: the hand-rolled culture.yaml parser (`read_agent_fields` in whoami.py), the doctor backend-consistency checks, and AGENTS.colleague.md residency remain as-is; adding a YAML dependency would be a separate architectural decision, not a side effect (whoami.py, doctor.py, culture.yaml)
- In the testing story WebGlass stays the operations and evidence plane: assertions, test orchestration, retries, and pass/fail logic belong to the caller (CI script or agent) — WebGlass does not grow a test-runner or assertion DSL, mirroring the Colleague ownership split and the section 21 discipline against mirroring Playwright (whose test framework already owns that space)
- The --out convenience never becomes a quarantine bypass: caller-path writes apply only to WebGlass-rendered artifacts (screenshots); remote-origin download bytes never reach a caller-given path and stay quarantined behind the shell-cli export bridge per brief section 13 (challenge pass: c30 vs section 13 tension examined)
  - instruction: assert in tests that the screenshot writer is the only caller-path write and that download flows route exclusively to quarantine
- WebGlass never starts, stops, or supervises the app-under-test server process — the caller owns that lifecycle (colleague or shell-cli territory); WebGlass only observes and drives it via URL targets under the declared test profile (challenge pass: issue 9 has colleague building and serving the game; ownership split)
  - instruction: return an actionable structured error on connection-refused and document the caller-owns-the-server contract in the M2 CI recipe

## Non-goals

- Refuse the section 21 non-goals: no general crawler framework, no stealth/anti-bot or CAPTCHA circumvention, no truth engine, no LLM summarizer hidden inside retrieval, no credential vault, no automatic execution of downloads, no automatic send/purchase/publish/delete/account mutation, no mirroring of every Playwright method (issue 1 section 21)

## Assumptions

- Every implementation PR bumps the version (version-check CI job) and any merge to main touching webglass/\*\* publishes to PyPI (PRs publish .devN to TestPyPI) — each milestone lands as a public release, so the pre-implementation status text in learn/README/explain must be updated in the same PRs that ship real capability (.github/workflows/publish.yml, tests.yml)
- Process: before product code, the implementation plan answers the twelve questions in issue 1 section 22 and runs through the devague chain (/think -> /spec-to-plan, with /challenge between them) — this scope pass is the opening leg of that chain (issue 1 section 22, CLAUDE.md)

## Scope exploration

- `s1` — `issue #1 sections 1+14 (operation contract, library-first)`: the brief mandates one operation lifecycle for API/CLI/Colleague adapter with identical semantic results; text output is a rendering, and Colleague composes the library with no CLI subprocess round trip
  - seeds: `c2`
- `s2` — `issue #1 section 2 (state separation + WebContext)`: browser session, exploration, evidence, and web-memory are four distinct objects with different volatility/sensitivity; WebContext connects them per task via references and policy, enabling reduced-capability child contexts
  - seeds: `c3`
- `s3` — `issue #1 section 3 (effect classes + prepare/commit/verify)`: observe / local-state / remote-action are explicit classes with classify-upward; remote actions are a three-step protocol bound to page generation, never a boolean on click, with `outcome_unknown` on uncertainty
  - seeds: `c4`
- `s4` — `issue #1 section 17 (delivery sequence M0-M6)`: six staged milestones; M0 contract-and-characterization explicitly precedes any Playwright code; M4 targets the colleague repo; M5 gates interaction; M6 is on-demand only
  - seeds: `c5`
- `s5` — `webglass/cli/__init__.py + _errors.py + _output.py`: registration (`_build_parser` with a marked extension point), the CliError/no-traceback contract, the stdout/stderr split with the hint: prefix, and centralized exit codes 0/1/2 are real, tested machinery every WebGlass noun registers onto
  - seeds: `c6`
- `s6` — `webglass/explain/catalog.py + _commands/cli.py + _commands/learn.py + CI lint job`: the explain catalog is a dict keyed by path tuples (root keyed under both webglass-cli and webglass for the rubric `explain_self` probe); the cli noun exists purely for `overview_cli_noun_exists`; teken cli doctor --strict is a hard CI gate, so every new noun needs catalog + overview + learn updates
  - seeds: `c7`
- `s7` — `pyproject.toml + issue #1 section 12 (replaceable adapter)`: dependencies = \[\] today with teken dev-only; the brief requires Playwright as a declared extra or adapter package behind seven protocol seams plus injectable Clock/ID providers for deterministic tests
  - seeds: `c8`
- `s8` — `tests/test_cli.py + tests/test_cli_introspection.py`: 25 tests already characterize version/help/unknown-command routing, whoami/learn/explain/overview/doctor text+JSON shapes, catalog resolution, console-script naming, and structured argparse errors — the M0 characterization baseline partially exists and must be extended, not replaced
  - seeds: `c9`
- `s9` — `sibling repos agentculture/shell-cli + agentculture/colleague`: shell-cli exists (guarded local operations plane) and colleague exists; the M5 artifact bridge and M4 provider have real counterparts, and the dependency direction plus .colleague-file ignorance are enforceable boundaries now
  - seeds: `c10`
- `s10` — `issue #1 section 21 (non-goals)`: twelve explicit refusals including crawler framework, stealth/CAPTCHA bypass, truth engine, hidden LLM summarizer, credential vault, auto-execution and auto-send — these become permanent boundary tests, not just doc text
  - seeds: `c11`
- `s11` — `issue #1 sections 10+11 (adversarial input + web policy)`: four trust zones must stay distinguishable in every result; scheme/target denylists, per-hop redirect revalidation, sandbox preservation, download quarantine, and malformed-policy-never-fails-open are baseline, not hardening backlog
  - seeds: `c12`
- `s12` — `issue #1 section 18 + pyproject coverage gate`: test ownership is split three ways (WebGlass / Colleague / shell-cli); no default test may hit a live public website; coverage `fail_under`=60 and the Sonar quality gate apply to all new product code
  - seeds: `c13`
- `s13` — `.github/workflows/publish.yml + tests.yml version-check`: publish triggers on any main push touching pyproject.toml or webglass/\*\*, and PRs publish .devN to TestPyPI; version-check fails any PR without a bump — so incremental milestone PRs are public releases and status text must track shipped reality
  - seeds: `c14`
- `s14` — `issue #1 section 22 + CLAUDE.md process requirements`: the brief itself requires a twelve-question implementation plan run through devague before product code, with named challenge areas (click semantics, SSRF, evidence immutability, child isolation, Playwright leakage)
  - seeds: `c15`
- `s15` — `culture.yaml + whoami.py read_agent_fields + doctor.py`: identity is a hand-rolled no-dependency parser reading the first agent block (suffix/backend/model); doctor enforces backend-consistency (colleague -> AGENTS.colleague.md) and skills-present; product work must not disturb this plumbing
  - seeds: `c16`
- `s16` — `README.md + CLAUDE.md known-gotchas + whoami.py docstring`: README already uses the correct webglass console script (the CLAUDE.md gotcha claiming otherwise is stale) and learn/explain/README are already WebGlass-branded with pre-implementation status; only whoami.py line 8 still carries clonable-template text
  - seeds: `c17`
- `s17` — `user direction (2026-08-07 session) + issue #1 framing gap`: issue 1 frames WebGlass around agent research (search/explore/evidence); the user adds testing web-based solutions incl. CI as a first-class use case — mechanically the same M2 observation verbs against a local target, so it rides the existing milestones rather than adding new ones
  - seeds: `c18`
- `s18` — `issue #1 section 10 default target denylist vs localhost testing`: the baseline denies loopback/link-local/private-network targets by default, which blocks exactly the CI app-under-test case; resolution is an explicit policy-profile allow for declared targets, keeping the default closed
  - seeds: `c19`
- `s19` — `issue #1 section 21 + ownership split (testing story)`: to keep non-goal discipline, WebGlass provides guarded operations + evidence for tests while assertions and orchestration stay caller-side; deterministic JSON/exit-code contracts already fit CI assertion use
  - seeds: `c20`
- `s20` — `q3 decision + issue #1 section 8 do-not-persist list`: choosing an API-backed first search provider pulls key handling forward from M6 to the search adapter; section 8 already forbids persisting secrets, so the key lives in config/env with redaction, not in any WebGlass store
  - seeds: `c21`
- `s21` — `issue #9 (colleague consumer brief, items 1-7 + field lesson)`: colleague needs localhost open, console/page-error evidence, selector extract, key-sequence press, screenshot-to-file, cross-invocation session reuse, and the agent-first contract — items 1-4 load-bearing for the 387 proof; the field lesson makes runtime console evidence the anti-false-positive requirement
  - seeds: `c27`, `c28`, `c29`, `c30`
- `s22` — `issue #9 scope relief + colleague spec claims c14/c21`: colleague consumes via CLI shell-out only (like its agtag/devex/sloth consumers) and needs no memory/exploration/search/Python API — confirming M3/M4 deferral in issue 8 and making the CLI subprocess contract the first-consumer surface
  - seeds: `c31`
- `s23` — `challenge pass / adjacent-systems lens: .github/workflows/tests.yml + publish.yml + doctor.py`: neither CI workflow has a browser-provisioning step and doctor has no browser checks — a hidden dependency of the Playwright-in-core decision; seeded the provisioning/diagnostics requirement
  - seeds: `c32`
- `s24` — `challenge pass / concurrency+security lens: c29 reattach mechanism (scratch probe)`: probe: detached Chromium + two sequential Playwright clients over CDP — in-memory JS state survived reattach (c29 mechanism feasible); same probe showed the sandbox is unavailable on AppArmor-userns-restricted hosts, and the CDP endpoint is secret-equivalent
  - seeds: `c32`, `c33`
- `s25` — `challenge pass / failure-modes lens: exported spec Requirements section`: budgets, timeouts, and cancellation appeared only in the `after_state`, never as a requirement — a converged-but-incomplete gap; brief section 1 makes budget exhaustion a structured result
  - seeds: `c34`
- `s26` — `challenge pass / overlooked-actors lens: app-under-test server lifecycle + press semantics`: nobody owned the dev-server process in the frame — assigned to the caller; press classification under a test profile (Enter can submit) surfaced as pending decision q4
  - seeds: `c36`
- `s27` — `challenge pass / SSRF-pivot check under test profile (clean)`: a hostile app-under-test page redirecting to metadata/private targets is contained by per-hop revalidation (c12) plus declared-target scoping (h12) — clean pass; residual risk only if profile scoping is implemented loosely
- `s28` — `challenge pass / reversibility + non-goal-creep lenses (clean)`: publish-on-merge irreversibility already carried by c14; testing-story scope creep already bounded by c20; token-estimate honesty folded into c34 — clean pass, no unrecorded findings
- `s29` — `challenge pass / security lens: c27 console evidence + c30 caller-path writes`: console text is an injection vector into tool output (spoofed WEBGLASS warnings) and --out could read as a download-quarantine bypass precedent; seeded the quarantine boundary c35 and honesty condition h28 on c27
  - seeds: `c35`

## Decisions

- The first real consumer (colleague, for the 387 self-learning proof) consumes webglass strictly as an operator-installed, allow-listed CLI subprocess — no Python import, no webglass memory/exploration/search — so the CLI contract is load-bearing from M2 and the M4 library provider stays deferred per issue 8 (issue 9 scope relief + timeline coupling; colleague spec 2026-08-07-prove-self-learning-387 claims c14/c21)
- Under a declared test profile, action press executes as observe for all keys including Enter — the explicit profile is the authorization, scoped to the declared app under test; outside a test profile classify-upward stands and ambiguous presses classify as remote-action (user decision 2026-08-07, resolving q4 from the challenge pass)

## Open parks

- [unknown_nonblocking] On-disk location and layout of the SQLite metadata store and content-addressed artifact store (per-user XDG data dir vs per-workspace) — issue 1 section 8 specifies the technology baseline but not placement; decidable at M3 planning.
- [unknown_nonblocking] Concrete shell-cli bridge protocol — the opaque artifact reference format and export/import verbs depend on the shell-cli artifact surface; needed for M5, not for M0-M2.
- [unknown_nonblocking] Credential brokering and authenticated sessions — issue 1 defers this to M6 as a separately threat-modelled capability; explicitly absent from the first useful release.
- [unknown_nonblocking] Platform support beyond Linux CI (macOS/Windows browser provisioning and session-reattach mechanics) — unexamined this pass; the first consumers (colleague, GitHub Actions) are Linux.
- [unknown_nonblocking] NDJSON/event streaming (brief section 14, listed in M1) is not carried in the spec claims — decide at plan time whether the M1 task includes it or it defers behind the subprocess-call pattern the first consumer uses.
