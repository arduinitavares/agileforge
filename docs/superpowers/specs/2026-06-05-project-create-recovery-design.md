# Project Create Recovery Design

## Purpose

`agileforge project create` can complete durable product/spec setup and then lose
the mutation lease while authority compilation is still running. The observed
event-159 scenario is the concrete regression target:

- Create started at `2026-06-05T11:13:04Z`.
- The mutation completed `product_created`, `product_spec_linked`,
  `spec_registry_written`, and `spec_marked_approved`.
- The lease expired at `2026-06-05T11:18:04Z`.
- The command returned at `2026-06-05T11:29:29Z`.
- No `CompiledSpecAuthority` was persisted.
- The CLI response reported `recovery_required`, but the stored ledger row still
  had status `pending`.

The immediate goal is to make authority compilation lease-safe and make recovery
state truthful. The larger structured-authority resume architecture is captured
as a follow-up, not this implementation cycle.

## Scope

Implement Layer 1 now:

- Keep `project create` alive during long authority compilation.
- Enforce a hard compiler deadline.
- Return truthful structured failure or recovery state.
- Repair expired `pending` create rows at setup retry entry.
- Make `project setup retry --dry-run` validate the same recovery rules as real
  retry.
- Align `mutation resume` with linked `project setup retry` so resume does not
  trap setup recovery in an unusable pending state.

Document Layer 2 as follow-up:

- Investigate natural authority compile units.
- Design resumable focused authority units.
- Persist and cache completed unit outputs.
- Resume missing units only.
- Merge validated units into final `CompiledSpecAuthority`.

Out of scope for this cycle:

- `agileforge project delete` or reset CLI.
- Auto-Vision behavior after setup.
- Changes to the `agileforge.spec.v1` schema.
- Live mutating smoke tests against the user's real AgileForge database.

## Current Authority Compilation Flow

For new project bootstrap, `project create` takes `specs/spec.json` and stores a
canonical `agileforge.spec.v1` JSON artifact in `SpecRegistry.content`. Authority
compilation then reads that structured content and sends it to
`spec_authority_compiler_agent` through `SpecAuthorityCompilerInput`.

The compiler input is not Markdown and is not the target repository source. It is
canonical `TechnicalSpecArtifact` JSON.

The current structured compiler flow has two levels:

1. A full-spec pass over the complete `TechnicalSpecArtifact`.
2. Focused structured-item passes for accepted high-priority items.

In the observed ASA spec, the artifact had 31 spec items and the current compiler
selected 16 accepted high-priority items for focused compilation. Each focused
item can currently retry up to 2 times. This explains why authority compilation
can legitimately run longer than the default 300 second mutation lease.

## Layer 1 Architecture

Layer 1 keeps the current final authority model. It does not introduce
per-item durable authority storage.

### Lease-Safe Compiler Invocation

`services/specs/compiler_service.py` should wrap the blocking authority compiler
invocation with a bounded runner that:

- Calls the existing `lease_guard` before invocation starts.
- Refreshes the lease periodically while the compiler is running.
- Enforces a maximum duration.
- Returns a structured compiler failure on timeout.
- Returns a mutation lease-loss envelope when the lease cannot be refreshed.

Heartbeat interval, lease duration, and max compile deadline must be specified
together. A healthy long compile may exceed the original 300 second lease only
because the heartbeat extends it. A stuck compile must not run forever.

### Truthful Create Recovery

`services/agent_workbench/project_setup.py` must not report
`recovery_required` unless the stored ledger state was actually moved to
`recovery_required`, or the response accurately reports a repairable stored
state.

The current failure class to avoid:

- `_mark_create_recovery_required(...)` calls `mark_recovery_required(...)`.
- The ledger update can fail if the lease already expired.
- The code ignores the boolean result.
- `_recovery_required_response(...)` then reports `recovery_required` even
  though the DB row remains `pending`.

Layer 1 must make CLI response state and DB ledger state agree.

### Setup Retry Repair

`project setup retry` with `--recovery-mutation-event-id` should validate and,
when safe, repair the target create row before deciding whether retry can run.

Rules:

- Expired `pending` row for the same `agileforge project create` command and
  same project can be repaired to `recovery_required`.
- Active non-expired `pending` row returns `MUTATION_IN_PROGRESS`.
- Wrong project, wrong command, missing event, or incompatible status returns
  `MUTATION_RECOVERY_INVALID`.
- Crash, SIGKILL, or OOM cannot run in-process handlers; retry-entry repair is
  the intended fallback for those cases.

### Dry-Run Parity

`project setup retry --dry-run` must validate the same recovery rules as real
retry. It may perform only the narrow ledger repair required to truthfully preview
the real retry path:

- Allowed dry-run mutation: expired `pending` create row to
  `recovery_required`.
- Forbidden dry-run mutations: product, spec, compiled authority, authority
  acceptance, and workflow setup writes.

After dry-run repair, the response must expose the post-repair status,
`recovery_mutation_event_id`, and the next real retry command.

### Mutation Resume Alignment

`mutation resume` must not leave a project-create recovery row in a pending
state that blocks linked `project setup retry`.

Layer 1 should either:

- Keep project-create recovery rows in `recovery_required` and direct the user to
  `project setup retry`, or
- Define an explicit setup-retry handoff that `project setup retry` accepts
  without treating it as a conflicting active pending mutation.

Silent lease stealing is not allowed. Active non-expired leases still require a
clear handoff rule or must return `MUTATION_IN_PROGRESS`.

## Error Handling Matrix

### Healthy Long Compile

Condition:

- Compiler runs longer than the original lease duration.
- Heartbeat keeps lease ownership valid.
- Compiler returns success before the hard deadline.

Outcome:

- Compiled authority is persisted.
- Product authority cache is persisted.
- Ledger records `pending_authority_compiled`.
- Create continues to workflow setup.

### Compiler Semantic Failure

Condition:

- Compiler returns a schema-valid failure or normalized output validation
  failure while the lease remains valid.

Outcome:

- Existing `SPEC_COMPILE_FAILED` path is used.
- Failure artifact metadata is preserved.
- Workflow setup status is marked failed.
- This is not `recovery_required`.

### Compiler Timeout

Condition:

- Compiler exceeds the configured hard deadline while heartbeat still owns the
  lease.

Outcome:

- Structured failed setup, not `recovery_required`.
- Error uses `SPEC_COMPILE_FAILED` with a timeout-specific failure stage such as
  `invocation_timeout`.
- No compiled authority is persisted.
- Retry can attempt setup again through the failed-setup path.

### Lease Lost Or Unknown Completion

Condition:

- Lease cannot be refreshed.
- Compiler returns after the lease expired.
- Process died after durable setup writes and before authority persistence.

Outcome:

- Stored create row becomes `recovery_required` when possible.
- If the stored row cannot be moved immediately, response must reflect the
  stored state and remediation accurately.
- Setup retry can repair expired pending rows at entry.

## Layer 2 Follow-Up Contract

Layer 2 is a separate investigation and design. Its end state is resumable
structured authority compilation at finer granularity than the coarse mutation
steps used today.

The natural source unit is likely `SpecItem`, because `SpecItem` is structured
compiler input and invariants are compiler output. The durable unit should not be
a naked item. It should be a focused authority unit containing:

- One primary `SpecItem`.
- Top-level title, summary, and problem statement.
- Controlled terms.
- External references.
- Enough relation and neighboring-item context to preserve meaning.

Layer 2 must investigate the current compile pipeline before selecting a schema:

- What are the natural compile units in current code?
- Does the focused pass need direct relation closure, neighboring item summaries,
  or full linked items?
- What output should each unit own: invariants, gaps, source maps, or all of
  them?
- How should duplicate or contradictory unit outputs be detected?
- Should the full-spec pass remain as an authority generator, become an audit
  pass, or become a merge-validation pass?

Likely cache key components:

- `spec_hash` or `spec_version_id`.
- `source_item_id`.
- Focused input hash.
- Compiler version.
- Prompt hash.

Cached unit outputs must be schema-revalidated and hash-checked before reuse.
The final persisted product of Layer 2 remains one merged
`CompiledSpecAuthority`.

The target behavior is: if 16 focused authority units exist and 2 complete before
a crash, retry resumes from the first missing or invalid unit rather than
restarting all authority work.

## Test Contracts

Layer 1 implementation must be test-driven. No production code should be written
before the relevant failing test is observed.

Required tests:

- Compiler heartbeat: a blocking compile longer than the heartbeat interval
  refreshes the lease and succeeds.
- Compiler timeout: a compile beyond the deadline returns structured
  `SPEC_COMPILE_FAILED`, persists no authority, and records failed setup rather
  than recovery-required.
- Lease loss after compile: compiler returns after lease expiry, persistence is
  blocked, and CLI response matches stored ledger state.
- Expired pending retry: seed a create row at `spec_marked_approved`, expire the
  lease, then verify setup retry repairs and resumes.
- Retry dry-run parity: dry-run on the same stale row performs only narrow ledger
  repair, writes no authority or workflow state, and returns a truthful preview.
- Resume/retry alignment: `mutation resume` on create recovery does not leave
  linked setup retry in an unusable pending state.
- Event-159 regression: a long structured compile path can exceed 300 seconds
  without losing the lease, using fake sleeps or fake invocations rather than a
  live model call.

Optional regression tests if cheap during implementation:

- Active pending retry: non-expired pending recovery event returns
  `MUTATION_IN_PROGRESS` and is not repaired.
- Recovery mark failure: if moving to `recovery_required` fails, response does
  not claim it succeeded.
- Idempotency replay: replaying the same create idempotency key after lease
  expiry returns a structured recovery envelope and leaves ledger state
  consistent.

Verification order:

1. Focused compiler and ledger tests.
2. Focused project setup retry tests.
3. CLI routing and dry-run tests.
4. Broader recovery/compiler pytest subset.
5. No live mutating `project create` against the user's real DB.

## Implementation Notes

- Reuse existing mutation ledger concepts where possible.
- Do not duplicate `_mutation_lease_lost_result`; extend it carefully if a
  boundary field is needed.
- Keep Layer 1 changes narrow and compatible with existing `CompiledSpecAuthority`
  persistence.
- Do not silently broaden dry-run semantics beyond the explicit ledger repair.
- Keep all response envelopes bounded and structured for CLI consumers.
