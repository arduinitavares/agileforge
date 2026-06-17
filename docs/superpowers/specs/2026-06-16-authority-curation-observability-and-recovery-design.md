# Authority Curation Observability And Recovery Design

**Date:** 2026-06-16
**Status:** Draft
**Spec mode:** proposed_change
**Owner:** AgileForge maintainers
**Scope:** `authority curate` traceability, ADK activity logging integration, failure diagnostics, and stale mutation recovery guidance

## Revision History

- 2026-06-16: Drafted after a real ASA `authority curate` run returned
  `MUTATION_RECOVERY_REQUIRED` with `STALE_PENDING`, no curation attempt id, no
  model information, no diff, and no durable step timeline.
- 2026-06-16: Amended after design review to pin the recovery command,
  attempt-to-mutation linkage, trace step names, trace error schema, and trace
  inspection guard behavior.

## Summary

`authority curate` is now a real long-running ADK-backed mutation. The command
can take long enough for agents to timeout, lose patience, or retry with stale
guards. Current metadata is not enough to explain what happened after the fact.

Add a durable local trace for every authority curation mutation. The trace is
append-only while the command runs and is linked from CLI/API responses,
`mutation show`, `authority status`, and failure envelopes.

Use three layers:

1. ADK activity logging for ADK internals, model calls, and tool calls.
2. AgileForge curation trace artifacts as the durable audit/debug source.
3. Python stdlib logging for compact operational events.

OpenTelemetry is a good export target, but it must not be the v1 source of
truth. v1 stays local-first, deterministic, redacted by default, and testable
without a collector.

## Problem

The ASA targeted curation run showed this failure shape:

```text
ok=false
first error code=MUTATION_RECOVERY_REQUIRED
mutation_event_id=647
command=agileforge authority curate
status=recovery_required
current_step=start
completed_steps=0
recovery_action=reconcile_then_resume
recovery_safe_to_auto_resume=false
last_error=STALE_PENDING
last_error message=Pending mutation lease expired.
```

The operator could not answer basic questions:

- Did the ADK workflow start?
- Which model was selected?
- Was the feedback file loaded?
- Did the command reach `authority_curating`?
- Did any candidate authority get generated?
- Did any candidate authority get published?
- Is resume safe, or should the mutation be reconciled as no-side-effect
  failure?
- What exact next command should the user run?

Existing state helps, but it is too coarse:

- `AuthorityCurationAttempt` stores final attempt status, candidate ids, quality
  report JSON, and `failure_artifact_id`.
- `write_failure_artifact(...)` stores bounded failure artifacts after known
  failures.
- `MutationLedgerRepository` stores coarse mutation status and completed steps.
- `utils.logging_config` configures stdlib rotating logs.
- `run_authority_curation_workflow(...)` stores ADK failure artifacts when ADK
  returns a gate failure or invocation failure.

The missing piece is a durable step-by-step trace that starts as soon as the
mutation event exists.

## On-Call Questions

Every signal in this design must answer one of these questions:

1. Where did this curation mutation stop?
2. Did it create, change, or publish any authority content?
3. Which model and prompt/compiler versions were used?
4. What can the operator safely do next?
5. Was private prompt/model content captured, elided, or exported?

If a trace field does not help answer one of those questions, omit it.

## Current Repo Evidence

- `pyproject.toml` already depends on `google-adk>=2.0.0,<3.0.0`.
- `uv.lock` already contains `opentelemetry-api` and `opentelemetry-sdk` through
  dependencies, but `pyproject.toml` does not declare direct OpenTelemetry use.
- `services/agent_workbench/authority_curation.py` already has
  `authority_curating`, `AuthorityCurationAttempt`, ADK invocation, diff
  validation, candidate publication, and failure artifact hooks.
- `models/authority_curation.py` stores one curation attempt row, but no
  append-only trace events.
- `utils/failure_artifacts.py` stores final failure artifacts under `logs/`.
- `utils/logging_config.py` uses stdlib `logging` plus rotating file handlers.

## Documentation Evidence

- Python `logging` is stable stdlib and intended for application/library event
  logging: https://docs.python.org/3/library/logging.html
- ADK activity logging uses host-language standard logging and structured GenAI
  OpenTelemetry events. ADK prompt content is elided by default and full prompt
  logging must be explicitly enabled because it can expose sensitive content.
- OpenTelemetry Python currently marks traces and metrics stable, while logs
  are still marked development. Its examples show `LoggingHandler`, OTLP
  exporters, and trace/log correlation. Direct OTel usage would require an
  explicit dependency and exporter configuration if AgileForge imports it
  directly.

## Goals

- Create a durable trace artifact for every `authority curate` mutation.
- Start tracing immediately after `mutation_event_id` is known, before curation
  attempt creation.
- Flush trace events incrementally so a killed process still leaves useful
  evidence.
- Include model id, compiler version, prompt hash, curation attempt id,
  feedback attempt id, source authority id, source authority fingerprint, and
  ADK event count when known.
- Record each host-owned phase:
  - mutation lease acquired;
  - guard validation started/completed;
  - curation attempt create started/completed;
  - workflow state marked `authority_curating`;
  - source authority and feedback loaded;
  - ADK invocation started/completed/failed;
  - gate result parsed;
  - candidate diff validation started/completed/failed;
  - candidate publication started/completed/failed;
  - workflow pending-review update completed/failed;
  - mutation finalization completed/failed;
  - recovery classification completed.
- Return trace identifiers in CLI/API envelopes, `mutation show`, and
  `authority status`.
- Keep prompt/model content redacted by default.
- Make recovery guidance more precise when a pending lease expires.
- Preserve current curation semantics: no auto-accept, no broad regenerate, no
  weakening of diff validation.

## Non-Goals

- Do not build a general observability platform.
- Do not require an OpenTelemetry Collector, cloud backend, or GCP logging.
- Do not enable full prompt/model-content logging by default.
- Do not replace existing failure artifacts.
- Do not add `structlog`, `loguru`, Sentry, LangSmith, or another logging
  dependency in v1.
- Do not alter authority candidate generation quality logic in this change.
- Do not auto-resume ambiguous mutations after candidate publication.

## Tech Stack Decision

### Recommended v1

Use existing local primitives:

- append-only JSONL trace artifact under `logs/traces/authority_curation/`;
- existing mutation event id as the primary trace id;
- existing `AuthorityCurationAttempt` row for attempt status;
- existing failure artifacts for detailed failure payloads;
- stdlib `logging` for compact operational events;
- ADK activity logging as optional diagnostic source.

This is the smallest useful design. It works offline, keeps data local, avoids a
collector, and is easy to test with plain filesystem assertions.

### OpenTelemetry Role

OpenTelemetry should be treated as an optional export path, not v1 storage.

Future v2 can convert curation trace events into OTel spans:

```text
authority.curate
  authority.curate.guard_validation
  authority.curate.adk_invocation
  authority.curate.diff_validation
  authority.curate.publish_candidate
  authority.curate.finalize_mutation
```

If AgileForge imports OTel directly, `pyproject.toml` must declare the direct
dependencies instead of relying on ADK transitive dependencies.

## Trace Artifact Contract

Trace files are JSONL, one event per line:

```text
logs/traces/authority_curation/authority_curation_trace-<mutation_event_id>.jsonl
```

Each event has this bounded schema:

```json
{
  "schema_version": "agileforge.authority_curation_trace.v1",
  "trace_artifact_id": "authority_curation_trace-647",
  "mutation_event_id": 647,
  "curation_attempt_id": "curation-...",
  "project_id": 3,
  "step": "adk_invocation_started",
  "status": "started",
  "recorded_at": "2026-06-16T22:55:48Z",
  "duration_ms": null,
  "correlation_id": "0b1a984a-c005-440f-b807-2f597588db44",
  "attributes": {
    "spec_version_id": 4,
    "source_authority_id": 6,
    "feedback_attempt_id": "feedback-...",
    "requested_model_id": "openrouter/deepseek/deepseek-v4-pro",
    "compiler_version": "authority-curation.v1",
    "prompt_hash": "sha256:..."
  },
  "error": null
}
```

### Step Names

Trace step names are a fixed string enum. Implementations must use these exact
values:

```text
mutation_lease_acquired
guard_validation_started
guard_validation_completed
guard_validation_failed
curation_attempt_create_started
curation_attempt_create_completed
curation_attempt_create_failed
workflow_curating_status_started
workflow_curating_status_completed
workflow_curating_status_failed
input_load_started
input_load_completed
input_load_failed
adk_invocation_started
adk_invocation_completed
adk_invocation_failed
adk_gate_parse_started
adk_gate_parse_completed
adk_gate_parse_failed
diff_validation_started
diff_validation_completed
diff_validation_failed
candidate_publication_started
candidate_publication_completed
candidate_publication_failed
workflow_pending_review_started
workflow_pending_review_completed
workflow_pending_review_failed
mutation_finalize_started
mutation_finalize_completed
mutation_finalize_failed
recovery_classification_started
recovery_classification_completed
recovery_classification_failed
```

`status` is also constrained:

```text
started
completed
failed
skipped
```

Rules:

- `schema_version`, `trace_artifact_id`, `mutation_event_id`, `project_id`,
  `step`, `status`, and `recorded_at` are required.
- `curation_attempt_id` is null until the attempt row exists.
- `attributes` is allowlisted and bounded.
- `error` contains only code, message, retryable flag, failure artifact id, and
  compact details.
- No raw prompt, full source authority JSON, full feedback JSON, full candidate
  JSON, API keys, tokens, or personal contact data may appear in default traces.
- Large values are represented by fingerprints, counts, ids, and artifact ids.

### Error Object

`error` is either null or this object:

```json
{
  "code": "SPEC_COMPILE_FAILED",
  "message": "Authority curation ADK workflow failed.",
  "retryable": false,
  "failure_artifact_id": "authority_curation-...",
  "details": {
    "failure_stage": "adk_invocation_failed",
    "validation_error_count": 2
  }
}
```

Rules:

- `code` and `message` are required strings.
- `retryable` is a required boolean.
- `failure_artifact_id` is optional and null when no failure artifact exists.
- `details` is optional, bounded, and allowlisted. It may contain ids, counts,
  hashes, step names, and short enum-like reasons. It must not contain raw
  prompt, feedback, source authority, candidate authority, request headers, API
  keys, or full model output.

## ADK Activity Logging Contract

ADK activity logging remains available for deep debugging, but AgileForge must
control it explicitly.

Default behavior:

- ADK prompt/model content stays elided.
- AgileForge captures ADK `event_count`, model id, final gate status, and
  failure artifact id when present.
- The durable curation trace stores ADK state summaries, not full ADK messages.

Debug behavior:

- A future explicit flag or env var may enable full ADK activity capture.
- The CLI must warn that full prompt/model logging may contain private product
  specs, source authority, feedback text, or PII.
- Debug capture must write to a separate artifact id and must not be enabled by
  `INFO` log level alone.
- Any OTLP export must require explicit endpoint configuration and must be off
  by default.

## Public CLI/API Contract

### `authority curate`

Every response should include:

```json
{
  "curation_attempt_id": "curation-...",
  "mutation_event_id": 647,
  "trace_artifact_id": "authority_curation_trace-647"
}
```

On failure, include both trace and failure ids when available:

```json
{
  "error_code": "SPEC_COMPILE_FAILED",
  "curation_attempt_id": "curation-...",
  "mutation_event_id": 647,
  "trace_artifact_id": "authority_curation_trace-647",
  "failure_artifact_id": "authority_curation-..."
}
```

If curation never reached attempt creation, `curation_attempt_id` is null but
`mutation_event_id` and `trace_artifact_id` still exist.

### `mutation show`

For `command="agileforge authority curate"`, `mutation show` should add:

```json
{
  "trace_artifact_id": "authority_curation_trace-647",
  "trace_artifact_present": true,
  "last_trace_step": "adk_invocation_started",
  "last_trace_status": "started"
}
```

### `authority status`

Authority projection should add:

```json
{
  "latest_curation_trace_artifact_id": "authority_curation_trace-647",
  "latest_curation_last_step": "adk_gate_failed",
  "latest_curation_last_status": "failed"
}
```

### Trace Inspection Command

Add a read-only command:

```bash
agileforge authority curation trace \
  --mutation-event-id 647 \
  --project-id 3
```

`--mutation-event-id` is required and globally identifies the mutation.
`--project-id` is optional. When supplied, the command validates that the
mutation ledger row belongs to that project before reading the trace. When
omitted, the command reads by mutation id alone.

Default output is bounded:

```text
trace_artifact_id=authority_curation_trace-647
event_count=7
last_step=adk_invocation_started
last_status=started
curation_attempt_id=null
candidate_published=false
failure_artifact_id=null
recommended_action=retry_with_fresh_idempotency_key
```

Add `--json` for full bounded JSON. Do not print raw prompt/model content.

## Recovery Classification

When a pending curation mutation expires, AgileForge should classify by durable
evidence:

| Last durable evidence | Candidate published? | Recovery behavior |
| --- | --- | --- |
| No trace or only `mutation_lease_acquired` | No | Mark failed/no-side-effect or allow fresh curation retry. |
| Attempt created, ADK not started | No | Mark attempt failed, restore `authority_rejected`, recommend fresh idempotency key. |
| ADK started or failed before publish | No | Mark attempt failed, restore `authority_rejected`, link trace and failure artifact if present. |
| Candidate published, workflow or ledger finalization failed | Yes | Return `MUTATION_RECOVERY_REQUIRED` with candidate id/fingerprint and exact reconcile command. |

`recovery_required` should be reserved for ambiguous or published side effects.
If the trace and DB prove no candidate was published, the user should not be
stuck with an unrecoverable expired lease.

### Recovery Command

When curation published a candidate authority row but failed before workflow or
ledger finalization, the next command must be explicit and must not rerun ADK:

```bash
agileforge authority curate \
  --project-id 3 \
  --recovery-mutation-event-id 647 \
  --expected-candidate-authority-id 7 \
  --expected-candidate-authority-fingerprint sha256:... \
  --idempotency-key recover-authority-curation-647-001
```

Recovery mode is mutually exclusive with normal curation inputs such as
`--spec-version-id`, `--source-authority-id`,
`--expected-source-authority-fingerprint`, and `--feedback-attempt-id`. The
runner loads those values from the original mutation/attempt records.

Recovery behavior:

1. Create a new mutation ledger row with `recovers_mutation_event_id=647`.
2. Verify the original ledger row exists, belongs to the project, has
   `command="agileforge authority curate"`, and is `recovery_required`.
3. Verify the original curation attempt and trace artifact agree on the recovery
   stage.
4. Verify the candidate authority row exists and its fingerprint matches the
   expected candidate fingerprint.
5. Update any missing curation attempt success metadata that can be recovered
   from persisted candidate, trace, and failure-response metadata.
6. Set workflow state to `authority_pending_review` with the recovered candidate
   id and fingerprint.
7. Finalize the recovery mutation as successful.
8. Mark the original ledger row superseded by the recovery mutation.

Recovery mode must fail closed if the candidate row is missing, the fingerprint
does not match, the original command is not `authority curate`, the original
row is not `recovery_required`, or the trace proves the failure happened before
candidate publication.

## Storage And Files

Add one small utility module:

```text
utils/authority_curation_trace.py
```

Responsibilities:

- build trace artifact ids from mutation event ids;
- append JSONL trace events atomically enough for local CLI use;
- read and summarize trace artifacts;
- enforce field allowlists and value size limits.

No new SQL table is needed in v1. Existing ids are sufficient:

- `CliMutationLedger.mutation_event_id`;
- `AuthorityCurationAttempt.curation_attempt_id`;
- `AuthorityCurationAttempt.failure_artifact_id`;
- deterministic trace artifact path.

Add one direct link to the existing curation attempt table:

```text
AuthorityCurationAttempt.mutation_event_id
```

This is not a new table. The runner already knows the mutation event id before
creating the attempt row, so new attempts must store it directly. Projection
code should use this column to resolve `trace_artifact_id`. For historical rows
created before the column exists or before it is populated, projection may
fallback to `CliMutationLedger` lookup by
`command="agileforge authority curate"` plus the attempt's `idempotency_key`.

If later dashboards need queryable per-step history, add a table then. Do not
add one speculatively.

## Security And Privacy

- Default traces never store raw prompts, full source authority JSON, full
  feedback JSON, or full candidate authority JSON.
- Store hashes, ids, counts, statuses, and bounded summaries.
- Full ADK prompt capture requires an explicit debug switch and warning.
- OTLP/cloud export is off by default and must require explicit endpoint
  configuration.
- External model provider id is allowed metadata; API keys and request headers
  are not.

## Testing Requirements

Focused tests should cover:

- successful curation writes trace start, ADK, validation, publish, and finalize
  events;
- ADK invocation failure writes trace events and links the failure artifact id;
- diff validation failure writes a trace event and no publish event;
- expired mutation at `start` returns trace metadata and a precise recovery
  classification;
- candidate-published finalization failure remains `MUTATION_RECOVERY_REQUIRED`
  and includes candidate id/fingerprint;
- trace reader redacts or rejects oversized/unallowlisted fields;
- `authority status` and `mutation show` expose trace metadata;
- no default trace event contains raw authority JSON, raw feedback JSON, or raw
  model output.

## Alternatives Considered

### Direct OpenTelemetry v1

Rejected for v1. OTel is the right vendor-neutral export story, but requiring a
collector/exporter makes local CLI recovery harder and adds privacy/export
configuration before the durable local trail exists.

### Stdlib logs only

Rejected. Rotating prose logs are useful operational breadcrumbs, but they are
not a stable artifact contract for agents, tests, or recovery logic.

### SQL event table first

Rejected for v1. Queryable trace rows are nice, but a local JSONL artifact plus
existing mutation/attempt ids is enough. Add a table only when dashboard query
needs prove it.

### Rely only on ADK activity logging

Rejected. ADK logs explain ADK internals, but AgileForge recovery depends on
host-owned side effects: mutation leases, workflow state, DB publication, diff
validation, and setup status. Those must be traced by AgileForge itself.

## Implementation Readiness

This design is ready for implementation planning after review. The plan should
start with failing tests for trace artifact creation and recovery
classification, then add the smallest trace writer and wire it through
`AuthorityCurationRunner`.
