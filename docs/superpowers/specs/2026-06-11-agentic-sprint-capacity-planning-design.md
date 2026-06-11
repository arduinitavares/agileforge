# Agentic Sprint Capacity Planning Design

**Date:** 2026-06-11
**Status:** Draft for review
**Spec mode:** proposed_change
**Scope:** Sprint planning UI, CLI/API contracts, Sprint planner schema,
capacity recommendation consumption, and calendar-field removal
**Builds on:**
`docs/superpowers/specs/2026-06-11-agentic-sprint-metrics-design.md`

## Revision History

- 2026-06-11: Drafted hard-break design that removes calendar duration from
  agentic Sprint planning and makes project-specific capacity the planning
  control.

## Summary

AgileForge Sprint planning is capacity-based, not calendar-based.

The current Sprint planner still asks operators and agents for calendar-oriented
inputs such as `sprint_duration_days` and `sprint_start_date`. That model is
wrong for agentic execution. ASA project history shows Sprints completing in
minutes while the UI still presents a 14-day Sprint duration field. The user
does not want a calendar planning concept for agentic Sprints.

This design makes a hard break:

- Remove duration/day fields from user-facing Sprint planning.
- Remove duration/day fields from CLI and API generation contracts.
- Remove duration/day fields from Sprint planner agent input and required
  output.
- Remove planned start/end date fields from Sprint save surfaces.
- Use `max_story_points` / capacity points as the only planning control.
- Default capacity from project-specific Sprint metrics when available.
- Treat elapsed minutes as observed telemetry only, not as a planning input.
- Clean up persistence so new Sprints no longer require synthetic calendar
  start/end dates.

Backward-compatible hidden flags and silent duration defaults are intentionally
out of scope. If an old script depends on calendar arguments, it must fail
clearly until it is updated to the capacity contract.

## Problem

The Sprint planner currently mixes two incompatible planning models:

- Human Scrum timeboxing: velocity bands, 14-day duration, planned start/end
  dates.
- Agentic delivery: capacity determined by observed project throughput and
  completed story points.

For real agentic projects, the calendar inputs are misleading:

- A 14-day duration field suggests elapsed calendar time drives capacity.
- Velocity bands such as Low/Medium/High ignore project-specific completed
  history once that history exists.
- Max story points is optional even when the metrics projection has a concrete
  recommendation.
- Sprint planner input and output still expose duration fields, so the LLM sees
  calendar planning even if the UI hides it.
- Sprint persistence requires `start_date` and `end_date`, forcing synthetic
  calendar data into new Sprints.

The result is a UI and API that can plan the wrong Sprint size or make agents
reason from obsolete Scrum defaults instead of AgileForge's own execution
history.

## Goals

- Make agentic Sprint planning use story-point capacity as the primary planning
  control.
- Use `recommended_next_sprint_points` from `agileforge sprint metrics` as the
  default capacity when available.
- Keep manual capacity override possible through `max_story_points`.
- Remove calendar duration from all normal user-facing and agent-facing
  planning contracts.
- Remove planned start/end date collection from normal Sprint save flows.
- Preserve execution timestamps such as `started_at` and `completed_at`; these
  are runtime evidence, not planning inputs.
- Surface observed elapsed time, throughput, and token-metric availability as
  read-only project telemetry.
- Fail old duration-based CLI/API usage clearly instead of silently accepting
  and ignoring it.
- Update tests so the new contract is enforced by UI, CLI, API, planner schema,
  and persistence behavior.

## Non-Goals

- Do not keep hidden deprecated CLI flags for `--sprint-duration-days`.
- Do not keep a hidden UI duration field or static frontend value such as 14.
- Do not pass `sprint_duration_days` into the Sprint planner prompt.
- Do not require the Sprint planner model to emit `duration_days`.
- Do not convert days to minutes as a planning input.
- Do not make elapsed minutes a capacity limit.
- Do not implement token capture or cost tracking in this feature.
- Do not rename the Sprint concept itself.
- Do not redesign post-sprint triage.
- Do not change Story, Backlog, or Roadmap phase semantics except where Sprint
  planning routes display capacity guidance.

## Design Principles

Agentic Sprints are execution batches, not calendar timeboxes.

Planning asks:

> How much work should this Sprint attempt?

It does not ask:

> How many days should this Sprint last?

Elapsed wall-clock time remains useful as telemetry. It helps operators compare
actual delivery speed, retry cost, and model/tooling overhead. It must not
become the primary planning knob because it is noisy and can be distorted by
human pauses, provider latency, retries, local machine load, and verification
depth.

## Public Contract

### CLI

`agileforge sprint generate` must expose capacity-oriented arguments:

```bash
agileforge sprint generate --project-id <id> \
  [--input <guidance>] \
  [--selected-story-ids <ids>] \
  [--max-story-points <points>] \
  [--no-task-decomposition]
```

`--sprint-duration-days` is removed from the active command parser. Passing it
must fail as an unrecognized argument.

`--team-velocity-assumption` is removed from the active command parser. Passing
it must fail as an unrecognized argument. Velocity bands are part of the old
calendar/heuristic model and must not be replaced by another hidden heuristic.

`agileforge sprint save` must no longer require `--sprint-start-date`. Passing
it must fail as an unrecognized argument. Saving a reviewed Sprint draft must
require only guarded persistence fields
and team ownership:

```bash
agileforge sprint save --project-id <id> \
  --team-name <name> \
  --attempt-id <attempt_id> \
  --expected-artifact-fingerprint <fingerprint> \
  --expected-state <state> \
  --idempotency-key <key>
```

### API

`POST /api/projects/{project_id}/sprint/generate` removes
`sprint_duration_days` from the request schema. Requests containing this field
must fail validation rather than be silently accepted.

The generation request must keep:

- `user_input`
- `max_story_points`
- `include_task_decomposition`
- `selected_story_ids`

The generation request must remove `team_velocity_assumption`. Requests
containing this field must fail validation.

`POST /api/projects/{project_id}/sprint/save` removes `sprint_start_date` from
the request schema. Requests containing this field must fail validation. New
planned Sprints must no longer require planned calendar dates.

### Dashboard UI

Sprint Planning must replace calendar controls with a project metrics panel
and a capacity input.

Remove from the normal form:

- Velocity Assumption
- Sprint Duration (Days)
- Sprint Start Date
- any generated text that describes Low/Medium/High story bands as the primary
  planner control

Add:

- Recommended Capacity
- Capacity Basis
- Average Points per Sprint
- Median Points per Sprint
- Completed Sprints
- Observed Runtime
- Token Metrics Status
- Max Story Points, prefilled with the recommendation when available

When project metrics provide `recommended_next_sprint_points`, the UI must
prefill `Max Story Points` with that value and make the source visible. For ASA
project 3, this means the Sprint planner must show and use 9 points unless
the operator changes it.

When metrics history is insufficient, the UI must show an explicit blocked
state and require manual capacity in points. It must not reintroduce calendar
duration.

### Sprint Planner Agent Contract

`SprintPlannerInput` must remove:

- `team_velocity_assumption`
- `sprint_duration_days`

It must include:

- `available_stories`
- `capacity_points`
- `capacity_source`
- `capacity_basis`
- `user_context`
- `include_task_decomposition`

`capacity_points` is the actual planning limit. It comes from
`max_story_points` when supplied, otherwise from project metrics when available.
If neither source exists, Sprint generation must block before invoking the
planner.

`capacity_source` must be one of:

- `user_override`
- `project_metrics`

`capacity_basis` must be a concise, model-visible explanation, such as:

```text
9 points, based on the last 3 completed Sprints: 18, 17, 16.
```

`SprintPlannerOutput` must remove `duration_days`.

Capacity analysis must stop reporting `velocity_assumption` and
`capacity_band`. It must report:

- `capacity_points`
- `capacity_source`
- `story_points_used`
- `remaining_capacity_points`
- `selected_count`
- `commitment_note`
- `reasoning`

The prompt instructions must say that duration, dates, and elapsed minutes
are not planning constraints. The planner must optimize selected Stories
against capacity points, dependencies, scope coherence, and task decomposition
quality.

## Persistence Contract

New Sprint records must no longer require planned `start_date` or `end_date`.

Implementation must update the domain model and migrations so `sprints.start_date`
and `sprints.end_date` are nullable or removed from new-write requirements.
Existing rows may retain historical values, but new code must not synthesize
calendar dates merely to satisfy the old schema.

Execution timestamps remain:

- `started_at`: set when Sprint execution starts.
- `completed_at`: set when Sprint execution closes.

Read projections must prefer execution timestamps for runtime evidence. If a
legacy row has `start_date` / `end_date`, those fields may be returned only as
historical data and must not be labeled as planning controls.

## Workflow Behavior

`workflow next` and Sprint runtime summaries must expose metrics-informed
planning guidance when Sprint generation is available:

- current candidate count
- recommended capacity, when available
- capacity source
- blocked reason when no capacity can be inferred and no manual capacity was
  supplied

Advertised Sprint generation commands must omit duration arguments and velocity
assumption arguments.

If no metrics recommendation exists and the operator has not supplied
`--max-story-points`, `workflow next` must return Sprint generation as blocked
with a `SPRINT_CAPACITY_REQUIRED` reason and remediation that names
`--max-story-points`.

## Error Handling

Old duration-based inputs must fail loudly:

- CLI: unknown argument for `--sprint-duration-days`.
- API: validation error for unknown or forbidden `sprint_duration_days`.
- Planner schema: validation error if duration fields appear in agent input or
  output.

Missing capacity must not be silently replaced by duration. The service must
choose one of these explicit paths:

- use user-provided `max_story_points`;
- use metrics-derived `recommended_next_sprint_points`;
- block generation with a capacity-required error.

## Compatibility Decision

This is a hard break.

The implementation must not preserve old calendar arguments through hidden
flags, ignored payload fields, compatibility defaults, or prompt-only shims.
Existing scripts and tests that still use `sprint_duration_days`,
`duration_days`, or `sprint_start_date` for normal Sprint planning must fail
and be updated to the capacity-based contract.

The only allowed compatibility is data preservation for already-saved Sprint
rows. Existing historical rows must not be deleted or rewritten merely because
they contain date fields.

## Testing Requirements

Tests must prove the contract across layers:

- CLI `sprint generate --help` does not list `--sprint-duration-days`.
- CLI `sprint generate --sprint-duration-days 14` fails.
- API Sprint generation rejects `sprint_duration_days`.
- API Sprint save no longer requires `sprint_start_date`.
- Frontend Sprint Planning does not render Sprint Duration, Velocity
  Assumption, or Sprint Start Date controls when metrics are available.
- Frontend pre-fills Max Story Points from
  `recommendation.recommended_next_sprint_points`.
- Sprint planner input schema has no duration fields.
- Sprint planner output schema has no `duration_days`.
- Planner prompt instructions do not mention duration as a planning input.
- Persistence can save a planned Sprint without planned start/end dates.
- Existing completed Sprint metrics still compute elapsed time from
  `started_at` / `completed_at`.
- `workflow next` advertises Sprint generation without duration arguments.

## Acceptance Criteria

- ASA project 3 Sprint Planning UI shows the project recommendation of 9 points
  and does not show 14 days or any Sprint duration control.
- ASA project 3 Sprint Planning can generate a Sprint without any calendar
  field.
- CLI and API Sprint generation no longer accept duration fields.
- Sprint planner model context contains capacity points and metrics basis, not
  calendar duration.
- New planned Sprint records do not require synthetic planned start/end dates.
- `pyrepo-check --all` passes after implementation.

## Open Decisions

None. The selected product decision is to remove calendar planning concepts
from agentic Sprint planning rather than deprecating them gradually.
