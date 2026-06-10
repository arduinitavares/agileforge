# Post-Sprint Learning Triage Design

**Date:** 2026-06-10
**Status:** In Review
**Spec mode:** proposed_change
**Scope:** `SPRINT_COMPLETE` next-cycle routing, durable sprint-learning impact
classification, and stale-guard-aware `workflow next`
**Builds on:**
`docs/superpowers/specs/2026-06-01-backlog-refinement-attempts-design.md`

## Revision History

- 2026-06-10: Initial in-review design.
- 2026-06-10: Revised after review to define bridge postconditions, backlog
  source resolution, planned-Sprint behavior, exact persistence keys,
  stale-guard cases, command response contracts, structured `workflow next`
  actions, and runnable-command acceptance criteria.

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

Storage keys:

- `post_sprint_triage`: latest triage decision for the latest completed Sprint.
- `post_sprint_triage_history`: append-only list of triage decisions and
  guarded corrections, preserved across Sprints.
- `post_sprint_triage_request_fingerprints`: mapping of idempotency key to
  canonical request fingerprint.
- `post_sprint_triage_fingerprint`: canonical fingerprint of the current
  `post_sprint_triage` payload, used for guarded correction.

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
  "request_fingerprint": "sha256:...",
  "triage_fingerprint": "sha256:...",
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
- `request_fingerprint` is computed from project id, sprint id, impact,
  affected fields, learning summary, decision reason, and correction flags. It
  excludes host-owned timestamps and actor fields.
- `triage_fingerprint` is computed from the normalized stored triage payload
  excluding itself.
- A newer Sprint close does not delete older triage decisions. It makes
  `post_sprint_triage_required=true` again because the latest completed Sprint
  id no longer matches `post_sprint_triage.sprint_id`.
- A current `post_sprint_triage` whose `sprint_id` differs from
  `latest_completed_sprint_id` is treated as historical only.

The triage payload is a routing decision, not a mutation of backlog, roadmap,
Story, or Sprint artifacts.

Lifecycle rules:

- `sprint triage` requires current workflow state `SPRINT_COMPLETE`.
- `sprint triage` records or corrects triage metadata only. It does not change
  `fsm_state`.
- `sprint triage` preserves existing backlog attempts, roadmap releases, Story
  runtime, Sprint attempts, saved Sprint rows, `planned_sprint_id`,
  `latest_completed_sprint_id`, stale markers, and sprint close snapshots.
- `sprint triage` clears no planning artifacts. The only writable workflow
  state fields are the triage storage keys and workflow event/audit metadata.
- Bridge commands may later move the workflow to existing states such as
  `STORY_REVIEW`, `ROADMAP_REVIEW`, `BACKLOG_REVIEW`, `SPRINT_DRAFT`, or
  `SPRINT_VIEW`. Those transitions happen in the bridge command, not in
  `sprint triage`.

## Command Surface

### `agileforge sprint review`

Read-only command that summarizes the latest completed Sprint as triage input.

```sh
agileforge sprint review --project-id 3
```

Optional `--sprint-id` selects a completed Sprint. Without it, the command uses
`latest_completed_sprint_id`.

Expected response data:

- `project_id`
- `sprint_id`
- `latest_completed_sprint_id`
- `current_fsm_state`
- `sprint`: status, goal, date range, completed timestamp, close snapshot
  summary
- `completed_stories`: story id, title, source requirement, resolution,
  completed timestamp, known gaps, follow-up notes, evidence links
- `roadmap_summary`: release count, requirement count, pending requirement
  names, saved/merged Story count
- `story_summary`: saved, merged, pending, and blocked requirement counts
- `sprint_runtime_summary`: planned Sprint id, reviewable draft id, active
  Sprint id, latest completed Sprint id
- `stale_guard`: `active`, `reason`, `attempt_id`,
  `active_backlog_reset_attempt_id`, and remediation text
- `post_sprint_triage`: current triage payload when recorded for this Sprint,
  otherwise `null`
- `post_sprint_triage_required`: boolean
- `impact_options`: array of `{impact, label, description, required_fields}`
- `source_fingerprint`

Example response shape:

```json
{
  "project_id": 3,
  "sprint_id": 13,
  "latest_completed_sprint_id": 13,
  "current_fsm_state": "SPRINT_COMPLETE",
  "post_sprint_triage_required": true,
  "post_sprint_triage": null,
  "stale_guard": {
    "active": false,
    "reason": null,
    "attempt_id": null,
    "remediation": []
  },
  "impact_options": [
    {
      "impact": "none",
      "label": "Confirmed plan",
      "description": "Sprint learning does not change backlog, roadmap, or Story work.",
      "required_fields": ["learning_summary", "decision_reason"]
    }
  ],
  "source_fingerprint": "sha256:..."
}
```

The command does not call backlog generation or create backlog attempts.

### `agileforge sprint triage`

Mutating command that records the routing decision for a completed Sprint.

```sh
agileforge sprint triage \
  --project-id 3 \
  --expected-state SPRINT_COMPLETE \
  --impact story \
  --affected-requirement "pyrepo-check Quality Gate Integration" \
  --learning-summary "The research spike confirmed the quality-gate story." \
  --decision-reason "The plan remains valid; only the next pending story needs the spike context." \
  --idempotency-key post-sprint-triage-001
```

Supported flags:

- `--project-id` required
- `--sprint-id` optional, defaults to latest completed Sprint
- `--expected-state SPRINT_COMPLETE` required
- `--impact` required
- `--affected-requirement` repeatable
- `--affected-story-id` repeatable
- `--affected-backlog-item-id` repeatable
- `--affected-roadmap-item-id` repeatable
- `--learning-summary` required
- `--decision-reason` required
- `--idempotency-key` required
- `--replace-existing` optional correction guard
- `--expected-triage-fingerprint` required when `--replace-existing` is used

Idempotency:

- Reusing the same idempotency key with the same body replays the recorded
  triage response.
- Reusing the same idempotency key with a different body fails with the existing
  idempotency mismatch behavior.
- Recording a different triage decision for the same Sprint without
  `--replace-existing` fails with `TRIAGE_ALREADY_RECORDED`.
- Correction is supported only with a new idempotency key,
  `--replace-existing`, and an `--expected-triage-fingerprint` that matches the
  currently stored triage payload.

Successful response data:

```json
{
  "project_id": 3,
  "sprint_id": 13,
  "fsm_state": "SPRINT_COMPLETE",
  "post_sprint_triage": {
    "schema_version": "agileforge.post_sprint_triage.v1",
    "sprint_id": 13,
    "impact": "story",
    "affected_requirements": ["pyrepo-check Quality Gate Integration"],
    "affected_story_ids": [],
    "affected_backlog_item_ids": [],
    "affected_roadmap_item_ids": [],
    "learning_summary": "The research spike confirmed the quality-gate story.",
    "decision_reason": "The plan remains valid; only the next pending story needs the spike context.",
    "request_fingerprint": "sha256:...",
    "triage_fingerprint": "sha256:...",
    "recorded_at": "2026-06-10T00:00:00Z",
    "recorded_by": "cli-agent"
  },
  "idempotency": {
    "replayed": false
  },
  "next_actions_status": "post_sprint_story_continuation_available",
  "source_fingerprint": "sha256:..."
}
```

## Workflow Next Routing

`workflow next` must evaluate post-sprint triage before advertising next-cycle
planning commands from `SPRINT_COMPLETE`.

`workflow next` keeps the existing `next_valid_commands`, `blocked_commands`,
and `blocked_future_commands` fields for compatibility, and adds a structured
`next_actions` array for agent guidance.

Stable `status` values:

- `post_sprint_triage_required`
- `post_sprint_triage_recorded`
- `post_sprint_blocked_by_stale_backlog`
- `post_sprint_backlog_refinement_available`
- `post_sprint_story_continuation_available`
- `post_sprint_roadmap_reconciliation_available`
- `post_sprint_task_followup_blocked`
- `post_sprint_multiple_impacts_need_decision`
- `post_sprint_planned_sprint_blocked_until_triage`
- `post_sprint_planned_sprint_start_available`

Structured action shape:

```json
{
  "id": "post_sprint.story.generate.pyrepo-check-quality-gate-integration",
  "label": "Generate affected Story",
  "command": "agileforge story generate",
  "command_text": "agileforge story generate --project-id 3 --parent-requirement \"pyrepo-check Quality Gate Integration\"",
  "runnable": true,
  "primary": true,
  "impact": "story",
  "target_state": "STORY_REVIEW",
  "reason": "POST_SPRINT_TRIAGE_IMPACT_STORY",
  "remediation": [],
  "blocked_by": []
}
```

Rules:

- Every action with `runnable=true` must also appear in
  `next_valid_commands`.
- Every action with `runnable=false` must appear in `blocked_commands` with the
  same reason and remediation.
- `target_state` names the expected state after successful execution when the
  command is mutating. Read-only commands use `null`.
- `reason` is a stable machine-readable code, not prose.
- `remediation` is a list of concrete commands or required guard values.
- `source_fingerprint` includes `next_actions`.

### Missing Triage

When `fsm_state=SPRINT_COMPLETE` and no triage decision exists for
`latest_completed_sprint_id`, `workflow next` should advertise:

```text
agileforge sprint review --project-id <project_id>
agileforge sprint triage --project-id <project_id> --expected-state SPRINT_COMPLETE --impact <impact> --learning-summary <summary> --decision-reason <reason> --idempotency-key <idempotency_key>
agileforge sprint history --project-id <project_id>
agileforge sprint status --project-id <project_id> --sprint-id <latest_completed_sprint_id>
```

It should not advertise backlog refinement as the default path unless an
existing stale guard already requires backlog reconciliation.

If `planned_sprint_id` or a reviewable Sprint draft already exists, missing
triage still blocks Sprint start/save/generate as primary runnable actions.
Those actions appear as blocked or secondary with status
`post_sprint_planned_sprint_blocked_until_triage` and remediation to record
triage first.

## Bridge Postconditions

`sprint triage` itself never changes `fsm_state`. Bridge commands are the only
commands that may leave `SPRINT_COMPLETE`.

| Impact | State after `sprint triage` | Bridge state transition | Preserved fields | Cleared fields | Runnable primary commands | Blocked or secondary commands |
| --- | --- | --- | --- | --- | --- | --- |
| `none` | `SPRINT_COMPLETE` | `story generate` may move to `STORY_INTERVIEW` or `STORY_REVIEW`; `sprint generate` may move to `SPRINT_DRAFT` only if candidates are ready and no stale guard is active; `sprint start` may move to `SPRINT_VIEW` only for an existing planned Sprint after triage confirms the plan remains valid. | backlog attempts, roadmap releases, Story runtime, Sprint history, planned Sprint id, saved Sprint drafts, stale markers | none | `story pending`; next pending `story generate`; `sprint candidates`; `sprint generate` when candidate readiness is ready; `sprint start` for an already planned Sprint confirmed by triage | backlog refinement; roadmap regeneration; Sprint generation when candidates are not ready |
| `task` | `SPRINT_COMPLETE` | No state transition in v1 unless a future task-carryover command exists. | all planning artifacts and Sprint history | none | `sprint review`; `sprint status --sprint-id`; `sprint history` | task carryover action with reason `TASK_CARRYOVER_NOT_IMPLEMENTED`; backlog, roadmap, Story, and Sprint planning commands unless another impact is recorded |
| `story` | `SPRINT_COMPLETE` | `story generate` may move to `STORY_INTERVIEW` or `STORY_REVIEW`; Story save later moves to `STORY_PERSISTENCE`. | backlog attempts, roadmap releases, unaffected Story runtime, Sprint history, stale markers | none | `story pending`; `story generate` for affected pending requirements; Story retry/refinement for affected requirements with attempts | backlog refinement; roadmap regeneration; Sprint generation until affected Story work is saved or explicitly skipped |
| `roadmap` | `SPRINT_COMPLETE` | `roadmap generate` may move to `ROADMAP_INTERVIEW` or `ROADMAP_REVIEW`; roadmap save later moves to `ROADMAP_PERSISTENCE`. | backlog attempts, Sprint history, stale markers | none | `roadmap generate --input <feedback>`; roadmap history | Story and Sprint planning commands until roadmap reconciliation is reviewed/persisted; backlog refinement unless backlog impact is also selected |
| `backlog` | `SPRINT_COMPLETE` | `backlog refine-record` from `SPRINT_COMPLETE` moves to `BACKLOG_REVIEW` and sets `downstream_backlog_stale=true`, `stale_backlog_reason=refined_backlog_recorded`; later save/reset follows existing backlog guards. | Sprint history, source backlog evidence, roadmap/Story evidence as stale until reconciled | none at triage time | `backlog refine-preview` when a source is available; `backlog refine-record` only when source attempt id and fingerprint are available; `backlog refine-import` when source artifact and expected fingerprint are supplied | Story and Sprint planning; reset-active until replacement is explicitly guarded and approved |
| `multiple` | `SPRINT_COMPLETE` | No layer bridge runs until the user records a guarded correction to one primary impact or an implementation supports grouped execution. | all planning artifacts and Sprint history | none | `sprint review`; guarded `sprint triage --replace-existing --expected-triage-fingerprint <fingerprint>` to select a primary impact | all layer-specific mutating bridges with reason `POST_SPRINT_MULTIPLE_IMPACTS_NEED_DECISION` |

Planned Sprint rule:

- Post-sprint triage is still required when `SPRINT_COMPLETE` has a
  `planned_sprint_id` or a saved/reviewable Sprint draft.
- Before triage, planned Sprint start/save/generate actions are blocked with
  reason `POST_SPRINT_TRIAGE_REQUIRED`.
- After `impact=none`, an existing planned Sprint can be started from
  `SPRINT_COMPLETE` only if the start command accepts
  `--expected-state SPRINT_COMPLETE`, references the planned Sprint id, and no
  stale guard is active.
- After `impact=story`, `roadmap`, `backlog`, or `multiple`, existing planned
  Sprint start remains blocked until the affected layer is reconciled or a
  guarded triage correction records `impact=none`.

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

Backlog source-resolution ladder:

1. Use the latest saveable backlog attempt with a concrete `attempt_id` and
   `artifact_fingerprint`.
2. If no saveable attempt exists, use a canonical active backlog snapshot only
   if AgileForge can produce a deterministic source artifact and fingerprint
   from active backlog rows without mutating state.
3. If the caller supplies `--source-artifact`, allow `refine-preview` and
   `refine-import` to use that artifact with an explicit
   `--expected-source-fingerprint`.
4. If none of the above can produce both a source artifact and fingerprint,
   block backlog refinement actions with reason `BACKLOG_SOURCE_UNAVAILABLE`.

Advertising rules:

- `backlog refine-preview` is runnable when a source attempt is available, a
  canonical active backlog snapshot is available, or `--source-artifact` is
  supplied.
- `backlog refine-record` is runnable only when a source attempt id and source
  fingerprint are available. It must not be advertised as runnable from only a
  local source artifact.
- `backlog refine-import` is runnable only when both source and edited artifact
  paths are supplied or can be named in the command template with an expected
  source fingerprint.
- `workflow next` must not place `refine-record` in `next_valid_commands` when
  source attempt id or source fingerprint is missing.

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

Known stale guard cases:

| Stale reason | State pattern | Primary routing | Blocked commands | Notes |
| --- | --- | --- | --- | --- |
| `refined_backlog_recorded` | `downstream_backlog_stale=true`, `stale_backlog_reason=refined_backlog_recorded`, `stale_since_backlog_attempt_id=<refined_attempt>` | Backlog review/save/reset guidance. `workflow next` should surface backlog review state and replacement guard remediation before Story/Sprint work. | Story generation, Story completion, Sprint candidates, Sprint generation, planned Sprint start | This means a refined backlog attempt has been recorded and downstream artifacts may be stale. It should not route directly to roadmap partial-unblock behavior. |
| `active_backlog_reset` | `downstream_backlog_stale=true`, `stale_backlog_reason=active_backlog_reset`, `stale_since_backlog_attempt_id=active_backlog_reset_attempt_id` | Roadmap regeneration or roadmap persistence flow that clears the reset-stale marker when the roadmap attempt matches the reset attempt. | Story generation, Sprint candidates, Sprint generation, planned Sprint start until reset-stale clearing occurs | This is the known reset-active partial-unblock path. Roadmap generation can be primary; Story/Sprint work remains blocked. |
| other or unknown | `downstream_backlog_stale=true` with any other reason | Block unsafe continuation and show stale reason plus a generic backlog/roadmap reconciliation remediation. | Story generation, Sprint candidates, Sprint generation, planned Sprint start | Unknown stale reasons must fail closed. |

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
- a triage decision already exists for the Sprint and the caller does not pass
  `--replace-existing`
- `--replace-existing` is passed without `--expected-triage-fingerprint`
- `--expected-triage-fingerprint` does not match the stored current triage
  fingerprint

Failure responses should include remediation that points to `sprint review` for
fresh evidence and to the correct required flags for the selected impact.

Named errors:

| Error code | Condition | Remediation |
| --- | --- | --- |
| `TRIAGE_ALREADY_RECORDED` | A triage decision already exists for the Sprint and the request is not a guarded correction. | Re-read `sprint review`, then rerun `sprint triage --replace-existing --expected-triage-fingerprint <fingerprint>` with a new idempotency key if correction is intended. |
| `TRIAGE_FINGERPRINT_MISMATCH` | Guarded correction used a stale or wrong triage fingerprint. | Re-read `sprint review` and retry with the current fingerprint. |
| `TRIAGE_EXPECTED_STATE_MISMATCH` | `--expected-state` is absent or does not match current `SPRINT_COMPLETE`. | Refresh `workflow state`; triage is only valid from `SPRINT_COMPLETE`. |
| `TRIAGE_IMPACT_FIELDS_INVALID` | Required affected fields for the selected impact are missing or forbidden fields are present. | Retry with the affected fields required by the chosen impact. |
| `BACKLOG_SOURCE_UNAVAILABLE` | Backlog impact was selected but no source attempt, canonical active snapshot, or explicit source artifact/fingerprint can be resolved. | Provide a source artifact or create/select a source backlog attempt before recording runnable refinement. |

## Observability And Audit

Recording triage creates a new `WorkflowEventType.POST_SPRINT_TRIAGE_RECORDED`
event. A guarded correction creates another event of the same type with
`metadata.action="post_sprint_triage_corrected"`.

The event includes:

- project id
- sprint id
- impact
- affected fields
- idempotency key
- request fingerprint
- triage fingerprint
- correction flag
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
- `impact=backlog` blocks `refine-record` with `BACKLOG_SOURCE_UNAVAILABLE`
  when no source attempt id and source fingerprint can be produced
- `impact=multiple` groups options without choosing a default layer
- planned Sprint start is blocked before triage and becomes runnable only after
  `impact=none`, matching planned Sprint id, and no stale guard
- active stale guards override `none` and `story` continuation routes
- `stale_backlog_reason=refined_backlog_recorded` routes through backlog
  review/save/reset behavior
- `stale_backlog_reason=active_backlog_reset` routes through roadmap
  regeneration or reset-stale clearing behavior while Story/Sprint remains
  blocked
- triage for an older completed Sprint does not satisfy triage for a newer
  completed Sprint
- guarded triage correction requires `--replace-existing` and
  `--expected-triage-fingerprint`
- `workflow next` emits stable post-sprint status strings and `next_actions`
  with runnable commands mirrored into `next_valid_commands`
- blocked post-sprint actions include stable reason codes and concrete
  remediation
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
- Every command advertised as runnable by `workflow next` is executable against
  the same workflow snapshot. Commands that cannot run from that snapshot are
  returned as blocked actions with a stable reason and concrete remediation.

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
