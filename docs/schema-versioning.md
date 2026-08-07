# Schema versioning policy

This is the M0 decision required before any M1 model code
(`WebOperation` / `WebOperationResult` / `Evidence`, per
[issue #1](https://github.com/agentculture/webglass-cli/issues/1) sections 1
and 11, and the build plan's t3 acceptance criterion). It is normative for
every record kind WebGlass persists or returns across a process boundary
(library return value, CLI `--json` payload, evidence store record). Nothing
here is implemented yet — this document is the contract the M1 models must
satisfy.

## The rule

Each independently-evolving record kind carries its own version:

- **One field, one integer.** Every `WebOperation`, `WebOperationResult`, and
  `Evidence` record carries a top-level `schema_version: int` field. There is
  no combined "API version" — operations, results, and evidence version
  independently because they change for different reasons and at different
  rates.
- **Start at `1`.** The first shipped shape of each record kind is
  `schema_version = 1`. Pre-M1 code (the introspection CLI's `whoami` /
  `learn` / `explain` JSON shapes) is out of scope for this policy — those
  contracts are characterized by tests, not by a schema version field.
- **Bump only on a breaking shape change.** Additive, backward-compatible
  changes (a new optional field with a safe default) do not bump the version.
  Anything a naive reader could misinterpret if it kept its old assumptions —
  a field renamed, removed, retyped, or a meaning changed under the same
  name — bumps the integer by exactly one.
- **Every bump ships a migration note.** The commit or PR that bumps a
  `schema_version` documents, in prose next to the version constant (or in
  this file's [History](#history) section once records exist), what changed
  and why a reader needs to know. "Bumped because X" is the minimum; a
  before/after shape diff is preferred when the change is non-trivial.
- **Readers reject what they don't know — fail closed.** A reader (library
  consumer, CLI renderer, evidence-store loader) that encounters a
  `schema_version` higher than the highest version it was built to understand
  must refuse to interpret the record as if it were a known shape. This
  surfaces as a structured error (per the CLI's `CliError` contract), never a
  best-effort partial parse and never a silent fallback to an older
  interpretation. A `schema_version` the reader recognizes as *older* than
  current may be read directly (no version-1-forever data is silently
  reinterpreted as version-2) or migrated forward by an explicit, tested
  migration path — never guessed at.
- **The version is data, not metadata-about-metadata.** `schema_version`
  lives in the record itself (the JSON payload, the persisted row), not in a
  side channel like a file extension or a database table name — so a record
  handed off in isolation (a citation, an exported evidence blob) still
  carries the information needed to interpret it correctly.

## Why per-record-kind, not one global version

Collapsing `WebOperation`, `WebOperationResult`, and `Evidence` under one
global version number would force every consumer to track breaking changes
to record kinds it never touches (a `WebPolicyEvaluator` adapter cares about
`WebOperation` shape, not `Evidence` shape) and would force a version bump on
one kind's schema whenever an unrelated kind changed. Evidence records in
particular are meant to be inspectable independently of the operation that
produced them (CLAUDE.md's evidence-is-inspectable-independently-of-Colleague
invariant) — an evidence record's own `schema_version` is what makes that
independence meaningful instead of aspirational.

## Scope

This policy governs the schema-versioned record kinds only:
`WebOperation`, `WebOperationResult`, `Evidence`. It does not extend to:

- The introspection CLI's existing JSON shapes (`whoami`, `learn`,
  `overview`, `doctor`) — those are characterized by
  `tests/test_cli.py` / `tests/test_cli_introspection.py`, and changing them
  is governed by the M0 characterization-test invariant (t1), not this file.
- `WebContext`, browser session records, or exploration-graph edges — these
  are runtime/process-scoped objects, not durable cross-boundary records, and
  do not need independent schema versions unless a future milestone persists
  them in a shape a stale reader could misread.

## History

No `schema_version` bumps have happened yet — `WebOperation`,
`WebOperationResult`, and `Evidence` do not exist as implemented models. When
M1 (t4) lands the first shape of each, record it here as:

```text
## History

### WebOperationResult

- v1 (M1, <PR link>) — initial shape.
```

Future breaking changes append additional entries under the relevant record
kind's subsection; entries are never edited or removed once merged.
