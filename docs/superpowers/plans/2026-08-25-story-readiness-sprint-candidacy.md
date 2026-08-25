# Story Readiness and Sprint Candidacy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Separate automatic provider-free Story structural eligibility from explicit, reversible human Sprint selection while preserving exact lineage, idempotency, stale-state safety, and partial refinement.

**Architecture:** Exact Story acceptance writes canonical v3 structural evidence inside its existing transaction. Human selection is an append-only canonical fact stored in `workflow_events`; projections independently derive eligibility, selection, dependency safety, and final candidacy. Dependency review and Sprint generation bind to the exact selected-and-eligible scope fingerprint and never infer selection.

**Tech Stack:** Python 3.13, SQLModel/SQLite, Pydantic v2, FastAPI, argparse CLI, vanilla JavaScript, Node test runner, pytest, Playwright.

**Spec:** `docs/feedback/2026-08-24-story-refinement-to-sprint-selection-design-handoff.md`

## Global Constraints

- Base and implementation SHA lineage starts at `25b4ac6d32796ac86e31edb7390495ad22a52f83`.
- Provider-free structural rules run in the exact acceptance transaction; no provider calls or semantic validation.
- Expected rule failures commit accepted Stories and diagnostics atomically; unexpected evaluator, persistence, or transaction failures roll back acceptance and evidence together.
- Human selection states are `unselected`, `selected`, and `deferred`; no state transfers to a superseding Story.
- Intent transitions are exact: `select` maps `unselected|deferred` to `selected`; `remove` maps `selected|deferred` to `unselected`; `defer` maps `unselected|selected` to `deferred`; applying an intent already represented by the current state is a receipt-backed no-op with no duplicate event.
- Selection requires canonical current v3 evidence for the exact accepted Story version. Removing or deferring remains allowed after evidence becomes stale.
- Preserved `selected` intent becomes candidacy-eligible again only after canonical v3 evidence is restored.
- No database table or column migration. Selection facts use the existing append-only `workflow_events` table.
- Legacy v2 evidence is stale. Reconciliation is explicit, provider-free, idempotent, and never infers human intent.
- The candidate equation is `selected AND structurally_eligible AND dependency_safe`.
- Dependency confirmation binds to the exact selected-and-eligible scope fingerprint.
- Selection events, eligibility evidence, or selected-scope membership changes produce a new selected-scope fingerprint and make prior dependency confirmation and pending Sprint plans stale. Reconciliation includes the canonical evidence fingerprint, so restored evidence cannot revive an old dependency decision accidentally.
- #224 owns team naming and defaults; do not change them.
- Do not auto-accept output, generate a Sprint, make provider calls, push, merge, close issues, or start manual acceptance.

---

### Task 1: Canonical v3 structural evidence and automatic acceptance checks

**Files:**
- Modify: `utils/spec_schemas.py`
- Modify: `services/specs/story_validation_service.py`
- Modify: `services/agent_workbench/story_phase.py`
- Test: `tests/test_story_validation_service.py`
- Test: `tests/services/test_story_validation_application.py`
- Test: `tests/test_story_validation_pinning.py`

**Interfaces:**
- Produces: `ValidationEvidence.schema_version == "agileforge.story-validation-evidence.v3"` and `structurally_eligible: bool`.
- Produces: `require_story_validation_evidence(...)` rejects v2, mismatched validator version, or mismatched accepted Story fingerprints.
- Produces: accepted Story materialization runs `validate_story_with_specification_in_session(session, {"story_id": story_id, "mode": "structural"}, now=lambda: accepted_at)` before the acceptance transition commits.

- [ ] **Step 1: Write RED tests for the v3 closed evidence contract.**

```python
assert evidence.schema_version == "agileforge.story-validation-evidence.v3"
assert evidence.structurally_eligible is True
with pytest.raises(ValueError):
    ValidationEvidence.model_validate_json(legacy_v2_json)
```

- [ ] **Step 2: Run the focused service tests and confirm failure because v2/`ready_for_sprint` is still produced.**

Run: `uv run --frozen pytest -q tests/test_story_validation_service.py`

- [ ] **Step 3: Implement the v3 schema, canonical serialization, and validator-version freshness check.**

```python
class ValidationEvidence(BaseModel):
    schema_version: Literal["agileforge.story-validation-evidence.v3"]
    structurally_eligible: bool
```

- [ ] **Step 4: Write RED acceptance tests proving every activated Story receives evidence without a validation request, rule failures persist, infrastructure failures roll back, and exact replay preserves one timestamp.**

```python
result = domain.transition(request=accept_request)
story = session.get(UserStory, result.activated_story_ids[0])
assert ValidationEvidence.model_validate_json(story.validation_evidence)
```

- [ ] **Step 5: Run the acceptance tests and confirm failure because materialized rows still contain `validation_evidence=None`.**

Run: `uv run --frozen pytest -q tests/test_story_validation_pinning.py tests/services/test_story_validation_application.py`

- [ ] **Step 6: Call the in-session structural evaluator after each materialized Story and update concurrent-winner verification to require exact current evidence.**

- [ ] **Step 7: Run the three focused suites and keep them green.**

Run: `uv run --frozen pytest -q tests/test_story_validation_service.py tests/test_story_validation_pinning.py tests/services/test_story_validation_application.py`

### Task 2: Explicit idempotent reconciliation and legacy validation hard rename

**Files:**
- Modify: `services/application.py`
- Modify: `api.py`
- Modify: `cli/main.py`
- Modify: `cli/workflow_commands.py`
- Test: `tests/services/test_story_validation_application.py`
- Test: relevant API adapter tests under `tests/`
- Test: relevant CLI adapter tests under `tests/`

**Interfaces:**
- Produces: `StoryEligibilityReconcileRequest(project_id, story_ids, idempotency_key, actor, correlation_id)`.
- Produces: `ApplicationService.reconcile_story_eligibility(request) -> JsonObject`.
- API: `POST /api/projects/{project_id}/story/structural-eligibility/reconcile`.
- CLI: `agileforge story eligibility reconcile --story-id N` with repeatable `--story-id`; omitting IDs reconciles all active accepted Stories.
- Removes: `/story/validate`, `agileforge story validate`, and the `validate_story` application contract. No alias remains.

- [ ] **Step 1: Write RED tests for all-active and explicit-subset reconciliation, legacy v2 replacement, current-v3 no-op behavior, replay, conflict, and rollback.**

```python
first = service.reconcile_story_eligibility(request)
replay = service.reconcile_story_eligibility(request)
assert replay == first
assert current_evidence.validated_at == original_validated_at
```

- [ ] **Step 2: Write RED API/CLI tests for the canonical route/command and rejection of the removed legacy route/command.**

- [ ] **Step 3: Run the focused tests and confirm the new contracts are absent.**

- [ ] **Step 4: Implement one writer-lock/receipt transaction that canonicalizes story IDs, skips current v3 evidence byte-for-byte, re-evaluates missing/stale evidence, and rolls back on infrastructure failure.**

- [ ] **Step 5: Implement the API/CLI hard rename and exact operator-facing output explaining what structural checks prove and do not prove.**

- [ ] **Step 6: Run focused application, API, and CLI tests to GREEN.**

### Task 3: Append-only human Sprint-selection state

**Files:**
- Modify: `models/enums.py`
- Create: `services/story_sprint_selection.py`
- Modify: `services/application.py`
- Modify: `workflow/facts.py`
- Modify: `repositories/workflow.py`
- Modify: `api.py`
- Modify: `cli/main.py`
- Modify: `cli/workflow_commands.py`
- Test: `tests/test_sprint_selection.py`
- Test: relevant API and CLI adapter tests under `tests/`

**Interfaces:**
- Adds: `WorkflowEventType.STORY_SELECTION_CHANGED = "story_selection_changed"`.
- Adds: `SprintSelectionState = Literal["unselected", "selected", "deferred"]`.
- Adds: `StorySprintSelectionRequest(project_id, story_id, intent, expected_state_fingerprint, idempotency_key, actor, correlation_id)`.
- Adds: `apply_story_sprint_selection_in_session(session, request) -> StorySprintSelectionFact`.
- API: `POST /api/projects/{project_id}/story/sprint-selection` with `story_id`, `intent: select|remove|defer`, `expected_state_fingerprint`, optional rationale, and standard mutation metadata.
- CLI: `agileforge story sprint-selection select|remove|defer --story-id N --expected-state-fingerprint sha256:...` with optional `--rationale`.
- `StoryFact` projects `structurally_eligible`, `structural_eligibility_status`, `sprint_selection_state`, `sprint_selection_state_fingerprint`, `sprint_selection_event_id`, and `sprint_selection_event_fingerprint`; candidacy requires current eligibility plus `selected` even before Task 4 adds selected-scope dependency confirmation.
- Default `unselected` has a deterministic state fingerprint derived from the exact accepted Story artifact/item identities and no event. Later fingerprints include the latest event ID and canonical event fingerprint.
- Event metadata uses `agileforge.story-sprint-selection.v1` and contains project ID, Story ID, source Story artifact ID/fingerprint, source item ID/fingerprint, accepted Specification ID/hash, actor, action, new state, previous state/fingerprint, observed eligibility evidence fingerprint, optional rationale, and event timestamp.
- Same-state intent is a receipt-backed no-op and never appends a duplicate event. Malformed, noncanonical, cross-project, out-of-sequence, or identity-mismatched history fails closed.
- `select` requires current eligible v3 evidence. `remove` and `defer` remain available after evidence becomes stale. No mutation is allowed after the exact Story is bound into an accepted Sprint plan or active Sprint.

- [ ] **Step 1: Write RED domain tests for default unselected, select, remove-to-unselected, defer, reselect, reload, exact artifact binding, replay, conflicting reuse, concurrent requests, and supersession.**

```python
assert facts.story_by_id(story_id).sprint_selection_state == "unselected"
selected = service.set_story_sprint_selection(select_request)
assert selected["selection_state"] == "selected"
```

- [ ] **Step 2: Write RED tests proving Select requires current v3 eligibility, while Remove and Defer remain available after staleness and preserved Select reactivates after reconciliation.**

- [ ] **Step 3: Run the focused selection tests and confirm the selection contract is absent.**

- [ ] **Step 4: Implement strict canonical event parsing and latest-event projection; malformed or cross-project metadata fails closed.**

- [ ] **Step 5: Implement the idempotent writer-locked mutation with expected-state fingerprint conflict handling.**

- [ ] **Step 6: Add canonical API/CLI Select, Remove, and Defer actions with stable Story selector instance keys.**

- [ ] **Step 7: Run domain, application, API, and CLI tests to GREEN.**

### Task 4: Selected-scope dependency confirmation and final candidate derivation

**Files:**
- Modify: `services/story_dependencies.py`
- Modify: `repositories/workflow.py`
- Modify: `services/read_projections.py`
- Modify: `workflow/definitions/planning.py`
- Modify: `workflow/handlers/planning.py`
- Modify: `services/sprint_selection.py`
- Modify: `services/application.py`
- Test: `tests/test_story_dependencies.py`
- Test: `tests/test_sprint_selection.py`

**Interfaces:**
- Produces: `structurally_eligible`, `sprint_selection_state`, `dependency_safe`, `sprint_candidate`, and `selected_scope_fingerprint` in Story/candidate projections.
- Defines: `selected_scope = exact active Stories whose latest selection is selected and whose v3 evidence is current and passing`.
- Defines: dependency review is current only when its selected IDs and source fingerprint equal the canonical selected-scope IDs/fingerprint.

- [ ] **Step 1: Write RED tests for candidate intersection, stale-evidence preservation of selection, one selected Story with unselected siblings/unrefined PBIs, and malformed projection failure.**

- [ ] **Step 2: Write RED dependency tests for exact selected scope, incomplete external prerequisite blocking, completed external prerequisite visibility, unrelated-edge preservation, selection-change invalidation, and duplicate replay/conflict.**

- [ ] **Step 3: Run the focused dependency and selection suites and confirm old implicit-candidate behavior fails the new assertions.**

- [ ] **Step 4: Split current evidence, selection, and dependency blockers in `StoryFact`; derive candidacy only after all three pass.**

- [ ] **Step 5: Update dependency application to mutate only edges whose dependent belongs to the reviewed selected scope; retain unrelated rows.**

- [ ] **Step 6: Bind dependency review, pending plan freshness, and Sprint generation to `selected_scope_fingerprint`; request-time Story IDs are at most an exact guard and never authority.**

- [ ] **Step 7: Run focused suites to GREEN.**

### Task 5: Browser semantics and fail-closed controls

**Files:**
- Modify: `frontend/project.js`
- Test: `tests/test_workflow_position_display.mjs`
- Test: `tests/e2e/test_single_project_lifecycle_ui.py`

**Interfaces:**
- Renders separate structural eligibility and Sprint-selection states.
- Renders `Select for Sprint`, `Remove from Sprint selection`, and `Defer` with exact Story/state fingerprints.
- Removes the normal `Validate Story` interaction; exposes `Re-run structural checks` only for missing or stale operational evidence. A current rule failure shows diagnostics without suggesting that repeating the same deterministic check is an approval path.
- Clears/locks dependent actions on malformed or unavailable eligibility, selection, dependency, or candidate projections.

- [ ] **Step 1: Write RED Node tests for three-state markup, proof/non-proof copy, exact mutation payloads, malformed projections, double submission, and mutation-success/reload-failure lockout.**

- [ ] **Step 2: Run Node tests and confirm the old Validate/Validated/Ready UI fails.**

Run: `node --test tests/test_workflow_position_display.mjs`

- [ ] **Step 3: Implement strict projection parsers, accessible status lists, explicit buttons, exact idempotency reuse during one mutation, and fail-closed control gating.**

- [ ] **Step 4: Run Node tests to GREEN.**

- [ ] **Step 5: Replace the old browser scenario with real acceptance-triggered evidence, select/reload/remove/defer/reselect, sibling exclusion, scoped dependency review, and Sprint-form gating.**

- [ ] **Step 6: Run the relevant Playwright scenarios to GREEN.**

### Task 6: Durable documentation, full verification, reviews, and local delivery

**Files:**
- Modify: `docs/superpowers/specs/2026-06-09-story-selection-sprint-scope-design.md`
- Modify: relevant durable API/CLI documentation discovered by exact reference search
- Test: all affected tests and repository gates

**Interfaces:**
- Marks parent-requirement-only selection explicitly superseded by #223.
- Documents v2-to-v3 hard break, removed validation surfaces, explicit reconciliation, state transitions, and no inferred selection.

- [ ] **Step 1: Add the supersession and migration documentation after behavior is GREEN.**

- [ ] **Step 2: Run all focused Python suites and `node --test tests/*.mjs`.**

- [ ] **Step 3: Run relevant Playwright scenarios.**

- [ ] **Step 4: Run `uv run --frozen pyrepo-check --all` and `git diff --check`.**

- [ ] **Step 5: Run independent specification, correctness, and lean-scope reviews; resolve every required finding and rerun affected gates.**

- [ ] **Step 6: Rehash protected files and verify exact before/after equality.**

- [ ] **Step 7: Commit locally with `#223` in the subject and verify the worktree is clean.**

- [ ] **Step 8: If all gates and reviews pass, prepare `manual-string-calculator-223-<shortsha>` through SQLite backup semantics, reconcile v3 evidence on the target only, verify logical equivalence/integrity/foreign keys/sidecars, and do not start the UI or a manual action.**
