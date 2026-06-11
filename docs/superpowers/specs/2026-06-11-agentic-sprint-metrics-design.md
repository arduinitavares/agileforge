# Agentic Sprint Metrics Design

**Date:** 2026-06-11
**Status:** Accepted
**Spec mode:** proposed_change
**Scope:** read-only Sprint throughput metrics, planning recommendation, CLI/API
projection, and future token-metrics contract
**Builds on:**
`docs/superpowers/specs/2026-06-10-post-sprint-learning-triage-design.md`

## Revision History

- 2026-06-11: Accepted read-only metrics design with recommendation-only
  planning support and no Sprint planner prompt changes.

## Summary

AgileForge should expose deterministic Sprint execution metrics for agentic
projects without changing Sprint planning behavior in the first version.

The selected design is a read-only metrics projection:

- It computes throughput and elapsed-time KPIs from durable Sprint, Story, Task,
  and Workflow Event records.
- It exposes a recommendation field,
  `recommended_next_sprint_points`, for human and agent operators.
- It keeps the Sprint planner prompt unchanged.
- It exposes token and cost metric fields as an explicit unavailable contract,
  without adding token columns or provider-integration logic in this version.

This gives users immediate feedback about actual agentic delivery speed while
keeping LLM planning stable until the metrics projection has proven correct.

## Problem

AgileForge still presents Sprint planning through calendar-oriented fields such
as a 14-day duration, but agentic Sprint execution can complete in minutes or
hours. Users need a project-specific view of observed delivery speed instead of
guessing capacity from a human Scrum cadence.

Recent Sprint history support made durable execution rows visible, but there is
not yet a first-class metrics command or API response that answers:

- How many points, Stories, and Tasks have actually completed?
- How much wall-clock time did completed Sprints take?
- What point budget is reasonable for the next Sprint based on this project's
  own recent history?
- Are token and cost metrics available, and if not, why not?

Without this projection, agents can either ignore real throughput or infer it
ad hoc from lower-level records.

## Goals

- Add a read-only Sprint metrics projection for one AgileForge project.
- Expose the projection through CLI and API surfaces.
- Compute completed Sprint throughput from durable execution records.
- Provide a deterministic `recommended_next_sprint_points` value when completed
  Sprint history exists.
- Make insufficient-history behavior explicit instead of inventing a fake
  velocity.
- Include a stable `token_metrics` object that reports token data as
  unavailable until runtime capture exists.
- Keep the projection safe for agents by making every field reproducible from
  existing persisted data.

## Non-Goals

- Do not change the Sprint planner prompt or inject metrics into LLM context.
- Do not make Sprint generation consume `recommended_next_sprint_points`.
- Do not add token, cost, or provider-response columns to the database.
- Do not capture token usage from LLM providers.
- Do not estimate provider cost from model names or pricing tables.
- Do not add a new metrics dashboard page in this version.
- Do not mutate workflow state, Sprint state, Story state, or planner attempts.
- Do not replace `agileforge sprint history`; metrics is a separate analytical
  projection.

## Current Behavior To Change

`agileforge sprint history --project-id <id>` now exposes both Sprint planner
attempt history and durable Sprint execution history. That command is still a
history projection, not a metrics summary.

The proposed change adds a separate read-only metrics surface:

```bash
agileforge sprint metrics --project-id <project_id>
```

The API should expose the same data through:

```http
GET /api/projects/{project_id}/sprint/metrics
```

Both surfaces must derive their Sprint execution rows from the same durable
records used by Sprint execution history.

## Public Contract

The metrics payload is a JSON object with these top-level fields:

```json
{
  "project_id": 3,
  "status": "ready",
  "summary": {
    "completed_sprint_count": 6,
    "completed_story_count": 23,
    "completed_task_count": 54,
    "completed_story_points": 64,
    "total_elapsed_seconds": 6390,
    "average_points_per_sprint": 10.67,
    "median_points_per_sprint": 11,
    "average_elapsed_seconds_per_sprint": 1065,
    "points_per_hour": 36.06,
    "sprints_with_elapsed_time_count": 6,
    "unestimated_completed_story_count": 0
  },
  "recommendation": {
    "recommended_next_sprint_points": 9,
    "basis": "last_3_completed_sprints_average",
    "source_sprint_ids": [18, 17, 16],
    "source_completed_points": [10, 12, 5],
    "sample_size": 3,
    "explanation": "Recommended from the rounded average of the last 3 completed Sprints."
  },
  "completed_sprints": [],
  "token_metrics": {
    "status": "unavailable",
    "prompt_tokens": null,
    "completion_tokens": null,
    "total_tokens": null,
    "estimated_cost_usd": null,
    "reason": "Token usage is not yet captured in durable AgileForge records."
  },
  "data_quality_warnings": []
}
```

Valid `status` values:

- `ready`: at least one completed Sprint exists.
- `insufficient_history`: no completed Sprint exists.
- `partial_history`: completed Sprint history exists, but one or more summary
  fields are unavailable because source timestamps, points, or Workflow Event
  metrics are missing.

## Completed Sprint Rows

`completed_sprints` contains one row for each durable Sprint whose status is
`Completed`, ordered by newest first:

```json
{
  "sprint_id": 18,
  "goal": "Establish quality gate integration.",
  "status": "Completed",
  "started_at": "2026-06-11T10:12:00Z",
  "completed_at": "2026-06-11T10:42:00Z",
  "start_date": "2026-06-11",
  "end_date": "2026-06-25",
  "story_count": 3,
  "completed_story_count": 3,
  "task_count": 9,
  "completed_task_count": 9,
  "story_points_planned": 10,
  "story_points_completed": 10,
  "elapsed_seconds": 1800,
  "workflow_event_count": 7,
  "workflow_event_duration_seconds": 1740,
  "turn_count": 32,
  "history_fidelity": "derived"
}
```

Field rules:

- `story_points_planned` is the sum of story points for all Stories linked to
  the Sprint. Missing story points count as `0` and increment
  `unestimated_completed_story_count` when the Story completed.
- `story_points_completed` is the sum of story points for linked Stories whose
  status is `Done` or `Accepted`.
- `elapsed_seconds` is computed only when both `started_at` and `completed_at`
  exist and `completed_at >= started_at`; otherwise it is `null` and a warning
  is added.
- `workflow_event_count` counts Workflow Event rows tied to the Sprint id.
- `workflow_event_duration_seconds` is the sum of non-null
  `WorkflowEvent.duration_seconds` values tied to the Sprint id.
- `turn_count` is the sum of non-null `WorkflowEvent.turn_count` values tied to
  the Sprint id. If no tied events have turn counts, the Sprint row uses
  `null`.
- `history_fidelity` preserves the existing Sprint execution-history semantics,
  such as `derived` or `snapshotted`.

## Summary Metrics

The `summary` object aggregates `completed_sprints`.

Rules:

- `completed_sprint_count` is the number of completed Sprint rows.
- `completed_story_count` is the sum of `completed_story_count`.
- `completed_task_count` is the sum of `completed_task_count`.
- `completed_story_points` is the sum of `story_points_completed`.
- `total_elapsed_seconds` is the sum of non-null `elapsed_seconds` values, or
  `null` when no completed Sprint has elapsed time.
- `average_points_per_sprint` is the arithmetic mean of
  `story_points_completed` across completed Sprints, rounded to two decimal
  places.
- `median_points_per_sprint` is the median of `story_points_completed` across
  completed Sprints, rounded to two decimal places when needed.
- `average_elapsed_seconds_per_sprint` is the arithmetic mean of non-null
  `elapsed_seconds` values, rounded to two decimal places.
- `points_per_hour` is
  `completed_story_points / (total_elapsed_seconds / 3600)`, rounded to two
  decimal places, and is `null` when elapsed time is unavailable or zero.
- `sprints_with_elapsed_time_count` is the number of completed Sprints with
  non-null `elapsed_seconds`.
- `unestimated_completed_story_count` is the number of completed linked Stories
  whose story points are missing.

## Recommendation Logic

The first version recommends a point budget only from actual completed Sprint
history.

Selection:

- Sort completed Sprints by `completed_at` descending, then `sprint_id`
  descending.
- Use the newest three completed Sprints when at least three exist.
- Use all completed Sprints when one or two exist.
- Ignore no completed Sprint with valid `story_points_completed`; a completed
  Sprint with `0` points remains part of the sample because it is real project
  history.

Calculation:

- `source_completed_points` is the list of sampled
  `story_points_completed` values.
- `average = sum(source_completed_points) / sample_size`.
- `recommended_next_sprint_points` is `average` rounded to the nearest integer,
  with `.5` values rounded up.
- The recommendation is clamped to `0` or greater.

No-history behavior:

- If there are no completed Sprints, `recommended_next_sprint_points` is `null`.
- `basis` is `insufficient_history`.
- `source_sprint_ids` and `source_completed_points` are empty lists.
- The explanation tells the caller to complete at least one Sprint or provide an
  explicit manual Sprint capacity.

This avoids importing a human Scrum default into an agentic project before the
project has its own execution evidence.

## CLI Behavior

`agileforge sprint metrics --project-id <project_id>` should print the metrics
payload as the command result and use wording that separates metrics from
planning mutation:

```text
Sprint metrics and planning recommendation
```

Human-readable CLI rendering should show:

- completed Sprint count
- completed points, Stories, and Tasks
- elapsed time totals and averages when available
- recent completed Sprint rows used by the recommendation
- `recommended_next_sprint_points`
- token metrics status and unavailable reason

Machine-readable CLI output should expose the full JSON payload without losing
fields.

The command must be read-only. It must not generate, save, start, close, or
triage a Sprint.

## API Behavior

`GET /api/projects/{project_id}/sprint/metrics` returns the same payload under
the existing API envelope convention.

The API must not require the project to be in `SPRINT_SETUP`, `SPRINT_DRAFT`,
`SPRINT_ACTIVE`, or `SPRINT_COMPLETE`. Metrics are valid whenever a project
exists.

The API must not return a successful payload for a missing project.

## Token Metrics Contract

Token and cost fields are part of the public response now, but runtime
population is out of scope.

The `token_metrics` object must use this shape:

```json
{
  "status": "unavailable",
  "prompt_tokens": null,
  "completion_tokens": null,
  "total_tokens": null,
  "estimated_cost_usd": null,
  "reason": "Token usage is not yet captured in durable AgileForge records."
}
```

Rules:

- No database columns are added for token or cost fields in this version.
- No LLM provider wrappers are changed in this version.
- No cost calculation is attempted from model names, pricing tables, or prompt
  estimates.
- Future token capture may change `status` to `available` only when durable
  runtime records contain token values.

This gives downstream CLI, API, and UI clients a stable contract without
pretending that token usage is already measured.

## Data Quality Warnings

The payload should include `data_quality_warnings` as a list of structured
objects:

```json
{
  "code": "SPRINT_ELAPSED_TIME_UNAVAILABLE",
  "sprint_id": 18,
  "message": "Sprint elapsed time is unavailable because started_at or completed_at is missing."
}
```

Initial warning codes:

- `SPRINT_ELAPSED_TIME_UNAVAILABLE`
- `SPRINT_ELAPSED_TIME_INVALID`
- `WORKFLOW_EVENT_TURN_COUNT_UNAVAILABLE`
- `COMPLETED_STORY_POINTS_MISSING`

Warnings do not block the metrics response. They explain why specific fields
are `null`, `0`, or partial.

## Acceptance Criteria

- `agileforge sprint metrics --project-id <id>` returns a read-only payload for
  an existing project.
- The API endpoint returns the same metrics contract as the CLI-facing
  application method.
- A project with no completed Sprints returns `status=insufficient_history` and
  `recommended_next_sprint_points=null`.
- A project with one or more completed Sprints returns completed Sprint rows,
  summary totals, and a recommendation derived from the newest one to three
  completed Sprints.
- Recommendation rounding uses nearest-integer half-up behavior.
- Token metrics fields are present and unavailable/null without any token
  database migration.
- Missing elapsed time, missing turn counts, and missing Story point estimates
  are reported through structured warnings.
- Existing Sprint planning commands behave exactly as before; no prompt or
  generation context changes occur in this version.

## Rejected Alternatives

### Change the Sprint planner prompt now

Rejected for this version. Injecting metrics into LLM prompt context before the
projection is proven would make planner failures harder to diagnose. The read
projection should be tested independently first.

### Add token database columns now

Rejected for this version. Token fields in API output are useful now, but
database columns without runtime capture would create migration surface without
measured data. The response contract is stable enough for clients, and a later
runtime-token feature can add persistence when the provider integration is
designed.

### Capture full provider token usage now

Rejected for this version. Capturing provider token usage requires changing the
LLM invocation path and normalizing provider-specific response metadata. That
belongs in a separate feature after the read-only metrics projection is stable.

### Use a fixed fallback velocity

Rejected for this version. A fixed default would reintroduce human-process
assumptions into an agentic project. With no completed Sprints, the
recommendation should be explicitly unavailable.

## Risks And Follow-Up Work

- The next feature can let Sprint planning optionally consume
  `recommended_next_sprint_points`, but only after this projection is tested
  against real project histories.
- A later UI page can visualize the same API payload without changing the
  backend contract.
- A future token-capture feature must define provider metadata extraction,
  persistence, replay/idempotency behavior, and cost-estimation policy before
  setting `token_metrics.status=available`.
- Projects with historical Sprints that lack timestamps or Story point
  estimates may produce partial metrics; warnings are required so agents do not
  treat partial values as complete.
