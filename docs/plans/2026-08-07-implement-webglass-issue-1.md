# Build Plan — implement WebGlass (issue #1)

slug: `implement-webglass-issue-1` · status: `exported` · from frame: `implement-webglass-issue-1`

> WebGlass ships its first useful release: agents search, open, read, inspect, follow, and screenshot the web through guarded, token-efficient, evidence-producing operations, per the issue 1 brief

## Tasks

### t1 — M0: extend characterization tests locking every existing contract

- covers: c9, h15, c16, h17, c24, h21, h5, c10, h16
- acceptance:
  - characterization suite locks the stdout/stderr split, exit codes, JSON shapes, explain catalog resolution, and whoami/doctor identity behavior — and passes unmodified against current main
  - an import-scan test asserts no playwright import and no Colleague import or .colleague read exists anywhere in the repo at M0

### t2 — M0: deterministic + hostile fixture site harness

- covers: c13, h9
- acceptance:
  - fixture sites served locally from the test harness with zero external network: clean page, throwing-on-load page, keydown-logging page, agent-state-node page, redirect chains including redirect-to-private-target, spoofed-console page, boilerplate-heavy page
  - the default suite passes with external network unavailable and coverage stays at or above `fail_under`=60

### t3 — M0: schema versioning decision, boundary docs, stale-text cleanup

- covers: c17, h10, c5
- acceptance:
  - operation/evidence schema version policy documented; Colleague and shell-cli boundaries documented; whoami.py clonable-template line and the stale CLAUDE.md README gotcha fixed; CLAUDE.md dependencies-are-empty language updated to record the Playwright-in-core decision
  - the M0 PR contains only tests, fixtures, and docs — no product code — and lands before any M1 PR

### t4 — M1: operation, result, and effects models (operations.py, results.py, effects.py)

- depends on: t1, t3
- covers: c4, h4
- acceptance:
  - WebOperation and WebOperationResult carry the section 1 field sets including schema version; lifecycle states enumerated; every operation kind declares exactly one effect class and ambiguous classification resolves upward (unit-tested)
  - result models carry structurally separate trusted/untrusted/sensitive/derived fields

### t5 — M1: policy core with default denylists and test-profile allows (policy.py)

- depends on: t3
- covers: c12, h8, c19, h12
- acceptance:
  - denies file:/javascript:/browser-internal schemes and loopback/link-local/private/metadata targets by default; re-evaluates every redirect hop; an explicit profile allow admits only declared app-under-test targets; malformed policy returns a structured error, never fails open (property-tested)

### t6 — M1: adapter protocols, fake backends, conformance tests (adapters/)

- depends on: t4
- covers: c8, h14
- acceptance:
  - SearchProvider/FetchBackend/BrowserBackend/BrowserSessionStore/ArtifactStore/WebPolicyEvaluator protocol seams plus Clock/ID providers defined; fake adapters drive the full operation lifecycle in tests; a conformance suite any adapter must pass exists; the import-boundary test asserts no playwright import outside the adapter module and no Playwright types in public API hints

### t7 — M1: page snapshot, extraction, and stable references (pages.py, extraction.py, references.py)

- depends on: t3
- acceptance:
  - PageSnapshot carries stable snapshot-scoped references; all lenses preserve block ids; deterministic extraction declares every omitted or truncated region; stale references fail clearly (unit-tested)

### t8 — M1: WebContext and four-store separation, sessions store first (sessions.py, context)

- depends on: t3
- covers: c3, h3
- acceptance:
  - the four state kinds are separate modules; WebContext holds only references and policy; closing a session touches neither evidence nor exploration; exploration and memory are typed interface stubs deferred to M3 (issue 8) with no implementation entangled

### t9 — M1: operation service with budgets, timeouts, cancellation (service.py)

- depends on: t4, t8
- covers: c2, c34, h31
- acceptance:
  - one service serves library and CLI callers; per-dimension budget counters (requests, bytes, browser time, artifact bytes, labeled heuristic token estimates); exhaustion, timeout, and cancellation yield structured budget-exhausted/`timed_out`/cancelled results preserving partial evidence; bounded for 120-300s subprocess budgets

### t10 — M1: CLI noun registration over fakes + introspection updates (cli/`_commands`, catalog, learn)

- depends on: t9, t6
- covers: c6, h6, c7, h7, h2
- acceptance:
  - search/page/action/session verbs register per the skeleton pattern; a per-verb contract test asserts the CLI --json payload equals the library result rendering; every new path tuple resolves in the explain catalog; every action-verb noun exposes overview; learn lists every implemented verb; uv run teken cli doctor . --strict passes

### t11 — M2: Playwright/Chromium adapter with detached-session reattach (adapters/playwright.py)

- depends on: t6, t2
- covers: c29, h26
- acceptance:
  - launches a pinned Chromium; an integration test proves CDP reattach to a detached browser with in-memory JS state surviving across separate processes (mirrors the challenge-pass probe); no Playwright types cross the protocol seam; sandbox availability is detected and unavailability yields a structured error — never a silent --no-sandbox fallback

### t12 — M2: session lifecycle verbs, endpoint secrecy, leases, cleanup (session create/list/show/close/clean)

- depends on: t11, t8
- covers: c33, h30
- acceptance:
  - the stored endpoint file is 0600-class and absent from JSON output, logs, and evidence (planted-secret test); concurrent attaches to one session: one proceeds, the other receives a structured lease refusal; session clean reaps expired leases and their browser processes

### t13 — M2: observation verbs live against fixtures (page open/read/inspect/extract/links/screenshot, action follow/press)

- depends on: t11, t12, t10, t5, t7
- covers: c27, h24, h28, c28, h25, c30, h27, c35, h32, c36, h33
- acceptance:
  - the throwing fixture yields error text and source location in --json and the clean page yields an explicitly empty error list; the spoofed-console fixture renders only under untrusted labels, never as a WebGlass warning
  - the keydown fixture shows the exact pressed sequence after action press (observe under the declared test profile, classify-upward elsewhere); selector-scoped extract returns exactly one selectors content; page screenshot --out writes a decodable PNG and is the only caller-path write
  - an unreachable app-under-test target yields a structured connection error telling the caller to check their server; webglass never spawns or kills that process

### t14 — M2: doctor browser diagnostics + CI browser provisioning (doctor.py, workflows)

- depends on: t11
- covers: c32, h29
- acceptance:
  - doctor reports playwright-importable, chromium-installed, pinned-version, and usable-sandbox checks with actionable remediation in the existing rubric check shape; the workflows provision a pinned cached Chromium only for the browser-integration job; a browserless run still passes the non-browser suite

### t15 — M2: real API search provider + key redaction (adapters/, proposed vendor: Brave Search API)

- depends on: t6
- covers: c21, h13
- acceptance:
  - a SearchProvider implementation for the chosen API vendor sits behind the seam and passes the adapter conformance suite; keys come only from environment/config at call time; a planted-key redaction test asserts absence from every output and store; provider tests use recorded/fake fixtures — no live API in the default suite

### t16 — M2: CI recipe, definition-of-done sweep, audience entry paths (docs, example workflow, status text)

- depends on: t13, t14, t15
- covers: c1, h1, c18, h11, c20, h18, c22, h19, c23, h20, c25, h22, c26, h23
- acceptance:
  - a documented GitHub Actions recipe starts a local fixture app and drives it via webglass, asserting on JSON results and exit codes with caller-side assertions only; the seven issue 9 acceptance criteria pass as fixture-based tests
  - the section 20 definition-of-done checklist restricted to M0-M2 is walked with each item pointing at a passing test; learn/README/explain status text updated to shipped reality; library import, console script, and CI recipe entry paths all demonstrated

## Risks

- [follow_up] NDJSON/event streaming (brief section 14, listed in M1) is deliberately deferred out of this plan — the first consumer calls one-shot subprocesses; revisit at M3/M4 planning (frame park v5)
- [unknown_nonblocking] Platform support beyond Linux CI (macOS/Windows provisioning and reattach mechanics) unexamined — first consumers are Linux (frame park v4)
- [unknown_nonblocking] Search API vendor: Brave Search API proposed in t15 (real API, free tier, key-based; DDG has no official API) — vendor is final only when the user confirms t15 (resolves the q3 finalize-at-plan-time clause)
- [unknown_nonblocking] Chromium sandbox on CI: ubuntu-24.04 runners restrict unprivileged user namespaces via AppArmor (probed locally: chrome aborts) — t14 must verify the provisioning path yields a usable sandbox on GitHub Actions runners (task t14)
- [unknown_nonblocking] colleague#387 timeline couples to M2 (issue 9): items 1-4 of their brief are load-bearing for the proof — if M1 slips materially, tell colleague on the issue 9 thread rather than letting silence stand
