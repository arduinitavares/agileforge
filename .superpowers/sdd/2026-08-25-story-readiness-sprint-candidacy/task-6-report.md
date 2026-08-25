# Task 6 report: durable #223 contract and provider-free integration

## Status

Complete on `dev/issue-223-story-readiness-sprint-candidacy` from baseline
`cd63f0e`. The worktree is clean after this report commit.

## Durable documentation

- Added `docs/superpowers/specs/2026-08-25-story-readiness-sprint-candidacy-contract.md`.
  It is the compact current contract for v3 structural evidence, explicit
  reconciliation, three-state Story selection, exact selected scope,
  dependency/candidate freshness, API/CLI/UI authority boundaries, historical
  dependency and review/close fingerprints, and the fresh-profile hard break.
- Added a prominent top-level #223 supersession notice to
  `docs/superpowers/specs/2026-06-09-story-selection-sprint-scope-design.md`.
  It explicitly retires parent-requirement selection authority and whole-parent
  scope in favor of durable Story-level intent and partial Story subsets.

## Integration fixes

- Completed-Sprint Story membership now excludes that Story from current
  planning scope and candidacy without clearing durable selection intent. The
  root graph exposes dependency confirmation for a new selected current scope
  after post-Sprint triage.
- Historical execution verification is isolated from later global selection.
  It uses the persisted dependency-review source fingerprint and fingerprints
  every direct selected-dependent row (all statuses) plus the active reachable
  external closure. Review/close fingerprints use immutable accepted-Story
  payloads, terminal tasks, and completion facts.
- The packet boundary now accepts only current v3 structural evidence, and the
  old lifecycle fixtures explicitly select Stories and confirm dependencies
  before exposing a Sprint form. No v2 compatibility branch was added.

## RED evidence and fixes

- `b9c7673` recorded the post-completed-Sprint current-candidacy integration
  gap; `5e59f80` recorded mutation of a future selection changing historical
  Sprint fingerprints.
- `5e5d2e6` recorded that a rejected direct row owned by the started selected
  scope was not fail-closed. Its initial focused RED was
  `1 failed` (`DID NOT RAISE WorkflowFactLoadError`).
- The first full gate then proved the reachable external closure had been
  omitted. The existing closure RED failed in the 777 matrix; the final union
  of direct rows and reachable closure made both contracts green.
- Packet/alignment, graph, and E2E full-gate failures were explicit stale-v2 or
  implicit-selection fixtures. Packet construction itself proved one direct
  production defect: its closed shape still required `ready_for_sprint` despite
  current persisted v3 evidence. The narrow v3 replacement and test migrations
  are covered below.

## Verification

Focused #223 matrix:

```text
uv run --frozen pytest -q <acceptance, validation, selection, dependency,
planning, execution, API, CLI, renderer targets>
777 passed, 5 warnings in 67.91s
```

Historical integrity adjacency:

```text
uv run --frozen pytest -q <reachable-closure and rejected-row REDs,
execution graph/transitions>
63 passed, 5 warnings in 27.04s
```

Integration splits:

```text
tests/test_alignment_evidence_persistence.py tests/test_canonical_packets.py tests/test_packet_renderer.py
79 passed, 5 warnings in 55.29s

tests/workflow/test_single_project_graph.py
3 passed, 5 warnings in 7.90s

tests/e2e/test_single_project_lifecycle_ui.py -k 'dashboard_live_surface_has_no_retired_stage_or_copy or issue_212_delivery_generation_lifecycle_flow'
2 passed, 20 deselected, 4 warnings in 8.64s
```

Required Node and test-owned Playwright commands:

```text
node --test tests/*.mjs
67 passed, 0 failed

uv run --frozen pytest -q tests/e2e/test_single_project_lifecycle_ui.py -k 'progressive or sprint_generation_requires_team or torn_candidate_dependency_scope or dependency_confirmation_stays_locked or dependency_confirmation_replacement_stays_locked or dependency_submission_survives_manual_refresh_race'
7 passed, 15 deselected, 4 warnings in 15.71s
```

The Playwright scenarios used only their test-owned ephemeral server, profile,
browser, and route fake; #223 scenarios did not reach `/sprint/generate`.

Full gate:

```text
uv run --frozen pyrepo-check --all
ruff: passed; annotations: passed; ty: passed; bandit: no issues
pytest: 2474 passed, 1 skipped, 1 deselected, 70 warnings in 468.45s
```

Warnings are existing deprecations/experimental ADK resumability notices and
the blocked-network guard exercise. `git diff --check` passed before this
report and will be rerun after its commit.

## Changed files

- `docs/superpowers/specs/2026-06-09-story-selection-sprint-scope-design.md`
- `docs/superpowers/specs/2026-08-25-story-readiness-sprint-candidacy-contract.md`
- `repositories/workflow.py`
- `services/packets/canonical.py`
- `workflow/definitions/planning.py`
- `workflow/execution_integrity.py`
- `workflow/handlers/planning.py`
- `tests/adapters/test_api_workflow_domain.py`
- `tests/e2e/test_single_project_lifecycle_ui.py`
- `tests/test_alignment_evidence_persistence.py`
- `tests/test_canonical_packets.py`
- `tests/workflow/test_execution_transitions.py`
- `tests/workflow/test_planning_transitions.py`
- `tests/workflow/test_single_project_graph.py`

## Commits

- `b9c7673` `test: expose post-sprint candidacy integrity (#223)`
- `5e59f80` `test: expose historical selection fingerprint regression (#223)`
- `7317bb2` `fix: preserve current and historical Sprint scope (#223)`
- `5e5d2e6` `test: expose historical rejected dependency tamper (#223)`
- `544238c` `fix: bind historical selected dependency rows (#223)`
- `fa00c5f` `test: require v3 validation evidence in packets (#223)`
- `9611da7` `fix: align v3 evidence integration contracts (#223)`
- `935b924` `fix: bind full historical dependency closure (#223)`

## Protected boundaries

No provider calls, real Sprint generation or persistence, manual profile,
manual UI startup, push, merge, issue mutation, or live manual acceptance
occurred. #224 team-name/default terminology was not changed. The only browser
work was the authorized test-owned E2E coverage.

## Review fix round 1: dependency-review lifecycle lock

Commit `9dc8970` adds one shared planning predicate for the dependency-review
lifecycle boundary. `planning.story_dependencies` is blocked, and
`execute_apply_story_dependencies` rechecks and rejects inside its caller-owned
mutation transaction, while an accepted unstarted plan exists, while any Sprint
is active, and while a completed Sprint has not received post-Sprint triage.
The handler uses the same current selected-scope projection as planning rules;
it writes no dependency-review or dependency row while locked. The existing API
test `test_completed_triaged_sprint_exposes_future_dependency_review` remains
the positive proof that a new current selected scope reopens after triage.

The packet schema now names only strict v3 automatic provider-free structural
evidence. The #210 accepted-delivery design retains its historical v2 prose but
has a prominent narrow #223 persisted-evidence supersession link; no unrelated
#210 content was rewritten.

### TDD and focused evidence

```text
RED: uv run --frozen pytest -q tests/workflow/test_planning_graph.py -k \
  'dependency_review_is_locked_until_prior_sprint_is_triaged or \
  dependency_review_reopens_for_current_scope_after_sprint_triage'
3 failed, 1 passed: planned, active, and completed-untriaged states exposed
planning.story_dependencies as AVAILABLE.

RED: uv run --frozen pytest -q tests/workflow/test_planning_transitions.py -k \
  apply_dependencies_rechecks_prior_sprint_lifecycle_before_mutation
3 failed: an otherwise exact ApplyStoryDependencies request returned success in
all three lifecycle states and persisted a review.

GREEN: targeted lifecycle graph/handler/API proof
8 passed, 352 deselected, 5 warnings in 7.52s

GREEN: planning graph and transitions
136 passed, 5 warnings in 35.46s

GREEN: execution graph and transitions
63 passed, 5 warnings in 29.06s

GREEN: API workflow domain
225 passed, 5 warnings in 15.36s

GREEN: packet/renderer/uv-doc static packets
88 passed, 4 warnings in 57.46s

GREEN: CLI workflow domain
129 passed, 4 warnings in 3.96s

GREEN: exact focused #223 Python matrix
777 passed, 5 warnings in 158.68s

GREEN: node --test tests/*.mjs
67 passed, 0 failed

GREEN: authorized test-owned Playwright subset
7 passed, 15 deselected, 4 warnings in 15.38s
```

The Node and Playwright coverage used only test-owned fixtures. The #223 UI
scenarios did not reach `/sprint/generate`; no live/manual UI or profile was
started or touched.

### Full gate and hygiene

```text
uv run --frozen pyrepo-check --all
ruff: passed; annotations: passed; ty: passed; bandit: no issues
pytest: 2480 passed, 1 skipped, 1 deselected, 70 warnings in 478.97s
```

The warnings are the existing FastAPI test-client deprecation, experimental ADK
resumability notices, and the test-owned blocked-network exercise. Changed-file
annotations, ty, and ruff passed before the full gate. `git diff --check` passed
before commit and will be rerun after this evidence commit.

Changed in this round:

- `workflow/definitions/planning.py`
- `workflow/handlers/planning.py`
- `tests/workflow/test_planning_graph.py`
- `tests/workflow/test_planning_transitions.py`
- `docs/task-packet-schema-v3.md`
- `docs/superpowers/specs/2026-08-21-accepted-specification-delivery-contract-design.md`

No provider calls, real Sprint generation/persistence, profile access, manual
UI startup, push, merge, issue mutation, live acceptance, or #224 terminology
changes occurred.
