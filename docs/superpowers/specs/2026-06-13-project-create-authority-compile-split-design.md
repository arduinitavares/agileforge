# Project Create / Authority Compile Split Design

**Date:** 2026-06-13
**Status:** Approved design
**Spec mode:** proposed_change
**GitHub issue:** #128, "Split project creation from authority compilation for first-run observability"
**Scope:** Project setup CLI/API contracts, setup workflow state, mutation ledger boundaries, authority compile command, dashboard create flow, and first-run command routing

## Revision History

- 2026-06-13: Drafted hard-split setup design after approving the sequence
  `project create -> authority compile -> authority review -> authority accept`.

## Summary

AgileForge project creation must become a fast, observable metadata mutation.
Authority compilation must become an explicit guarded command.

The current `agileforge project create` command creates the project, registers the
specification, runs the LLM-backed authority compiler, writes pending authority,
and initializes setup workflow state in one operation. When compilation takes
minutes or fails late, the command appears hung and redirected JSON remains empty
until process exit.

This design makes a hard break:

- `agileforge project create` validates and persists project/spec metadata only.
- `agileforge authority compile` performs the long-running compiler step.
- `workflow next` routes setup projects through authority compilation before
  authority review.
- The dashboard create endpoint returns immediately with a project id and an
  explicit compile-required state.
- Existing projects already at `authority_pending_review`, accepted authority,
  or later workflow phases continue to work.

No legacy auto-compile path is retained.

## Problem

First-run setup currently hides the most expensive and failure-prone operation
inside project creation.

Observed problems:

- Operators cannot tell whether `project create` is hung, compiling, or blocked.
- Redirected CLI output is not useful until the entire command exits.
- A compiler failure after several minutes makes project creation feel unreliable,
  even when the project/spec rows were successfully created.
- Recovery semantics mix project creation failures with authority compilation
  failures.
- Future workflows such as brownfield setup and scope-extension will need the
  same explicit `spec curation -> authority compile -> review` boundary.

## Goals

- Make `project create` return quickly after project and spec registry persistence.
- Add an explicit authority compilation mutation with its own idempotency and
  stale guards.
- Preserve guarded setup state so agents can follow `workflow next` without
  guessing.
- Keep authority human review/acceptance mandatory after compilation.
- Keep mutation ledger recovery semantics for interrupted writes.
- Make API and dashboard behavior match CLI behavior.
- Produce useful JSON even when authority compilation fails.
- Prepare a clean foundation for issue #130, brownfield setup, and future
  product-goal/scope-delta workflows.

## Non-Goals

- Do not implement authority compiler source-map repair or model overrides.
  Those belong to #130.
- Do not implement brownfield product-spec curation. That belongs to #129.
- Do not implement the new Product Goal / scope-extension workflow in this
  branch.
- Do not auto-run Vision after project creation.
- Do not keep a hidden `project create` auto-compile compatibility mode.
- Do not auto-accept authority.
- Do not change authority review/accept guard semantics except to ensure the new
  setup status is represented clearly.

## Public Workflow

The new first-run flow is:

```text
agileforge spec profile validate
agileforge project create
agileforge authority compile
agileforge authority review
agileforge authority accept
agileforge workflow next
```

`project create` produces a project shell plus registered spec version.
`authority compile` produces a pending compiled authority for review.
Only after authority acceptance can the project advance into Vision and later
phases.

## Setup States

AgileForge setup state keeps `fsm_state=SETUP_REQUIRED` until authority is
accepted. `setup_status` becomes the user-facing substate.

### `authority_compile_required`

Meaning:

- Project exists.
- Spec version is registered.
- No pending authority exists for the selected spec version.
- Next action is `agileforge authority compile`.

Required workflow fields:

- `fsm_state="SETUP_REQUIRED"`
- `setup_status="authority_compile_required"`
- `setup_error=null`
- `setup_spec_file_path=<resolved path>`
- `setup_spec_version_id=<spec_version_id>`
- `setup_spec_hash=<sha256>`
- `setup_next_actions=[authority compile action]`

### `authority_pending_review`

Meaning:

- Pending compiled authority exists.
- Human/agent review is required before acceptance.
- Next actions are authority status/review/accept/reject.

This state already exists conceptually and remains the success state of
authority compilation.

### `authority_compiling`

Meaning:

- An `agileforge authority compile` mutation has acquired its ledger row and is
  invoking the compiler.
- The operation may be long-running.
- Operators can inspect mutation state while the compile command is still
  running.

Required workflow fields:

- `fsm_state="SETUP_REQUIRED"`
- `setup_status="authority_compiling"`
- `setup_error=null`
- `setup_spec_file_path=<resolved path>`
- `setup_spec_version_id=<spec_version_id>`
- `setup_spec_hash=<sha256>`
- `setup_compile_mutation_event_id=<mutation_event_id>`
- `setup_compile_started_at=<timestamp>`
- `setup_next_actions=[mutation show/list action]`

The compile command should write this state before invoking the LLM-backed
compiler. On success it transitions to `authority_pending_review`; on handled
compiler failure it transitions to `authority_compile_failed`. If the process is
interrupted, the mutation ledger remains the recovery source of truth.

### `authority_compile_failed`

Meaning:

- Project and spec version remain valid.
- Authority compilation failed.
- Operator should inspect failure fields and retry `authority compile` after
  correcting model/config/spec issues.

Required workflow fields:

- `fsm_state="SETUP_REQUIRED"`
- `setup_status="authority_compile_failed"`
- `setup_error=<machine error code>`
- `setup_failure_stage="authority_compile"`
- `setup_failure_summary=<bounded summary>`
- `setup_failure_artifact_id=<artifact id or null>`
- `setup_raw_output_preview=<bounded preview or null>`
- `setup_has_full_artifact=<bool>`
- `setup_next_actions=[authority compile action]`

`setup_status="failed"` remains available for non-authority setup failures that
do not have a more specific setup status.

## CLI Contract

### `agileforge project create`

Required command:

```bash
agileforge project create \
  --name <project_name> \
  --spec-file <specs/spec.json> \
  --idempotency-key <key>
```

Dry run remains:

```bash
agileforge project create \
  --name <project_name> \
  --spec-file <specs/spec.json> \
  --dry-run \
  --dry-run-id <preview_id>
```

Successful response data must include:

- `project_id`
- `name`
- `resolved_spec_path`
- `spec_hash`
- `spec_version_id`
- `setup_status="authority_compile_required"`
- `fsm_state="SETUP_REQUIRED"`
- `mutation_event_id`
- `next_actions`

The primary next action must be:

```bash
agileforge authority compile \
  --project-id <project_id> \
  --spec-version-id <spec_version_id> \
  --expected-spec-hash <spec_hash> \
  --expected-state SETUP_REQUIRED \
  --expected-setup-status authority_compile_required \
  --idempotency-key <idempotency_key>
```

`project create` must not create a `CompiledSpecAuthority` row.
`project create` must not return `pending_authority_id`.

### `agileforge authority compile`

New command:

```bash
agileforge authority compile \
  --project-id <project_id> \
  --spec-version-id <spec_version_id> \
  --expected-spec-hash <spec_hash> \
  --expected-state SETUP_REQUIRED \
  --expected-setup-status <authority_compile_required|authority_compile_failed> \
  --idempotency-key <key>
```

Required guards:

- `project_id`
- `spec_version_id`
- `expected_spec_hash`
- `expected_state`
- `expected_setup_status`
- `idempotency_key`

The command must support dry-run preview using the existing mutation contract
style:

```bash
agileforge authority compile \
  --project-id <project_id> \
  --spec-version-id <spec_version_id> \
  --expected-spec-hash <spec_hash> \
  --expected-state SETUP_REQUIRED \
  --expected-setup-status authority_compile_required \
  --dry-run \
  --dry-run-id <preview_id>
```

Dry-run must validate guards and return the compile command that would be run,
without writing authority rows or invoking the compiler.

While running, the compile mutation must be visible through mutation inspection:

```bash
agileforge mutation list --project-id <project_id> --status pending
agileforge mutation show --mutation-event-id <mutation_event_id>
```

Successful response data must include:

- `project_id`
- `spec_version_id`
- `spec_hash`
- `pending_authority_id`
- `compiled_authority_id`
- `setup_status="authority_pending_review"`
- `fsm_state="SETUP_REQUIRED"`
- `mutation_event_id`
- `next_actions`

The primary next action must be:

```bash
agileforge authority review --project-id <project_id>
```

Failed compile response data must include:

- `project_id`
- `spec_version_id`
- `spec_hash`
- `setup_status="authority_compile_failed"`
- `setup_error`
- `setup_failure_stage="authority_compile"`
- `setup_failure_summary`
- `setup_failure_artifact_id`
- `raw_output_preview`
- `has_full_artifact`
- `mutation_event_id`
- `next_actions`

The failed response next action must be another guarded `authority compile`
command with `--expected-setup-status authority_compile_failed`.

### `agileforge project setup retry`

`project setup retry` remains only for recovering interrupted setup mutations
that cannot be safely completed by re-running `project create` or
`authority compile`.

New authority compilation failures should route to `authority compile`, not
`project setup retry`.

## API Contract

### `POST /api/projects`

The dashboard/API project create endpoint must call the fast project-create path.

Successful response data must include:

- `id`
- `name`
- `setup_status="authority_compile_required"`
- `fsm_state="SETUP_REQUIRED"`
- `spec_version_id`
- `spec_hash`
- `next_actions`

It must not block on authority compilation.
It must not auto-run Vision.

### Authority Compile Endpoint

Add an API endpoint equivalent to the CLI authority compile command:

```text
POST /api/projects/{project_id}/authority/compile
```

Request body fields:

- `spec_version_id`
- `expected_spec_hash`
- `expected_state`
- `expected_setup_status`
- `idempotency_key`

The API request model must forbid extra fields. Responses must mirror the CLI
success/failure payloads.

## Workflow Routing

`agileforge workflow next --project-id <id>` must route setup projects by
`setup_status`.

Routing table:

| FSM state | setup_status | Next status | Runnable commands |
| --- | --- | --- | --- |
| `SETUP_REQUIRED` | `authority_compile_required` | `authority_compile_required` | `authority compile`, `authority status` |
| `SETUP_REQUIRED` | `authority_compiling` | `authority_compiling` | `mutation show`, `mutation list`, `authority status` |
| `SETUP_REQUIRED` | `authority_compile_failed` | `authority_compile_failed` | `authority compile`, `authority status` |
| `SETUP_REQUIRED` | `authority_pending_review` | `authority_pending_review` | `authority status`, `authority review`, guarded `authority accept`, guarded `authority reject` |
| `SETUP_REQUIRED` | `failed` | `setup_failed` | existing setup recovery commands |

Every command advertised as runnable by `workflow next` must be executable
against the same workflow snapshot or returned as blocked with concrete reason
and remediation.

## Internal Design

The existing `ProjectSetupMutationRunner` should be split into two mutation
responsibilities:

1. Project/spec registration.
2. Authority compilation for an already-registered spec version.

The implementation may keep one class if that minimizes churn, but the methods
must be separate and testable:

- `create_project(ProjectCreateRequest)`
- `compile_authority(AuthorityCompileRequest)`

Project creation should:

- validate the spec file exists and can be normalized into the spec registry;
- create the `Product` row;
- create or reuse the `SpecRegistry` row for the spec hash;
- initialize workflow setup state with `authority_compile_required`;
- finalize the project-create ledger event without invoking the LLM compiler.

Authority compilation should:

- validate stale guards against current workflow state and spec registry state;
- create/load an idempotent mutation ledger row for `agileforge authority compile`;
- update workflow setup state to `authority_compiling` with the compile
  mutation event id before invoking the compiler;
- invoke `compile_pending_authority_for_project`;
- persist pending compiled authority on success;
- update workflow setup state to `authority_pending_review`;
- record bounded compile failure metadata on failure;
- support idempotent replay of success and failure responses.

## Mutation Ledger

`agileforge project create` and `agileforge authority compile` must use distinct
mutation ledger command names:

- `agileforge project create`
- `agileforge authority compile`

Project-create completed steps should no longer include
`pending_authority_compiled`.

Authority-compile completed steps may include:

- `workflow_session_compile_started`
- `pending_authority_compiled`
- `workflow_session_status_written`

Interrupted product/spec registration remains a project-create recovery concern.
Interrupted authority compilation is an authority-compile recovery/retry concern.

## Dashboard Behavior

After creating a project, the dashboard should show the project immediately with
state `authority_compile_required`.

The UI should present an explicit authority compile action before authority
review. It must not show Vision, Backlog, Roadmap, Story, or Sprint controls
until authority is accepted.

If authority compilation fails, the dashboard should show:

- failure stage;
- failure summary;
- failure artifact id when available;
- raw output preview when available;
- retry compile action.

## Compatibility And Migration

Existing projects are not migrated backward.

Projects with accepted authority or later workflow phases continue unchanged.
Projects already in `authority_pending_review` continue through authority review
and acceptance.

Old scripts that expect `project create` to return a pending authority must be
updated. This is an intentional hard break because the old behavior hides a
long-running LLM operation behind a metadata creation command.

## Test Requirements

Focused tests must cover:

- `project create` creates project/spec rows and no compiled authority rows.
- `project create` returns `setup_status=authority_compile_required`.
- `project create` returns a guarded `authority compile` next action.
- `authority compile` succeeds from `authority_compile_required` and creates
  pending authority.
- `authority compile` writes `authority_compiling` with mutation event id before
  invoking the compiler.
- `authority compile` transitions setup to `authority_pending_review`.
- `authority compile` failure records `authority_compile_failed` and bounded
  failure metadata.
- `authority compile` retries from `authority_compile_failed` with fresh
  idempotency key.
- Stale `expected_state`, `expected_setup_status`, `expected_spec_hash`, and
  `spec_version_id` are rejected.
- `workflow next` advertises runnable compile/review commands for each setup
  status.
- CLI routes `authority compile` args to the application facade.
- API project creation returns quickly without authority compilation.
- API authority compile mirrors CLI payload semantics.
- Command registry/schema exposes the new command contract.
- Existing authority review/accept tests continue to pass for
  `authority_pending_review`.

Full verification for implementation must include focused setup/authority/API
tests and `pyrepo-check --all`.

## Acceptance Criteria

- `agileforge project create` returns without invoking authority compilation.
- Project creation output is useful when redirected to a file.
- The next command after create is a guarded `agileforge authority compile`.
- `agileforge authority compile` is the only normal path that invokes the
  authority compiler during setup.
- Compiler success routes to authority review.
- Compiler failure routes to retryable authority compile with actionable failure
  metadata.
- While compile is running, mutation inspection can expose current phase and
  mutation event id.
- `workflow next` never advertises `authority review` before a pending authority
  exists.
- Dashboard/API project creation is no longer a hidden long-running compile
  operation.
- No legacy auto-compile mode remains.

## Relationship To Future Work

This split is a prerequisite for cleaner follow-up work:

- #130 can harden the explicit authority compile command without touching
  project creation.
- #129 can add brownfield spec curation before authority compilation.
- A future scope-extension/Product Goal delta feature can reuse the same
  `spec curation -> authority compile -> authority review` boundary.
