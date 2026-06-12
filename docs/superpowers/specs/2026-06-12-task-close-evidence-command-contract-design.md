# Task Close Evidence Command Contract Design

## Summary

Issue: https://github.com/arduinitavares/agileforge/issues/137

AgileForge currently advertises Sprint task update commands that are not
directly runnable for `Done` transitions. `workflow next` and
`sprint task next/show` expose guarded task update commands, but omit the
close-evidence flags required by the task completion contract:

- `--outcome-summary`
- `--validation-summary`
- `--checklist-result`
- `--artifact-ref` when artifact targets exist

The CLI parser already accepts these flags, and task tickets already expose
`work_contract.done_requires`. The bug is a command-contract mismatch between
read projections and mutation validation.

## Goals

- Make every advertised Sprint task update command runnable from the same
  workflow snapshot when the operator supplies placeholder values.
- Keep task completion evidence explicit; do not relax the `Done` contract.
- Align `work_contract.done_requires`, task update validation, command metadata,
  `workflow next`, and `sprint task next/show`.
- Preserve existing guarded task update semantics:
  `task_id`, `expected_status`, `expected_task_fingerprint`, and
  `idempotency_key` remain required.

## Non-Goals

- Do not change task execution storage tables.
- Do not add automatic task completion.
- Do not infer artifact refs from Git state.
- Do not redesign the Sprint dashboard task board.
- Do not implement issue #136 stale Story scope repair on this branch.

## Root Cause

The task ticket builder in `services/agent_workbench/sprint_phase.py` includes
`work_contract.done_requires`, but builds `next_actions.update` without the
required evidence placeholders.

The Sprint workflow command builder in
`services/agent_workbench/application.py` also exposes a generic
`agileforge sprint task update` command without evidence placeholders.

Validation happens later in `services/agent_workbench/sprint_phase.py`:

- `_task_execution_write_request()` rejects `Done` without
  `validation_summary`.
- `_assert_task_update_guards()` rejects `Done` with artifact targets but no
  artifact refs.

The command registry lists evidence fields as optional because they are not
needed for all status transitions, but a `Done` transition requires them. The
read projections must therefore advertise evidence placeholders for the `Done`
path instead of presenting a minimal update command as if it is sufficient.

## Design

### Shared Command Builder

Add a small helper in `services/agent_workbench/sprint_phase.py` responsible for
rendering Sprint task update commands.

Inputs:

- `project_id`
- `task_id` or placeholder
- `status` or placeholder
- `expected_status` or placeholder
- `expected_task_fingerprint` or placeholder
- `idempotency_key` placeholder
- `include_done_evidence`
- `artifact_targets`

Behavior:

- Always include the guard arguments.
- When `include_done_evidence=true`, append:
  - `--outcome-summary <outcome_summary>`
  - `--validation-summary <validation_summary>`
  - `--checklist-result fully_met`
  - `--artifact-ref <artifact_ref>` when artifact targets are unknown or
    present.
- When artifact targets are known and empty, omit `--artifact-ref`.

The builder should be pure string rendering. It should not query the database
or mutate state.

### Task Ticket Commands

Use the shared builder for `sprint task next/show` task tickets.

For current task tickets:

- Use actual `task_id`.
- Use actual `expected_status`.
- Use actual `expected_task_fingerprint`.
- Use actual artifact target list.
- Use `--status Done` in the runnable close command because this action is the
  primary completion path agents need.

The ticket may keep generic update affordances later, but v1 should prioritize
the runnable close command because the bug is specifically about closing tasks.

### Workflow Next Commands

Use the same builder for the generic `SPRINT_VIEW` `workflow next` task update
command.

Because `workflow next` does not know the current task id or artifact targets,
it should use placeholders:

- `--task-id <task_id>`
- `--expected-status <expected_status>`
- `--expected-task-fingerprint <task_fingerprint>`
- `--idempotency-key <idempotency_key>`
- `--status Done`
- `--outcome-summary <outcome_summary>`
- `--validation-summary <validation_summary>`
- `--checklist-result fully_met`
- `--artifact-ref <artifact_ref>`

This makes the advertised command shape honest for the most common terminal
task transition. Agents can still inspect `sprint task next` or
`sprint task show` to replace placeholders with concrete values.

### Validation Parity

Tighten the `Done` mutation validation to match the published
`done_requires` contract:

- Missing or blank `validation_summary` blocks `Done`.
- Missing or blank `outcome_summary` blocks `Done`.
- Missing `checklist_result` blocks `Done`.
- `checklist_result=not_checked` blocks `Done`.
- Artifact refs remain required when artifact targets exist.

`checklist_result=partially_met` should remain accepted for now because the
existing enum permits it and issue #137 is about command executability, not
policy for partial acceptance. Sprint/story close readiness can still decide
whether partial task evidence is acceptable at higher levels.

### Error Details

Keep the existing top-level error envelope compatible if the project-wide
`ErrorCode` enum does not include task-specific evidence codes.

When evidence is missing, include structured details:

- `reason_code=TASK_CLOSE_EVIDENCE_REQUIRED`
- `task_id`
- `missing_fields`
- `done_requires`

When artifact refs are missing, preserve the existing
`SPRINT_TASK_ARTIFACT_REFS_REQUIRED` reason and include `artifact_targets`.

## Tests

Add or update focused tests:

- `tests/test_agent_workbench_application.py`
  - `workflow next` in `SPRINT_VIEW` advertises a task update command with all
    required Done evidence placeholders.

- `tests/test_agent_workbench_sprint_phase.py`
  - `sprint task next` ticket `next_actions.update` includes actual guard
    values and Done evidence placeholders.
  - If artifact targets are present, command includes `--artifact-ref`.
  - If artifact targets are absent, command omits `--artifact-ref`.
  - `Done` without `outcome_summary` fails with
    `TASK_CLOSE_EVIDENCE_REQUIRED`.
  - `Done` without `checklist_result` fails with
    `TASK_CLOSE_EVIDENCE_REQUIRED`.
  - `Done` with `checklist_result=not_checked` fails with
    `TASK_CLOSE_EVIDENCE_REQUIRED`.
  - `Done` with all evidence succeeds.

- `tests/test_agent_workbench_command_schema.py`
  - Command metadata still lists evidence fields as optional globally because
    they are conditional on `status=Done`.

## Acceptance Criteria

- `workflow next` never advertises a `Done` task update shape that omits
  required close-evidence placeholders.
- `sprint task next/show` tickets expose a runnable task close command for the
  current ticket.
- A user or agent following the advertised command shape can complete a task by
  replacing placeholders with concrete values.
- Missing task close evidence fails with structured details instead of forcing
  the operator to discover missing flags one at a time.
- Existing task update idempotency and stale-fingerprint guards still pass.

## Self-Review

- Placeholder scan: no unresolved placeholder markers.
- Internal consistency: command rendering, validation, and tests all target
  task close evidence for `status=Done`.
- Scope check: one workflow-contract bug only; stale Story scope repair and UI
  active Sprint view remain separate issues.
- Ambiguity check: `partially_met` remains accepted for task update in this
  fix; only missing or `not_checked` checklist results block `Done`.
