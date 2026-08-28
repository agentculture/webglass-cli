# Build Plan — session and browser lifecycle hygiene (issue 14)

slug: `session-and-browser-lifecycle-hygiene-issue-14` · status: `exported` · from frame: `session-and-browser-lifecycle-hygiene-issue-14`

> webglass never leaves a session record or headless browser nobody can see or reap: ephemeral navigations are labelled honestly, doctor warns on session-store growth, and session clean --older-than sweeps stale records and orphaned browsers

## Tasks

### t1 — Shared test harness: seed a session store with records of given status, expiry, owner, and hosts

- instruction: Files: tests/conftest.py + a new tests/helpers module. Build on the existing `isolated_session_state` autouse fixture (pins `WEBGLASS_STATE_DIR` per test) rather than replacing it.
- covers: h20, c19
- acceptance:
  - A fixture builds a store with N records at chosen status/expiry/owner/host-set, used by every later task's tests
  - A helper asserts the before-state numbers from the reporting host (134 records: 115 closed, 19 active-with-dead-pids) are reproducible from seeded state

### t2 — Report ephemeral: true for a CLI-provisioned throwaway session

- instruction: Files: webglass/operations.py (or context.py) for the carried flag, webglass/service.py:1185-1198, webglass/cli/`_factory.py`:579-655. Set the flag in `ephemeral_session`; read it in `_session_for_navigation` instead of inferring from `session_id` is None.
- depends on: t1
- covers: c2, h2
- acceptance:
  - A navigating verb with no --session-id renders ephemeral: true in --json; the fact travels on the operation, not inferred from `session_id` is None
  - A library caller creating a session explicitly still renders ephemeral: false, from the same field
  - The test goes red against current main

### t3 — Report observed liveness for a record whose recorded pid is dead

- instruction: Files: webglass/adapters/`session_store.py` `to_public_dict` (403-416). Use the existing `_is_running` (line 264) at render time. Never write status back on a read path.
- depends on: t1
- covers: c7, h4
- acceptance:
  - session list/show render a record with a dead pid as not-live, without mutating status on the read path
  - A pid alive but owned by another user is not claimed as ours
  - The test goes red against current main

### t4 — Drop record retention from 7 days to 3

- instruction: File: webglass/adapters/`session_store.py`:167 only. Smallest task in the plan; keep it that way so the store chain has a cheap first link.
- depends on: t1
- covers: c18
- acceptance:
  - `DEFAULT_RECORD_RETENTION_SECONDS` is 3\*24\*3600 and a record is purgeable at the 3-day boundary, not before
  - No test, doc, or explain entry still promises 7 days

### t5 — Make store read paths survive a corrupt record

- instruction: Files: webglass/adapters/`session_store.py` list()/`_read_unlocked` (498-516, 712-728). clean() already purges unreadable records correctly — mirror that tolerance in the read paths rather than inventing a second policy.
- depends on: t1
- covers: c32, h26
- acceptance:
  - A planted corrupt record does not raise out of the read path; it is reported as a corruption finding
  - doctor --json still returns its full check list, naming the corrupt record
  - The test goes red against current main

### t6 — Gate clean() on liveness so a sweep never kills a live browser

- instruction: Files: webglass/adapters/`session_store.py` clean() (533-578) and `acquire_lease` (580-620). Extend `expires_at` on lease acquisition; skip records with a held unexpired lease. Liveness is the gate — do NOT filter by owner here (c38).
- depends on: t1, t3
- covers: c28, h24, c31, h28
- acceptance:
  - clean() skips a record whose lease is held and unexpired, and `expires_at` is extended on lease acquisition
  - A long operation past its TTL survives a concurrent session-creating invocation with its browser intact
  - The sweep never signals a pid absent from a WebGlass record, and reaps an expired record belonging to another owner (liveness gates, ownership does not)
  - The test goes red against current main

### t7 — Add an owner token to the record, scoped to reuse eligibility only

- instruction: Files: webglass/adapters/`session_store.py` record + `_to_payload`/`_from_payload`, webglass/cli/`_factory.py` for minting. Additive with a safe default; `_from_payload` must default it so `RECORD_SCHEMA_VERSION` stays 1.
- depends on: t1, t4
- covers: c35, h29
- acceptance:
  - A unique owner token is minted per invocation and stored additively with a safe default; `RECORD_SCHEMA_VERSION` stays 1
  - A pre-upgrade record with no owner token is never claimed by any owner
  - The token gates reuse matching only — clean() does not filter on it (c38 supersedes c35's sweep-filtering clause)

### t8 — Record top-level navigation hosts on the session record

- instruction: Files: webglass/adapters/`session_store.py` record fields, plus the navigation-commit hook in webglass/service.py or adapters/playwright.py. Hook top-level navigation commit ONLY — never subresource requests.
- depends on: t1, t7
- covers: c25, h12, c16, h17, c27, h23
- acceptance:
  - Only navigated document hosts are recorded — a page pulling subresources from other hosts records just its own
  - The host set is deduplicated and bounded; a redirect chain records the hosts actually navigated to
  - The field lands additively (`RECORD_SCHEMA_VERSION` stays 1) and a pre-upgrade record reads as unknown-hosts, not no-hosts
  - The host set stays out of the redacting repr() and out of every log line; it holds host-level facts only, never content, cookies, or credentials

### t9 — Give session clean its filter flags: --older-than, --status, --site

- instruction: Files: webglass/adapters/`session_store.py` clean() signature, webglass/cli/`_commands`/session.py:145-148 (the flagless clean parser). Filters evaluated inside the store under its lock; the CLI only parses and passes.
- depends on: t6, t8
- covers: c13, c5, h3, h5
- acceptance:
  - Filters are evaluated inside store.clean under its per-record lock, never by post-filtering in the CLI
  - Flags compose as AND; an unmatched filter reaps nothing and exits 0
  - A malformed duration is a structured `invalid_argument`, not a silent default
  - --site matches any host in the record's top-level navigation set

### t10 — Wire the time-bounded opportunistic sweep into session-creating invocations

- instruction: Files: webglass/cli/`_factory.py` (the `ephemeral_session`/session-create path). Time-bounded, outside the result path, errors swallowed. Do not hook read verbs.
- depends on: t6, t9
- covers: c14, h6, c1, h1
- acceptance:
  - The sweep runs on session-creating invocations only; a read verb leaves the record count unchanged
  - It is bounded by TIME per invocation and stops when the budget is spent
  - It runs outside the operation result path and swallows its own errors — a sweep failure never fails the observation
  - An ordinary invocation against a store of expired records shrinks it with no session clean call; the test goes red against current main

### t11 — Give the automatic sweep an observability surface

- instruction: Files: webglass/service.py, webglass/results.py. Reuse the diagnostics/effects channel; keep `_op_session_clean`'s cross-owner warning (service.py:2191-2198) intact.
- depends on: t10
- covers: c30, h25
- acceptance:
  - An invocation whose sweep reaped something says so in its --json envelope
  - A caller can reconstruct what disappeared without having watched it happen
  - The cross-owner warning from `_op_session_clean` is preserved

### t12 — Add the session-store health check to doctor

- instruction: Files: webglass/cli/`_browser_doctor.py` (`browser_checks`, 358-366) and webglass/cli/`_commands`/doctor.py. Follow `check_state_dir_writable`'s shape. Build on the corruption-tolerant read from t5, not on list().
- depends on: t3, t5
- covers: c4, h7, c37, h31
- acceptance:
  - `browser_checks`() gains a session-store check reporting live/stale counts and record-directory size
  - It warns above 10 live sessions for one owner with passed=true, severity=warning — doctor's exit code never fails on it
  - It is read-only and cheap: no browser launch, no lock, no record mutation
  - Numbers are asserted directly at the 10/11 boundary; the test goes red against current main

### t13 — Session reuse: match an active session and bump its generation

- instruction: Files: webglass/cli/`_factory.py` for the match, webglass/adapters/`session_store.py` `bump_generation` (631). Call `bump_generation` on reuse or the stale-reference guarantee in references.py breaks.
- depends on: t7, t8
- covers: c23, h9, h13, c33, h27
- acceptance:
  - A new case matches the owner's recent sessions by visited host; only an ACTIVE, unexpired, lease-acquirable record is offered
  - A closed or expired record is never offered for reuse, even inside the 3-day window
  - Reuse bumps the session generation; a reference minted before the reuse is refused with the stale-reference error
  - Reuse is opt-in and observable: the result names the session it reused, and a caller can always demand a fresh context

### t14 — Pin the behaviours this work must not break

- instruction: Files: tests only — tests/`test_playwright_adapter.py`, tests/`test_import_boundaries.py`, and a new ephemeral-failure test. Pure regression pins; no production code changes.
- depends on: t1
- covers: c3, h14, c8, h15, c10, h16
- acceptance:
  - A failed navigation in an ephemeral session ends closed, browser terminated, profile directory gone
  - `start_new_session`=True in `launch_detached` is asserted by a test, not merely left alone
  - An import-boundary test asserts the session plane imports no evidence.py, exploration.py, or memory.py

### t15 — Update every surface describing the throwaway-session contract

- instruction: Files: webglass/cli/`_commands`/page.py:31/75/208, webglass/explain/catalog.py:226, webglass/cli/`_commands`/session.py:41-56. Run last among code tasks so the wording describes what actually shipped.
- depends on: t2, t10
- covers: c11, h8
- acceptance:
  - page.py help/docstring, explain/catalog.py, and session overview's Persistence section all describe the shipped behaviour
  - A test asserts the wording is consistent across all four surfaces, the way `test_every_catalog_path_resolves` guards the catalog

### t16 — Report the outcome: issue reply, PR evidence, and the audience-facing framing

- instruction: Not a code task: the GitHub reply on issue #14 and the PR body. Re-measure the reporting host before and after. Also bump the version (version-check CI gate) and update CLAUDE.md's status paragraph if the shipped nouns change.
- depends on: t11, t12, t13, t14, t15
- covers: c17, h18, c20, h21, c21, h22, c18, h19
- acceptance:
  - Issue #14's three asks are answered individually, naming which were real and which rested on the wrong premise (the 187 chromium processes were the reporter's desktop Chromium)
  - The PR body carries the before and after record counts measured on the reporting host
  - Every clause of the after-state is separately observable, and all five success-signal tests are shown red against main before implementation

## Risks

- [out_of_scope] `session_store.py` is touched by t3, t4, t5, t6, t7, t8, t9, and t13. The dependency chain serializes them deliberately — these waves are narrow on purpose, and running them in parallel would collide at merge regardless of what 'waves' reports.
- [follow_up] c35's text says the owner token filters the sweep; c38/c39 supersede that clause (liveness gates the sweep, ownership gates reuse only). t7's third acceptance criterion carries the correction, but a reader of c35 alone would build the wrong thing.
