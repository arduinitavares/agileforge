# Active Backlog Reset Design

**Date:** 2026-06-02
**Status:** Draft
**Spec mode:** proposed_change
**Scope:** Explicit reset of active Product Backlog rows from an approved refined backlog attempt
**Builds on:** `docs/superpowers/specs/2026-06-01-backlog-refinement-attempts-design.md`

## Summary

AgileForge now supports brownfield-aware backlog refinement. In caRtola, that
flow produced a complete and approved refined backlog attempt, but guarded
`backlog save` still fails with `BACKLOG_REPLACEMENT_BLOCKED` because older
stories and Sprint links already exist.

For this class of project, full reconciliation is unnecessary and too complex:
the old backlog predates As-Built analysis and is not a trustworthy baseline.
The desired operation is an explicit Product Owner reset:

```text
approved refined backlog attempt
  -> soft-archive all old active UserStory rows
  -> preserve Sprint/story/task history
  -> create new active backlog_seed rows from the refined attempt
  -> mark downstream artifacts stale against the new baseline
  -> move workflow to BACKLOG_PERSISTENCE
```

This is not ordinary save and not direct edited-file persistence. It is an
explicit, idempotent, host-validated override of the backlog replacement guard
for projects where the Product Owner accepts that the old active backlog should
be retired.

## Problem Statement

`backlog save` protects existing downstream work. It refuses to replace active
backlog rows when those rows have progressed, have acceptance criteria, are
linked to Sprints, or are no longer pure backlog seed rows. That guard is
correct for normal replacement.

caRtola has a different problem:

- the old backlog was generated from a mixed current-state/future-state spec
  before brownfield As-Built assessment existed;
- only some old stories were completed during the first Sprint;
- the remaining active stories are low-trust planning residue;
- the refined backlog attempt is now complete, approved, and brownfield-aware;
- full item-by-item reconciliation would spend effort preserving a baseline the
  Product Owner no longer trusts.

AgileForge therefore needs a reset operation that preserves audit history while
retiring the current active backlog rows and installing the approved refined
attempt as the new active baseline.

## Goals

- Let the Product Owner explicitly accept an approved refined backlog attempt as
  the new active backlog baseline.
- Soft-archive all old active `UserStory` rows, including `Done` stories, without
  deleting rows or historical links.
- Preserve Sprint history, Sprint-story links, story completion logs, task rows,
  and task execution logs.
- Create new active `backlog_seed` rows from the approved refined attempt.
- Record both queryable row-level archive metadata and an audit `WorkflowEvent`.
- Keep reset-archival distinguishable from ordinary save supersession.
- Keep downstream artifacts stale until roadmap/story/sprint artifacts are
  regenerated or acknowledged against the new baseline.
- Make the command idempotent and fingerprint-guarded.
- Fail closed when ordinary `backlog save` would work, so reset remains an
  exceptional override path.

## Non-Goals

- Do not implement full backlog reconciliation.
- Do not map old stories to new backlog items unless a future reconciliation
  feature proves a deterministic mapping.
- Do not delete old stories, tasks, Sprint rows, Sprint-story links, or logs.
- Do not mutate completed Sprint snapshots.
- Do not make reset available for initial backlog creation or normal clean saves.
- Do not bypass approved-attempt recording, artifact fingerprint guards, or
  idempotency.
- Do not clear downstream stale markers as part of reset.

## Proposed Command

```sh
agileforge backlog reset-active \
  --project-id 2 \
  --attempt-id backlog-attempt-12 \
  --expected-artifact-fingerprint sha256:... \
  --expected-state BACKLOG_REVIEW \
  --reset-reason "pre-brownfield backlog reset" \
  --archive-all-active-stories \
  --idempotency-key reset-active-cartola-001
```

`--archive-all-active-stories` is required in Phase 1. This makes the destructive
intent explicit even though the mutation is soft-archive only.

## Preconditions

The command must fail closed unless all conditions hold:

1. `expected_state` is `BACKLOG_REVIEW`.
2. Workflow state is `BACKLOG_REVIEW`.
3. `backlog_review_origin` is `next_cycle_refinement`.
4. `attempt_id` exists in backlog attempt history.
5. Attempt artifact fingerprint equals `expected_artifact_fingerprint`.
6. Attempt artifact is complete.
7. Attempt is a refined attempt or imported refined attempt.
8. Attempt is approved by the host approval boundary.
9. Attempt is `refinement_saveable=true`.
10. `reset_reason` is non-empty.
11. `archive_all_active_stories=true`.
12. Request idempotency key is non-empty.
13. Existing active backlog rows are blocked by the normal replacement guard.

If normal `backlog save` would succeed, `reset-active` must fail with guidance to
use ordinary `backlog save`. This avoids two persistence paths for the trivial
case.

## Mutation Semantics

Within one transaction, reset performs:

1. Load the approved refined backlog artifact.
2. Project savable backlog items by stripping host-only metadata.
3. Validate projected items with the existing `BacklogItem` schema.
4. Find all active `UserStory` rows for the project:
   `is_superseded = false`.
5. Soft-archive every active story row:
   - set `is_superseded = true`;
   - leave `status` unchanged;
   - leave `superseded_by_story_id = NULL` unless a future deterministic 1:1
     mapping exists;
   - set reset archive metadata columns.
6. Create new active `UserStory` rows from the refined attempt:
   - `story_origin = "backlog_seed"`;
   - `is_superseded = false`;
   - title/rank/source fields follow the existing `save_backlog_tool`
     projection semantics;
   - no story refinement or Sprint planning occurs.
7. Write a `WorkflowEvent` with
   `event_type = WorkflowEventType.BACKLOG_SAVED` and
   `action = "active_backlog_reset"`.
8. Update workflow state:
   - `fsm_state = "BACKLOG_PERSISTENCE"`;
   - `backlog_saved_at = now`;
   - `downstream_backlog_stale = true`;
   - `stale_backlog_reason = "active_backlog_reset"`;
   - `stale_since_backlog_attempt_id = <reset attempt id>`;
   - `active_backlog_reset_at = now`;
   - `active_backlog_reset_attempt_id = <reset attempt id>`.

Reset unblocks backlog persistence. It does not make existing downstream
roadmap/story/sprint artifacts current. Those artifacts now reference
superseded stories and must regenerate or explicitly acknowledge the new
baseline later.

## Data Model

Reuse existing `UserStory` fields:

```text
is_superseded
superseded_by_story_id
status
completion_notes
completed_at
```

Add nullable archive metadata columns to `user_stories`:

```text
archived_reason: str | null
archived_at: datetime | null
archived_by: str | null
archive_reset_attempt_id: str | null
archive_previous_status: str | null
```

Rules:

- all new columns are nullable with default `NULL`;
- migration is additive only;
- existing rows are not transformed during migration;
- `status` is not overwritten during reset;
- `archive_previous_status` snapshots the story status at reset time;
- `archived_by` is recorded from the host command boundary and defaults to
  `"po"` in Phase 1;
- `archived_reason = "active_backlog_reset"` identifies reset-archived rows;
- ordinary save supersession may continue to set `is_superseded=true` without
  setting `archived_reason`;
- reset-archived rows are queryable with:

```text
is_superseded = true
AND archived_reason = "active_backlog_reset"
AND archive_reset_attempt_id = <attempt_id>
```

WorkflowEvent remains required for audit, but row columns are the queryable
source of truth for active-baseline filtering and reset archive inspection.

## WorkflowEvent Payload

The command records a `WorkflowEvent` using existing
`WorkflowEventType.BACKLOG_SAVED` with:

```json
{
  "action": "active_backlog_reset",
  "project_id": 2,
  "attempt_id": "backlog-attempt-12",
  "artifact_fingerprint": "sha256:...",
  "reset_reason": "pre-brownfield backlog reset",
  "archived_story_ids": [1, 2, 3],
  "created_story_ids": [41, 42, 43],
  "archived_count": 21,
  "created_count": 13,
  "idempotency_key": "reset-active-cartola-001"
}
```

The event is audit evidence, not the primary projection for determining whether
a story is active or reset-archived.

Because reset reuses the `BACKLOG_SAVED` event type, all readers must filter by
`event_metadata.action`, not event type alone:

- normal backlog-save idempotency replay must replay only
  `action = "backlog_saved"`;
- backlog-reconcile idempotency replay must replay only
  `action = "backlog_reconciled"`;
- backlog-reconcile saved-count inference must count only
  `action = "backlog_saved"`;
- reset-active idempotency replay must replay only
  `action = "active_backlog_reset"`.

`active_backlog_reset` events must not be interpreted as ordinary saves or
reconcile events.

## Idempotency

The request fingerprint includes:

- command name;
- project id;
- attempt id;
- expected artifact fingerprint;
- expected state;
- reset reason;
- archive-all-active-stories flag;
- approved artifact fingerprint from workflow state.

Same idempotency key and same request fingerprint returns the prior result.
Same key with different request fingerprint fails with reused-key error. Replay
must not archive again or create duplicate seed rows.

## History Preservation

Reset is soft archive only. Acceptance tests must prove:

- old story rows still exist after reset;
- old `Done` story status is preserved on archived rows;
- old `archive_previous_status` captures pre-reset status;
- `SprintStory` links still point to archived story rows;
- completed Sprint list/detail remains queryable;
- `Sprint.close_snapshot_json` remains unchanged;
- `StoryCompletionLog` rows remain queryable;
- `Task` rows and `TaskExecutionLog` rows remain queryable;
- no task rows are deleted by reset;
- active backlog queries exclude archived rows.

This is the critical safety boundary for Option A′. If any history surface
breaks, reset must not ship.

## Workflow Next Semantics

After reset:

```text
fsm_state = BACKLOG_PERSISTENCE
downstream_backlog_stale = true
stale_backlog_reason = active_backlog_reset
stale_since_backlog_attempt_id = backlog-attempt-12
```

`workflow next` should not route to Sprint planning. It should guide toward
roadmap regeneration or whatever command refreshes downstream artifacts from the
new backlog baseline.

The next valid action should be equivalent to:

```text
agileforge roadmap generate --project-id 2
```

Roadmap generation is the stale-exit path for reset. A stale marker with
`stale_backlog_reason = "active_backlog_reset"` must not block
`roadmap generate` when the workflow is in `BACKLOG_PERSISTENCE` and the active
backlog baseline is the reset attempt. Story and Sprint generation still block
until a new roadmap has been generated and saved against that baseline.

This requires changing the generic stale guard or the roadmap generation entry
point. The current helper blocks roadmap, story, and sprint generation
unconditionally whenever `downstream_backlog_stale = true`. Reset-active must add
a roadmap-only exception:

```text
allow roadmap generate when:
  fsm_state = BACKLOG_PERSISTENCE
  downstream_backlog_stale = true
  stale_backlog_reason = active_backlog_reset
  stale_since_backlog_attempt_id = active_backlog_reset_attempt_id
```

The same marker must continue to block story and sprint generation.

After the regenerated roadmap is saved, AgileForge may clear
`downstream_backlog_stale` or update the marker to the next stale boundary that
still needs regeneration. It must not silently reuse pre-reset roadmap/story/sprint
artifacts as current.

## Failure Modes

| Code | Meaning |
|---|---|
| `RESET_ATTEMPT_NOT_FOUND` | `attempt_id` is not in backlog attempt history |
| `RESET_ARTIFACT_FINGERPRINT_MISMATCH` | expected artifact fingerprint does not match attempt |
| `RESET_ATTEMPT_INCOMPLETE` | attempt artifact is incomplete |
| `RESET_ATTEMPT_NOT_APPROVED` | attempt lacks host-mediated approval |
| `RESET_NOT_REFINEMENT_ATTEMPT` | attempt kind is unsupported |
| `RESET_WRONG_REVIEW_ORIGIN` | workflow is not a next-cycle refinement review |
| `RESET_NOT_REQUIRED` | ordinary `backlog save` would not be replacement-blocked |
| `RESET_REASON_REQUIRED` | reset reason is blank |
| `RESET_ARCHIVE_FLAG_REQUIRED` | `--archive-all-active-stories` missing |
| `RESET_IDEMPOTENCY_CONFLICT` | same idempotency key used with different request |
| `RESET_HISTORY_PRESERVATION_FAILED` | postcondition check detects missing history rows |

## Acceptance Criteria

1. Command schema exposes `agileforge backlog reset-active` as mutating,
   non-destructive, and idempotency-required.
2. Reset fails without `attempt_id`, expected fingerprint, expected state,
   reset reason, archive-all flag, and idempotency key.
3. Reset fails when `backlog_review_origin` is not `next_cycle_refinement`.
4. Reset fails if the attempt is not complete.
5. Reset fails if the attempt is not approved.
6. Reset fails if normal `backlog save` would not be replacement-blocked.
7. Reset soft-archives every active pre-reset story row, including `Done` rows.
8. Reset never deletes UserStory, Sprint, SprintStory, Task, StoryCompletionLog,
   TaskExecutionLog, or WorkflowEvent rows.
9. Reset creates new active `backlog_seed` rows from the approved refined
   attempt.
10. Reset stamps archive metadata columns on archived rows.
11. Reset leaves archived row `status` unchanged and snapshots it into
    `archive_previous_status`.
12. Reset records `WorkflowEventType.BACKLOG_SAVED` with
    `metadata.action = "active_backlog_reset"`.
13. Existing save/reconcile replay and saved-count readers ignore
    `active_backlog_reset` except in reset-active replay.
14. Reset moves workflow to `BACKLOG_PERSISTENCE`.
15. Reset keeps `downstream_backlog_stale = true`.
16. Reset sets `stale_since_backlog_attempt_id` to the reset attempt id.
17. `workflow next` after reset does not route to Sprint planning.
18. `workflow next` after reset routes to roadmap regeneration from the reset
    backlog baseline.
19. Roadmap generation is not blocked by the reset stale marker, but story and
    sprint generation remain blocked until downstream artifacts are regenerated
    or explicitly acknowledged against the reset baseline.
20. Same idempotency request replays without duplicate story creation.
21. Same idempotency key with different fingerprint fails.
22. Completed Sprint history remains visible and linked to the archived stories.

## Closed Decisions

- `archived_by` is recorded from the host command boundary and defaults to
  `"po"` in Phase 1. There is no separate `--archived-by` option in the first
  implementation slice.
- Reset-active is allowed only from `BACKLOG_REVIEW` when
  `backlog_review_origin = "next_cycle_refinement"`.
- Roadmap generation is the first stale-exit path after reset. Story and Sprint
  generation remain blocked until downstream artifacts are regenerated or
  explicitly acknowledged against the reset baseline.

## Deferred Questions

- After roadmap save, should AgileForge clear `downstream_backlog_stale`
  entirely or update it to a narrower story/sprint stale boundary?
- Should a later admin-only reset support non-`next_cycle_refinement` cases?

These deferred questions do not block the core design.

## Implementation Boundaries

The implementation plan should be limited to:

- additive schema support for archive metadata;
- one reset-active CLI/service mutation with idempotency;
- one transaction that archives old active stories and creates new seed rows;
- workflow-next routing for roadmap regeneration after reset;
- tests for history preservation, idempotency replay/conflict, and stale
  downstream blocking.

Full reconciliation, item mapping, and versioned backlog save remain out of
scope.
