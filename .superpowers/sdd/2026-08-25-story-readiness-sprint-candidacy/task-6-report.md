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
