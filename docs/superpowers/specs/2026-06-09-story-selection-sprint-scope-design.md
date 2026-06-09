# Story Selection Sprint Scope Design

## Problem

AgileForge currently lets Story phase advance to Sprint setup only when either every roadmap requirement is saved or merged, or every requirement in a roadmap milestone is saved or merged. That is too coarse for research-heavy projects. A team may want to implement and learn from a small approved slice before decomposing the next requirements, because later Story requirements can change after the first sprint.

The current behavior pressures users to generate Story drafts too far ahead of execution. That can produce stale or speculative Story work and undermines iterative planning.

## Goal

Allow Sprint planning from an explicit selection of saved parent requirements while preserving Story quality gates and downstream traceability.

The first implementation selects scope by parent requirement, not by individual user story ID.

## Non-Goals

- Do not allow Sprint planning from unsaved or unreviewed Story drafts.
- Do not split an individual saved parent requirement into a partial subset of its user stories.
- Do not change the existing full Story completion flow.
- Do not remove existing milestone-scoped Story completion.
- Do not generate Sprint work directly from roadmap requirements that lack saved Story output.

## User Workflow

After saving one or more parent requirements, the user can complete Story for a selected planning slice:

```bash
agileforge story complete \
  --project-id 3 \
  --expected-state STORY_PERSISTENCE \
  --scope selection \
  --parent-requirement "Technology and Model Research Spike" \
  --parent-requirement "Python Project Scaffold and uv Management Setup" \
  --idempotency-key <idempotency_key>
```

If the selected parent requirements are valid and saved or merged, AgileForge moves to `SPRINT_SETUP` and stores a `story_completion_scope` object that filters Sprint candidates to the selected parent requirements.

## Scope Contract

`story_completion_scope` keeps the existing schema version and adds a new scope value:

```json
{
  "schema_version": "agileforge.story_completion_scope.v1",
  "scope": "selection",
  "scope_id": "selection:<hash>",
  "requirements": [
    "Technology and Model Research Spike",
    "Python Project Scaffold and uv Management Setup"
  ],
  "completed_at": "2026-06-09T00:00:00Z"
}
```

Rules:

- `scope=selection` requires at least one `--parent-requirement`.
- Every selected parent requirement must exist in the roadmap.
- Every selected parent requirement must be saved or merged.
- Duplicate selected requirements are normalized away while preserving roadmap order.
- `scope_id` is derived deterministically from the normalized selected requirement list.
- Reusing the same idempotency key replays the original completion result.
- A caller that wants a different selection must use a new idempotency key.

## Candidate Filtering

Existing Sprint candidate filtering already reads `state["story_completion_scope"]` and filters candidates by `requirements`. The selection scope should reuse that path.

Sprint candidates and Sprint generation must only see user stories whose `source_requirement` belongs to the selected parent requirements.

The candidate payload should expose the scope so users can verify what will be planned:

- `story_completion_scope.scope`
- `story_completion_scope.scope_id`
- selected requirement count
- excluded candidate count

## Dependency Guardrails

Selection-based Sprint planning must not silently create a sprint whose selected stories depend on excluded stories.

Initial rule:

- If any selected candidate has `prerequisite_story_ids` or `blocked_by_story_ids` pointing to a story outside the selected candidate set, Sprint candidates should report readiness `blocked`.
- The blocking code should be explicit, for example `SPRINT_SCOPE_EXTERNAL_DEPENDENCY`.
- The blocked story IDs should be listed in existing readiness diagnostics.
- Sprint generation must refuse to run while candidate readiness is blocked.

This keeps partial planning honest without requiring the user to finish an entire milestone.

## Commands And APIs

CLI:

- Extend `agileforge story complete` with repeatable `--parent-requirement`.
- `--parent-requirement` is only valid with `--scope selection`.
- Existing `--scope milestone --scope-id milestone_0` remains valid.
- Existing full completion with no `--scope` remains valid.

API:

- Extend `StoryCompleteRequest` with `parent_requirements: list[str] = []`.
- Route the list into the Story service.

Application facade:

- Extend `AgentWorkbenchApplication.story_complete(...)`.
- Extend the Story phase runner and command schema so `workflow next` can advertise the installed selection path when saved requirements exist.

UI:

- Story phase should expose a “Plan Sprint from saved selection” path only when at least one parent requirement is saved.
- The UI should make the selected parent requirements visible before completion.
- It should not hide the existing “Complete Story Phase” whole-phase behavior.

## Workflow Next

When the project is in `STORY_PERSISTENCE` and at least one saved or merged parent requirement exists, `workflow next` should include an installed selection completion command such as:

```bash
agileforge story complete --project-id 3 --expected-state STORY_PERSISTENCE --scope selection --parent-requirement <parent_requirement> --idempotency-key <idempotency_key>
```

Milestone completion should still be advertised only when an entire milestone is covered.

Full completion should still be advertised only when all roadmap requirements are covered.

## Error Handling

Selection completion fails without advancing state when:

- selection is empty
- any selected parent requirement is unknown
- any selected parent requirement is not saved or merged
- `--parent-requirement` is provided with a non-selection scope
- `scope=selection` is used together with `--scope-id`

Sprint candidate readiness blocks when selected candidate dependencies point outside the selection.

## Testing

Add regression coverage for:

- completing Story with `scope=selection` for two saved parent requirements advances to `SPRINT_SETUP`
- saved selection stores deterministic `story_completion_scope`
- Sprint candidates are filtered to selected parent requirements
- unknown selected parent requirement is rejected
- unsaved selected parent requirement is rejected
- empty selection is rejected
- `--parent-requirement` with milestone or full completion is rejected
- external candidate dependency sets readiness to blocked and prevents Sprint generation
- `workflow next` advertises selection completion when saved requirements exist
- existing full and milestone completion behavior remains unchanged

## Acceptance Criteria

- User can create a Sprint planning scope from saved parent requirements without completing a whole milestone.
- Sprint candidates include only stories from the selected parent requirements.
- Sprint generation is blocked if selected stories depend on excluded stories.
- Existing full and milestone completion flows continue to pass their tests.
- ASA can proceed from the two saved requirements to Sprint setup without generating the remaining milestone requirements.
