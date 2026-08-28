# Delivery Summary — session and browser lifecycle hygiene (issue 14)

plan: `session-and-browser-lifecycle-hygiene-issue-14` · run: `complete` · date: `2026-08-28`
baseline: `devague summary skeleton`

## Intent

Issue [#14](https://github.com/agentculture/webglass-cli/issues/14) reported 126 session
records, 187 chromium processes and 42 GB RSS on one host, and filed three asks against
WebGlass. This run executed the converged plan seeded from the challenged frame: 16 tasks
across 8 dependency waves, fanned out one agent per task per isolated git worktree, each
merge TDD-gated by the main agent.

Investigation corrected the issue's premise before any code was written. Two of the three
asks were real; the third rested on a misattribution.

## Planned Work

Quoted verbatim from the `devague summary` skeleton:

- `t1` — Shared test harness: seed a session store with records of given status, expiry, owner, and hosts
- `t2` — Report ephemeral: true for a CLI-provisioned throwaway session
- `t3` — Report observed liveness for a record whose recorded pid is dead
- `t4` — Drop record retention from 7 days to 3
- `t5` — Make store read paths survive a corrupt record
- `t6` — Gate clean() on liveness so a sweep never kills a live browser
- `t7` — Add an owner token to the record, scoped to reuse eligibility only
- `t8` — Record top-level navigation hosts on the session record
- `t9` — Give session clean its filter flags: --older-than, --status, --site
- `t10` — Wire the time-bounded opportunistic sweep into session-creating invocations
- `t11` — Give the automatic sweep an observability surface
- `t12` — Add the session-store health check to doctor
- `t13` — Session reuse: match an active session and bump its generation
- `t14` — Pin the behaviours this work must not break
- `t15` — Update every surface describing the throwaway-session contract
- `t16` — Report the outcome: issue reply, PR evidence, and the audience-facing framing

## Actual Delivery

| Plan task | Status | What actually landed |
|-----------|--------|----------------------|
| `t1` | delivered | `tests/helpers/session_seed.py` + `session_store`/`seed_session_records` fixtures; `spawn_dead_pid()` mints a genuinely dead pid via a reaped subprocess. Forward-compat `owner_token`/`hosts` kwargs gated on `dataclasses.fields()` so t7/t8 needed no harness rewrite. |
| `t2` | delivered | `WebOperation.session_ephemeral` carried from `ephemeral_session` (new `ProvisionedSession` dataclass) and read by `_session_for_navigation`. Also fixed the live-read path, which hardcoded the same `False`. |
| `t3` | delivered | `observed_liveness` (running/dead/unknown) on `to_public_dict()`, computed fresh per call from a tri-state `_pid_liveness()`. No read path mutates a record. |
| `t4` | delivered | `DEFAULT_RECORD_RETENTION_SECONDS` 7d → 3d; no remaining 7-day promise outside historical spec/plan text. |
| `t5` | delivered | `list()` skips corrupt records instead of raising; new `list_corrupt()` reports which and why. `get()`'s contract unchanged. |
| `t6` | delivered | `clean()` skips a record holding a live unexpired lease; `acquire_lease` slides `expires_at` by the record's original lifetime; `_reap_browser` no longer signals a foreign pid. |
| `t7` | delivered | `FileSessionRecord.owner_token` (additive, `RECORD_SCHEMA_VERSION` stays 1), minted per invocation. `""` = claimed by nobody. Not used by `clean()`. |
| `t8` | delivered | `FileSessionRecord.hosts` (an optional host tuple) recorded at top-level navigation only, capped at 32 with a declared truncation diagnostic. `None` (never tracked) stays distinct from `()`. Kept out of `repr()` via a new `REPR_REDACTED` field-metadata seam. |
| `t9` | delivered | `--older-than` / `--status` / `--site` on `session clean`, AND-composed, evaluated inside `store.clean` under its per-record lock. Malformed duration → structured `invalid_argument`, exit 1. |
| `t10` | delivered | Time-bounded opportunistic sweep (`OPPORTUNISTIC_SWEEP_BUDGET_SECONDS = 0.030`) on session-creating invocations only; errors swallowed; never on a read verb. |
| `t11` | delivered | `WebOperationResult.swept_sessions`, attached in `_build_result()` regardless of lifecycle outcome, rendered through `to_public_dict()` so `endpoint_ref` cannot leak. |
| `t12` | delivered | `session_store_health` check in `browser_checks()`; warns above 10 live sessions per owner with `passed=true`, never failing doctor's exit code. Built on t5's corruption-tolerant read. |
| `t13` | delivered | Flow-scoped reuse via `$WEBGLASS_SESSION_OWNER`, `--fresh-session` opt-out, lease acquired (not merely read), `bump_generation` on reuse. Also wired `_session_generation()` into `_record_snapshot` — without it generation was always 0 and the bump protected nothing. |
| `t14` | delivered | Regression pins for the close-on-failure path, `start_new_session=True`, and the M3 import boundary. |
| `t15` | delivered | New `webglass/cli/_session_wording.py` renders shared claims across four surfaces, guarded by `test_session_contract_wording_is_consistent`. |
| `t16` | partial | Version bumped 0.7.0 → 0.8.0 with changelog; CLAUDE.md status paragraph refreshed; before/after host measurements captured; issue reply drafted. **The issue comment and the PR are not yet posted** — those land in the `/cicd` leg and the final human PR gate. |

## Mid-work Decisions

No `/deviate` records were created during this run (`devague deviate --list` → none), so
every decision below is captured directly rather than quoted from a `dN` record. That is
itself a process gap worth noting: several of these were plan-affecting and would have
been better recorded as they happened.

- **Sweep gated on liveness, not ownership** — the plan's `c35` text said the owner token
  filters the sweep; claims `c38`/`c39` superseded it during the challenge pass. An
  expired record means its owner is finished or crashed, so reaping it is safe, and
  owner-filtering would have made the sweep useless (a one-shot invocation owns one
  session and would reap nothing). Plan risk `r2` predicted exactly this contradiction.
- **Time budget placed inside `clean()`, not the factory** (t10) — a deadline can only be
  checked between records; wrapping from outside would have duplicated the lock/read/reap
  loop. Budget set to 30ms rather than the spec's 50ms because the check happens between
  records, so the real bound is budget-plus-one-record — and that record is the expensive
  reaping one.
- **`max(expires_at, lease.expires_at)` would have been a no-op** (t6) — the lease TTL is
  30s and the session TTL 300s, so the obvious fix never moves anything in the real case.
  The expiry is instead slid by the record's original lifetime, recovered from the
  `expires_at - last_used_at` invariant the store already maintains — no new field, no
  schema bump.
- **Reuse made opt-in via an environment variable** (t13) — a one-shot process cannot
  discover that another process was its previous step, so a flag alone would either never
  match or leak a retained session per call. `$WEBGLASS_SESSION_OWNER` names the flow;
  unset (the default) behaviour is byte-identical to before.
- **Hosts recorded before the policy re-check** (t8) — `--site` should find a session that
  touched a host regardless of whether the caller was allowed to see the response. This is
  consistent with deviation `d1` (the browser has already contacted intermediate hops), but
  it means the host set can contain a host whose content was denied.
- **`repr()` no, `to_public_dict()` yes for hosts** (t8) — `repr()` surfaces in tracebacks
  and third-party logs scoped to nobody; `to_public_dict()` is caller-scoped, and data a
  caller cannot see is data they cannot knowingly forget.
- **A weak t14 test was replaced by the merge gate, not the author** — the merged
  regression pin asserted `start_new_session=True` by regex-scanning module source text,
  which would keep passing if `launch_detached` stopped calling `Popen` at all. Replaced
  with an interception test and mutation-verified.
- **`InMemorySessionStore` parity deliberately not decided** — t6 flagged that the
  reference store does not mirror the new liveness gate, argued the harm case does not
  exist there (its `clean()` kills no browser), and declined to change another agent's
  file surface. t9 later extended it with the filter arguments only.

## Drift From Plan

| Plan item | Reason for divergence | Classification |
|-----------|-----------------------|----------------|
| `t16` | Issue comment and PR intentionally deferred to the `/cicd` leg and the human PR gate; the rest of the task (version bump, CLAUDE.md, measurements, drafted reply) is delivered. | acceptable |
| `t7` | Its acceptance criterion inherited `c35`'s superseded claim that the owner token filters the sweep. Built to `c38`/`c39` instead — ownership gates reuse only. Predicted by plan risk `r2`. | acceptable |
| `t14` | Delivered as planned, then hardened by the merge gate after the fact (source-scan → behavioural assertion). The plan did not anticipate the main agent strengthening a merged test. | acceptable |
| `t15` | Briefed before `t11` existed, so it did not cover `swept_sessions`. Closed by the main agent in a follow-up commit, along with two gaps t15 flagged in its own docstring (an unchecked duplicated constant, and a normalizer that stripped markup but not newlines). | acceptable |
| `t8`/`t13` | Both touched files outside their listed sets (`webglass/sessions.py`, `operations.py`, `service.py`, `page.py`). Each reported it explicitly; none conflicted at merge. | acceptable |

Wave 2 also produced one semantic conflict git could not see: `t14`'s test bound
`ephemeral_session`'s yield as a bare id while `t2` changed it to a `ProvisionedSession`.
Both merged textually clean and the suite went red. Reconciled by the main agent. This is
not plan drift — it is the TDD gate working as designed — but it is the concrete
justification for the "tests pass **after** merge" rule.

## Evidence

- tests: full suite `uv run pytest -n auto -q` — **2148 passed, 34 skipped** (baseline before this run: 2016 passed, 34 skipped)
- tests: coverage `uv run pytest --cov=webglass` — **96.42%** against a 60% floor
- tests: opt-in real-browser suite (`WEBGLASS_TEST_BROWSER=1`, sandbox intact) — **124 passed, 13 skipped**
- lint: `black --check`, `isort --check-only`, `flake8`, `bandit -c pyproject.toml -r webglass` — all clean
- lint: `markdownlint-cli2 "**/*.md" ...` — 0 errors
- rubric: `uv run teken cli doctor . --strict` — pass (8 doctor checks)
- commits: `b4a52bc..e290101` — 35 commits, 15 merge commits, 36 files changed (+5414/−95)
- issues: `#14`; related `agentculture/colleague#436`, `#435`

Live measurements on the reporting host:

| | before | after |
|---|---|---|
| session records | 134 (0 live, 19 stale) | 29, then bounded to ~3 under ordinary traffic |
| state directory | 215,828,742 bytes | 22,654 bytes |

Seeded with 300 stale records, repeated ordinary invocations drove the store
301 → 6 → 3 with no `session clean` call. 296 of 300 were expired on the first pass before
the time budget stopped it — graceful degradation, not a stall.

## Delivery Claims

| Claim | Confidence | Evidence |
|-------|------------|----------|
| A CLI throwaway navigation reports `ephemeral: true` | high | live `page open` against real Chromium returned `ephemeral: True`; test `tests/test_verbs_live.py::test_a_cli_provisioned_throwaway_session_reports_itself_ephemeral` |
| The session store bounds itself with no `session clean` call | high | 300 seeded stale records → 301 → 6 → 3 across ordinary invocations; test `tests/test_opportunistic_sweep.py::test_an_ordinary_invocation_sweeps_the_store_with_no_session_clean_call` |
| A read verb never mutates the store | high | test `tests/test_opportunistic_sweep.py::test_a_read_verb_leaves_the_record_count_unchanged` |
| The sweep never kills a live browser mid-operation | high | test `tests/test_session_clean_liveness.py` concurrency case, run 4× without flake |
| The sweep stays under the latency target | medium | test asserts <50ms against 300 records; measured on this host only |
| `doctor` warns on store growth without failing its exit code | high | live: `[ok] session_store_health: 134 record(s) … 215828742 bytes`; tests at the 10/11 boundary |
| `session clean --older-than` reaps by age | high | live run reaped 19 stale records and 206 MB on the reporting host; exit 1 + `hint:` on a malformed duration |
| A dead-pid record no longer reads as live | high | test `tests/test_session_liveness.py`; live doctor reported `0 live, 19 stale` |
| Flow reuse works across separate processes and is off by default | high | live: 3 one-shot processes shared one session id with `$WEBGLASS_SESSION_OWNER` set; two calls without it never shared; `--fresh-session` opted out |
| Reuse cannot resolve a stale element reference | high | test `tests/test_session_reuse.py` end-to-end `ERROR_STALE_REFERENCE` case |
| Visited hosts never reach `repr()` or logs | high | tests assert empty `caplog` across a recorded navigation and absence from `repr()` |
| The 187 chromium processes were not WebGlass | high | `ps` showed one `/snap/chromium/3506` desktop parent + 186 `--type=` children; no process carried a `--user-data-dir` under the WebGlass state dir; all 134 recorded pids dead |
| The ephemeral close path was already correct before this work | high | 115/115 `ephemeral-*` records measured `closed` with `browser_reaped=true`; pinned by `t14` |
| Behaviour is unchanged for callers who do not opt in | medium | reuse is off without `$WEBGLASS_SESSION_OWNER` (tested); the sweep and the new record fields still apply to every caller |
| Issue #14 is answered on the issue itself | unverified | reply drafted, not yet posted — deferred to `/cicd` |
| CI passes on the PR | unverified | PR not yet opened |

## Remaining Work / Follow-up

- `t16` — post the drafted issue reply to `#14` and open the PR (`/cicd` leg, then the
  human PR gate). The reply answers all three asks individually and names the
  misattributed one.
- **`InMemorySessionStore` liveness parity** — flagged by t6, deliberately undecided. Its
  `clean()` kills no browser so the harm case does not exist, but the reference store now
  diverges from the file store's semantics. Needs a decision, not a patch.
- **`--site` cannot match a pre-upgrade record** whose `hosts` is `None`. Correct by
  design (unknown ≠ empty), but it means the filter is only fully effective for sessions
  created after this release.
- **Sweep latency measured on one host only** — the <50ms claim is medium-confidence
  until it is seen on a slower or loaded filesystem, which is precisely the case the
  time bound (rather than a count bound) exists for.
- **No `/deviate` records were created** during this run despite several plan-affecting
  mid-work decisions. Next workforce run should record them as they happen rather than
  reconstructing them here.
