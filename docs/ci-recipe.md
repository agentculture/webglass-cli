# CI recipe: driving a local web app with `webglass`

WebGlass is also a first-class tool for testing web-based solutions,
including on CI: an agent or pipeline drives a locally served app headlessly
through the same guarded observation verbs it uses for research (`open` /
`read` / `inspect` / `extract` / `screenshot`), asserts against structured
JSON results and exit codes, and keeps evidence records (snapshots,
screenshots) as CI artifacts.

This is the recipe [issue #9](https://github.com/agentculture/webglass-cli/issues/9)
asked for: colleague's browser-driven verification arm for its self-learning
proof runs a browser game locally and needs to tell "the game runs" from "the
game is a dead canvas" by CLI evidence alone — the exact field lesson that
motivated the console/page-error work in the M2 slice. The pattern below is
general-purpose: any CI job or agent that already has a locally served app
(a dev server, a static build, a game) can use it.

This file is kept in sync with
[`.github/workflows/example-webapp-test.yml`](../.github/workflows/example-webapp-test.yml),
which runs the recipe below for real in this repo's own CI on every PR that
touches `webglass/**` or `pyproject.toml`. That workflow is the executable
proof that this page is not aspirational.

## What this is not

**WebGlass does not grow a test framework or an assertion DSL.** Every
assertion in this recipe is an ordinary `jq` expression over `webglass`'s
`--json` output, run by the caller — the CI script, or an agent reading the
same JSON. WebGlass's job stops at producing a structured, machine-readable
result; deciding pass/fail, retrying, and reporting stay entirely
caller-side, mirroring the Colleague/WebGlass ownership split
([`CLAUDE.md`](../CLAUDE.md) "Test ownership").

**WebGlass never starts, stops, or supervises the app under test.** That
process's lifecycle — starting the dev server, waiting for it to be ready,
killing it afterward — is always the caller's job. An unreachable target
(connection refused, DNS failure, navigation timeout) is a structured
`navigation_failed` result telling you to check your server, never a crash
and never an attempt by WebGlass to manage that process for you.

## The moving pieces

1. **Install `webglass-cli`.** It ships Playwright as a core runtime
   dependency (no separate install step for the Python package itself), but
   Chromium is a separate download — see step 3.
2. **Write a policy profile.** Loopback and private-network targets are
   **denied by default** ([issue #1 section 10](https://github.com/agentculture/webglass-cli/issues/1)) —
   that default does not change for CI. Reaching your app under test requires
   an explicit, scoped allow: a JSON file naming exactly that origin in
   `declared_targets`. This is data evaluated by the same policy core that
   denies everything else, never a flag that widens policy or a code path
   that bypasses it (`webglass explain page` and `webglass explain action
   press` cover the same mechanism for other verbs).
3. **Provision a pinned, cached Chromium**, including the AppArmor sysctl
   step GitHub's `ubuntu-24.04` runners need — copied verbatim from the
   `browser-test` job in [`tests.yml`](../.github/workflows/tests.yml),
   because every job that launches a real Chromium needs it.
4. **Start your app under test** any way you like — `python -m http.server`
   over a couple of static HTML files is enough to exercise every verb below,
   and is what the example workflow does.
5. **Drive it**: `session create` once, then `page open` / `page inspect
   --lens console` / `page extract --selector` / `page screenshot --out`
   against that one session — each a separate one-shot CLI invocation, none
   of them holding a daemon connection, all of them seeing the same live page
   (issue #9 item 6: session state persists across separate processes until
   `session close`).
6. **Assert with `jq`** on the JSON each verb printed to stdout, and on the
   process exit code (`0` success/`previewed`, `1` a structured failure —
   see `webglass learn`'s exit-code table).
7. **Close the session**, stop your app under test, and **upload the
   screenshot** as a CI artifact for a human (or another agent) to look at
   later.

## Step by step

### 1. Install

```bash
uv pip install webglass-cli   # or: pip install webglass-cli
uv run playwright install --with-deps chromium
```

(The example workflow instead runs `uv sync` against this repo's own
checkout, since it is exercising this repo's own code — install however
fits your pipeline; the verbs below are identical either way.)

### 2. Write the policy profile

```bash
cat > policy-profile.json <<'JSON'
{
  "name": "ci-example-webapp",
  "declared_targets": ["127.0.0.1:8000"]
}
JSON
```

`declared_targets` entries are `host:port` (or `host:*` for an ephemeral dev
port); a malformed profile is a structured, fail-closed error — never a
silent "everything allowed" — see `webglass explain page open`.

### 3. Provision Chromium (GitHub Actions, `ubuntu-24.04`)

```yaml
- run: uv run playwright install --with-deps chromium

- name: Allow unprivileged user namespaces (risk r4)
  run: |
    # ubuntu-24.04 GitHub-hosted runners restrict unprivileged user
    # namespace creation via AppArmor
    # (kernel.apparmor_restrict_unprivileged_userns=1), which makes a
    # default *sandboxed* Chromium launch abort with "No usable
    # sandbox!" — reproduced locally; see
    # webglass/adapters/playwright.py's SANDBOX_REMEDIATION and risk
    # r4 in docs/plans/2026-08-07-implement-webglass-issue-1.md
    # ("t14 must verify the provisioning path yields a usable sandbox
    # on GitHub Actions runners"). GitHub-hosted runners permit
    # passwordless sudo, so this job restores a genuinely usable
    # sandbox for Chromium rather than ever passing --no-sandbox,
    # which the adapter refuses to do as an automatic fallback.
    sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
```

Skip this step on a runner that already provides a usable sandbox (most
non-GitHub-hosted or non-`ubuntu-24.04`-class hosts); `webglass doctor`
reports `usable_sandbox` so you can check before you rely on it, and `page
open` refuses with an actionable structured error rather than silently
falling back to `--no-sandbox` if the sandbox is unavailable and this step
was skipped.

### 4. Start the app under test

```bash
python3 -m http.server 8000 --directory ./my-app --bind 127.0.0.1 &
echo $! > server.pid
for _ in $(seq 1 20); do
  curl -sf http://127.0.0.1:8000/ > /dev/null && break
  sleep 0.5
done
```

Any static file server or dev server works — WebGlass only ever observes it
over HTTP from the outside.

### 5. Drive it

```bash
uv run webglass session create --session-id ci-recipe --ttl-seconds 300 --json \
  | tee session.json
jq -e '.lifecycle_state == "succeeded"' session.json

uv run webglass page open http://127.0.0.1:8000/index.html \
  --session-id ci-recipe --policy-profile policy-profile.json --json \
  | tee open.json
jq -e '.lifecycle_state == "succeeded"' open.json

# The field-lesson check (issue #9 item 2): a page whose JS throws on load
# yields error text and a source location here; a clean page yields two
# explicitly empty lists — "observed, and there was nothing" rather than
# "nobody looked". Console text is untrusted source material either way: it
# renders under content.untrusted, never as a WebGlass warning.
uv run webglass page inspect --session-id ci-recipe --lens console \
  --policy-profile policy-profile.json --json | tee console.json
jq -e '
  .lifecycle_state == "succeeded"
  and .content.untrusted.console_messages == []
  and .content.untrusted.page_errors == []
' console.json

# Token-efficient, agent-readable state: exactly one selector's own content,
# not the whole page.
uv run webglass page extract --selector '#agent-state' --session-id ci-recipe \
  --policy-profile policy-profile.json --json | tee extract.json
jq -e '.content.untrusted.matches | length == 1' extract.json
jq -r '.content.untrusted.matches[0].text' extract.json | jq -e '.status == "ready"'

# Visual evidence, written to a caller-chosen path — the one place WebGlass
# writes bytes to a caller-supplied path, because these are WebGlass's own
# rendered PNG bytes, never remote-origin response bytes.
uv run webglass page screenshot --session-id ci-recipe --out shot.png \
  --policy-profile policy-profile.json --json | tee screenshot.json
jq -e '.lifecycle_state == "succeeded"' screenshot.json
test -s shot.png
```

### 6. Close up

```bash
uv run webglass session close ci-recipe --json | tee close.json
jq -e '.lifecycle_state == "succeeded"' close.json

kill "$(cat server.pid)" 2>/dev/null || true
```

### 7. Upload the screenshot as a CI artifact

```yaml
- name: Upload the screenshot as a CI artifact
  if: always()
  uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a # v7.0.1
  with:
    name: webglass-ci-recipe-screenshot
    path: shot.png
    if-no-files-found: ignore
```

## The complete workflow

The block below is the same recipe as one copy-pasteable GitHub Actions job —
identical in substance to
[`.github/workflows/example-webapp-test.yml`](../.github/workflows/example-webapp-test.yml),
which runs it for real against the two-page fixture app under
[`docs/examples/ci-recipe-app/`](examples/ci-recipe-app/) on every PR that
touches `webglass/**` or `pyproject.toml`.

```yaml
name: Example webapp test

on:
  pull_request:
    branches: [main]
    paths: ["webglass/**", "pyproject.toml"]
  push:
    branches: [main]
    paths: ["webglass/**", "pyproject.toml"]

jobs:
  example-webapp-test:
    runs-on: ubuntu-24.04
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v4
      - run: uv python install 3.12
      - run: uv sync   # or: uv pip install webglass-cli

      - run: uv run playwright install --with-deps chromium
      - name: Allow unprivileged user namespaces (risk r4)
        run: sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0

      - name: Write the policy profile declaring the app under test
        run: |
          cat > policy-profile.json <<'JSON'
          {"name": "ci-example-webapp", "declared_targets": ["127.0.0.1:8000"]}
          JSON

      - name: Start the app under test
        run: |
          python3 -m http.server 8000 --directory ./my-app --bind 127.0.0.1 &
          echo $! > server.pid
          for _ in $(seq 1 20); do
            curl -sf http://127.0.0.1:8000/ > /dev/null && break
            sleep 0.5
          done

      - name: session create
        run: |
          uv run webglass session create --session-id ci-recipe --ttl-seconds 300 --json \
            | tee session.json
          jq -e '.lifecycle_state == "succeeded"' session.json

      - name: page open
        run: |
          uv run webglass page open http://127.0.0.1:8000/index.html \
            --session-id ci-recipe --policy-profile policy-profile.json --json \
            | tee open.json
          jq -e '.lifecycle_state == "succeeded"' open.json

      - name: page inspect --lens console
        run: |
          uv run webglass page inspect --session-id ci-recipe --lens console \
            --policy-profile policy-profile.json --json | tee console.json
          jq -e '
            .lifecycle_state == "succeeded"
            and .content.untrusted.console_messages == []
            and .content.untrusted.page_errors == []
          ' console.json

      - name: page extract --selector
        run: |
          uv run webglass page extract --selector '#agent-state' --session-id ci-recipe \
            --policy-profile policy-profile.json --json | tee extract.json
          jq -e '.content.untrusted.matches | length == 1' extract.json

      - name: page screenshot --out
        run: |
          uv run webglass page screenshot --session-id ci-recipe --out shot.png \
            --policy-profile policy-profile.json --json | tee screenshot.json
          jq -e '.lifecycle_state == "succeeded"' screenshot.json
          test -s shot.png

      - name: session close
        if: always()
        run: uv run webglass session close ci-recipe --json | jq -e '.lifecycle_state == "succeeded"'

      - name: Stop the app under test
        if: always()
        run: kill "$(cat server.pid)" 2>/dev/null || true

      - name: Upload the screenshot as a CI artifact
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: webglass-ci-recipe-screenshot
          path: shot.png
          if-no-files-found: ignore
```

## What this proves against issue #9's acceptance criteria

| # | issue #9 item | Where above |
|---|----------------|--------------|
| 1 | Open a localhost page without a policy refusal | `policy-profile.json` + `page open` |
| 2 | Console + page-error evidence, empty list on a clean page | `page inspect --lens console` |
| 3 | Token-efficient, selector-scoped page state | `page extract --selector` |
| 4 | Keyboard input for platformer-style controls | not exercised here — see `webglass explain action press` and `tests/test_verbs_live.py` |
| 5 | Screenshot to a caller-chosen path | `page screenshot --out` |
| 6 | Session reuse across separate one-shot CLI invocations | one `--session-id` threaded through every step |
| 7 | Agent-first contract (`--json`, stdout/stderr split, exit codes, bounded runtime) | every step's `jq` assertion and exit-code check |

Item 4 (`action press`) is not part of this particular recipe because the
two-page static fixture has nothing that reacts to a keypress; it works the
same way — one more `webglass` call against the same `--session-id`, gated by
the same `--policy-profile`. See `webglass explain action press` and the
live end-to-end presses in `tests/test_verbs_live.py`.
