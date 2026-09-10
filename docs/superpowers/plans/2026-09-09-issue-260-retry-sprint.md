# Retry one completed Sprint Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an owner retry the exact latest eligible completed Sprint with fresh evidence while preserving the approved work and all previous execution history.

**Architecture:** Add typed retry records and resolve one effective execution scope over the immutable original requirements. Reuse the existing graph nodes, semantic execution commands, transaction boundary, and evidence validation; bind retry commands through a scoped instance key. Preview and apply share one eligibility calculation, and transports expose one contract.

**Tech Stack:** Python 3.13.15, uv, SQLModel/SQLAlchemy, SQLite, Pydantic, FastAPI, existing JavaScript dashboard and pytest/browser tests. No new dependency.

**Spec:** `docs/superpowers/specs/2026-09-09-issue-260-retry-sprint-design.md`

## Global Constraints

- The command targets the exact requested Sprint; it never substitutes another.
- Only the latest eligible completed Sprint can be retried in issue #260.
- There can be only one current planned/active attempt for a project.
- Use the original accepted plan, Story identities and acceptance criteria, Task identities and checklists, selected scope, and dependency contract.
- Keep the original Sprint, accepted artifacts, Task evidence, Story closures, Sprint start/review/closure, triage, audit events, and transition receipts unchanged.
- Original execution remains readable without a historical backfill.
- New planning remains blocked while the current retry is planned or active, despite the original Sprint's completed status.
- Creating the attempt, its initial progress, and its successful receipt is atomic.
- Opening the preview, reloading the dashboard, or completing the original Sprint never retries it automatically.
- There is no target-branch field, branch-selection policy, worktree manager, Git revert, automatic repository rebinding, or special branch migration in this feature.
- A retry must not introduce a requirement to execute on the original attempt's branch.
- Use disposable synthetic repositories, profiles, and databases only. Never copy an operator's business or trace database into a test fixture.
- Work only in `C:/Users/atavares/Projects/agileforge/.worktrees/issue-260`, branch `alex/issue-260-retry-sprint`. Original master at `da3dbf630e43a16561b335087888c0e688754925` stays untouched.
- Use absolute issue-worktree paths for every patch. Before any shell edit or check, verify the resolved working directory and branch, then invoke the command with that exact workdir. Relative patch paths use the shared starting checkout, even when earlier shell commands used the issue worktree. Preserve and report any misplaced edit before a scoped correction; independently verify both checkout states afterward.
- Use `uv run --locked --exact --python 3.13.15` for Python checks; only this checkout's `./agileforge-dev` for application commands. Its disposable `issue-260-execution` profile passed runtime/schema preflight after the retry audit-event changes; the earlier design and verification profiles are preserved.
- Each task uses RED-GREEN-REFACTOR, preserves raw check output in its report, commits only its named work, and gets separate spec-compliance and quality verdicts. No worker subdelegation. Run checks through `.superpowers/sdd/2026-09-09-issue-260-retry-sprint/run-check.ps1` using a unique evidence name and the prescribed executable/argument array. The task-owned helper preserves command metadata, stdout/stderr and the native exit code without overwriting prior evidence. Retain tool session IDs, wait for the active run, and never start a duplicate because a tool yielded early. A missing completed `.exit` record is an unverified check.

- For Tasks 6 and 7, the controller launches every pytest, behavioral Node suite, real-browser check, and canonical repository check through the capture helper, owns their sessions, and returns the evidence. The implementer freezes source/tests before each launch and may run captured short formatting, lint, type, and syntax checks. No duplicate launches or source/test edits while a controller check is active.
- For the remaining Task 7 work, the controller also launches every short formatting, lint, type and syntax check. The worker edits only explicitly released files and reports; it does not invoke checks. Unsupported launcher invocations without actual check artifacts are unverified, even when reported as exit 0. Preserve that chronology and use the prescribed capture helper with actual paths before claiming a gate passed.

## File and interface map

New `models/sprint_retry.py` owns persistence, `repositories/sprint_retry.py` owns fact loading, `workflow/execution_identity.py` owns stable action bindings, and `workflow/execution_scope.py` owns effective state. `services/sprint_retry.py` owns eligibility/preview/create/start. Existing execution services retain their semantic responsibilities and use the resolved scope. New small request/handler modules are permitted to avoid expanding the already large application/domain files beyond registration and delegation.

Do not add a nullable retry field to existing serialized transition request models: their dumps participate in durable request fingerprints. Original instance keys stay `task:<id>`, `story:<id>`, or `sprint:<id>`; retry keys are `retry:<retry_id>:<kind>:<id>`. `PositionedRequest.attempt_id` remains the provider node-attempt identifier. Two new request kinds are `RetrySprint` and `StartSprintRetry`; completion kinds remain the existing ones.

The business schema manifest is strict and currently rejects missing tables before `create_all`. Upgrade only an exact verified immediately preceding schema in a write transaction: add the retry tables, validate the full new static manifest, commit. No existing table/row rewrite, schema repair, inference from partial migrations, or operator database experiment. Reject unknown, malformed, retired, and partially upgraded schemas. Fresh databases use the full current manifest.

### Task 1: Add retry persistence and durable typed facts

**Files:**
- Create: `models/sprint_retry.py`, `repositories/sprint_retry.py`, `tests/workflow/test_sprint_retry_models.py`, `tests/workflow/test_sprint_retry_schema.py`.
- Modify: `models/db.py`, `workflow/facts.py`, `workflow/fingerprints.py`, `repositories/workflow.py`, `tests/workflow/test_workflow_models.py`, `tests/workflow/test_fresh_project_schema.py`.

**Interfaces:**
- Consumes: existing immutable completion/review/closure/triage fact types and `WorkflowFactSnapshot`; existing SQLModel schema-manifest helpers.
- Produces: `SprintRetryAttempt`, `SprintRetryStoryState`, `SprintRetryTaskState`, `SprintRetryStart`, `SprintRetryTaskEvidence`, `SprintRetryStoryClosure`, `SprintRetryReview`, `SprintRetryClosure`, `SprintRetryTriage` in `models.sprint_retry`.
- Produces: `SprintRetryFact` in `workflow.facts`, and `load_sprint_retry_facts(session: Session, *, project_id: int) -> tuple[SprintRetryFact, ...]` in `repositories.sprint_retry`.
- `WorkflowFactSnapshot.sprint_retries: tuple[SprintRetryFact, ...] = ()`; omit that field from snapshot fingerprints only when empty to retain the exact old hash inputs.

Persistence contract:

```python
# Common owning columns on every retry row:
# retry_attempt_id -> sprint_retry_attempts.retry_attempt_id
# project_id -> projects.project_id; sprint_id -> sprints.sprint_id
# Evidence carries a composite FK to its owning attempt. Task/Story evidence
# also carries a composite FK to its attempt's progress row.
# Attempt identity:
# retry_attempt_id PK, project_id, sprint_id, ordinal >= 2,
# predecessor_retry_attempt_id nullable FK (null means original attempt),
# contract_fingerprint, created_by, rationale, creation_fingerprint,
# creation_receipt_key, created_at,
# status in ('Planned', 'Active', 'Completed'), started_at?, completed_at?.
# UNIQUE(project_id, sprint_id, ordinal), UNIQUE(project_id, retry_attempt_id).
# Partial unique project_id index WHERE status IN ('Planned', 'Active').
# UNIQUE(project_id, sprint_id, predecessor_retry_attempt_id) plus original
# predecessor uniqueness (SQLite null uniqueness alone is insufficient).
# Progress rows: (retry_attempt_id, story_id/task_id) unique, status.
# Start: one per attempt; actor/start time/contract and decision fingerprints.
# TaskEvidence: same semantic fields as TaskCompletionEvidence, new PK.
# StoryClosure: same semantic fields as StoryClosure, new PK.
# Review/Closure: same semantic fields as originals, new PK.
# Triage: same semantic fields as PostSprintTriage, correction FK is retry-local.
```

`SprintRetryFact` holds the immutable attempt identity/audit metadata, status/times, ordered `(story_id, status)` and `(task_id, status)` tuples, optional typed start fact, and tuples of existing typed task/story/review/closure/triage facts nested inside this attempt. A new `SprintRetryStartFact` holds its persisted start fields. Nested facts use the original source sprint ID; the enclosing retry supplies identity. Load in deterministic identity order; use existing JSON validators for refs/checklists/triage. The loader must not overwrite original facts or silently drop malformed rows.

- [x] **Step 1: Add meaningful failing persistence tests.** Use the existing synthetic `_file_engine` and fixture builders, with raw SQL snapshots of old tables and direct invalid inserts.

```python
def test_retry_fact_default_does_not_change_legacy_snapshot_hash(snapshot):
    legacy_payload = snapshot.model_dump(mode="json")
    legacy_payload.pop("sprint_retries", None)
    assert snapshot.sprint_retries == ()
    assert _snapshot_fingerprint(snapshot) == fingerprint(legacy_payload)

def test_two_live_attempts_in_a_project_are_rejected(session, retry_rows):
    session.add(retry_rows.planned)
    session.flush()
    session.add(retry_rows.another_planned)
    with pytest.raises(IntegrityError):
        session.flush()
```

Define fixtures locally with real Project/Sprint/Story/Task ownership. Also assert cross-attempt subject FK rejection, duplicate starts/evidence rejection, valid correction chain, and deterministic loader roundtrip. Use the repository's actual canonical hash helper signature for the first assertion.

- [x] **Step 2: Run RED and record actual output.** `uv run --locked --exact --python 3.13.15 pytest tests/workflow/test_sprint_retry_models.py -q`; require failure from absent retry functionality, not fixture/setup defects.
- [x] **Step 3: Implement typed rows/facts/loader and static schema entries.** Mirror the existing evidence semantics and meaningful constraints; explicitly import the new model module in the manifest initialization list. Append loaded retry facts only after the original workflow snapshot has been loaded. Preserve old fact payloads.

```python
payload = snapshot.model_dump(mode="json")
if not snapshot.sprint_retries:
    payload.pop("sprint_retries")
# Continue with the existing canonical snapshot hashing algorithm.
```

- [x] **Step 4: Add RED migration tests before writing the additive upgrade.** Build the baseline schema from the frozen old static manifest/metadata fixture. Insert synthetic history, snapshot rows, open through schema setup, and compare. Inject failure after the first new DDL statement and require every new table absent after rollback. Start two connections upgrading the same file and require exactly one complete current schema. Unknown extra columns and a single prematurely added retry table must fail closed.

```python
before = snapshot_original_rows(engine)
ensure_business_db_ready(engine)
assert snapshot_original_rows(engine) == before
assert inspect_schema(engine) == CURRENT_BUSINESS_SCHEMA_MANIFEST
```

Test helpers must call the actual database entrypoint and compare explicit inspected structures; do not mock the migration outcome or derive expected manifests from the implementation under test.
- [x] **Step 5: Implement atomic exact-baseline upgrade.** Serialize with the existing SQLite write-lock conventions. Inspect inside the transaction. Accept empty/new schema, exact current schema, or exact immediately preceding schema only. Create only missing retry tables for the latter, validate, commit; rollback propagates failure. Preserve current strict rejection diagnostics for other shapes.
- [x] **Step 6: Run GREEN/refactor.** `uv run --locked --exact --python 3.13.15 pytest tests/workflow/test_sprint_retry_models.py tests/workflow/test_sprint_retry_schema.py tests/workflow/test_workflow_models.py tests/workflow/test_fresh_project_schema.py -q`; run ruff and ty on affected boundaries. Record tests and remaining dependent integration scope.
- [x] **Step 7: Commit named files.** `git commit -m "feat: persist isolated Sprint retry attempts and evidence"` after staging only this task's files and checking the staged diff.

**Task 1 review adjustment:** Fix round 1 also implements the targeted `ExecutionScope` / `resolve_execution_scope` foundation and shared scope-aware fingerprints described in Task 2, plus a shared canonical evidence payload parser and repository error normalization. These are required to validate durable retry evidence before accepting Task 1. Task 2 retains current-scope selection, lineage extraction, action identities, and request binding tests; it reuses the reviewed foundation.

### Task 2: Resolve retry execution scope without changing historical bindings

**Files:**
- Create: `workflow/execution_identity.py`, `workflow/sprint_lineage.py`.
- Modify: `workflow/execution_scope.py`, `tests/workflow/test_execution_scope.py`, `workflow/execution_integrity.py`, `workflow/requests/execution.py`, `workflow/definitions/planning.py`, `tests/workflow/test_execution_requests.py` (create if absent).

**Interfaces:**
- Consumes: Task 1 `SprintRetryFact`, snapshot collection, targeted `ExecutionScope`/`resolve_execution_scope`, and shared Task/Story scope-aware fingerprint foundation. Extend this reviewed foundation; current-scope selection, action bindings, and review/close hash support remain this task.
- Produces: frozen `ExecutionIdentity(kind: Literal['task', 'story', 'sprint'], entity_id: int, retry_attempt_id: int | None)`; `execution_instance_key(kind, entity_id, retry_attempt_id=None) -> str`; `parse_execution_instance_key(value: str) -> ExecutionIdentity`.
- Produces: frozen `ExecutionScope` with `sprint_id`, `retry_attempt_id`, normalized lower-case `status`, `started_at`, `completed_at`, `contract`, selected `stories`, all effective `project_stories`, selected `tasks`, contract `dependencies`, `task_completions`, `story_completions`, `sprint_reviews`, `sprint_closures`, `post_sprint_triage`. Original start time comes from `SprintStartFact`; retry fact status follows the same lower-case normalization as `SprintFact`.
- Produces: `resolve_execution_scope(snapshot: WorkflowFactSnapshot, *, sprint_id: int, retry_attempt_id: int | None = None) -> ExecutionScope` and `current_execution_scope(snapshot: WorkflowFactSnapshot) -> ExecutionScope | None`.
- Existing execution fingerprint functions accept optional keyword-only `scope: ExecutionScope | None = None`, preserving byte-identical default behavior. Import scope only under `TYPE_CHECKING` in integrity to avoid a circular import.

- [x] **Step 1: Write failing scope/identity tests.** Seed original Done work plus new To Do progress. Exact accepted requirements remain identical, inner scoped dependencies become pending, completed external dependencies remain valid, original snapshot stays byte-equal. Missing/duplicate progress or changed contract must raise integrity errors.

```python
@pytest.mark.parametrize("kind", ["task", "story", "sprint"])
def test_scoped_instance_roundtrip(kind):
    assert execution_instance_key(kind, 7) == f"{kind}:7"
    key = execution_instance_key(kind, 7, 2)
    assert key == f"retry:2:{kind}:7"
    assert parse_execution_instance_key(key) == ExecutionIdentity(kind, 7, 2)

@pytest.mark.parametrize("key", ["retry:0:task:7", "retry:2:task:8:extra", "task:-1", "retry:02:task:7"])
def test_invalid_binding_rejected(key):
    with pytest.raises(ValueError):
        parse_execution_instance_key(key)
```

- [x] **Step 2: Run RED.** `uv run --locked --exact --python 3.13.15 pytest tests/workflow/test_execution_scope.py -q`.
- [x] **Step 3: Implement strict identity parsing and effective scope.** Parse canonical positive decimal IDs only. Original resolver delegates to existing `execution_contract`; retry resolver verifies owner, lineage, exact progress membership, and stored approved-contract fingerprint. Replace statuses only in scope-owned Story/Task facts. Preserve source requirements, original history, and completed external Stories.

```python
def execution_instance_key(kind, entity_id, retry_attempt_id=None):
    suffix = f"{kind}:{entity_id}"
    return suffix if retry_attempt_id is None else f"retry:{retry_attempt_id}:{suffix}"
```

Keep explicit type annotations and validate constructor inputs as well as parser input. Current scope rejects multiple live originals/retries; a planned retry outranks source completed execution. For terminal routing, use proven latest accepted-plan lineage and the highest valid linked retry ordinal, not arbitrary numeric Sprint IDs.

An original planned Sprint has no `SprintStartFact` or `ExecutionContract`; keep it in the existing planning/start route and return no current execution scope until it starts. Count it when rejecting overlap with a live retry. Exact original-scope resolution requires original start lineage; retry planned scopes can resolve because their source Sprint already started. Read projections retain their existing original-planned branch.

Share lineage without an import cycle: move `_sprint_stream_nodes`, `_sprint_stream_starts`, `_sprint_stream_lifecycle`, `_current_sprint_stream_artifacts`, and `_plan_has_matching_sprint_start` from `workflow/definitions/planning.py` to `workflow/sprint_lineage.py`, retaining their algorithms. Export `current_sprint_stream_artifacts` and `plan_has_matching_sprint_start` (same signatures without the leading underscore); import aliases into planning so current call sites remain unchanged. Scope and eligibility import the shared module, never the graph-definition module. Add characterization tests before this move, and run existing planning graph/transition tests after it. This lets planning rules later consume scope without a circular import.
- [x] **Step 4: Bind evidence hashes and existing request validation.** Retry contract hash wraps original immutable contract hash plus retry identity. Existing completion/triage request validators accept a matching scoped identity without adding serialized fields. Validate kind and exact requested subject. Hash defaults stay unchanged.

```python
identity = parse_execution_instance_key(request.instance_key)
if identity.kind != "task" or identity.entity_id != request.task_id:
    raise ValueError("Task binding does not match the requested Task.")
```

- [x] **Step 5: GREEN and regressions.** Run new scope/request tests and `tests/workflow/test_execution_graph.py`, `tests/workflow/test_execution_transitions.py`. Add golden original request dump/hash assertions from existing real request fixtures. Check original snapshot equality before/after scope resolution and old evidence fingerprints.
- [x] **Step 6: Commit named files.** `git commit -m "feat: resolve attempt-bound Sprint execution scope"`.

### Task 3: Add guarded preview, transactional retry, and explicit start

**Files:**
- Create: `services/sprint_retry.py`, `workflow/sprint_retry_eligibility.py`, `workflow/requests/sprint_retry.py`, `workflow/handlers/sprint_retry.py`, `tests/workflow/test_sprint_retry_transitions.py`.
- Modify tests: `tests/workflow/test_workflow_models.py`, `tests/workflow/test_node_attempts.py` for unchanged legacy snapshot hashes and ordinary provider continuation with nonempty retry-only guards.
- Modify request-registration tests: `tests/workflow/test_execution_transitions.py`, `tests/workflow/test_planning_transitions.py`; each currently pins 33 variants. Register the two exact new kinds and keep explicit closed-union coverage (35 variants), without weakening the legacy request assertions.
- Modify: `workflow/requests/__init__.py`, `workflow/domain.py`, `workflow/definitions/execution.py`, `workflow/definitions/planning.py`, `workflow/definitions/product_goal.py`, `services/agent_workbench/sprint_phase.py`, `workflow/facts.py`, `workflow/fingerprints.py`, `repositories/workflow.py`.
- Fix-round audit integration: `models/enums.py` for explicit retry-planned/retry-started event types. Reuse `WorkflowEvent`; preserve its existing schema and old rows. Shared execution test setup may be extracted to `tests/workflow/execution_retry_support.py` and its existing callers updated without changing their default behavior. Narrow follow-up-cycle helpers in `tests/workflow/execution_fixtures.py` and `tests/workflow/test_planning_transitions.py` support real later Sprint lineages, explicit clocks, accepted Story references, and distinct receipt keys; retain the original helper defaults and regressions.
- Narrow direct-guard integration: `services/story_dependencies.py`, `tests/test_story_dependencies.py`, `workflow/execution_scope.py`, `tests/workflow/test_execution_scope.py`. Preserve original direct dependency behavior when no retry exists; avoid the existing `repositories.workflow -> services.story_dependencies` import cycle.

**Interfaces:**
- Consumes: Task 1 rows, Task 2 identities/scopes; existing session-bound workflow domain, receipts, facts repository and accepted-plan stream selectors.
- Produces: frozen `SprintRetryPreview` with `project_id`, `sprint_id`, `predecessor_retry_attempt_id`, `next_ordinal`, exact Story/Task scope, preserved-history summary, repository provenance, `blockers`, `expected_state_fingerprint`.
- Produces: `build_sprint_retry_preview(session: Session, *, snapshot: WorkflowFactSnapshot, sprint_id: int) -> SprintRetryPreview`; this function is read-only.
- Produces: `RetrySprint` positioned request (`kind='retry_sprint'`, project/sprint, `confirm`, actor, rationale, expected-state fingerprint, idempotency plus existing graph/decision bindings) and `StartSprintRetry` positioned request (`kind='start_sprint_retry'`, project/sprint/retry identity, actor, idempotency and normal decision bindings). The class names denote operations; all serialized kind/graph request_kind values follow existing snake_case conventions. Require literal true confirmation and nonblank actor/rationale at the new request boundary before the domain claims a receipt, so an unconfirmed invocation has no persistence effect; do not change legacy request models. Test omission, false, numeric 0/1, and string true/yes as rejected confirmation inputs; only the boolean true is confirmation.
- Graph nodes: `execution.sprint.retry` optional owner reentry, `execution.sprint.retry.start` human start of planned retry. Neither duplicates completion nodes.
- Writes: `retry_sprint_in_session(session, *, request, snapshot, now)` and `start_sprint_retry_in_session(session, *, request, snapshot, now)` invoked only inside the established domain transaction.

**Task 3 review adjustment:** `workflow/handlers/sprint_retry.py` is the dedicated integration point for the two new request types, with explicit registration in `workflow/domain.py`. It replaces the originally listed Task 3 edit to `workflow/handlers/execution.py`; that existing completion handler remains a Task 4 integration file. Add direct dependency and execution-scope regressions in their named owning test modules. Planning and start each append an explicitly retry-bound `WorkflowEvent` in the same transaction as their new rows and successful receipt; include actor, source Sprint and retry identity, and prove rollback/replay behavior.

Separate pure eligibility from persistence: `evaluate_sprint_retry_eligibility(snapshot: WorkflowFactSnapshot, *, sprint_id: int) -> SprintRetryEligibility` lives in `workflow/sprint_retry_eligibility.py` and returns exact source/predecessor/ordinal/contract and structured blockers. Both graph rules and the preview service consume it. The service adds descriptive current persisted repository provenance to the preview fingerprint.

Retry guard facts: add a separate default-empty `SprintPlanGenerationGuardFact` projection with attempt ID, durable `started_at`, outcome/time, generated plan ID/fingerprint when linked, and explicit `linked`/`unlinked`/`malformed` integrity classification. Derive it from the exact `planning.sprint.plan` attempt/outcome rows; never infer order from lease expiry. Validate canonical output hashes and project-owned plan links where present. This is a retry guard, not a new requirement that the source plan was generated by a provider: valid directly recorded accepted plans remain eligible. Older incomplete provider provenance must not invalidate ordinary historical reads. Use the existing NodeAttemptFact business/input identity to distinguish a demonstrated superseding successful attempt from unresolved failure; do not label a lease expiry alone resolved. The existing validated current-stream selector proves later draft/accepted/started streams; guard metadata covers later generation that produced no plan.

Other reachable provider operations retain their current-business recovery guards. A separate retry-only provider guard projection may carry the existing exact node/instance/business/input identity, durable start/outcome times, and canonical output integrity for this purpose. Failed, obsolete, or in-flight current work must not become retry-eligible without demonstrated matching recovery; successful competing Sprint-plan generation remains blocked by lineage even when it also recovered a failure. Never alter the legacy node-attempt fact shape to add this metadata.

In-flight guard: add `IncompleteTransitionFact` with receipt identity, request kind/fingerprint, started time, and explicit malformed/unassignable classification. Load canonical project-owned pending receipts (or classify unassignable malformed receipts as retry blockers), without breaking ordinary historical reads. The domain's own newly claimed receipt must be excluded during its transaction's fact reads: bracket `_execute_request` with an explicit session-local active receipt ID marker, restore it in `finally`, and exclude only that exact validated row. Known non-Project creation requests do not belong to an existing Project. This avoids self-blocking while preserving graph/preview parity for every preexisting incomplete receipt; no committed receipt is changed or hidden globally.

All retry-only guard collections are ALWAYS excluded from general `fact_fingerprint` and `business_fact_fingerprint`, even when nonempty. Existing node-attempt facts still serve normal execution, and business fingerprints intentionally exclude attempts. Explicitly hash the complete sorted retry guards in retry preview state and retry-node `FactReference`s, so the positioned retry decision and transactional preview recheck detect guard changes. Add nonempty compatibility tests (including an ordinary provider continuation), guard-only stale-preview tests, exact-ID marker cleanup on success/failure, and other-receipt rejection tests. Do not add fields to legacy NodeAttemptFact or PlanningArtifactFact. Keep the existing old-schema hash expectation independent: its explicit legacy payload excludes all newly added retry-only collections, and nonempty-guard tests prove those guards never enter the general hash.

- [x] **Step 1: Write failing preview/transition tests using synthetic complete/triaged execution.** Existing helpers: `_complete_execution_sprint`, `_triage_execution_sprint` in `tests/workflow/test_execution_transitions.py`; extract reusable helpers into a dedicated test support module if needed without changing their behavior.

```python
before = original_rows(engine)
preview = preview_for(domain, project_id=project_id, sprint_id=sprint_id)
assert preview.blockers == ()
assert preview.next_ordinal == 2
assert original_rows(engine) == before
result = apply_retry(domain, preview, key="retry-2")
assert result.retry_attempt_id > 0
assert original_rows(engine) == before
assert retry_status(engine, result.retry_attempt_id) == "Planned"
assert next_action(domain, project_id).request_kind == "start_sprint_retry"
```

Helper implementations must use the real workflow domain, evaluated positioned bindings, and persisted rows. Define the helpers in the test file as part of this step.
- [x] **Step 2: Run RED.** `uv run --locked --exact --python 3.13.15 pytest tests/workflow/test_sprint_retry_transitions.py -q`.
- [x] **Step 3: Implement one eligibility function and preview hash.** Prove source accepted plan/decision/start/closure, current resolved triage, original integrity, linked latest retry, exact approved content/dependency fingerprint, no competing delivery. Inspect later stream artifacts, node attempts and incomplete-transition facts. Historical execution contracts intentionally use stored dependency snapshots; retry eligibility must additionally compare the live selected dependency rows and current accepted requirements to that stored scope. Block later drafts/accepted plans/generation/dependent artifacts, failed unresolved work, missing or conflicting lineage. Return named structured blockers. Include every eligibility input plus repository binding in the preview hash.

```python
# Required application sequence under the existing write transaction:
# 1. Existing receipt replay/conflict checks.
# 2. Reload authoritative snapshot and recompute preview.
# 3. Reject missing confirm/blank actor/rationale, blockers, stale hash.
# 4. Insert Planned attempt and flush ID.
# 5. Insert exactly its selected Story/Task To Do progress.
# 6. Existing domain persists event/success receipt and commits together.
```

- [x] **Step 4: Add retry/start dispatch and graph routing.** Register the two request classes in every closed union/dispatch table. Start verifies planned current retry and unchanged source contract, writes immutable start and changes only retry status. Block plan generation, plan acceptance/start, and scope mutation while a retry is live. Extend the existing shared `workflow/definitions/product_goal.py:lifecycle_is_quiescent` guard for planned/active retries and completed retries awaiting valid triage, so root lifecycle operations do not incorrectly see the preserved original Completed Sprint as quiescent. Retry remains optional and is not the default next action after terminal triage.

Share `retry_blocks_planning(snapshot) -> bool` in `workflow/execution_scope.py`: return false immediately without retries, block planned/active retries, and validate every completed retry's own closed-attempt and resolved-triage facts using the existing scope validators. Treat missing/malformed completion or triage as blocked. Do not select the current planning stream in this predicate; a valid later pending plan must remain reviewable after all retries are fully triaged. Use it in quiescence/planning/direct-service guards. The low-level dependency service must not import graph definitions or the workflow repository at module initialization; retain a no-retry fast path and a cycle-free canonical retry check. Add actual direct-service and original dependency regressions.
- [x] **Step 5: Write and run concurrency/rollback RED before hardening.** Same key/same request returns original result; same key/changed request conflicts; two different keys from one preview yield one attempt; injected failure after progress insertion leaves no attempt/progress/event/success receipt. Stale repo provenance, stale content, older target, ambiguous IDs/timestamps, later plans/attempts, unresolved triage and in-flight transitions all reject without old-row changes.

```python
with ThreadPoolExecutor(max_workers=2) as pool:
    results = list(pool.map(apply_same_preview_with_distinct_key, ["left", "right"]))
assert sum(result.success for result in results) == 1
assert len(load_attempts(engine)) == 1
assert original_rows(engine) == before
```

- [x] **Step 6: GREEN and commit.** Run the new transition suite plus execution/planning domain tests affected by registration and routing. Stage named files; `git commit -m "feat: preview and start guarded Sprint retries atomically"`.

### Task 4: Execute and close retries through existing workflow semantics

**Files:**
- Create: `tests/workflow/retry_execution_fixtures.py` for shared persisted multi-Story execution setup and normal lifecycle helper actions, reusable by Task 7.
- Review fix support: extract the generic planning helpers used by that fixture into `tests/workflow/planning_fixtures.py` and update `tests/workflow/test_planning_transitions.py` to consume the same implementation; preserve defaults and accepted payloads, avoid duplicated helper bodies and private concrete-test imports.
- Modify: `services/task_execution_service.py`, `services/story_close_service.py`, `services/agent_workbench/sprint_phase.py`, `services/agent_workbench/post_sprint_triage.py`, `workflow/definitions/execution.py`, `workflow/handlers/execution.py`, `services/application.py`.
- Conditional prerequisite correction: `workflow/definitions/planning.py` and `tests/workflow/test_planning_graph.py`, limited to the reproduced selected-cohort planning guard described below.
- Test: `tests/workflow/test_sprint_retry_execution.py`, `tests/workflow/test_execution_graph.py`, `tests/workflow/test_execution_transitions.py`, `tests/test_task_execution_service.py`, `tests/test_story_close_service.py`.

**Interfaces:**
- Consumes: Task 2 `ExecutionScope` and scoped identity parser; Task 3 planned/start lifecycle.
- Produces: existing CompleteTask/CloseStory/ReviewSprint/CloseSprint/RecordPostSprintTriage semantic operations working with original or retry scope. Transport-facing semantic input dataclasses may gain optional scope identity, but original positioned request serialization must remain stable.
- `ExecutionActionSelectionService` resolves exact scoped subject/attempt from positioned bindings; no fallback from a stale retry key to current work.

- [x] **Step 1: Write failing lifecycle tests.** Start a retry of completed/triaged work; old Done state supplies no new completion. Complete each task using a new key, close its Story, review Sprint, explicitly close, then record triage. Compare all old records at each checkpoint and reload the domain from disk between transitions.

```python
assert retry_scope.tasks[0].status == "To Do"
assert retry_scope.task_completions == ()
assert original_scope.tasks[0].status == "Done"
assert original_scope.task_completions
complete_retry_task(domain, retry_id, task_id)
assert original_rows(engine) == before
assert load_retry_scope(domain, retry_id).tasks[0].status == "Done"
```

Implement helper actions using the real evaluated graph and existing request models. Add two inner dependent Tasks/Stories and one completed external dependency to prove proper isolation. Keep their reusable persisted setup in `tests/workflow/retry_execution_fixtures.py`; preserve existing fixture defaults. If the same fixture proves real next-plan availability after triage, retain a fourth unselected Story as a candidate rather than treating absence of candidates as successful planning. Task 7 still has its separately required one-artifact, three-Story, two-Sprint acceptance scenario.

The persisted setup exposed a preexisting planning guard that treats an unfinished prerequisite inside the exact reviewed selected cohort as external. Add a focused graph RED and make the smallest planning-only correction: exclude only canonical prerequisite blockers belonging to the selected cohort from the external-incomplete check. Keep readiness blockers on facts and preserve execution ordering, external and transitive prerequisite checks, proposed-edge rejection, and cycle rejection. Verify the persisted B/C setup then executes in dependency order; do not fake completed state or inject dependencies after Sprint start.
- [x] **Step 2: Run RED.** `uv run --locked --exact --python 3.13.15 pytest tests/workflow/test_sprint_retry_execution.py -q`.
- [x] **Step 3: Refactor shared validation to consume scope.** CompleteTask validates effective task status/checklist/references/dependencies, then writes original rows for original scope or retry evidence/progress for retry scope. Story close, review, close, and triage follow the same ownership choice after shared validations. Keep existing original transaction behavior and errors. No reset of Task/UserStory/Sprint or copy of previous evidence.

```python
identity = parse_execution_instance_key(request.instance_key)
scope = resolve_execution_scope(
    snapshot, sprint_id=sprint_id, retry_attempt_id=identity.retry_attempt_id
)
# Shared contract/evidence validation consumes scope. Persistence chooses
# original model only when scope.retry_attempt_id is None.
```

- [x] **Step 4: Route all existing graph rules through scope.** Replace raw active/completed selection in task/story/review/close/triage rules with the scope boundary. Keep original historical integrity checks on original facts; validate each retry's own evidence against its scoped contract. Bind action fingerprints and instance keys to the exact retry. Planning remains blocked until terminal valid triage; triage impact backlog/specification preserves existing downstream behavior.
When connecting the current selector, add a regression for a valid newly drafted Sprint-plan stream with no accepted leaf after prior execution is fully triaged. Preserve the ordinary pending-plan review route. The persisted probe reproduced this boundary: after the current stream has passed lineage and lifecycle validation, catch only `ACCEPTED_LEAF_MISSING` from its accepted-leaf selection and return no current execution scope. Keep older completed history readable through explicit scope resolution; never substitute it as the current execution for a newer pending plan. The narrow correction belongs in `workflow/execution_scope.py` with `tests/workflow/test_execution_scope.py`, including malformed and multiple-pending-stream controls; do not catch all lineage errors or weaken malformed-history rejection.

- [x] **Step 5: Add replay and repeat-retry tests, then GREEN.** Original receipt replay is allowed to return original result but cannot mark retry progress. Captured attempt-2 actions cannot mutate attempt 3. Complete retry 2 and retry it again with ordinal 3 linked to 2; prove fresh To Do plus unchanged prior evidence, and normal next planning availability after valid completion/triage. Use a changed review binding and an old task key to prove rejection.
- [x] **Step 6: Run focused regressions and commit.** Run retry execution/transitions, existing execution graph/transitions and scope tests, and the owning Task/Story service tests. The existing execution-transition suite covers Sprint review, closure and triage service behavior. For the selected-cohort guard correction, also run the owning planning-graph suite and the existing dependency selection tests in `tests/test_sprint_selection.py`. Also run `tests/adapters/test_api_workflow_domain.py -k execution`, which owns the existing application selection, replay and transportability regressions for the methods changed here. `git commit -m "feat: require fresh evidence throughout Sprint retry execution"`.

### Task 5: Expose matching CLI, API, and read projections

**Files:**
- Modify: `services/application.py`, `services/read_projections.py`, `cli/main.py`, `cli/workflow_commands.py`, `api.py`, `docs/agent-cli-manual.md`.
- Test: `tests/adapters/test_cli_sprint_retry.py`, `tests/adapters/test_api_sprint_retry.py`, `tests/adapters/test_cli_workflow_domain.py`, `tests/adapters/test_api_workflow_domain.py`, `tests/services/test_sprint_status_projection.py`.
- Create test support: `tests/adapters/sprint_retry_fixtures.py` for shared synthetic transport setup and durable row snapshots reused by the CLI/API tests; capture original rows, retry progress, audit events, and receipts so no-write assertions cannot pass on counts alone. Do not import a concrete test module or duplicate fixture bodies.

**Interfaces:**
- Consumes: Task 3 preview/apply and Task 4 semantic lifecycle.
- CLI: `sprint retry-preview --project-id P --sprint-id S`; `sprint retry --project-id P --sprint-id S --confirm --expected-state-fingerprint F --rationale R --actor A --idempotency-key K`.
- API: `GET /api/projects/{project_id}/sprint/{sprint_id}/retry-preview`, `POST /api/projects/{project_id}/sprint/retry` using the same semantic fields.
- Existing Sprint start accepts a retry instance binding, producing StartSprintRetry when bound; original unbound start remains StartSprint.
- Projections add `current_retry` metadata (ID, ordinal, status, predecessor) and effective current progress; history includes explicitly attempt-bound entries while existing original records remain accessible.

- [x] **Step 1: Write failing transport parity tests.** For one synthetic database, compare preview scope/blockers/hash across CLI and API; GET and unconfirmed calls write nothing. Each successful transport apply routes through the real domain; stale hash returns existing conflict style. Tests verify all required fields and unknown/mismatched IDs fail.

```python
before = original_rows(engine)
response = client.get(f"/api/projects/{project_id}/sprint/{sprint_id}/retry-preview")
assert response.status_code == 200
assert response.json()["sprint_id"] == sprint_id
assert original_rows(engine) == before
assert load_attempts(engine) == []
```

Use the API's actual envelope conventions and adapt assertion location to that documented envelope; do not introduce a special response envelope.
- [x] **Step 2: Run RED on new CLI/API tests.** Use the existing isolated transport fixtures, no server attached to operator data.
- [x] **Step 3: Implement application methods and transport registration.** Reuse decision preparation/fingerprint/receipt code. Register semantic command/API mappings, instance selectors, and optional retry reentry allowlists. The preview action carries no provider request. Resolve start using the supplied retry instance binding and expose its normal confirmation action.

```python
identity = parse_execution_instance_key(instance_key) if instance_key else None
if identity is not None and identity.retry_attempt_id is not None:
    # Prepare StartSprintRetry with its evaluated retry.start binding.
    return prepare_retry_start(identity)
return prepare_original_sprint_start()
```

- [x] **Step 4: Update read projections using scope.** Sprint status, current tasks, task show, review and timeline use scoped progress and name the attempt. Preserve original history entries and requirement text. Include required scoped action bindings so adapters never guess an attempt from latest numeric IDs.
- [x] **Step 5: Document exact supported commands and failures.** Explain executor-prepared branch/worktree, preview/confirmation, only latest eligible Sprint, new start and fresh evidence, old history preserved. Canonical Story/Task packet schemas remain unchanged: they carry original accepted requirements and source-execution status snapshots, while workflow position/next and the scoped Sprint/Task reads supply current retry progress and action bindings. Explain this distinction in the manual. No instructions suggesting cleanup or branch rollback.
- [x] **Step 6: GREEN and commit.** Run new transport tests, Sprint status and CLI/API workflow contract suites. `git commit -m "feat: expose Sprint retry across CLI API and projections"`.

### Task 6: Add dashboard preview and confirmation with current/history display

**Files:**
- Modify: `frontend/project.js` and its existing Sprint markup/styles if needed.
- Create: `tests/test_sprint_retry_dashboard.mjs`.
- Test: `tests/test_dashboard_review_safety.mjs`, `tests/test_cockpit_action_synchronization.mjs`, `tests/test_workflow_position_display.mjs`.
- Modify: `tests/e2e/test_single_project_lifecycle_ui.py`, `cli/dev_checks.py`, `tests/dev_runtime/test_dev_checks.py`, `.github/workflows/ci.yml` to cover the real browser flow and register the new Node suite in the existing quality/CI command lists.

**Interfaces:**
- Consumes: Task 5 preview endpoint, confirmed POST, retry metadata/history, scoped action bindings.
- Produces: optional **Retry Sprint** action on selected completed Sprint; preview shows exact target, scope and blockers; confirmation requires rationale, internally carries fingerprint/idempotency/actor. No target branch field. Current attempt label and prior history stay visible after reload.

- [x] **Step 1: Write failing UI tests.** Mock or intercept network at existing browser test boundary: opening preview performs only GET; blocked preview explains reason and prevents confirmation; cancel sends no POST; confirm sends exact current Sprint and preview fingerprint once.

```javascript
expect(requests.filter((request) => request.method === "POST")).toHaveLength(0);
await retryDialog.getByLabel("Rationale").fill("Re-execute the approved work");
await retryDialog.getByRole("button", {name: "Retry Sprint", exact: true}).click();
expect(retryPosts).toHaveLength(1);
expect(retryPosts[0].sprint_id).toBe(selectedSprintId);
expect(retryPosts[0].expected_state_fingerprint).toBe(previewFingerprint);
```

Use the project's existing browser harness syntax and selectors; bind to accessible labels rather than screenshot coordinates.
- [x] **Step 2: Run RED with `node --test tests/test_sprint_retry_dashboard.mjs`.** Record the failing assertion in the report. Node suites use node:test/VM/fake fetch; the real browser suite is separate Playwright pytest in `tests/e2e/test_single_project_lifecycle_ui.py` with the task-owned `dashboard_harness` fixture.
- [x] **Step 3: Implement the optional action and existing confirmation surface.** Fetch preview on opening, render approved scope and structured blockers, disable apply when blocked, retain captured fingerprint until confirmation. On stale conflict reload preview and require a new explicit confirmation. Prevent duplicate submissions while pending.
- [x] **Step 4: Update effective progress/history rendering.** Show `Attempt 2` with planned/start action then pending Task/Story progress, while original completion history remains readable. Pass the provided scoped bindings for every action. Reload must produce the same display from server state.
- [x] **Step 5: GREEN and browser verification.** Exercise blocked/cancel/confirm, stale preview, normal retry start and task completion display, original completion history, and reload. Use only a disposable synthetic server/profile. Add `test_issue_260_retry_sprint_requires_preview_confirmation_and_preserves_history` using the existing browser harness. Run `uv run --locked --exact --python 3.13.15 pytest tests/e2e/test_single_project_lifecycle_ui.py -k 'issue_260 or issue_259 or issue_227' -q` and `node --test tests/test_sprint_retry_dashboard.mjs tests/test_dashboard_review_safety.mjs tests/test_cockpit_action_synchronization.mjs tests/test_workflow_position_display.mjs`. Capture evidence artifact paths. Register the new Node file in `cli/dev_checks.py` and `.github/workflows/ci.yml`; update the corresponding exact argv test after observing RED.
- [x] **Step 6: Commit named files.** `git commit -m "feat: add owner-confirmed Sprint retry dashboard flow"`.

### Task 7: Prove preserved history, lifecycle parity, and final repository quality

**Files:**
- Create: `tests/workflow/test_sprint_retry_acceptance.py`.
- Modify only necessary coverage/docs or implementation files with demonstrated test failures; record exact changed files and causes.
- Update: `docs/testing/workflow-graph-acceptance-checklist.md`.
- Reproduced canonical-quality corrections: `tests/services/test_durable_product_definition_projections.py` (import the extracted assessment helper from its existing shared owner), `tests/workflow/test_execution_requests.py` (precise six-field TypedDict, unchanged JSON/hash oracle), `pyproject.toml` (existing B101 test-assert convention extended only to three named shared test-helper paths), and six schema-owned fixture SQL lines in `tests/adapters/sprint_retry_fixtures.py`, `tests/workflow/test_sprint_retry_execution.py`, `tests/workflow/test_sprint_retry_schema.py`, `tests/workflow/test_sprint_retry_transitions.py` (documented per-line B608 exceptions). Retain all assertions, production checks, identifier provenance and bound values; no SQL behavior change or blanket suppression.
- Reproduced full-suite contract corrections: `tests/adapters/test_command_renderer.py` (valid examples for the two new placeholders), `tests/issue_210/test_authority_surface_removed.py` (closed recognition of the reviewed atomic schema initializer plus adversarial probes), `tests/test_ci_contract.py` (the exact four-suite Node command), `tests/workflow/test_graph_kernel.py` (the exact seven-node execution order), and `tests/workflow/test_graph_properties.py` (complete, disjoint hash-sensitive and hash-stable snapshot variants). Reword only the accidental retired-route token in the existing retry-transition test docstring. Move the existing automated retry checklist block under `Distribution And Quality Evidence` as a third-level heading; preserve its closed top-level section contract and external `acceptance_status: not_run`.

**Interfaces:**
- Consumes: all prior public behavior and persisted state. Produces final integrated acceptance evidence and a passing canonical quality gate.

- [x] **Step 1: Add the decisive acceptance regression first.** Create two sequential completed/triaged Sprints from one accepted multi-Story artifact. Snapshot original rows/events/receipts and accepted content; retry exactly Sprint 2, finish every normal action, reload, and compare the historical subset. Unselected unrelated Story remains untouched. Misleading numeric ID/completion-time fixtures cannot change target eligibility.

The existing `seed_started_execution_with_unselected_story` fixture creates separate Story artifacts; it does not satisfy this regression. Build one `CanonicalStoryOutput` containing at least three distinct `StoryItemEnvelope` identities, record/accept it once through normal Story transitions, and assert every scoped/unrelated Story has the same `source_story_artifact_id`. Execute Story 1 in Sprint 1 and Story 2 in Sprint 2, retaining Story 3 unselected. Adapt each Sprint plan's `story_item_id` to its actual accepted item. Advance the synthetic clock between cycles so lineage times are deliberately ordered.
Verified fixture building blocks: `tests/test_create_user_story.py:_story_content(item_count=3)` constructs the required one-artifact payload with three distinct item fingerprints. Adapt the real `RecordStoryDraft` / `DecideStory` sequence from `tests/workflow/test_planning_transitions.py:_record_and_accept_story`, retaining all returned activated identities and validating each structurally; its unmodified form asserts only `US-0001`. Use the `_JourneyClock` pattern from `tests/workflow/test_single_project_graph.py`, distinct receipt keys for each Sprint, and the actual accepted `source_story_item_id` in each plan. Existing close helpers hardcode receipt keys and must be parameterized or composed locally for two cycles. Capture rows with the existing schema test helper, then compare the preexisting primary-key subset so appended audit/receipt rows remain allowed.

```python
original = capture_original_execution_and_requirements(engine)
retry = retry_and_start(domain, sprint_id=second_sprint_id)
finish_retry_through_normal_actions(domain, retry)
reloaded = fresh_domain(engine)
assert capture_original_execution_and_requirements(engine) == original
assert reloaded.position(project_id).has_no_retry_work_pending
assert current_retry(reloaded, project_id).status == "Completed"
assert first_sprint_id not in retry.selected_sprint_ids
```

Implement test helper assertions against actual evaluation fields; the final condition should compare the retry's exact `sprint_id` where the public model uses a scalar. Capture original rows by their preexisting primary keys so newly appended audit events do not count as corruption.
- [x] **Step 2: Add no-side-effect and historical-compatibility assertions.** Patch actual provider invocation/Git mutation/rebind/file cleanup boundaries to fail if called during preview/apply/start/completion. Inspect original serialized request/fingerprint golden fixtures from before retry. Exercise profile reopen after additive schema upgrade, failure rollback, concurrency, and repeat retry.
- [x] **Step 3: Run focused acceptance suite and fix only reproduced defects through TDD.** `uv run --locked --exact --python 3.13.15 pytest tests/workflow/test_sprint_retry_acceptance.py tests/workflow/test_sprint_retry_execution.py tests/workflow/test_sprint_retry_transitions.py -q`.
- [x] **Step 4: Run canonical checks once focused checks pass.** The controller loaded `pyrepo-check` and verified its interface. `./agileforge-dev check` runs lock validation, `pyrepo-check --python 3.13.15 --all` (ruff/annotations/ty/bandit/pytest), registered Node suites, whitespace, and distribution verification. Run that once, plus formatting and all Node suites below; do not repeat whole pytest/ruff/ty after their successful canonical run without a new failure or code change. Real browser coverage is the explicit Task 6 command and is not implied by Chromium installation or Node tests. Run the canonical check in normal output mode so no stage output is truncated; preserve the full transcript and native exit, and inspect every stage.

Freeze every tracked file and the index/HEAD during canonical validation, including this plan and the acceptance checklist. Only ignored reports and evidence may change until native completion. The first canonical attempt exposed the quality corrections named above; its pytest process was intentionally interrupted after confirmed static failures and the gate remains incomplete. A permitted checklist edit also triggered the full tracked-state guard, which must remain strict. After the minimal fixes, run the affected projection/request tests and the complete canonical gate again on the frozen state; do not label the interrupted pytest/coverage or unrun later stages as passing.

The second canonical run completed with native exit 1: 7 failed, 3252 passed, 33 skipped, 1 deselected and 119 warnings in 5987.51 seconds. Lock, Ruff, annotations, Ty and Bandit passed; registered Node, whitespace and distribution stages did not run. Coverage was reported as partial guidance (83.54%), with the minimum not applied. ROOT inspected all seven failures and their separate focused reproductions. The before/after manifests match all 689 tracked file hashes, semantic index entries, HEAD, branch and status. Release corrections only after that verified completion; no production defect was established by these failures.

For the authority scanner, Ruling 28 requires a closed normalized AST match for the reviewed atomic initializer, exact module-wide guard/create call and binding checks, and unchanged individual sentinel allowances and retired-token rules. Add meaningful source mutations covering unsafe DDL/rejection ordering, broadened manifests, unrestricted retry DDL, missing postvalidation, premature commit, weakened locking, missing rollback/re-raise, narrowed exception handling, extra guard/DDL calls and guard rebinding/deletion. Each probe must assert that its input changed; retain valid-source acceptance and formatting/comment normalization. Do not change `models/db.py` to satisfy the old scanner syntax.

For snapshot properties, Ruling 6 keeps nonempty `sprint_retries` hash-sensitive and the three derived guard collections excluded from ordinary fact/business hashes. Add real nonempty typed values for every new field. Require sensitive and stable sets to be disjoint and their union to equal all `WorkflowFactSnapshot.model_fields`. Verify ordinary graph decision stability for each guard collection. Also compare two nonempty guard values differing only in metadata through the actual `execution.sprint.retry` NodeSpec: ordinary hashes, decision identity/category/reason stay equal, while the named guard reference and retry decision fingerprints change. Use a local pure typed completed-Sprint fixture, not a copied reference algorithm or another persisted lifecycle.

Write and freeze the new scanner probes and fingerprint characterizations before replacing the scanner helper or correcting the existing seven failing assertions/fixtures. ROOT captures those selectors together; initial passes over already reviewed behavior are valid characterization, and the existing failures supply the observed RED. Then make only the matching test/document corrections and run short captured statics. ROOT's bounded GREEN is `uv run --locked --exact --python 3.13.15 pytest tests/adapters/test_command_renderer.py tests/issue_210/test_authority_surface_removed.py tests/workflow/test_fresh_project_schema.py tests/workflow/test_sprint_retry_schema.py tests/test_ci_contract.py tests/test_workflow_acceptance_document.py tests/workflow/test_graph_kernel.py tests/workflow/test_graph_properties.py -q`. Keep the acceptance-document test unchanged. After GREEN, repeat the unchanged complete canonical gate with a new full tracked-state freeze and provenance comparison.

Canonical03 then completed with native exit 0. Lock, Python quality (Ruff, annotations, Ty, Bandit and full pytest), all four registered Node suites, whitespace and wheel/source-distribution verification passed. Pytest reported 3267 passed, 33 skipped, 1 deselected and 119 warnings in 5547.66 seconds; registered Node reported 164 passed with no failures or skips. Coverage 83.57% remains partial guidance with the 80% minimum not applied. ROOT inspected the retained native output, restored the truncated middle of the Node transcript from the full raw log, read metadata/exit, and verified all 689 tracked file hashes plus semantic index/HEAD/branch/status unchanged in the before/after manifests. The reported 32 skip selectors exactly match canonical02; the aggregate collection skip remains separately qualified by the source inventory. No source or quality rule was changed during the run.
The required global formatting command was run and reported 44 files, all verified unchanged from the starting commit with unchanged Ruff 0.15.8 and formatting configuration. Preserve that failed result as existing repository formatting debt; do not reformat unrelated baseline files or claim global formatting passed. The same formatter must pass on every Python file changed by this issue (61 files at the recorded Task 7 gate, including the new acceptance test). Keep the full canonical gate unchanged and mandatory. Exact path comparison and expanded scoped formatter argv are retained in the Task 7 evidence and report for independent review.

After the related contract corrections, ROOT's eight-file focused run passed: 250 passed, four existing BaseAgentConfig warnings, native exit 0 in 81.66 seconds. ROOT's format-all-02 reported 42 files that would be reformatted and 345 already formatted (native exit 1); format-changed-02 passed all 66 issue-changed Python paths (native exit 0). The exact remaining debt set is the earlier 44 paths minus the now-corrected renderer and CI tests; every remaining path is unchanged from the starting commit, and the Ruff lock/configuration are unchanged. The exact path proof and expanded formatter argv are retained; preserve the original 44/61 evidence as chronology. The existing all-Node result remains valid on unchanged frontend files; the final canonical run still executes its registered Node stage.

```text
sh ./agileforge-dev info --profile issue-260-execution --json
sh ./agileforge-dev check
uv run --locked --exact --python 3.13.15 ruff format --check .
node --test tests/*.mjs
```

- [x] **Step 5: Update acceptance checklist with commands/results and commit.** `git commit -m "test: prove Sprint retry history preservation and lifecycle parity"`. Verify clean worktree and original master unchanged.
- [ ] **Step 6: Receive independent task review then broad branch review.** Controller generates diff packages from recorded bases, resolves findings under the SDD loop, and independently inspects acceptance evidence. Finish using `superpowers:finishing-a-development-branch` with merge/PR/keep/discard options. Do not retry an operator Sprint, merge, publish, discard deliverables, or close issue #260 without the corresponding authorization.

The independent Task 7 review is complete through `9cc1d5051d083aac5b453491793e2b6db72fec20`. Its initial review found one schema-scanner gap: a decorator could replace the guard while its function definition remained the sole apparent binding. The added source mutation first failed for that exact bypass; the minimal guard check then passed the focused test and all 117 covering scanner/schema tests. Ruff and Ty passed, formatting passed after newline normalization, and an explicit normalized-AST comparison confirmed the formatting preserved the tested code. A fresh scoped re-review accepted the fix with no new findings. The earlier complete canonical run was not repeated for this one-file test-only strengthening; all production, frontend and configuration bytes still match that run. Cross-task rollback, concurrency, repeat-retry and browser evidence was checked against the retained earlier reviews and native results. Existing warning debt and unexecuted platform skips remain explicit limitations.

Task 7's implementation and independent task-review gates are complete. Step 6 remains unchecked because the final whole-branch review and finishing workflow are still pending. The original master is preserved at the starting commit. The protected untracked `task-3-workflow-test.log` remains after automatic approval review rejected deletion; a clean tracked worktree must not be described as having no untracked evidence.

The whole-branch review then found one retry-start lifecycle gap and one delayed-preview ownership defect. The first coordinated fix at `df58c17d` added the shared exact Planned-scope guard and preview request ownership. ROOT captured genuine failing regressions before production changes, then passing start, Node, browser and scoped static checks. The single scoped review confirmed the start guard and identified one remaining preview interleaving: a newer mutation starts and finishes before the old GET returns.

The next full gate was interrupted after it exposed a new stale-start reason name that collided with the repository's removed-name inventory. ROOT reproduced that exact existing inventory assertion. Ruling 33 authorizes one bounded follow-up correction and scoped review: rename the new reason to `SPRINT_RETRY_START_SCOPE_STALE`, invalidate preview ownership when a cockpit mutation begins, and add a retained test using the actual busy-start/busy-end hooks. The earlier temporary deferral of the minor preview defect is superseded by this correction plan. Keep the inventory rules and all existing assertions unchanged. ROOT owns RED/GREEN, the relevant start and browser checks, all Node suites, and a fresh unchanged complete gate on the final frozen commit. Step 6 remains pending until these results and the finishing handoff are complete.

## Plan self-review

### Task 8: Address PR 264 provenance feedback and CI timeouts

Approved follow-up, 2026-09-10: finish PR 264 before beginning issue 265. Base is
`d98c16b55fcbfb5cf01aa5152da9f8e8e240629f`. The completed broad review and earlier
fixes remain accepted; this task receives a fresh review of its bounded delta.
ROOT owns all checks through the existing capture helper and every phase release.
The implementer owns only the files explicitly released below, with no subagents,
GitHub writes, operator runtime changes, cleanup, or unrequested production edits.

**Files:** `frontend/project.js`, `tests/test_sprint_retry_dashboard.mjs`,
`tests/workflow/test_sprint_retry_transitions.py`, `.github/workflows/ci.yml`, and
`tests/test_ci_contract.py`, plus the existing retry browser case in
`tests/e2e/test_single_project_lifecycle_ui.py`. ROOT owns this plan and the existing
SDD ledger.

**Requirements and decisions:**

- Show the retry preview's descriptive persisted repository provenance before
  confirmation: binding ID, worktree path, branch, HEAD and binding fingerprint.
  Use accessible labels, wrapping for long values, and the existing escaping
  helper for every interpolated string. Keep scope, blockers and dialog ownership.
- Validate the API's actual variants: `null` means unbound; `state: invalid` has a
  positive binding ID; `state: bound` has a positive binding ID, nonempty worktree
  path and HEAD strings, a null or nonempty branch, and a SHA-256 binding
  fingerprint. A null branch is valid detached HEAD; label it explicitly. Render clear
  unbound/invalid explanations. These recognized variants remain descriptive;
  add no branch restriction, Git operation, server blocker or binding mutation.
  Missing, unknown or malformed provenance rejects the preview through the
  existing invalid-preview path. Do not invent tighter Git syntax constraints.
- Exercise bound/unbound/invalid display and malformed response rejection through
  the actual dashboard harness, including escaping and no automatic POST. Preserve
  explicit confirmation and its existing server-bound fingerprint.
- Strengthen the existing issue-260 browser lifecycle case with representative
  bound provenance and visible-value assertions before its existing screenshot
  and confirmation. Preserve the request counts and rest of that lifecycle.
- Characterize an old-business, pre-completion Sprint-plan attempt with no terminal
  outcome: it must not block retry; its later continuation must return
  `ATTEMPT_OBSOLETE` and create no planning artifact or changes to existing business
  rows. Use existing synthetic lifecycle fixtures and real domain continuation,
  with matching durable request/attempt identity and an unexpired lease so the
  business mismatch is the relevant guard. Test accepted current behavior; do not
  change generation eligibility or manufacture a failing production bug. Point out
  any limitations of the fixture or additional mismatch guards in the report.
- Set only the Linux full gate's job timeout to **90 minutes** and the Windows
  full gate's to **180 minutes**. Update their existing CI contract coverage.
  Preserve all test selection, security cases, runner versions and canonical
  commands. These are validation budgets, not a performance improvement for #265.
  GitHub run 34427482972 stopped at 45/90 minutes; local final pytest completed in
  101m46s. Keep sufficient room for the entire gate and hosted-runner variation.

- [x] Phase 1: add tests only, freeze files, and report exact selectors to ROOT.
  ROOT captures genuine failing UI/CI contracts and the passing or failing
  characterization separately. Explain each assertion's protected behavior.
- [x] Phase 2: after ROOT releases production, implement only the dialog and CI
  changes; freeze files for ROOT's checks. No checks or commits by the implementer.
- [x] Phase 3: ROOT verifies all Node suites, owning Python characterization and
  existing late-output/relevance tests, CI contracts, focused browser coverage,
  scoped Ruff/format/Ty and whitespace. Correct genuine failures within this task.
  CI will run the unchanged full canonical gate on both hosted platforms after
  push; do not duplicate the approximately 102-minute local gate for this delta.
- [ ] Phase 4: ROOT inspects the actual diff, commits the reviewed candidates,
  obtains fresh independent spec/quality review, and addresses scoped findings.
  Push the accepted follow-up, verify hosted CI on that exact commit, and merge
  only after successful checks and review as authorized by the user's proceed.
  Preserve all worktree evidence; do not run an operator Sprint retry or #265 work.

Coverage: storage/history/schema (1), binding/dependencies/hash compatibility (2), eligibility/atomicity/start (3), full lifecycle/replay/repeat retry (4), CLI/API/read parity (5), dashboard/reload (6), two-Sprint acceptance/no external mutation/quality and delivery (7). Interfaces are additive and source requirements remain original. Original serialized transition payloads and empty-retry snapshot fingerprints remain unchanged. Steps carry concrete tests, implementation algorithms, verification commands, and commits; exact existing adapter/test paths are resolved in the task report before edits where repository organization supplies their names.
