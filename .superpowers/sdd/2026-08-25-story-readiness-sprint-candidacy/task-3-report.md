# Task 3 report: append-only human Sprint-selection state

## Status

Complete for the Task 3 boundary on branch
`dev/issue-223-story-readiness-sprint-candidacy`, based on `84e7038`.

Human Sprint-selection is now a durable append-only workflow fact. An accepted,
structurally eligible Story defaults to `unselected`; only a current `selected`
intent can make that exact non-superseded Story a candidate. No table or column
was added.

## Delivered behavior

- Added `WorkflowEventType.STORY_SELECTION_CHANGED` and the central
  `services/story_sprint_selection.py` module. That module alone owns the
  selection types, canonical v1 event schema, strict parsing, state/event
  fingerprints, structural-eligibility derivation, lifecycle lock, and
  in-session append.
- Added deterministic default `unselected` state derived from the exact current
  accepted Story artifact/item and Specification identities.
- Added append-only transitions for `select`, `remove`, and `defer`. Real state
  changes append one canonical `WorkflowEvent`; same-state intent appends no
  event.
- Added strict history validation. Malformed or noncanonical JSON, unexpected
  keys, timestamp mismatch, cross-project binding, wrong exact Story or
  Specification identity, invalid transition chains, and wrong previous-state
  fingerprints fail through `WorkflowFactLoadError`.
- Added the application mutation with SQLite writer serialization and durable
  `WorkflowTransitionReceipt` replay/conflict behavior. Its request fingerprint
  covers project, Story, intent, expected state fingerprint, rationale, actor,
  and correlation ID.
- Added the intent-based API mutation at
  `POST /api/projects/{project_id}/story/sprint-selection`.
- Added explicit CLI verbs at
  `agileforge story sprint-selection select|remove|defer`.
- Added current-evidence gating for `select`. `remove` and `defer` remain usable
  after evidence stales. A preserved selected intent becomes a candidate again
  after successful reconciliation without a duplicate selection event.
- Added lifecycle locking when the exact Story is in an accepted Sprint-plan
  artifact or an active/started Sprint.
- Prevented superseding Story IDs from inheriting state and prevented the
  replaced Story from remaining a candidate.
- Updated `StoryFact` and repository projection so eligibility is not selection:
  eligible Stories default to unselected/not-candidate; selected plus eligible
  Stories are candidates for this slice.

`cli/workflow_commands.py` was intentionally unchanged. Selection is an
operator-owned mutation, not a graph-produced `NodeDecision` request kind;
adding it to `COMMAND_PREFIXES` would claim a workflow renderer contract that
does not exist. The canonical CLI parser and handler live in `cli/main.py` and
are covered by the real-database adapter test.

## TDD evidence

RED was committed before each production increment:

- `ab52ab2` defined default unselected/not-candidate projection.
- `73e0bc8` defined the append-only transition table and reload behavior.
- `9631e85` defined receipt replay, conflict, stale guard, and concurrency.
- `424464d` defined the API and explicit CLI verbs.
- `e110738` defined stale-intent preservation/reactivation and corrupt-history
  failure.
- `21714d7` defined evidence gates, supersession, lifecycle locking, and exposed
  the replaced-Story candidacy defect.

The focused real-database suite covers the complete transition table, reload,
canonical audit metadata, exact binding, default state, current-evidence gate,
stale preservation/reactivation, supersession, corrupt history shapes,
same-state receipts, idempotency conflict, stale optimistic guards, concurrent
identical requests, API/CLI adapters, and lifecycle lock.

## Verification

GREEN focused behavior:

```text
uv run --frozen pytest -q tests/services/test_story_sprint_selection.py tests/test_sprint_selection.py tests/test_story_dependencies.py tests/services/test_story_validation_application.py tests/adapters/test_command_renderer.py
123 passed, 5 warnings in 10.99s
```

Changed-file quality:

```text
uv run --frozen pyrepo-check ruff annotations ty api.py cli/main.py models/enums.py repositories/workflow.py services/application.py services/story_sprint_selection.py tests/services/test_story_sprint_selection.py tests/test_sprint_selection.py workflow/facts.py
ruff passed; annotations passed; ty passed
```

Directly affected `StoryFact` fixture regression set:

```text
uv run --frozen pytest -q tests/test_sprint_metrics.py tests/workflow/test_direct_specification_lineage.py tests/workflow/test_execution_graph.py tests/workflow/test_graph_kernel.py tests/workflow/test_graph_properties.py tests/workflow/test_planning_graph.py tests/workflow/test_planning_joins.py
200 passed, 5 warnings in 4.90s
```

For the full repository command, Ruff, annotations, ty, and Bandit passed. The
pytest phase was stopped after its dependency/plan integration fixtures exposed
the expected next-slice mismatch:

```text
uv run --frozen pyrepo-check --all
pytest: older tests/adapters/test_api_workflow_domain.py planning fixtures create
an accepted eligible Story but do not append human selected intent; candidate
selection is therefore empty and ApplyStoryDependencies rejects
selected_story_ids=().
```

Observed examples include
`test_execution_selection_derives_all_current_durable_identities`,
`test_planning_selection_derives_dependency_and_readiness_guards`,
`test_application_repairs_durable_invalid_story_rank_and_replays`,
`test_planning_selection_derives_sprint_start_from_accepted_current_plan`,
`test_sprint_generation_fails_closed_without_host_capacity_input`,
`test_delivery_review_selection_verifies_each_durable_artifact`, and
`test_explicit_sprint_capacity_locks_exact_durable_cohort`. These are not Task 3
contract failures: they are pending Task 4/6 selected-scope dependency/plan
fixture integration. The run was interrupted at 26% after documenting the
shared cause, per task-owner direction.

Warnings in the green suites are the existing Starlette `TestClient`/httpx and
`BaseAgentConfig` deprecations (plus one existing pytest-socket warning in the
200-test fixture set).

## Commits

- `ab52ab2` test: define default Story selection state (#223)
- `a33addb` feat: separate Story selection from eligibility (#223)
- `73e0bc8` test: define append-only selection transitions (#223)
- `9496ba3` feat: append canonical Story selection events (#223)
- `9631e85` test: define selection replay contract (#223)
- `504f094` feat: make Story selection idempotent (#223)
- `424464d` test: define Story selection adapters (#223)
- `0f2bb81` feat: expose Story selection API and CLI (#223)
- `e110738` test: define stale and corrupt selection behavior (#223)
- `4822a2e` fix: fail closed on corrupt Story selection history (#223)
- `21714d7` test: cover selection gates supersession and lifecycle (#223)
- `4b314d2` fix: exclude superseded Stories from candidacy (#223)
- `08245ca` test: harden Story selection audit coverage (#223)
- `8cc75dc` test: align Sprint fixtures with explicit selection (#223)
- `064eda9` test: type-check Story selection results (#223)
- `3acccf7` test: update Story fact fixtures for selection (#223)

## Protected boundaries and follow-up

- No profiles, frontend, provider code, dependency-review semantics, Sprint
  generation behavior, or team naming were changed.
- Task 4 still owns selected-scope dependency confirmation and using selection
  fingerprints to stale pending dependency/plan evidence. Until that slice,
  this task deliberately treats selected plus structurally eligible as a
  candidate even when current dependency blockers exist.
- Task 4/6 integration fixtures that previously relied on automatic
  eligibility-as-selection must append explicit selection intent before
  dependency review or Sprint planning.
