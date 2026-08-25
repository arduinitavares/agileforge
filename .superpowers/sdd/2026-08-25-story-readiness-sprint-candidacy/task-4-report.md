# Task 4 report: selected-scope dependency confirmation and candidacy

## Status

Complete on `dev/issue-223-story-readiness-sprint-candidacy`, starting from
clean Task 3 HEAD `5aeacc0`.

Task 4 now derives Sprint candidacy only from the exact intersection of current
passing v3 structural evidence, durable human `selected` intent, and one current
dependency-safe review for the canonical selected scope. The implementation is
provider-free and preserves partial refinement: one selected eligible Story can
progress while accepted siblings remain unselected and other PBIs remain
unrefined.

## Delivered behavior

- `StoryFact` separately exposes `structurally_eligible`,
  `sprint_selection_state`, `selected_scope_fingerprint`, `dependency_safe`, and
  `sprint_candidate`. Its validator rejects a fabricated candidate unless it is
  selected, eligible, dependency-safe, and bound to a selected-scope
  fingerprint.
- The repository computes the selected scope from exact active,
  non-superseded Stories with current passing v3 evidence and latest durable
  `selected` state. It sorts by Story ID and derives one canonical fingerprint
  from each Story's exact lineage, validation evidence fingerprint, selection
  state fingerprint, and latest selection-event identity/fingerprint.
- `StoryDependencyReview.source_fingerprint` is the sole durable
  `selected_scope_fingerprint` binding. A review is current only when both its
  selected IDs and source fingerprint match the canonical selected scope. No
  compatibility inference exists for older or nonmatching reviews.
- Dependency application mutates only rows owned by dependents in the reviewed
  selected scope. It preserves unrelated rows, allows active prerequisites
  outside the selected scope, and keeps completed external prerequisites visible
  without promoting them into candidacy.
- Missing, incomplete, or malformed external prerequisites block candidacy.
  Selected-scope proposed dependencies, invalid endpoints, cycles, conflicting
  reviews, and malformed projections remain human-readable and fail closed.
- Selection or v3 evidence changes produce a different selected-scope
  fingerprint, invalidating the old dependency review, candidate set, pending
  plan freshness, and Sprint boundary. Durable human selection remains intact
  through evidence staleness and reconciliation, but candidacy waits for a new
  matching dependency review.
- Readiness repair and dependency review now target durable selected intent and
  selected-plus-eligible scope respectively. They no longer use candidacy as
  selection authority.
- Sprint planning request IDs are an exact canonical candidate-scope guard.
  Omitted, extra, reordered, duplicated, or stale IDs fail with
  `SPRINT_CANDIDATE_SET_STALE`; no request can auto-add a Story or bypass durable
  selection.
- Candidate-set and execution dependency fingerprints consume the canonical
  selected-scope fingerprint and dependency rows owned by selected dependents.
  The old project-global dependency graph gate was removed from Sprint-plan
  persistence because it incorrectly blocked a valid selected scope on an
  unrelated future-work proposal. Current repository projection, exact review,
  candidate-set, plan freshness, and start-time dependency snapshot checks still
  recheck the paid boundary and fail closed.
- Read projections required no special serializer: their existing typed
  `StoryFact.model_dump()` paths now expose all five Task 4 Story/candidate
  fields.

## TDD evidence

### RED: candidacy intersection and partial refinement

Committed before production changes in `9a403e4`:

```text
uv run --frozen pytest -q tests/test_sprint_selection.py -k 'story_fact_rejects_candidate_without_dependency_safe_scope or selected_story_waits_for_current_dependency_review_and_reconciliation or one_selected_story_progresses_with_unselected_and_unrefined_backlog'
2 failed, 1 passed
```

The failures showed missing Task 4 fields and the old implicit
selected-plus-eligible candidate derivation.

### RED: exact selected dependency scope

Committed before production changes in `860c239`:

```text
uv run --frozen pytest -q tests/test_story_dependencies.py -k 'selected_scope_review_preserves_unrelated_edges_and_external_visibility or external_prerequisite_blocks_until_complete_without_joining_scope or selection_change_invalidates_review_until_exact_scope_is_confirmed or dependency_review_duplicate_replays_and_changed_payload_conflicts'
3 failed, 1 passed
```

The failures showed that cross-selection external prerequisites were rejected,
unrelated rows could not be preserved under the selected-scope contract, and no
selected-scope fingerprint invalidated stale review evidence. Existing replay
and conflict behavior already passed.

### RED: exact Sprint request guard

Committed before production changes in `dc3e135`:

```text
uv run --frozen pytest -q tests/test_sprint_selection.py -k sprint_input_requires_exact_current_candidate_story_ids
1 failed
```

The failure proved omitted request IDs were still treated as authority to
auto-select the durable candidate set.

### RED: execution consumes selected-scope dependency evidence

Committed before production changes in `b8f2199`:

```text
uv run --frozen pytest -q tests/test_story_dependencies.py::test_external_prerequisite_blocks_until_complete_without_joining_scope
1 failed, 4 warnings in 1.06s
```

The old execution snapshot failed with `Selected Stories are not closed over
active dependencies.` instead of accepting a completed external prerequisite
and binding the canonical selected-scope fingerprint.

## GREEN verification

Focused Task 4 and directly affected workflow/application/API/CLI matrix:

```text
uv run --frozen pytest -q tests/test_story_dependencies.py tests/test_sprint_selection.py tests/workflow/test_planning_transitions.py tests/workflow/test_execution_graph.py tests/workflow/test_graph_properties.py tests/workflow/test_planning_graph.py tests/workflow/test_planning_joins.py tests/services/test_story_sprint_selection.py tests/adapters/test_api_workflow_domain.py::test_planning_selection_derives_dependency_and_readiness_guards tests/adapters/test_api_workflow_domain.py::test_planning_selection_derives_sprint_start_from_accepted_current_plan tests/adapters/test_api_workflow_domain.py::test_sprint_generation_fails_closed_without_host_capacity_input tests/adapters/test_api_workflow_domain.py::test_invalid_manual_sprint_selection_fails_before_model tests/adapters/test_api_workflow_domain.py::test_semantic_sprint_generation_api_is_strict tests/adapters/test_cli_workflow_domain.py::test_semantic_sprint_generation_command_parses tests/adapters/test_cli_workflow_domain.py::test_removed_sprint_generation_flags_fail_parser_validation tests/adapters/test_command_renderer.py::test_sprint_generation_advertises_parser_valid_capacity_remediation
308 passed, 5 warnings in 48.74s
```

The warnings are existing Starlette `TestClient`/httpx, `BaseAgentConfig`, and
pytest-socket warnings.

Changed-file quality:

```text
uv run --frozen pyrepo-check ruff annotations ty services/application.py services/sprint_selection.py services/agent_workbench/sprint_phase.py workflow/definitions/planning.py workflow/handlers/planning.py workflow/execution_integrity.py tests/test_sprint_selection.py tests/adapters/test_api_workflow_domain.py tests/services/test_story_sprint_selection.py tests/workflow/test_planning_transitions.py tests/workflow/test_execution_graph.py tests/workflow/test_graph_properties.py tests/workflow/test_planning_graph.py tests/workflow/test_planning_joins.py
ruff passed; annotations passed; ty passed
```

## Changed files

- `repositories/workflow.py`
- `services/agent_workbench/sprint_phase.py`
- `services/application.py`
- `services/sprint_selection.py`
- `services/story_dependencies.py`
- `workflow/definitions/planning.py`
- `workflow/execution_integrity.py`
- `workflow/facts.py`
- `workflow/handlers/planning.py`
- `workflow/requests/planning.py`
- `tests/test_story_dependencies.py`
- `tests/test_sprint_selection.py`
- `tests/services/test_story_sprint_selection.py`
- `tests/adapters/test_api_workflow_domain.py`
- `tests/workflow/test_execution_graph.py`
- `tests/workflow/test_graph_properties.py`
- `tests/workflow/test_planning_graph.py`
- `tests/workflow/test_planning_joins.py`
- `tests/workflow/test_planning_transitions.py`

`services/read_projections.py` was inspected but did not require a change; its
typed Story projection already serializes the new `StoryFact` fields.

## Commits

- `9a403e4` `test: define selected-scope candidacy contract (#223)`
- `860c239` `test: define selected-scope dependency review (#223)`
- `5d4d00d` `feat: derive candidates from selected dependency-safe scope (#223)`
- `dc3e135` `test: require exact candidate scope at Sprint input (#223)`
- `b8f2199` `test: bind execution to selected dependency scope (#223)`
- `27438a7` `feat: bind Sprint flow to selected candidate scope (#223)`

## Remaining concerns

- Task 6 still owns the full `uv run --frozen pyrepo-check --all` integration
  run and any broader fixture alignment outside the focused Task 4 surface.
- Live provider behavior, UI presentation, and SHA-pinned manual acceptance were
  intentionally not exercised. Those remain human/integration-owned boundaries.
- No real `generate_sprint` workflow transition or Sprint persistence was run.
  Adapter tests stopped before model execution or used existing capturing
  boundaries.

## Protected-boundary confirmation

- No provider adapter call, network-backed model action, real Sprint generation,
  or Sprint persistence occurred.
- No UI process or browser was started.
- No profile was created, read, or mutated.
- No push, merge, issue mutation, or live manual acceptance occurred.
- No schema table or column was added; the implementation reuses
  `StoryDependencyReview.source_fingerprint` as ruled.
- Task 1 remains the authority for current v3 evidence and
  `require_current_story_validation_evidence`.
- Task 3 remains the sole strict append-only Story Sprint-selection parser and
  state map in `services/story_sprint_selection.py`; Task 4 consumes it without
  duplicating selection history parsing or inferring intent.
- Issue #224 team-name/default ownership behavior was not changed.
- No semantic/model-output validation was added.
