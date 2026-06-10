# Post-Sprint Learning Triage Design

**Date:** 2026-06-10
**Status:** In Review
**Spec mode:** proposed_change
**Scope:** `SPRINT_COMPLETE` next-cycle routing, durable sprint-learning impact
classification, and stale-guard-aware `workflow next`
**Builds on:**
`docs/superpowers/specs/2026-06-01-backlog-refinement-attempts-design.md`

## Summary

AgileForge should treat completed Sprints as learning events before it routes a
project back into backlog, roadmap, Story, or Sprint work.

The selected design is a durable post-sprint triage decision inside
`SPRINT_COMPLETE`. It does not add a new FSM enum. It does not require a
backlog attempt to exist. `workflow next` becomes triage-gated: after Sprint
close, AgileForge first asks what the Sprint changed, then advertises the
targeted bridge for that impact.

```text
SPRINT_COMPLETE
  -> post-sprint triage not recorded
  -> sprint review / sprint triage commands
  -> durable impact decision
  -> explicit per-impact bridge
```

This keeps Scrum-compatible learning loops without making Product Backlog
refinement the default after every Sprint.

## Problem

Today a completed Sprint can leave AgileForge in `SPRINT_COMPLETE` while
`workflow next` strongly suggests backlog refinement commands. If a backlog
refinement attempt is then recorded, the user can be parked in `BACKLOG_REVIEW`
and lose confidence that existing roadmap and Story work is still valid.

That behavior mixes two different concepts:

- Sprint learning is always valid input after a Sprint.
- Product Backlog refinement is only the right next action when the learning
  changes product-level scope, priority, or item content.

The workflow currently lacks a durable decision that says whether the completed
Sprint affected tasks, Stories, roadmap ordering, backlog scope, or nothing at
all. Without that decision, agents and users can over-rotate into backlog
review and make existing roadmap or Story evidence look stale when it is not.

## Goals

- Add a durable post-sprint triage decision while remaining in
  `SPRINT_COMPLETE`.
- Gate `workflow next` on whether triage has been recorded for the latest
  completed Sprint.
- Keep Product Backlog refinement available only as one possible impact bridge,
  not the default next-cycle path.
- Preserve existing roadmap, Story, and Sprint evidence unless a triage decision
  explicitly routes to a layer that may invalidate or reconcile it.
- Let agents continue directly to pending Story work when Sprint learning has
  no downstream planning impact.
- Keep routing aware of existing stale guards such as
  `downstream_backlog_stale`.
- Keep the command surface explicit enough that agents can explain why a given
  next command is shown.

## Non-Goals

- Do not add a new FSM enum such as `POST_SPRINT_REVIEW`.
- Do not make backlog refinement depend on a post-sprint backlog attempt.
- Do not auto-save, auto-approve, or auto-discard backlog refinement attempts.
- Do not reset active backlog, roadmap, Story, or Sprint state from triage
  alone.
- Do not implement a retrospective note-taking product beyond the minimal
  learning summary needed for routing.
- Do not let triage bypass existing stale guards, save guards, idempotency
  checks, or downstream replacement protections.

## Current Behavior To Change

When a Sprint closes successfully, AgileForge snapshots the Sprint, marks it
`Completed`, records a `SPRINT_COMPLETED` workflow event, clears the active
Sprint, stores the latest completed Sprint id, and moves workflow state to
`SPRINT_COMPLETE`.

The changed behavior begins after that point:

- `SPRINT_COMPLETE` remains the FSM state.
- A missing triage decision means the next cycle has not been routed yet.
- `workflow next` should not imply that backlog refinement is the default
  action while triage is missing.
- Backlog refinement commands should appear only when triage impact is
  `backlog`, `multiple`, or when a stale guard requires backlog reconciliation.

## Triage Decision Contract

The durable triage payload is stored in workflow state and projected through
status surfaces that already expose `latest_completed_sprint_id`.

```json
{
  "schema_version": "agileforge.post_sprint_triage.v1",
  "sprint_id": 13,
  "impact": "story",
  "affected_requirements": ["pyrepo-check Quality Gate Integration"],
  "affected_story_ids": [],
  "affected_backlog_item_ids": [],
  "affected_roadmap_item_ids": [],
  "learning_summary": "The research spike confirmed the next quality-gate story.",
  "decision_reason": "No backlog-level scope change was found.",
  "recorded_at": "2026-06-10T00:00:00Z",
  "recorded_by": "cli-agent"
}
```

Rules:

- `schema_version` is required and equals
  `agileforge.post_sprint_triage.v1`.
- `sprint_id` is required and must equal the latest completed Sprint for the
  project unless the caller passes an explicit completed Sprint id.
- `impact` is required and must be one of:
  - `none`
  - `task`
  - `story`
  - `roadmap`
  - `backlog`
  - `multiple`
- `learning_summary` is required and must be a non-empty text summary.
- `decision_reason` is required and must be a non-empty text rationale for the
  selected impact.
- `recorded_at` and `recorded_by` are host-owned fields.
- affected item arrays default to empty arrays.
- `impact=story` requires at least one affected requirement or story id.
- `impact=roadmap` requires at least one affected requirement, roadmap item id,
  or release/milestone reference.
- `impact=backlog` requires at least one affected backlog item id or a
  decision reason stating that new product backlog discovery is needed.
- `impact=task` requires at least one affected story id, task id, or affected
  requirement.
- `impact=none` requires all affected item arrays to be empty.
- `impact=multiple` requires at least two affected layers to be represented in
  the affected fields or decision reason.

The triage payload is a routing decision, not a mutation of backlog, roadmap,
Story, or Sprint artifacts.

## Command Surface

### `agileforge sprint review`

Read-only command that summarizes the latest completed Sprint as triage input.

```sh
agileforge sprint review --project-id 3
```

Expected response data:

- latest completed Sprint id
- Sprint close snapshot summary
- completed stories and their source requirements
- known gaps and follow-up notes when available
- current roadmap and Story coverage summary
- current stale guard summary
- existing triage decision when one is recorded
- suggested impact options with plain-language meanings

The command does not call backlog generation or create backlog attempts.

### `agileforge sprint triage`

Mutating command that records the routing decision for a completed Sprint.

```sh
agileforge sprint triage \
  --project-id 3 \
  --impact story \
  --affected-requirement "pyrepo-check Quality Gate Integration" \
  --learning-summary "The research spike confirmed the quality-gate story." \
  --decision-reason "The plan remains valid; only the next pending story needs the spike context." \
  --idempotency-key post-sprint-triage-001
```

Supported flags:

- `--project-id` required
- `--sprint-id` optional, defaults to latest completed Sprint
- `--impact` required
- `--affected-requirement` repeatable
- `--affected-story-id` repeatable
- `--affected-backlog-item-id` repeatable
- `--affected-roadmap-item-id` repeatable
- `--learning-summary` required
- `--decision-reason` required
- `--idempotency-key` required

Idempotency:

- Reusing the same idempotency key with the same body replays the recorded
  triage response.
- Reusing the same idempotency key with a different body fails with the existing
  idempotency mismatch behavior.
- Recording a different triage decision for the same Sprint requires a new
  idempotency key and explicit overwrite semantics. The first implementation may
  reject replacement decisions until a separate correction command exists.

## Workflow Next Routing

`workflow next` must evaluate post-sprint triage before advertising next-cycle
planning commands from `SPRINT_COMPLETE`.

### Missing Triage

When `fsm_state=SPRINT_COMPLETE` and no triage decision exists for
`latest_completed_sprint_id`, `workflow next` should advertise:

```text
agileforge sprint review --project-id <project_id>
agileforge sprint triage --project-id <project_id> --impact <impact> --learning-summary <summary> --decision-reason <reason> --idempotency-key <idempotency_key>
agileforge sprint history --project-id <project_id>
agileforge sprint status --project-id <project_id> --sprint-id <latest_completed_sprint_id>
```

It should not advertise backlog refinement as the default path unless an
existing stale guard already requires backlog reconciliation.

### Impact `none`

Use when the completed Sprint confirms the current plan.

Primary bridge:

- continue existing roadmap and Story flow
- show `agileforge story pending --project-id <project_id>` when pending
  requirements exist
- show the next pending Story generation command when a pending requirement can
  be selected
- show create-next-Sprint actions only when there are already eligible Story
  candidates

Backlog refinement should not be shown as a primary next command.

### Impact `task`

Use when learning creates execution follow-up work but does not change planning
artifacts.

Primary bridge:

- show the affected Story or Sprint history context
- show Story-level or Sprint-level follow-up surfaces when available
- do not reset backlog or roadmap state

If AgileForge lacks a first-class task carryover command, `workflow next` should
make that limitation explicit instead of routing to backlog refinement.

### Impact `story`

Use when the learning changes or clarifies one or more pending requirements or
saved Story drafts.

Primary bridge:

- show `agileforge story pending --project-id <project_id>`
- show `agileforge story generate --project-id <project_id> --parent-requirement <affected_requirement>` for pending affected requirements
- show Story retry/refinement commands for affected requirements that already
  have Story attempts

Backlog and roadmap commands remain secondary unless stale guards or the triage
decision identify broader impact.

### Impact `roadmap`

Use when learning changes milestone ordering, release content, or requirement
structure.

Primary bridge:

- show roadmap reconciliation or generation commands appropriate for the
  current saved backlog state
- preserve Story/Sprint commands as blocked or secondary until roadmap impact
  is resolved

The bridge must not imply that backlog scope changed unless the triage decision
also records backlog impact.

### Impact `backlog`

Use when learning changes product backlog scope, priority, split/merge, or item
content.

Primary bridge:

- show backlog refinement commands:
  - `agileforge backlog refine-preview`
  - `agileforge backlog refine-record`
  - `agileforge backlog refine-import`
  - `agileforge backlog approve`
- show reset-active only behind the existing guarded replacement path when a
  saved active backlog replacement is requested

The bridge does not require a backlog attempt to pre-exist. If no suitable
source attempt exists, the command guidance should explain which current backlog
artifact or source attempt is needed before recording a refinement.

### Impact `multiple`

Use when more than one layer is affected or when the user cannot confidently
choose one layer.

Primary bridge:

- show explicit decision options grouped by layer
- avoid presenting any one layer as the default
- show stale guard blockers first when present

## Stale-Guard-Aware Routing

Triage routing must respect existing stale guards before showing continuation
commands.

Rules:

- If `downstream_backlog_stale=true`, `workflow next` must not advertise Story
  generation, Sprint candidate, or create-next-Sprint commands as runnable
  primary actions.
- If `downstream_backlog_stale=true`, `workflow next` must show the stale reason
  and the required backlog/roadmap reconciliation path before any per-impact
  bridge.
- If triage impact is `none` but a stale guard is active, stale guard routing
  wins. The response should explain that the triage says no new Sprint learning
  impact, but pre-existing stale state still blocks continuation.
- If triage impact is `story` but the affected Story belongs to stale downstream
  backlog or roadmap state, stale guard routing wins.
- If no stale guard is active, per-impact bridge routing controls the next
  command list.

This prevents triage from becoming a bypass around existing backlog replacement
and downstream invalidation protections.

## API And Projection Surfaces

The dashboard and agent-facing API should expose triage state wherever they
already expose workflow state and latest completed Sprint state.

Projected fields:

```json
{
  "latest_completed_sprint_id": 13,
  "post_sprint_triage": {
    "schema_version": "agileforge.post_sprint_triage.v1",
    "sprint_id": 13,
    "impact": "story",
    "affected_requirements": ["pyrepo-check Quality Gate Integration"],
    "learning_summary": "The research spike confirmed the next quality-gate story.",
    "decision_reason": "No backlog-level scope change was found.",
    "recorded_at": "2026-06-10T00:00:00Z",
    "recorded_by": "cli-agent"
  },
  "post_sprint_triage_required": false
}
```

Visibility rules:

- `post_sprint_triage_required=true` when `fsm_state=SPRINT_COMPLETE`,
  `latest_completed_sprint_id` is present, and no triage exists for that Sprint.
- Existing triage for an older completed Sprint does not satisfy the requirement
  for a newer completed Sprint.
- Project status and dashboard views should label the state as post-sprint
  triage pending rather than implying that backlog review is mandatory.

## UI Semantics

The dashboard should render `SPRINT_COMPLETE` as a next-cycle decision point
when triage is missing.

Required copy behavior:

- show the latest completed Sprint summary
- show that roadmap and Story work are preserved unless triage selects an
  impact that requires reconciliation
- show impact options in product terms:
  - confirmed plan
  - task follow-up
  - Story update
  - roadmap change
  - backlog change
  - multiple impacts
- show the resulting recommended next action after triage is recorded
- do not show "Backlog Review" as the sole or default destination after every
  completed Sprint

The UI may start as read-only projection plus CLI command guidance. A full
interactive triage form is not required for the first implementation.

## Error Handling

Triage recording fails without mutation when:

- no completed Sprint exists for the project
- `--sprint-id` does not belong to the project
- `--sprint-id` is not completed
- `impact` is unknown
- required affected fields for the chosen impact are missing
- `learning-summary` is blank
- `decision-reason` is blank
- the same idempotency key is reused with a different body
- a triage decision already exists for the Sprint and replacement is not
  supported

Failure responses should include remediation that points to `sprint review` for
fresh evidence and to the correct required flags for the selected impact.

## Observability And Audit

Recording triage should create a durable workflow event or equivalent audit
entry that includes:

- project id
- sprint id
- impact
- affected fields
- idempotency key
- actor
- recorded timestamp

The audit entry should not include hidden model reasoning. It should contain
only user-provided summaries, host-owned identifiers, and normalized routing
metadata.

## Testing Requirements

Regression coverage should prove:

- closing a Sprint still moves workflow to `SPRINT_COMPLETE`
- `workflow next` in `SPRINT_COMPLETE` without triage advertises sprint review
  and sprint triage, not backlog refinement as the default
- `sprint review` returns latest completed Sprint context without mutation
- `sprint triage` records a durable decision for the latest completed Sprint
- idempotent replay returns the same triage response
- `impact=none` routes to existing Story/roadmap continuation instead of
  backlog refinement when no stale guard is active
- `impact=story` routes to Story pending/generate/refine commands for affected
  requirements
- `impact=backlog` routes to backlog refinement commands without requiring a
  pre-existing post-sprint backlog attempt
- `impact=multiple` groups options without choosing a default layer
- active stale guards override `none` and `story` continuation routes
- triage for an older completed Sprint does not satisfy triage for a newer
  completed Sprint
- existing backlog reset-active and downstream stale behavior remains unchanged

## Acceptance Criteria

- After Sprint close, AgileForge can represent "post-sprint triage pending"
  while remaining in `SPRINT_COMPLETE`.
- `workflow next` is triage-gated in `SPRINT_COMPLETE`.
- Product Backlog refinement is no longer presented as the default next action
  after every completed Sprint.
- Users and agents can record a durable impact decision for the latest completed
  Sprint without creating or depending on a backlog attempt.
- Each impact value has an explicit bridge to the next appropriate workflow
  surface.
- Existing stale guards still block unsafe continuation even when triage says
  the Sprint introduced no new planning impact.
- Existing roadmap and Story evidence remains valid unless triage or stale
  guards route to a reconciliation path that explicitly invalidates it.

## Rejected Alternatives

### New FSM enum: `POST_SPRINT_REVIEW`

Rejected for the first implementation. It would make the concept explicit, but
it would require broader enum, dashboard, command schema, and migration work.
The same behavior can be represented as durable triage state inside
`SPRINT_COMPLETE`.

### Backlog refinement as the default post-sprint path

Rejected because it treats every Sprint lesson as a product backlog change. That
creates unnecessary review churn and makes existing roadmap or Story work look
stale without evidence.

### Copy-only workflow guidance

Rejected as insufficient. Better wording would reduce confusion, but agents
still need a durable routing decision that can be inspected by `workflow next`,
dashboard state, and command responses.

## Assumptions

- Workflow state remains the correct durable home for next-cycle routing
  metadata in the first implementation.
- Existing mutation ledger and idempotency patterns are sufficient for the
  triage command.
- The first implementation can expose CLI/API behavior before adding a full
  dashboard triage form.
