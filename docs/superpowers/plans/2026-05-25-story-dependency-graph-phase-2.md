# Story Dependency Graph Phase 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add first-class, reviewable story dependency data so AgileForge can distinguish real prerequisite relationships from rank/order hints before Sprint selection moves to DAG rules.

**Architecture:** Store story dependency edges as explicit rows with `proposed` vs `active` status. Story generation may propose dependency candidates, but only guarded dependency apply commands promote edges into active project structure. Sprint candidates expose active dependency metadata and block invalid graphs, while Phase 3 will use those active edges for dependency-closed Sprint selection.

**Tech Stack:** Python, SQLModel, SQLite migrations, Pydantic, AgileForge CLI, pytest, existing agent-workbench phase services.

---

## Scope Boundary

Implement **Phase 2 only**:

1. Persist explicit story dependency edges.
2. Let Story artifacts propose dependency candidates safely.
3. Add CLI inspect/propose/apply path for reviewing dependency graph.
4. Add graph validation and cycle diagnostics.
5. Surface dependency metadata in Sprint candidates.

Do **not** implement DAG-based Sprint auto-selection in this phase. Phase 3 will upgrade `services/sprint_selection.py` after active dependency rows are trusted.

## Key Safety Rules

- LLM output may propose dependency candidates; it must not directly create active dependency truth.
- Active graph changes require guarded CLI apply with `attempt_id`, `artifact_fingerprint`, `expected_state`, and `idempotency_key`.
- Dependency propose/apply idempotency is stored in workflow state under phase-specific keys:
  - `story_dependency_propose_idempotency_keys`
  - `story_dependency_apply_idempotency_keys`
  and mirrored in `WorkflowEvent.event_metadata`.
- Dependency proposal attempts are stored under `state["story_dependency_attempts"]`.
  Keep only the latest 20 dependency attempts per project/session to avoid unbounded session-state growth.
- Dependency repair/proposal may infer candidate edges from rank/order, but inferred edges remain `proposed`.
- Sprint planning must block on graph corruption: self-dependencies, cross-project edges, missing stories, superseded stories, and cycles.
- Sprint planning must not yet treat rank as dependency truth.
- Once a story is linked to a saved Sprint, dependency replacement for that story is unsafe and must block.
- Existing `proposed` dependency rows for a story must be cleared before writing that story's newly proposed candidates, so story refinement cannot leave stale proposed edges behind.
- Dependency inspect/apply must not crash on orphan edges. Orphans are surfaced as `STORY_DEPENDENCY_ORPHAN` issues and ignored for active planning truth until repaired or rejected.
- Dependency apply must be able to reject/deactivate active edges in `STORY_PERSISTENCE`, `SPRINT_SETUP`, and `SPRINT_DRAFT`, even when the current active graph has a cycle. Cycle validation must block Sprint planning, not trap the project in an unrepairable state.
- Test DB helpers must explicitly assert SQLite foreign key enforcement with `PRAGMA foreign_keys` so in-memory tests mirror production connection behavior.
- If a project/product hard-delete path exists, dependency rows for that `product_id` must be purged in the same transaction or covered by verified FK cascade behavior.

## File Map

- Modify: `models/core.py`
  - Add `UserStoryDependency` table model and relationships if needed.
- Modify: `db/migrations.py`
  - Create dependency table and indexes for existing SQLite databases.
- Create: `services/story_dependencies.py`
  - Pure-ish dependency graph service: load nodes, validate edges, detect cycles, build inspect payloads, apply reviewed graph changes.
- Modify: `orchestrator_agent/agent_tools/user_story_writer_tool/schemes.py`
  - Add dependency candidate schema on story output items.
- Modify: `orchestrator_agent/agent_tools/user_story_writer_tool/tools.py`
  - Resolve dependency candidates during story save and persist them as `proposed`.
- Modify: `services/agent_workbench/story_phase.py`
  - Add dependency inspect/propose/apply service methods and unsafe-change guards.
- Modify: `services/agent_workbench/application.py`
  - Expose story dependency methods through workbench application.
- Modify: `services/agent_workbench/command_registry.py`
  - Register installed dependency commands and command contracts.
- Modify: `cli/main.py`
  - Add `agileforge story dependencies inspect|propose|apply`.
- Modify: `services/orchestrator_query_service.py`
  - Include active dependency metadata in Sprint candidates.
- Modify: `services/sprint_input.py`
  - Pass dependency metadata through to Sprint planner input context as informational hints.
- Modify: `orchestrator_agent/agent_tools/sprint_planner_tool/schemes.py`
  - Add optional `prerequisite_story_ids` and `blocked_by_story_ids` to `SprintPlannerStory`.
- Modify: `docs/agent-cli-manual.md`
  - Document dependency workflow.
- Tests:
  - Create: `tests/test_story_dependencies.py`
  - Modify: `tests/test_user_story_writer_schemas.py`
  - Modify: `tests/test_save_stories_tool.py`
  - Modify: `tests/test_agent_workbench_story_phase.py`
  - Modify: `tests/test_agent_workbench_cli.py`
  - Modify: `tests/test_agent_workbench_command_schema.py`
  - Modify: `tests/test_sprint_runtime.py`
  - Modify: `tests/test_orchestrator_tools.py`

---

## Task 1: Add Dependency Storage

**Files:**
- Modify: `models/core.py`
- Modify: `db/migrations.py`
- Create: `tests/test_story_dependencies.py`

- [ ] **Step 1: Write model/migration tests**

Create `tests/test_story_dependencies.py` with tests for:

```python
def test_dependency_table_accepts_proposed_edge() -> None:
    ...


def test_dependency_table_prevents_duplicate_edge() -> None:
    ...


def test_dependency_validation_rejects_self_edge() -> None:
    ...


def test_dependency_test_engine_enforces_sqlite_foreign_keys() -> None:
    ...
```

Expected model behavior:

- one edge row per `(product_id, dependent_story_id, prerequisite_story_id)`
- `status` is one of `proposed`, `active`, `rejected`
- `source` identifies origin: `story_writer`, `dependency_repair`, `manual_review`
- `confidence` identifies confidence: `explicit`, `inferred`, `reviewed`
- self-edge is invalid at service layer even if DB permits insert during migration compatibility

- [ ] **Step 2: Run tests red**

Run:

```bash
uv run --frozen pytest tests/test_story_dependencies.py -q
```

Expected: FAIL because model/service do not exist.

- [ ] **Step 3: Add `UserStoryDependency` model**

Add to `models/core.py` near `UserStory`:

```python
class UserStoryDependency(SQLModel, table=True):
    """Explicit prerequisite edge between two user stories."""

    __tablename__ = "user_story_dependencies"  # type: ignore[assignment]

    dependency_id: int | None = Field(default=None, primary_key=True)
    product_id: int = Field(foreign_key="products.product_id", index=True)
    dependent_story_id: int = Field(foreign_key="user_stories.story_id", index=True)
    prerequisite_story_id: int = Field(foreign_key="user_stories.story_id", index=True)
    status: str = Field(default="proposed", index=True)
    confidence: str = Field(default="inferred", index=True)
    source: str = Field(default="story_writer", index=True)
    reason: str | None = Field(default=None, sa_type=Text)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column_kwargs={"server_default": func.now()},
        nullable=False,
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column_kwargs={
            "server_default": func.now(),
            "onupdate": func.now(),
        },
        nullable=False,
    )
```

- [ ] **Step 4: Add migration**

In `db/migrations.py`, add migration function:

```python
def migrate_user_story_dependencies(engine: Engine) -> list[str]:
    """Ensure user story dependency graph table and indexes exist."""
    actions: list[str] = []
    # create table if missing
    # add indexes:
    # ix_user_story_dependencies_product_status
    # ix_user_story_dependencies_dependent
    # ix_user_story_dependencies_prerequisite
    # unique product_id/dependent_story_id/prerequisite_story_id
    return actions
```

Wire it into the main migration runner near other user story migrations.

Migration requirements:

- create `user_story_dependencies` with foreign keys using `ON DELETE CASCADE` where SQLite supports it
- create unique index on `(product_id, dependent_story_id, prerequisite_story_id)`
- create lookup indexes:
  - `(product_id, status)`
  - `(dependent_story_id)`
  - `(prerequisite_story_id)`
- do not rely on cascade for superseded stories, because AgileForge usually soft-supersedes rows rather than deleting them
- tests that use in-memory SQLite must run `PRAGMA foreign_keys` and assert the value is `1`

- [ ] **Step 5: Verify**

Run:

```bash
uv run --frozen pytest tests/test_story_dependencies.py -q
uv run --frozen ruff check models/core.py db/migrations.py tests/test_story_dependencies.py
```

Expected: PASS.

---

## Task 2: Add Graph Service And Cycle Diagnostics

**Files:**
- Create: `services/story_dependencies.py`
- Modify: `tests/test_story_dependencies.py`

- [ ] **Step 1: Write service tests**

Add tests:

```python
def test_build_dependency_graph_rejects_missing_story() -> None:
    ...


def test_build_dependency_graph_rejects_superseded_story() -> None:
    ...


def test_detect_cycle_returns_cycle_path() -> None:
    ...


def test_inspect_payload_separates_active_and_proposed_edges() -> None:
    ...
```

- [ ] **Step 2: Run red**

```bash
uv run --frozen pytest tests/test_story_dependencies.py -q
```

Expected: FAIL.

- [ ] **Step 3: Implement graph service**

Create `services/story_dependencies.py` with:

```python
@dataclass(frozen=True)
class DependencyGraphIssue:
    code: str
    message: str
    story_ids: list[int]


@dataclass(frozen=True)
class DependencyGraph:
    active_edges: dict[int, set[int]]
    proposed_edges: dict[int, set[int]]
    issues: list[DependencyGraphIssue]
    cycle_paths: list[list[int]]
```

Required functions:

```python
def load_story_dependency_graph(session: Session, *, project_id: int) -> DependencyGraph:
    ...


def detect_dependency_cycles(edges: dict[int, set[int]]) -> list[list[int]]:
    ...


def dependency_inspect_payload(session: Session, *, project_id: int) -> dict[str, Any]:
    ...


def assert_dependency_graph_valid_for_sprint(
    session: Session,
    *,
    project_id: int,
) -> None:
    ...
```

Rules:

- active edges only matter for Sprint blocking
- proposed edges shown in inspect payload, not used as truth
- graph issue codes:
  - `STORY_DEPENDENCY_SELF_EDGE`
  - `STORY_DEPENDENCY_MISSING_STORY`
  - `STORY_DEPENDENCY_SUPERSEDED_STORY`
  - `STORY_DEPENDENCY_ORPHAN`
  - `STORY_DEPENDENCY_CYCLE`
  - `STORY_DEPENDENCY_CROSS_PROJECT`

Inspect payload cycle format:

```json
{
  "cycle_count": 1,
  "cycle_paths": [
    {
      "story_ids": [66, 85, 66],
      "story_titles": [
        "Enforce Required Budget Parameter",
        "Execute Live Pre-Lock Recommendation Workflow",
        "Enforce Required Budget Parameter"
      ]
    }
  ]
}
```

Orphan rule:

- if either endpoint story row is missing, emit `STORY_DEPENDENCY_ORPHAN`
- if either endpoint is superseded, emit `STORY_DEPENDENCY_SUPERSEDED_STORY`
- do not dereference missing story objects
- invalid active edges are excluded from planner-ready dependency metadata

- [ ] **Step 4: Verify**

```bash
uv run --frozen pytest tests/test_story_dependencies.py -q
uv run --frozen ruff check services/story_dependencies.py tests/test_story_dependencies.py
```

Expected: PASS.

---

## Task 3: Story Writer Dependency Candidates

**Files:**
- Modify: `orchestrator_agent/agent_tools/user_story_writer_tool/schemes.py`
- Modify: `orchestrator_agent/agent_tools/user_story_writer_tool/tools.py`
- Modify: `tests/test_user_story_writer_schemas.py`
- Modify: `tests/test_save_stories_tool.py`

- [ ] **Step 1: Add schema tests**

Add tests proving output accepts:

```json
{
  "dependency_candidates": [
    {
      "prerequisite_ref": "Capture Pre-Lock Cartola Market Data",
      "reason": "Live recommendation needs capture before run.",
      "confidence": "explicit"
    }
  ]
}
```

Also test invalid confidence fails.

- [ ] **Step 2: Add output schema**

In `UserStoryItem`, add:

```python
class StoryDependencyCandidate(BaseModel):
    """Candidate prerequisite edge proposed by Story generation."""

    model_config = ConfigDict(extra="forbid")

    prerequisite_ref: Annotated[
        str,
        Field(min_length=1, description="Story id, title, or requirement/slot reference."),
    ]
    reason: Annotated[
        str,
        Field(min_length=3, description="Why prerequisite must precede this story."),
    ]
    confidence: Annotated[
        Literal["explicit", "inferred"],
        Field(description="Whether source explicitly states dependency or model inferred it."),
    ]
```

Then:

```python
dependency_candidates: list[StoryDependencyCandidate] = Field(default_factory=list)
```

- [ ] **Step 3: Persist resolved candidates as proposed**

In `user_story_writer_tool/tools.py`, after story rows are created/updated:

- collect IDs of stories created/updated in the current save batch
- delete existing dependency rows where:
  - `product_id == input_data.product_id`
  - `dependent_story_id` is one of those current batch story IDs
  - `status == "proposed"`
  - `source == "story_writer"`
- do not delete `active` rows during normal story save
- do not delete `manual_review` rows during normal story save
- resolve candidate `prerequisite_ref` against active non-superseded stories in same project
- matching order:
  1. numeric story ID
  2. exact normalized story title
  3. `source_requirement#slot` syntax
  4. same `source_requirement` + lower `refinement_slot` only when the `prerequisite_ref` string matches the exact normalized title of the parent requirement or its requirement key
- if `confidence="explicit"` and unresolved: fail story save with clear error
- if `confidence="explicit"` resolves to multiple stories: fail story save with clear ambiguity diagnostic
- if `confidence="inferred"` and unresolved: skip and include warning metadata
- if resolver raises an unexpected parsing/lookup exception: convert it to a structured dependency warning for inferred candidates, or a structured save failure for explicit candidates; do not leak `AttributeError` / `ValueError` stack failures
- persisted rows use `status="proposed"`, not `active`

- [ ] **Step 4: Verify**

```bash
uv run --frozen pytest tests/test_user_story_writer_schemas.py tests/test_save_stories_tool.py -q
uv run --frozen ruff check orchestrator_agent/agent_tools/user_story_writer_tool/schemes.py orchestrator_agent/agent_tools/user_story_writer_tool/tools.py tests/test_user_story_writer_schemas.py tests/test_save_stories_tool.py
```

Expected: PASS.

---

## Task 4: Dependency Inspect / Propose / Apply CLI

**Files:**
- Modify: `cli/main.py`
- Modify: `services/agent_workbench/story_phase.py`
- Modify: `services/agent_workbench/application.py`
- Modify: `services/agent_workbench/command_registry.py`
- Modify: `tests/test_agent_workbench_story_phase.py`
- Modify: `tests/test_agent_workbench_cli.py`
- Modify: `tests/test_agent_workbench_command_schema.py`

- [ ] **Step 1: Add command contract tests**

Expected commands:

```bash
agileforge story dependencies inspect --project-id 2
agileforge story dependencies propose --project-id 2 --expected-state SPRINT_DRAFT --idempotency-key dep-propose-2-001
agileforge story dependencies apply --project-id 2 --attempt-id <attempt_id> --expected-artifact-fingerprint <fingerprint> --expected-state SPRINT_DRAFT --idempotency-key dep-apply-2-001
```

States allowed for propose/apply:

- `STORY_PERSISTENCE`
- `SPRINT_SETUP`
- `SPRINT_DRAFT`

Block if saved Sprint links exist for affected stories.

Do not block `inspect`, `propose`, or `apply` merely because the current active graph has a cycle. Otherwise an active bad graph can deadlock the project in `SPRINT_SETUP` or `SPRINT_DRAFT`.

- [ ] **Step 2: Add service behavior**

In `story_phase.py`, add:

```python
def dependency_inspect(project_id: int) -> dict[str, Any]:
    ...


def dependency_propose(
    *,
    project_id: int,
    expected_state: str,
    idempotency_key: str,
) -> dict[str, Any]:
    ...


def dependency_apply(
    *,
    project_id: int,
    attempt_id: str,
    expected_artifact_fingerprint: str,
    expected_state: str,
    idempotency_key: str,
) -> dict[str, Any]:
    ...
```

`propose` builds a pending dependency artifact from:

- proposed rows already saved by Story phase
- deterministic candidate edges from `source_requirement` / `refinement_slot`
- never active by default

`apply` consumes the exact reviewed artifact identified by `attempt_id` and `expected_artifact_fingerprint`.

Apply semantics:

- promote artifact edges with `selected=true` to `active`
- mark all other proposed edges for the given project that were not included in the applied fingerprint's edge list as `rejected`
- support explicit `status="rejected"` entries for active edges so an operator can break a bad active cycle
- reject applying a graph that would introduce a new active cycle unless the artifact action is removing/rejecting edges and the resulting active graph has fewer or no cycle paths
- record replay payload in `story_dependency_apply_idempotency_keys`
- record proposal replay payload in `story_dependency_propose_idempotency_keys`

- [ ] **Step 3: Add guarded artifact metadata**

Dependency proposal output must include:

```json
{
  "attempt_id": "story-dependencies-550e8400-e29b-41d4-a716-446655440000",
  "artifact_fingerprint": "sha256:...",
  "is_complete": true,
  "active_edge_count": 0,
  "proposed_edge_count": 12,
  "cycle_count": 0,
  "edges": [...]
}
```

Attempt ID rule:

- generate dependency proposal attempt IDs with `uuid.uuid4()`
- do not use sequential strings like `story-dependencies-attempt-1`
- store attempt payload under `state["story_dependency_attempts"]`
- prune `state["story_dependency_attempts"]` to the latest 20 attempts after each successful proposal
- store propose/apply idempotency replay payloads under the phase-specific idempotency maps

`WorkflowEvent.event_metadata` schema for `dependencies propose`:

```json
{
  "action": "story_dependencies_proposed",
  "idempotency_key": "dep-propose-2-001",
  "attempt_id": "story-dependencies-550e8400-e29b-41d4-a716-446655440000",
  "artifact_fingerprint": "sha256:...",
  "project_id": 2,
  "proposed_edge_count": 12,
  "active_edge_count": 0,
  "cycle_count": 0,
  "edge_ids": [1, 2, 3]
}
```

`WorkflowEvent.event_metadata` schema for `dependencies apply`:

```json
{
  "action": "story_dependencies_applied",
  "idempotency_key": "dep-apply-2-001",
  "attempt_id": "story-dependencies-550e8400-e29b-41d4-a716-446655440000",
  "artifact_fingerprint": "sha256:...",
  "project_id": 2,
  "activated_edges": [
    {
      "dependent_story_id": 85,
      "prerequisite_story_id": 66,
      "reason": "Live workflow requires explicit budget guard first.",
      "source": "manual_review",
      "confidence": "reviewed"
    }
  ],
  "rejected_edges": [
    {
      "dependent_story_id": 79,
      "prerequisite_story_id": 78,
      "reason": "Reviewer rejected this candidate for current scope."
    }
  ],
  "active_edge_count": 1,
  "rejected_edge_count": 1,
  "cycle_count_after_apply": 0
}
```

- [ ] **Step 4: Wire CLI**

Add subcommands under `story dependencies`.

CLI envelopes must be normal AgileForge envelopes:

- `ok: true` on inspect/proposal/apply success
- `ok: false` with `MUTATION_FAILED` / `INVALID_COMMAND` on guarded failures

- [ ] **Step 5: Verify**

```bash
uv run --frozen pytest tests/test_agent_workbench_story_phase.py tests/test_agent_workbench_cli.py tests/test_agent_workbench_command_schema.py -q
uv run --frozen ruff check cli/main.py services/agent_workbench/story_phase.py services/agent_workbench/application.py services/agent_workbench/command_registry.py tests/test_agent_workbench_story_phase.py tests/test_agent_workbench_cli.py tests/test_agent_workbench_command_schema.py
```

Expected: PASS.

---

## Task 5: Sprint Candidate Dependency Visibility And Graph Gate

**Files:**
- Modify: `services/orchestrator_query_service.py`
- Modify: `services/sprint_input.py`
- Modify: `orchestrator_agent/agent_tools/sprint_planner_tool/schemes.py`
- Modify: `tests/test_orchestrator_tools.py`
- Modify: `tests/test_sprint_runtime.py`

- [ ] **Step 1: Add candidate payload tests**

Test `sprint candidates` includes:

```json
{
  "prerequisite_story_ids": [67],
  "blocked_by_story_ids": [],
  "dependency_status": "ready"
}
```

If graph has cycle, readiness must be blocked:

```json
{
  "status": "blocked",
  "blocking_codes": ["STORY_DEPENDENCY_CYCLE"],
  "dependency_cycle_paths": [[66, 85, 66]]
}
```

- [ ] **Step 2: Add graph validation to candidate fetch**

In `fetch_sprint_candidates_from_session`, load active graph:

- add prerequisite IDs for each story
- add blocker IDs when prerequisite story is not completed and not sprint-eligible
- add readiness blockers for invalid graph issues
- if dependency table is unavailable because a test or legacy DB has not run migrations, return candidates with empty dependency metadata and a readiness warning `STORY_DEPENDENCY_SCHEMA_MISSING`; do not crash candidate listing

Do not remove candidates solely because they have prerequisites. Phase 3 will select dependency-closed cohorts.

- [ ] **Step 3: Pass metadata into Sprint input**

In `services/sprint_input.py`, include:

```python
"prerequisite_story_ids": list(row.get("prerequisite_story_ids") or []),
"blocked_by_story_ids": list(row.get("blocked_by_story_ids") or []),
"dependency_status": row.get("dependency_status"),
```

- [ ] **Step 4: Add schema fields**

In `SprintPlannerStory`, add optional input fields:

```python
prerequisite_story_ids: list[int] = Field(default_factory=list)
blocked_by_story_ids: list[int] = Field(default_factory=list)
dependency_status: str | None = Field(default=None)
```

- [ ] **Step 5: Verify**

```bash
uv run --frozen pytest tests/test_orchestrator_tools.py tests/test_sprint_runtime.py -q
uv run --frozen ruff check services/orchestrator_query_service.py services/sprint_input.py orchestrator_agent/agent_tools/sprint_planner_tool/schemes.py tests/test_orchestrator_tools.py tests/test_sprint_runtime.py
```

Expected: PASS.

---

## Task 6: Documentation And Live caRtola Procedure

**Files:**
- Modify: `docs/agent-cli-manual.md`

- [ ] **Step 1: Document dependency workflow**

Add section:

```markdown
### Story Dependency Graph

Use this before Sprint save when stories have prerequisite relationships.

1. Inspect current graph:
   `agileforge story dependencies inspect --project-id <id>`

2. Generate/review proposed graph:
   `agileforge story dependencies propose --project-id <id> --expected-state <state> --idempotency-key <key>`

3. Apply reviewed graph:
   `agileforge story dependencies apply --project-id <id> --attempt-id <attempt_id> --expected-artifact-fingerprint <fingerprint> --expected-state <state> --idempotency-key <key>`

Proposed edges are not active authority. Active edges are the only dependency truth used by Sprint diagnostics.
```

- [ ] **Step 2: Add caRtola smoke procedure**

Document:

```bash
cd /Users/aaat/projects/caRtola
agileforge story dependencies inspect --project-id 2
agileforge story dependencies propose --project-id 2 --expected-state SPRINT_DRAFT --idempotency-key dep-propose-cartola-001
agileforge story dependencies apply --project-id 2 --attempt-id <attempt_id> --expected-artifact-fingerprint <fingerprint> --expected-state SPRINT_DRAFT --idempotency-key dep-apply-cartola-001
agileforge sprint candidates --project-id 2
```

- [ ] **Step 3: Verify**

```bash
rg -n "Story Dependency Graph|dependencies inspect|dependencies propose|dependencies apply" docs/agent-cli-manual.md
```

Expected: matches.

---

## Task 7: Phase 2 Verification

**Files:**
- Test only.

- [ ] **Step 1: Focused tests**

```bash
uv run --frozen pytest \
  tests/test_story_dependencies.py \
  tests/test_user_story_writer_schemas.py \
  tests/test_save_stories_tool.py \
  tests/test_agent_workbench_story_phase.py \
  tests/test_agent_workbench_cli.py \
  tests/test_agent_workbench_command_schema.py \
  tests/test_orchestrator_tools.py \
  tests/test_sprint_runtime.py \
  -q
```

Expected: PASS.

- [ ] **Step 2: Full suite**

```bash
uv run --frozen pytest -q
```

Expected: PASS.

- [ ] **Step 3: Lint / format**

```bash
uv run --frozen ruff check .
uv run --frozen ruff format --check \
  models/core.py \
  db/migrations.py \
  services/story_dependencies.py \
  orchestrator_agent/agent_tools/user_story_writer_tool/schemes.py \
  orchestrator_agent/agent_tools/user_story_writer_tool/tools.py \
  services/agent_workbench/story_phase.py \
  services/agent_workbench/application.py \
  services/agent_workbench/command_registry.py \
  services/orchestrator_query_service.py \
  services/sprint_input.py \
  orchestrator_agent/agent_tools/sprint_planner_tool/schemes.py \
  tests/test_story_dependencies.py
git diff --check
```

Expected: PASS.

- [ ] **Step 4: Live caRtola no-save smoke**

Run inspect/propose. Do not apply without reviewing generated edges:

```bash
cd /Users/aaat/projects/caRtola
agileforge story dependencies inspect --project-id 2 > dep-inspect-before.json
agileforge story dependencies propose --project-id 2 --expected-state SPRINT_DRAFT --idempotency-key dep-propose-cartola-smoke-001 > dep-propose.json
python -m json.tool dep-propose.json >/dev/null
```

Expected:

- `ok: true`
- proposal has `attempt_id`
- proposal has `artifact_fingerprint`
- proposal has zero cycles
- proposal has proposed edges only, no active edges changed

---

## Phase 3 Handoff

After Phase 2 lands and active edges are reviewed for caRtola, create Phase 3 plan:

- `services/sprint_selection.py` consumes active graph.
- Auto selector selects dependency-closed cohorts.
- Manual `--selected-story-ids` validates dependency closure.
- Optional `--force-selection` requires explicit audit event.
- Sprint generation blocks if selected cohort omits active prerequisites.

## Self-Review

- Scope: Phase 2 creates trusted dependency data and review/apply workflow, but does not change Sprint selector semantics yet.
- Safety: proposed/inferred edges cannot silently affect downstream planning as active truth.
- Existing caRtola state: plan allows dependency proposal/apply from `SPRINT_DRAFT`, before saved Sprint links exist.
- Missing risk covered: cycles and graph corruption block Sprint readiness before planner runs.
- No placeholders: every task names files, commands, expected behavior, and pass criteria.
