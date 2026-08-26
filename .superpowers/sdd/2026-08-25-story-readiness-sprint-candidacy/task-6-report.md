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

## Final whole-branch review fix

The final #223 review RED was committed first as `2d949f4`
(`test: capture final #223 review regressions`). The GREEN implementation is
`52c13d4` (`fix: close final selection contracts (#223)`).

### Contract changes

- Dependency planning now takes `selected_scope_stories(snapshot)` as its sole
  current-scope authority. The dependency projection carries the exact
  `selected_story_ids` and selected-scope fingerprint; the browser parses and
  submits those projected values rather than recalculating them. Completed
  Sprint Story A can retain historical selected intent while only future Story
  B is in the next current scope.
- The selection writer returns a post-event repository `StoryFact`, including
  the current `dependency_safe` and `sprint_candidate` truth. Same-state
  responses also use that authoritative projection.
- Every selection event has a required, completed
  `WorkflowTransitionReceipt` anchor. Integrity verifies the canonical request
  JSON/fingerprint, identity, actor and intent metadata, expected prior-state
  fingerprint, result event identity/fingerprint/state, and lineage. Missing,
  malformed, mismatched, and coherent-tail rewrite anchors fail closed. The
  transaction creates the pending receipt before the event, completes it with
  the canonical result, and rolls both back together on failure.
- Story selection/reconciliation uses a token-bound browser mutation phase.
  A successful POST followed by a failed authoritative reload, including 409,
  keeps stale controls locked until a matching successful current load; a
  pre-commit POST failure restores the prior controls.
- Direct Story API and CLI reads removed `ready_for_sprint` and conflated
  validation fields. They expose canonical structural eligibility/evidence,
  selection state/event and scope fingerprints, dependency safety, candidacy,
  and blockers. Malformed projections fail closed.
- `services/story_evidence_scope.py` is the stable provider-free disclosure:
  it proves exact Story identity; immutable accepted artifact/item binding;
  accepted Backlog and Specification lineage; parent-bounded Specification
  references; required Story shape; non-empty acceptance criteria; and current
  evidence/input fingerprints. It does not prove semantic/model quality,
  product value, human Sprint selection, dependency safety, Sprint candidacy,
  or Sprint-generation readiness. The API, CLI, direct/candidate/dependency
  reads, and browser use the same exact lists.

### RED and GREEN evidence

```text
RED (committed before production): 2d949f4
- next-Sprint projected-scope API/browser path;
- canonical mutation response/reload truth;
- missing, malformed, mismatched, and coherent-tail receipt anchors;
- selection 409 lock/recovery/double-click path;
- direct Story API/CLI hard-break fields and exact disclosure.

GREEN: uv run --frozen pytest -q \
  tests/services/test_story_sprint_selection.py \
  tests/services/test_story_validation_application.py \
  tests/services/test_durable_product_definition_projections.py \
  tests/test_story_dependencies.py tests/test_sprint_selection.py \
  tests/workflow/test_planning_transitions.py \
  tests/adapters/test_api_workflow_domain.py
489 passed, 5 warnings in 67.96s

GREEN: node --test tests/*.mjs
67 passed, 0 failed

GREEN: authorized test-owned Playwright subset
uv run --frozen pytest -q tests/e2e/test_single_project_lifecycle_ui.py -k \
  'progressive or sprint_generation_requires_team or \
  torn_candidate_dependency_scope or dependency_confirmation_stays_locked or \
  dependency_confirmation_replacement_stays_locked or \
  dependency_submission_survives_manual_refresh_race or \
  completed_sprint_next_scope or story_selection_stays_locked_through_409'
9 passed, 15 deselected, 4 warnings in 18.83s

GREEN: uv run --frozen pyrepo-check --all (clean commit 52c13d4)
ruff: passed; annotations: passed; ty: passed; bandit: no issues
pytest: 2488 passed, 1 skipped, 1 deselected, 70 warnings in 461.44s
```

The first full-gate attempt was intentionally retained as diagnostic evidence:
from the dirty pre-GREEN checkout, only
`tests/test_ci_launcher_smoke.py::test_real_script_runs_complete_launcher_lifecycle`
and
`tests/test_ci_launcher_smoke.py::test_real_pre_identity_failure_cleans_process_group_and_profiles`
failed. A no-UI diagnostic `./agileforge-dev init` returned
`acceptance checkout must be clean` before profile creation. The clean-head
rerun above passed both tests, proving an acceptance-mode checkout-cleanliness
precondition rather than a #223 production defect. No packaging configuration
was changed.

### Lean dispositions and changed files

`rg` found no production caller for the retired global `DependencyGraph`
load/inspect/assert pipeline, so it and its helper-only tests were deleted.
`DependencyGraphIssue`, `StoryDependencyGraphError`,
`detect_dependency_cycles`, and current mutation validation remain. The dead
frontend `canonicalCandidateDependencies` helper and its helper-only tests were
also removed; render/path proofs remain. `SelectedScopeStory` direct-lineage
fields were retained as auditable scope identity, and concurrency coverage was
not reduced.

Changed in this final round:

- `services/application.py`, `services/read_projections.py`,
  `services/story_dependencies.py`, `services/story_evidence_scope.py`, and
  `services/story_sprint_selection.py`
- `frontend/project.js`
- `docs/superpowers/specs/2026-08-25-story-readiness-sprint-candidacy-contract.md`
- `tests/adapters/test_api_workflow_domain.py`,
  `tests/e2e/test_single_project_lifecycle_ui.py`,
  `tests/services/test_durable_product_definition_projections.py`,
  `tests/services/test_story_sprint_selection.py`,
  `tests/services/test_story_validation_application.py`,
  `tests/test_sprint_selection.py`, `tests/test_story_dependencies.py`,
  `tests/test_workflow_position_display.mjs`, and
  `tests/workflow/test_planning_transitions.py`

Warnings are existing FastAPI test-client deprecation, experimental ADK
resumability, and the test-owned blocked-network exercise. The E2E work was
test-owned only; #223 scenarios did not reach `/sprint/generate`. No provider
call, protected/source profile action, manual/live UI start, real Sprint
generation or persistence, push, merge, issue mutation, or live manual
acceptance occurred.

## Independent review fix round 2: final authority boundaries

The second independent review found five Important defects that green tests had
not exposed: dependency confirmation silently replaced a stale observed scope
fingerprint; browser Sprint generation omitted the exact candidate IDs; a
successful selection receipt could replay mutable or tampered result data;
later current dependency edits could rewrite historical transitive closure; and
the old automatic Sprint selector was unreachable dead code behind the exact-ID
guard. RED `41d25a2` (`test: capture final authority regressions (#223)`) records
those gaps before production changes. GREEN `5a42093`
(`fix: close final authority gaps (#223)`) closes them.

### Contract changes

- API, CLI, application replay input, and browser dependency submission now
  require the exact observed `selected_scope_fingerprint`. Pre-dispatch and
  transactional checks reject a same-ID scope whose evidence or selection
  identity changed; no layer substitutes the newest fingerprint.
- Browser Sprint-plan generation submits the exact projected candidate IDs and
  blocks transport if candidate and dependency projections disagree.
- Successful selection receipts have one closed immutable result schema. They
  contain only Project, Story, resulting state/state fingerprint, and optional
  event identity. Replay validates canonical request input, no-op state, or the
  exact anchored event. Mutable eligibility, dependency, and candidacy fields
  are reloaded from the repository rather than persisted in the receipt.
- Sprint-start audit metadata now stores the canonical immutable union of all
  direct selected-dependent rows and the active reachable external-prerequisite
  closure. Historical execution integrity reads that snapshot, while current
  dependency rows remain editable for a later scope. Row order, identity,
  endpoints, Project ownership, and fingerprint fail closed on tampering.
- Empty Sprint selection is rejected. The unreachable automatic selector,
  dependency promotion helpers, and helper-only tests were deleted; exact human
  candidate IDs are the only supported input.

### RED and focused GREEN evidence

```text
RED: historical dependency mutation/snapshot packet
2 failed: Sprint-start evidence had no immutable row snapshot, and removing a
later B -> C row rewrote an earlier A -> B -> C execution contract.

RED: observed-scope API/CLI/application packet
4 failed, 4 passed: omission and same-ID stale-scope requests were still
accepted or lacked an explicit transport requirement.

RED: receipt authority packet
2 failed: an extra result field and an allowed-field transition rewrite replayed.

RED: node --test tests/*.mjs
2 failed: dependency mutation omitted the observed fingerprint and Sprint
generation omitted exact projected candidate IDs.

RED: test-owned Playwright packets
1 failed in each affected packet: the browser dependency/Sprint transports did
not carry the new exact authority fields. The zero-provider backend guard passed.

GREEN: authority/API/CLI/selection/dependency matrix
448 passed, 5 warnings in 33.83s

GREEN: planning and immutable execution-history matrix
286 passed, 5 warnings in 73.80s

GREEN: node --test tests/*.mjs
69 passed, 0 failed

GREEN: authorized test-owned Playwright subset
9 passed, 15 deselected, 4 warnings in 18.92s

GREEN: changed-file ruff and ty; git diff --check
passed
```

The Playwright work used only its ephemeral test server, profile, browser, and
route fake. It made no provider call and persisted no real Sprint. No protected
profile, manual/live UI, push, merge, issue mutation, #224 terminology, or live
manual acceptance action was touched.

## Completion-gate and final correctness fixes

The first clean full-gate attempt after review round 2 found one migration gap:
the CLI parser required the observed selected-scope fingerprint, but
`workflow next` still rendered the old dependency command. The aggregate result
was `2491 passed, 1 failed, 1 skipped, 1 deselected`; the only failure was the
command-renderer parser contract. The test was tightened first to require the
exact placeholder. Commit `6f33fee` (`fix: render selected scope guard (#223)`)
adds that task-specific field to the existing renderer and documents why it is
operator-observed scope rather than an internal graph guard.

Focused renderer GREEN:

```text
tests/adapters/test_command_renderer.py
39 passed, 4 warnings

focused dependency CLI
4 passed, 125 deselected, 4 warnings

CLI documentation contracts
16 passed, 4 warnings

changed-file ruff, ty, and git diff --check
passed
```

The final correctness re-review then found that browser candidate items preserve
rank order while the backend exact guard requires numeric Story-ID order. It
also proved that a torn candidate subset sharing the selected-scope fingerprint
was accepted locally. Both browser REDs failed before production changes:

```text
Sprint generation rejects a torn candidate vector within one selected scope
failed: canGenerateSprintPlan returned true

Sprint generation submits the exact projected candidate IDs
failed: sent [103, 101], backend canonical vector is [101, 103]
```

Commit `6faedb7` (`fix: canonicalize browser Sprint candidates (#223)`) sorts
only the transport guard IDs and exact-cross-checks them against the candidacy
flags in the current dependency projection. Candidate display order remains
rank-based. GREEN:

```text
targeted browser authority tests
2 passed

node --test tests/*.mjs
70 passed, 0 failed

authorized test-owned Playwright subset
9 passed, 15 deselected, 4 warnings

git diff --check
passed
```

The interrupted aggregate rerun after `6f33fee` is not completion evidence; it
was stopped once the independent reviewer confirmed the browser defect. A new
clean-head full gate follows the final review-fix documentation commit. No
provider, protected profile, real Sprint, live/manual UI, or remote mutation was
used during either fix.

Independent re-review verdicts after the fixes:

- Specification: APPROVED, no Important findings.
- Correctness: APPROVED after `6faedb7`, no remaining Important findings.
- Lean scope: APPROVED, no Important bloat or dead paths.

The specification/lean reviewer ran a 621-test provider-free branch matrix,
Node contract coverage, and bounded delta checks. The correctness reviewer ran
the focused unchanged domain matrix, full Node coverage, both candidate-vector
regressions, and exact diff checks. Reviewers made no repository or external
state changes.
