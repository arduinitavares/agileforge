# Sprint Generation Retryable Errors Design

## Problem

When Sprint generation receives malformed, empty, or schema-invalid model output,
AgileForge records a failed Sprint attempt but the CLI facade reports the failure
as generic `MUTATION_FAILED`. Agents cannot tell whether an attempt was persisted,
whether workflow state changed, or whether retrying `sprint generate` is safe.

## Root Cause

`services/sprint_runtime.py` already classifies runtime failures with
`failure_stage` values such as `invalid_json`, `output_validation`, and
`invocation_exception`. `services/phases/sprint_service.py` persists those
failed attempts and keeps incomplete generations in `SPRINT_SETUP`. The signal is
lost in `services/agent_workbench/sprint_phase.py::_sprint_runtime_error()`,
which maps every recorded Sprint runtime failure to `MUTATION_FAILED`.

## Design

Add a registered retryable error code:

```text
SPRINT_GENERATION_MODEL_RESPONSE_INVALID
```

Use it only for recorded Sprint generation failures whose `failure_stage` is one
of:

- `invalid_json`
- `output_validation`
- `invocation_exception`

Keep other Sprint runtime failures on the existing `MUTATION_FAILED` path.

## CLI Error Contract

For those retryable model-response failures, `agileforge sprint generate` returns
`ok=false` with:

- `errors[0].code = SPRINT_GENERATION_MODEL_RESPONSE_INVALID`
- `errors[0].message` from `failure_summary`, `error`, or a safe fallback
- `errors[0].details.project_id`
- `errors[0].details.sprint_run_success = false`
- `errors[0].details.failure_stage`
- `errors[0].details.failure_artifact_id`, when present
- `errors[0].details.attempt_count`
- `errors[0].details.attempt_id`
- `errors[0].details.attempt_persisted = true` when an attempt id or count exists
- `errors[0].details.fsm_state`
- `errors[0].details.safe_retry_command`

Remediation must tell agents to inspect history/failure artifacts and retry
Sprint generation from the current `workflow next` route. The exact retry command
is:

```bash
agileforge sprint generate --project-id <project_id>
```

Agents may add the same capacity and input flags they used originally.

## API Contract

Do not change HTTP status semantics in this fix. The existing API path returns
`status=success` with a failed-generation data payload for recorded failed
attempts. Add `error_code=SPRINT_GENERATION_MODEL_RESPONSE_INVALID` and
`attempt_persisted=true` to the data payload for the retryable model-response
stages so API consumers can use the same structured contract without a breaking
status-code change.

## Workflow Contract

No FSM mutation change. The failed attempt remains recorded, `fsm_state` remains
`SPRINT_SETUP` unless existing reviewed-draft preservation logic applies, and
`workflow next` continues to advertise the safe retry path when candidates are
available. This fix only makes the failure response explicit and retryable.

## Out Of Scope

- Changing model invocation/provider behavior.
- Retrying automatically.
- Changing API HTTP status codes.
- Changing failure artifact storage.
- UI rendering of this error.

## Tests

- `SprintPhaseRunner.generate()` maps `invalid_json`, `output_validation`, and
  `invocation_exception` failures to
  `SPRINT_GENERATION_MODEL_RESPONSE_INVALID`.
- Details include attempt persistence and safe retry command.
- The API generation payload includes `error_code` and `attempt_persisted` for
  retryable model-response failures.
- Command schema registers the new error for `agileforge sprint generate`.
- Existing non-model Sprint errors stay unchanged.

## Self-Review

- Placeholder scan: no TODO/TBD placeholders.
- Internal consistency: CLI and API share one error code while preserving current
  HTTP behavior.
- Scope check: focused on Sprint generation failure normalization only.
- Ambiguity check: retryable stages and non-goals are explicit.
