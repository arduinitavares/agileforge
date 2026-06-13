# Stale Story Scope Repair Routing Design

## Problem

Issue #136 is still reproducible on current `master`: when workflow state is `SPRINT_SETUP` and `sprint candidates` reports zero rows because `story_completion_scope` excludes every candidate, `workflow next` still advertises `agileforge sprint generate --project-id <id>` as runnable.

This violates the workflow contract: every command advertised as runnable by `workflow next` must be executable from the same snapshot, or it must be returned as blocked with a concrete reason and remediation.

## Root Cause

`SPRINT_COMPLETE` routing already checks Sprint candidate availability before advertising Sprint generation. `SPRINT_SETUP` routing does not. It uses `_sprint_command_candidates()` as a static command list, so it cannot distinguish normal Sprint setup from stale Story completion scope.

The existing `agileforge story repair-readiness` command is a real guarded repair path for Story planning metadata from `SPRINT_SETUP`, but `workflow next` does not advertise it when stale scope blocks candidates.

## Chosen Approach

Add read-only candidate inspection to `workflow_next()` only for `fsm_state == "SPRINT_SETUP"`.

When all of these are true:

- workflow state is `SPRINT_SETUP`
- `sprint candidates` returns `count=0`
- `excluded_counts.story_completion_scope` is a positive integer
- `story_completion_scope` exists in the candidate projection

Then `workflow next` must:

- remove runnable `agileforge sprint generate --project-id <id>`
- include it in `blocked_commands`
- use reason `STALE_STORY_COMPLETION_SCOPE`
- preserve candidate count, excluded counts, and `story_completion_scope`
- set status `sprint_setup_story_scope_repair_required`
- advertise:
  `agileforge story repair-readiness --project-id <id> --expected-state SPRINT_SETUP --idempotency-key <idempotency_key>`

If the repair command is not installed, it must appear in `blocked_future_commands` instead of `next_valid_commands`.

## Non-Goals

- Do not merge `dev/stale-story-scope-repair-wip`.
- Do not auto-repair from `workflow next`; it must remain read-only.
- Do not alter `story repair-readiness` mutation semantics.
- Do not change post-sprint `SPRINT_COMPLETE` routing.
- Do not regress issue #137 task-update command rendering in `SPRINT_VIEW`.

## Data Flow

1. `AgentWorkbenchApplication.workflow_next()` reads workflow state.
2. If state is `SPRINT_SETUP`, it loads `self.sprint_candidates(project_id=project_id)`.
3. `_sprint_workflow_next()` receives that candidate projection.
4. `_sprint_setup_stale_story_scope_blocker()` detects stale scope only from candidate projection fields.
5. `_sprint_workflow_next()` blocks `sprint generate` and adds guarded `story repair-readiness`.

## Error Contract

Blocked command:

```json
{
  "command": "agileforge sprint generate",
  "reason": "STALE_STORY_COMPLETION_SCOPE",
  "message": "Sprint generation is blocked because the active Story completion scope excludes all current Sprint candidates. Run story repair-readiness to refresh Story planning metadata.",
  "candidate_count": 0,
  "excluded_counts": {"story_completion_scope": 28},
  "story_completion_scope": {"scope": "milestone", "scope_id": "milestone_1"}
}
```

The exact `excluded_counts` and `story_completion_scope` payloads must be copied from the candidate projection without inventing values.

## Tests

Add an application regression that builds a fake read projection with:

- `fsm_state="SPRINT_SETUP"`
- candidate `count=0`
- `excluded_counts.story_completion_scope=28`
- candidate `story_completion_scope.scope="milestone"`

The regression must assert:

- status is `sprint_setup_story_scope_repair_required`
- `sprint generate` is not in `next_valid_commands`
- `sprint generate` is in `blocked_commands` with `STALE_STORY_COMPLETION_SCOPE`
- `story repair-readiness` is in `next_valid_commands`

Run #137 protection by keeping `test_workflow_next_routes_sprint_view_to_execution_commands` passing.

## Acceptance Criteria

- `workflow next` no longer advertises runnable Sprint generation for stale Story scope in `SPRINT_SETUP`.
- The repair command is visible and guarded.
- Existing normal `SPRINT_SETUP` routing still advertises Sprint generation.
- Existing `SPRINT_VIEW` task-update command rendering remains unchanged.
- `pyrepo-check --all` passes.

## Spec Self-Review

- Placeholder scan: no placeholders remain.
- Internal consistency: the status, blocker reason, and repair command are named consistently.
- Scope check: focused on SPRINT_SETUP routing only.
- Ambiguity check: stale scope detection is explicitly tied to `excluded_counts.story_completion_scope > 0` and candidate count zero.
