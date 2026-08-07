# System boundaries: Colleague, WebGlass, shell-cli, providers

This documents the ownership split WebGlass is built against, per the M0 t3
acceptance criterion (build plan, task t3). It restates decisions already made
in [issue #1](https://github.com/agentculture/webglass-cli/issues/1) and this
repo's `CLAUDE.md` — nothing here is new policy. Read `CLAUDE.md`'s "Target
architecture" section (especially points 1, 3, and 8) and the implementation
spec's "Scope / boundaries" section for the full rationale; this file is the
short, standalone reference.

## The four parties and what each owns

```text
Colleague intent
  -> WebOperation
  -> capability + web policy
  -> search / fetch / browser backend
  -> PageSnapshot + Evidence + Effects
  -> Colleague interpretation and next decision
```

- **Colleague owns** the research question, strategy, role/capability
  authorization, hooks, conclusions, and user communication. Colleague decides
  *why* to look at something and *what it means* once WebGlass reports back.
  Colleague resolves `.colleague` overlay files and passes WebGlass an
  already-effective policy profile — WebGlass never resolves overlays itself.
- **WebGlass owns** operation normalization, URL/network policy, browser
  sessions, navigation mechanics, extraction, token budgets, evidence capture,
  exploration history, and web-specific memory. WebGlass decides *how* a web
  operation executes safely and *what was actually observed* — it does not
  decide whether the observation is true or what to do next.
- **shell-cli owns** local workspace operations — the guarded local operations
  plane for AI agents (`agentculture/shell-cli`). Anything that touches the
  caller's filesystem (importing a quarantined download into a worktree,
  supplying a local file as an upload) is shell-cli's territory, reached only
  through an explicit bridge.
- **External providers own** ranking, page content, identity, and site
  behavior — search engines, the sites themselves, auth providers. WebGlass
  records what a provider returned (with retrieval time, redirect chain,
  content hash); it does not certify that content as true, current, or safe.

## The one-way dependency direction

```text
Colleague -> webglass-cli
Colleague -> shell-cli
```

- WebGlass and shell-cli are **sibling capability providers**, not layered on
  top of each other. Neither becomes the other's god object.
- **WebGlass must never reach into Colleague internals.** No import of a
  Colleague package, no reverse call into Colleague's process, no assumption
  about Colleague's internal state. The M4 Colleague provider (composing
  WebGlass as a library) lands in the `colleague` repo, not here — WebGlass
  stays the callee.
- **shell-cli must never learn WebGlass semantics.** WebGlass does not expose
  its operation model, policy verdicts, or evidence schema to shell-cli as a
  dependency; the only contact point is the explicit artifact bridge (below).
- Concretely for this repo: WebGlass code never imports Colleague and never
  reads `.colleague` files, at any milestone. This is enforced by an
  import-boundary characterization test (build plan task t1), not left as
  aspirational prose.

## The `.colleague`-files-never-read rule

The **policy core** is the specific place this rule bites hardest: it is
evaluated entirely by WebGlass (only WebGlass understands URLs, redirects,
methods, elements, sessions, and downloads well enough to evaluate policy
against them), and it "consumes explicit data" — a policy profile object
passed in by the caller. It must not know `.colleague` files exist, must not
locate them, and must not parse them. Colleague resolves whatever overlay
files, role restrictions, or per-task narrowing it needs and hands WebGlass
the *result*: an effective policy profile. This keeps WebGlass usable by any
caller (a bare CLI invocation, a CI pipeline, a mesh agent with no Colleague
runtime at all) without dragging in Colleague's config format as an implicit
dependency.

A related consequence: absent policy and malformed policy are different
states, and malformed policy must never silently fail open. A caller that
forgets to pass a profile gets an explicit "no policy" state (denied by
default, per the baseline denylists); a caller that passes a profile
WebGlass cannot parse gets a structured error — never a fallback to
"treat it as if nothing was passed" and never a fallback to "allow
everything."

## The artifact bridge (workspace crossings)

Files crossing between the web plane and the local workspace go through an
explicit bridge — **never** arbitrary browser filesystem access in either
direction.

- **Downloads (web -> workspace): quarantined by default, exported
  explicitly.** A download WebGlass observes is retained as a quarantined,
  content-addressed artifact — it is never written into a caller-given path,
  never executed, and never assumed safe. Moving a downloaded artifact into a
  workspace worktree is an explicit shell-cli export step, not a side effect
  of the download operation itself. The one caller-path write WebGlass makes
  directly is a screenshot it rendered itself (`page screenshot --out PATH`)
  — WebGlass-originated bytes, not remote-origin bytes, so it is not a
  quarantine bypass.
- **Uploads (workspace -> web): opaque references only.** WebGlass never
  accepts an arbitrary host filesystem path as upload content. A caller
  supplies an opaque artifact reference (something shell-cli or the caller's
  own tooling produced and vouches for); WebGlass reads the referenced bytes
  through that reference, not through a path it resolves itself against the
  local filesystem.
- **Why the indirection matters**: it is what keeps "the browser can read
  your files" and "the agent can silently exfiltrate a download into your
  repo" off the table by construction, and it is what lets a child Colleague
  session get a narrowed capability set (e.g., no upload capability at all)
  without WebGlass needing to know why.

## What this document does not decide

This file records the ownership split as already specified; it does not
introduce the concrete shell-cli artifact-reference wire format (still an
open park in the implementation spec — needed for M5, not M0-M2) or change
any invariant in `CLAUDE.md` section 11. Where the two disagree, `CLAUDE.md`
and issue #1 win, and this file should be corrected to match.
