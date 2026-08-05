# Single Project Lifecycle Hard Break Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the dual-origin setup architecture with one durable Project lifecycle that starts with a Vision interview, treats local repository attachment as optional Git provenance, and preserves the complete specification-to-delivery workflow.

**Architecture:** Product state lives in versioned business tables and is projected into one `agileforge.workflow.v2` graph; ADK sessions and attempt outputs remain execution traces only. Project Vision and Product Goal use separate host-prepared interviews and separate fingerprint-bound human decisions. A small GitPython adapter probes repository identity and status without reading source contents, while application services prepare every model-backed input from durable facts. Human UI and agent CLI expose task language and semantic choices, never raw workflow JSON or derived guards.

**Tech Stack:** Python 3.12+, SQLModel 0.0.27+, FastAPI 0.119+, Google ADK 2.2.0, GitPython 3.1.57, argparse, vanilla JavaScript, Tailwind, Material Symbols, pytest 9+, Node test runner, Playwright 1.58+, uv.

## Global Constraints

- This is a hard break. Initialize fresh business and trace databases; add no migration, compatibility alias, fallback route, or old-record importer.
- Set `GRAPH_VERSION` to `agileforge.workflow.v2` when the root graph cuts over.
- Project creation accepts only `name`, optional `description`, and optional `repository_path` as business input.
- Vision is Project-owned. Vision records and rules have no Authority prerequisite, Authority ID, or Authority fingerprint.
- Vision and Product Goal are interviewed, reviewed, and accepted separately. Accepted Vision unlocks only the Product Goal interview; accepted Product Goal unlocks discovery.
- Exactly one accepted Product Goal may be active. It remains active across Sprints until a human records it fulfilled or abandoned while no Sprint is active and all closed Sprints under it have completed triage.
- The accepted sequence is Vision, Product Goal, discovery artifact, specification review, Authority review, Backlog, Roadmap, Stories, Sprint, execution, closure, and triage.
- Feature-level Current-State/Gap Assessment is a separate follow-up module after this hard break. It will consume accepted Authority plus targeted repository evidence before Backlog admission.
- Repository attachment never changes graph availability or the graph fact fingerprint.
- Repository probing reads Git metadata and status only. It reads no source-file contents, performs no network request, calls no model, and mutates no Git state.
- Dirty and detached repositories succeed with explicit structured state; only typed probe errors fail.
- Durable business tables remain the source of truth. ADK session state and `WorkflowNodeAttemptOutcome.output_json` may support execution replay but must not determine product position.
- Public UI and CLI commands never ask for commit SHA, dirty state, remotes, artifact fingerprints, graph version, fact fingerprint, decision fingerprint, or model-owned JSON.
- Automated tests use `config/models.test.yaml`, fake agents, and disabled sockets. They make zero paid provider calls.
- Production model roles remain `openrouter/openai/gpt-5.6-luna`; the test role remains `openrouter/openai/gpt-oss-20b:free`. Remove only the retired repository-curation role.
- Use uv only. Do not add pip, Poetry, or npm-based project dependency management.
- Fix every Ruff, annotation, `ty`, and Bandit finding directly. Add no typing suppression, broad `noqa`, or disabled quality rule.
- Define shell-only names for the two retired origin labels without placing either complete label in this plan:

```bash
RETIRED_ORIGIN_A="$(printf '%s%s' 'brown' 'field')"
RETIRED_ORIGIN_B="$(printf '%s%s' 'green' 'field')"
```

- The final tracked-path and tracked-content scan must find zero case-insensitive occurrences of either computed label.
- Preserve existing uncommitted generic work in `services/authority_compilation_input.py`, `services/node_attempt_replay.py`, the ADK runner replay path, project deletion, and their tests. Delete uncommitted files dedicated only to the retired repository-curation path.
- Do not reset, clean, checkout, or overwrite the existing dirty worktree. Every commit stages only the exact paths named in its task.
- After each task, run its focused tests and `git diff --check`. Run the complete repository gate only in Task 10.

---

## Target File Map

### New focused modules

- `services/repository_probe.py`: framework-neutral probe contracts, result models, warnings, and typed errors.
- `adapters/git/repository_probe.py`: GitPython-only production adapter.
- `models/repository.py`: immutable repository observations.
- `models/product_definition.py`: Vision interview, Vision, Product Goal, discovery, and specification-review records.
- `workflow/requests/vision.py`: Vision interview, review, and revision requests.
- `workflow/handlers/vision.py`: transactional Vision interview and review handlers.
- `workflow/requests/product_goal.py`: Product Goal interview, review, fulfillment, and abandonment requests.
- `workflow/handlers/product_goal.py`: transactional Product Goal interview, review, and outcome handlers.
- `workflow/definitions/product_goal.py`: Product Goal interview, review, and outcome graph rules.
- `workflow/requests/product_discovery.py`: discovery and specification requests.
- `workflow/handlers/product_discovery.py`: transactional discovery, specification, and registry handlers.
- `workflow/definitions/product_discovery.py`: discovery and specification graph rules.
- `services/vision_interview_input.py`: host preparation of one Vision turn from durable state.
- `services/product_goal_interview_input.py`: host preparation of one Product Goal turn from accepted Vision and durable Goal state.
- `services/contracts/product_goal.py`: strict Product Goal interview input and output contracts.
- `adapters/adk/agents/product_goal.py`: Product Goal interview agent with no discovery or implementation responsibility.
- `services/project_lifecycle.py`: create, attach, replace, refresh, and status application boundary.

### Existing modules retained and narrowed

- `workflow/domain.py`: one transition authority plus explicit non-positioned Project creation and orthogonal repository mutations.
- `workflow/definitions/root.py`: one graph composed in lifecycle order, without setup or scope-extension wrappers.
- `workflow/definitions/authority.py`: compile only an accepted current specification; remove the Vision boundary.
- `workflow/definitions/backlog.py`, `workflow/definitions/planning.py`, `workflow/definitions/execution.py`: retain delivery behavior and bind it to the current Product Goal through accepted Backlog lineage.
- `repositories/workflow.py`: load only current domain facts and validate exact lineage.
- `services/application.py`: compose repository probe, Vision and Product Goal input preparation, existing Authority input preparation, ADK recipes, and durable reads.
- `services/read_projections.py`: project, repository, Vision, Goal, specification, Authority, and delivery reads from durable facts.
- `api.py`, `cli/main.py`, `cli/workflow_commands.py`: task-specific transports.
- `frontend/index.html`, `frontend/app.js`, `frontend/project.html`, `frontend/project.js`: human-oriented creation, separate Vision and Product Goal interviews, repository status, review, and workflow views.

### Modules deleted at cutover

- `models/${RETIRED_ORIGIN_A}.py`
- `services/contracts/${RETIRED_ORIGIN_A}.py`
- `adapters/adk/agents/${RETIRED_ORIGIN_A}.py`
- `services/${RETIRED_ORIGIN_A}_curation_input.py`
- `utils/${RETIRED_ORIGIN_A}_annotations.py`
- `workflow/definitions/onboarding.py`
- `workflow/definitions/scope_extension.py`
- `workflow/requests/onboarding.py`
- `workflow/requests/scope_extension.py`
- `workflow/requests/project_shell.py`
- `workflow/handlers/onboarding.py`
- `workflow/handlers/scope_extension.py`
- `workflow/handlers/project_shell.py`
- `services/agent_workbench/repository_inventory.py`
- `models/agent_workbench.py`
- `services/specs/lifecycle_service.py`
- `services/agent_workbench/backlog_reconciliation.py`
- `services/agent_workbench/vision_phase.py`
- `services/vision_runtime.py`
- `tests/test_specs_lifecycle_service.py`
- old tests, plans, specs, feedback artifacts, examples, and manuals that exist only to preserve the deleted architecture.

---

### Task 1: Deterministic Repository Probe

**Files:**
- Create: `services/repository_probe.py`
- Create: `adapters/git/__init__.py`
- Create: `adapters/git/repository_probe.py`
- Create: `models/repository.py`
- Modify: `models/__init__.py`
- Modify: `models/db.py`
- Modify: `agile_sqlmodel.py`
- Test: `tests/services/test_repository_probe.py`
- Test: `tests/workflow/test_repository_binding_model.py`

**Interfaces:**
- Consumes: `workflow.contracts.FrozenModel`, `workflow.fingerprints.canonical_hash`, GitPython `Repo.working_tree_dir`, `Repo.common_dir`, `Repo.head.commit`, `Repo.head.is_detached`, `Repo.active_branch`, `Repo.index.diff`, `Repo.untracked_files`, and `Remote.urls`.
- Produces: `RepositoryProbe.inspect(path: Path | str) -> RepositoryProbeResult`, `GitPythonRepositoryProbe`, `RepositoryBinding`, `RepositoryStatusEntry`, `RepositoryProbeWarning`, and `RepositoryProbeError`.

- [ ] **Step 1: Write the probe contract and error tests**

Add exact assertions for all closed outcomes:

```python
def test_missing_path_has_typed_error(tmp_path: Path) -> None:
    path = tmp_path / "missing"
    with pytest.raises(RepositoryProbeError) as caught:
        GitPythonRepositoryProbe().inspect(path)
    assert caught.value.code is RepositoryProbeErrorCode.PATH_MISSING
    assert caught.value.path == str(path)


def test_non_repository_directory_has_typed_error(tmp_path: Path) -> None:
    with pytest.raises(RepositoryProbeError) as caught:
        GitPythonRepositoryProbe().inspect(tmp_path)
    assert caught.value.code is RepositoryProbeErrorCode.NOT_GIT_WORKTREE


def test_unborn_head_has_typed_error(tmp_path: Path) -> None:
    Repo.init(tmp_path)
    with pytest.raises(RepositoryProbeError) as caught:
        GitPythonRepositoryProbe().inspect(tmp_path)
    assert caught.value.code is RepositoryProbeErrorCode.UNBORN_HEAD
```

- [ ] **Step 2: Run the contract tests and verify RED**

Run: `uv run --frozen pytest tests/services/test_repository_probe.py -q`

Expected: collection fails because `services.repository_probe` and `adapters.git.repository_probe` do not exist.

- [ ] **Step 3: Add the strict public probe types**

Implement this closed interface in `services/repository_probe.py`:

```python
class RepositoryProbeErrorCode(StrEnum):
    PATH_MISSING = "PATH_MISSING"
    PATH_NOT_DIRECTORY = "PATH_NOT_DIRECTORY"
    NOT_GIT_WORKTREE = "NOT_GIT_WORKTREE"
    GIT_METADATA_UNREADABLE = "GIT_METADATA_UNREADABLE"
    UNBORN_HEAD = "UNBORN_HEAD"
    REPOSITORY_CHANGED_DURING_PROBE = "REPOSITORY_CHANGED_DURING_PROBE"
    MALFORMED_PATH = "MALFORMED_PATH"


class RepositoryStatusEntry(FrozenModel):
    area: Literal["index", "worktree", "untracked"]
    change: Literal["added", "modified", "deleted", "renamed", "type_changed"]
    path: str
    previous_path: str | None = None


class RepositoryProbeWarning(FrozenModel):
    code: Literal["DIRTY_WORKTREE"]
    message: str


class RepositoryProbeResult(FrozenModel):
    worktree_path: str
    common_git_dir: str
    head_sha: str
    branch_name: str | None
    detached_head: bool
    dirty: bool
    status_entries: tuple[RepositoryStatusEntry, ...]
    status_fingerprint: str
    remotes: tuple[str, ...]
    probe_version: Literal["agileforge.repository-probe.v1"]
    inspected_at: datetime
    warnings: tuple[RepositoryProbeWarning, ...]


class RepositoryProbe(Protocol):
    def inspect(self, path: Path | str) -> RepositoryProbeResult: ...
```

`RepositoryProbeError` must retain `code`, normalized `path`, and a stable human message. Its constructor must not inspect the filesystem.

- [ ] **Step 4: Write clean, dirty, detached, linked-worktree, remotes, encoding, race, and replay tests**

Use temporary repositories with local Git identity and no network. Add these exact tests:

```text
test_clean_branch_returns_identity_and_empty_status
test_staged_unstaged_deleted_renamed_and_untracked_entries_are_sorted
test_dirty_probe_returns_dirty_warning
test_detached_head_succeeds_without_branch_name
test_linked_worktree_uses_worktree_root_and_shared_common_dir
test_zero_one_and_multiple_remote_urls_are_sorted
test_non_ascii_and_surrogateescaped_paths_have_stable_normalization
test_regular_file_path_returns_path_not_directory
test_unreadable_git_metadata_has_typed_error
test_malformed_path_has_typed_error
test_head_change_during_probe_writes_no_result
test_equivalent_probe_replays_the_same_status_fingerprint
test_status_fingerprint_changes_when_untracked_path_changes
```

In the race test, inject `_read_head_sha: Callable[[Repo], str]` returning two different 40-character SHAs. Assert `REPOSITORY_CHANGED_DURING_PROBE`.

- [ ] **Step 5: Implement the GitPython adapter without source reads**

`GitPythonRepositoryProbe.inspect()` must execute this exact sequence:

```text
normalize and validate the supplied path
open Repo(path, search_parent_directories=False)
reject bare repositories and unborn HEAD
read HEAD SHA once
collect staged diff from HEAD to index
collect unstaged diff from index to working tree
collect untracked paths
read branch/detached state and all configured remote URLs
read HEAD SHA again
reject if the two SHAs differ
sort normalized status entries and remote URLs
hash probe version, canonical paths, SHA, branch marker, entries, and remotes
return a DIRTY_WORKTREE warning exactly when at least one status entry exists
```

Use `os.fsencode`/`os.fsdecode` with the platform filesystem error handler for path round trips. Convert `BadName`, `InvalidGitRepositoryError`, `NoSuchPathError`, `UnicodeError`, and `OSError` into the closed error enum. Do not call `Path.read_text`, `Path.read_bytes`, `Repo.git.fetch`, `Repo.remote().fetch`, or any write API.

- [ ] **Step 6: Add the immutable repository observation model**

Create `RepositoryBinding` with these persisted fields and constraints:

```python
class RepositoryBinding(SQLModel, table=True):
    __tablename__ = "repository_bindings"

    repository_binding_id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.project_id", index=True)
    worktree_path: str = Field(sa_type=Text)
    common_git_dir: str = Field(sa_type=Text)
    head_sha: str = Field(index=True, min_length=40, max_length=40)
    branch_name: str | None = Field(default=None)
    detached_head: bool
    dirty: bool
    status_fingerprint: str = Field(index=True)
    remotes_json: str = Field(sa_type=Text)
    warnings_json: str = Field(sa_type=Text)
    probe_version: str
    inspected_at: datetime
    supersedes_repository_binding_id: int | None = Field(default=None, index=True)
    recorded_by: str = Field(index=True)
```

Add unique constraints for `(project_id, repository_binding_id)`, `(project_id, status_fingerprint, inspected_at)`, and a same-Project self-reference from `supersedes_repository_binding_id`.

- [ ] **Step 7: Run focused tests and commit**

Run:

```bash
uv run --frozen pytest tests/services/test_repository_probe.py tests/workflow/test_repository_binding_model.py -q
git diff --check
```

Expected: all focused tests pass; no provider or socket is used.

Commit:

```bash
git add services/repository_probe.py adapters/git/__init__.py adapters/git/repository_probe.py models/repository.py models/__init__.py models/db.py agile_sqlmodel.py tests/services/test_repository_probe.py tests/workflow/test_repository_binding_model.py
git commit -m "feat: add deterministic repository probe"
```

---

### Task 2: Durable Product Definition Records And Facts

**Files:**
- Create: `models/product_definition.py`
- Modify: `models/specs.py`
- Modify: `models/__init__.py`
- Modify: `models/db.py`
- Modify: `agile_sqlmodel.py`
- Modify: `workflow/facts.py`
- Modify: `repositories/workflow.py`
- Modify: `tests/test_task17_review_absence.py`
- Modify: `tests/workflow/test_graph_properties.py`
- Test: `tests/workflow/test_product_definition_models.py`
- Test: `tests/workflow/test_product_definition_facts.py`
- Test: `tests/workflow/test_fingerprints.py`

**Interfaces:**
- Consumes: `RepositoryBinding` from Task 1 and existing canonical JSON/hash utilities.
- Produces: append-only Vision interview, Product Goal interview, Product Goal outcome, discovery, and specification records plus corresponding immutable facts in `WorkflowFactSnapshot`. Existing Vision records remain in place until Task 3 moves and narrows them in one commit.

- [ ] **Step 1: Write fresh-schema model tests**

Assert exact new tables, columns, and constraints without changing the active root graph yet:

```python
def test_fresh_schema_has_versioned_product_definition_tables(engine: Engine) -> None:
    names = set(inspect(engine).get_table_names())
    assert {
        "vision_revision_intents",
        "vision_interview_turns",
        "product_goal_interview_turns",
        "product_goal_artifacts",
        "product_goal_artifact_decisions",
        "product_goal_outcomes",
        "discovery_artifacts",
        "specification_candidates",
        "specification_decisions",
    } <= names
```

- [ ] **Step 2: Run the model tests and verify RED**

Run: `uv run --frozen pytest tests/workflow/test_product_definition_models.py -q`

Expected: fail because the new model module and tables are absent.

- [ ] **Step 3: Add exact product-definition tables**

Implement these records in `models/product_definition.py`:

```text
VisionRevisionIntent
  vision_revision_intent_id, project_id, source_vision_artifact_id,
  source_vision_fingerprint, reason, initiated_by, initiated_at

VisionInterviewTurn
  vision_interview_turn_id, project_id, mode(initial|revision), turn_number,
  revision_intent_id nullable, prior_turn_id nullable, user_text,
  components_json, vision_statement, is_complete, clarifying_questions_json,
  output_fingerprint,
  workflow_node_attempt_id, attempt_fingerprint, recorded_at

ProductGoalInterviewTurn
  product_goal_interview_turn_id, project_id, vision_artifact_id,
  vision_fingerprint, goal_number, revision_number, prior_turn_id nullable,
  user_text, components_json, goal_statement, is_complete,
  clarifying_questions_json, output_fingerprint, workflow_node_attempt_id,
  attempt_fingerprint, recorded_at

ProductGoalArtifact
  product_goal_artifact_id, project_id, vision_artifact_id,
  vision_fingerprint, goal_number, revision_number, statement,
  content_fingerprint, supersedes_product_goal_artifact_id nullable,
  source_interview_turn_id, created_by, created_at

ProductGoalArtifactDecision
  product_goal_artifact_decision_id, project_id, product_goal_artifact_id,
  artifact_fingerprint, decision(accepted|rejected|feedback), rationale,
  reviewer, idempotency_key, decided_at

ProductGoalOutcome
  product_goal_outcome_id, project_id, product_goal_artifact_id,
  artifact_fingerprint, outcome(fulfilled|abandoned), rationale,
  decided_by, idempotency_key, decided_at

DiscoveryArtifact
  discovery_artifact_id, project_id, vision_artifact_id,
  vision_fingerprint, product_goal_artifact_id, product_goal_fingerprint,
  canonical_content_json, content_fingerprint, content_ref nullable,
  producer, supersedes_discovery_artifact_id nullable, recorded_by, recorded_at

SpecificationCandidate
  specification_candidate_id, project_id, vision_artifact_id,
  vision_fingerprint, product_goal_artifact_id, product_goal_fingerprint,
  discovery_artifact_id, discovery_fingerprint, base_spec_version_id nullable,
  base_spec_hash nullable, canonical_content_json, content_fingerprint,
  content_ref nullable, supersedes_specification_candidate_id nullable,
  recorded_by, recorded_at

SpecificationDecision
  specification_decision_id, project_id, specification_candidate_id,
  artifact_fingerprint, decision, rationale, reviewer,
  idempotency_key, decided_at
```

Use strict check constraints for modes, decisions, and outcomes. Use same-Project composite foreign keys for every parent link. `ProductGoalArtifact.source_interview_turn_id` is required and points to the exact completed Goal interview turn. References to `vision_artifacts` use the existing table during this staging task. Make every new artifact immutable; only append decisions, outcomes, and replacements. Enforce at most one outcome per accepted Goal with a unique Goal reference; the handler and fact loader enforce at most one accepted Goal without an outcome per Project.

- [ ] **Step 4: Stage nullable `SpecRegistry` lineage for the later atomic cutover**

Add temporarily nullable source fields so the existing graph remains runnable until Task 4 replaces specification registration:

```python
source_specification_candidate_id: int | None = Field(
    default=None,
    foreign_key="specification_candidates.specification_candidate_id",
    unique=True,
)
source_vision_artifact_id: int | None = Field(default=None, index=True)
source_vision_fingerprint: str | None = Field(default=None, index=True)
source_product_goal_artifact_id: int | None = Field(default=None, index=True)
source_product_goal_fingerprint: str | None = Field(default=None, index=True)
source_discovery_artifact_id: int | None = Field(default=None, index=True)
source_discovery_fingerprint: str | None = Field(default=None, index=True)
supersedes_spec_version_id: int | None = Field(
    default=None,
    foreign_key="spec_registry.spec_version_id",
    index=True,
)
```

Task 4 makes these fields required and limits registry status after the new specification handler owns all writes. These temporary nulls are implementation staging inside the feature branch, not a shipped compatibility mode.

- [ ] **Step 5: Write fact-loader and tamper tests**

Add exact tests proving:

```text
repository binding does not appear in WorkflowFactSnapshot
an incomplete Vision turn changes business_fact_fingerprint
an incomplete Product Goal turn changes business_fact_fingerprint
deleting the ADK trace database does not remove an interview turn
cross-Project Goal, discovery, or specification references fail loading
content or parent fingerprint tampering raises WorkflowFactLoadError
Product Goal interview, artifact, outcome, discovery, and specification facts retain exact parent identity
two accepted Goals without an outcome raise WorkflowFactLoadError
Goal decision facts distinguish pending, accepted, rejected, and feedback snapshots
Goal acceptance changes business_fact_fingerprint before discovery exists
historical discovery remains valid after a later Goal outcome or revision
discovery recorded at or after its Goal outcome raises WorkflowFactLoadError
legacy spec rows load with nullable staged lineage until Task 4
```

- [ ] **Step 6: Implement focused immutable facts and loader methods**

Add these exact fact types:

```python
VisionRevisionIntentFact
VisionInterviewTurnFact
ProductGoalInterviewTurnFact
ProductGoalArtifactFact
ProductGoalArtifactDecisionFact
ProductGoalOutcomeFact
DiscoveryArtifactFact
SpecificationCandidateFact
```

Extend `SpecVersionFact` with nullable source Vision, Goal, discovery, and specification-candidate identity/fingerprints for this staging task. Add tuple fields with the same names to `WorkflowFactSnapshot`. Load and validate the new records in `WorkflowFactRepository`; do not consult ADK session tables. Discovery validation is causal: the exact Goal must have an accepted decision at or before discovery, and discovery must precede any Goal outcome. Derive the current active Goal separately as an accepted Goal without an outcome so later events never make valid historical facts unloadable.

Extend the graph-property sensitivity matrix for every new `WorkflowFactSnapshot` field. Narrow the old review-absence policy so it rejects only deleted runtime symbols and permits the new Product Goal domain vocabulary required by this plan.

- [ ] **Step 7: Run focused tests and commit**

Run:

```bash
uv run --frozen pytest tests/workflow/test_product_definition_models.py tests/workflow/test_product_definition_facts.py tests/workflow/test_fingerprints.py tests/workflow/test_graph_properties.py tests/test_task17_review_absence.py -q
git diff --check
```

Commit:

```bash
git add models/product_definition.py models/specs.py models/__init__.py models/db.py agile_sqlmodel.py workflow/facts.py repositories/workflow.py tests/workflow/test_product_definition_models.py tests/workflow/test_product_definition_facts.py tests/workflow/test_fingerprints.py tests/workflow/test_graph_properties.py tests/test_task17_review_absence.py
git commit -m "feat: add durable product definition facts"
```

---

### Task 3: Vision Interview And Revision Flow

**Files:**
- Create: `workflow/requests/vision.py`
- Create: `workflow/handlers/vision.py`
- Create: `services/vision_interview_input.py`
- Create: `services/node_attempt_replay.py`
- Modify: `models/product_definition.py`
- Modify: `models/workflow.py`
- Modify: `workflow/facts.py`
- Modify: `repositories/workflow.py`
- Modify: `workflow/definitions/vision.py`
- Modify: `workflow/requests/__init__.py`
- Modify: `workflow/handlers/__init__.py`
- Modify: `workflow/domain.py`
- Modify: `services/contracts/vision.py`
- Modify: `adapters/adk/agents/vision.py`
- Modify: `adapters/adk/prompts/vision.txt`
- Modify: `adapters/adk/recipes.py`
- Modify: `adapters/adk/runner.py`
- Modify: `adapters/adk/model_roles.py`
- Modify: `services/application.py`
- Test: `tests/workflow/test_vision_interview_graph.py`
- Test: `tests/workflow/test_vision_interview_transitions.py`
- Test: `tests/services/test_vision_interview_input.py`
- Test: `tests/services/contracts/test_vision.py`
- Test: `tests/adapters/test_vision.py`
- Test: `tests/adapters/test_adk_graph_recipes.py`
- Test: `tests/adapters/test_adk_workflow_runner.py`
- Test: `tests/adapters/test_adk_session_independence.py`
- Test: `tests/workflow/test_node_attempts.py`

**Interfaces:**
- Consumes: product-definition models/facts from Task 2 and generic durable attempt replay from `services/node_attempt_replay.py`.
- Produces: `vision.interview`, `vision.review`, `vision.revision.start`, `VisionInterviewRequest`, `VisionReviewRequest`, `BeginVisionRevision`, `VisionInterviewInputService`, and a host-prepared application method.

- [ ] **Step 1: Write contract tests for initial and revision turns**

Use this exact contract shape:

```python
class VisionInterviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_name: str
    project_description: str | None
    mode: Literal["initial", "revision"]
    user_response: str
    prior_components: VisionComponents | None
    accepted_vision_statement: str | None


class VisionInterviewOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    updated_components: VisionComponents
    project_vision_statement: str
    is_complete: bool
    clarifying_questions: list[str]
```

Validators must enforce:

```text
incomplete output has at least one non-empty clarifying question
is_complete equals VisionComponents.is_fully_defined()
all strings are stripped and blank strings are rejected where required
```

- [ ] **Step 2: Run contract tests and verify RED**

Run: `uv run --frozen pytest tests/services/contracts/test_vision.py -q`

Expected: fail because the old input still requires specification and Authority content and does not implement the focused interview contract.

- [ ] **Step 3: Replace the prompt and agent contract**

The prompt must state these exact boundaries:

```text
Human answers are the authority for product intent.
Do not infer Vision from repository contents.
Ask one question or one tightly related question set per turn.
Preserve already answered components unless the human corrects them.
Produce Project Vision only. Do not define a Product Goal, feature, specification, or implementation task.
Return only the strict output schema.
```

Keep the production `product_vision` model role. Rename the recipe node from `vision.generate` to `vision.interview`.

- [ ] **Step 4: Move and narrow the Vision persistence model**

Move `VisionArtifact` and `VisionArtifactDecision` from `models/workflow.py` to `models/product_definition.py` in the same edit that updates every import. Replace Authority lineage with these fields:

```text
VisionArtifact
  vision_artifact_id, project_id, version_number, components_json,
  statement, content_fingerprint, supersedes_vision_artifact_id nullable,
  source_interview_turn_id, created_by, created_at

VisionArtifactDecision
  vision_artifact_decision_id, project_id, vision_artifact_id,
  artifact_fingerprint, decision, rationale, reviewer, idempotency_key,
  decided_at
```

Add `VisionArtifactFact` and its validated loader. Remove Vision from the generic phase-artifact loader so there is exactly one Vision source of truth.

- [ ] **Step 5: Extend completion context with trusted normalized input**

Add `normalized_input: JsonObject` to `AttemptCompletionContext`. Populate it from the persisted `StartNodeAttempt.normalized_input` when completing an attempt. Add a runner test proving the Vision output adapter copies `user_response` and `mode` from trusted input rather than model output.

- [ ] **Step 6: Write graph and transaction tests**

Add exact tests proving:

```text
vision.interview is immediately available on a new Project with no Authority
an incomplete turn persists and keeps vision.interview available
a complete turn creates one pending Vision only
vision.review references the exact Vision fingerprint
accept inserts one Vision decision exactly once
feedback records one Vision decision and reopens the Vision interview
initial acceptance makes goal.interview the next required stage while discovery remains blocked
vision.revision.start is optional only when an accepted Vision exists and no Product Goal is active
a complete revision creates Vision only
accepted revision makes older resolved Goal and specification lineage non-current
removing ADK trace rows does not change Vision position
```

- [ ] **Step 7: Implement typed requests and handlers**

Define:

```python
class RecordVisionInterviewTurn(PositionedRequest):
    kind: Literal["record_vision_interview_turn"] = "record_vision_interview_turn"
    node_id: ClassVar[str] = "vision.interview"
    mode: Literal["initial", "revision"]
    user_text: str
    updated_components: JsonObject
    project_vision_statement: str
    is_complete: bool
    clarifying_questions: tuple[str, ...]
    attempt_id: int
    attempt_fingerprint: str


class DecideVisionReview(PositionedRequest):
    kind: Literal["decide_vision_review"] = "decide_vision_review"
    node_id: ClassVar[str] = "vision.review"
    vision_artifact_id: int
    vision_fingerprint: str
    decision: Literal["accepted", "rejected", "feedback"]
    rationale: str


class BeginVisionRevision(PositionedRequest):
    kind: Literal["begin_vision_revision"] = "begin_vision_revision"
    node_id: ClassVar[str] = "vision.revision.start"
    source_vision_artifact_id: int
    source_vision_fingerprint: str
    reason: str
```

Handlers compute canonical fingerprints server-side, append immutable rows, and return assigned identities. Initial and revision acceptance each insert only one Vision decision in the caller-owned transaction. Vision revision acceptance fails without writes while any accepted Product Goal lacks a fulfilled or abandoned outcome.

- [ ] **Step 8: Implement host-prepared Vision input**

`VisionInterviewInputService.build(project_id, decision, user_text)` must load Project identity, the latest valid turn, an open revision intent when present, and the accepted Vision when revising. It may read Product Goal outcomes only to enforce the no-active-Goal revision rule. It must reject ambiguous turn chains and never read specification, Authority, repository contents, or ADK session state.

Define the application request exactly:

```python
class VisionInterviewRequest(FrozenModel):
    project_id: int
    graph_version: str
    fact_fingerprint: str
    decision_fingerprint: str
    user_text: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    correlation_id: str | None = None


class VisionReviewRequest(FrozenModel):
    project_id: int
    decision: Literal["accepted", "rejected", "feedback"]
    rationale: str
    idempotency_key: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    correlation_id: str | None = None


class VisionRevisionRequest(FrozenModel):
    project_id: int
    reason: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    correlation_id: str | None = None
```

Add `AgileForgeApplication.run_vision_interview(request: VisionInterviewRequest)`, `review_vision(request: VisionReviewRequest)`, and `begin_vision_revision(request: VisionRevisionRequest)`. Each method replays before current-state reads, validates the unique current decision, and prepares exact IDs/fingerprints internally. Interview execution chooses the configured production model internally, builds input internally, and calls the generic ADK runner.

- [ ] **Step 9: Run focused tests and commit**

Run:

```bash
uv run --frozen pytest tests/services/contracts/test_vision.py tests/services/test_vision_interview_input.py tests/adapters/test_vision.py tests/adapters/test_adk_graph_recipes.py tests/adapters/test_adk_workflow_runner.py tests/adapters/test_adk_session_independence.py tests/workflow/test_vision_interview_graph.py tests/workflow/test_vision_interview_transitions.py tests/workflow/test_node_attempts.py -q
git diff --check
```

Commit:

```bash
git add workflow/requests/vision.py workflow/handlers/vision.py services/vision_interview_input.py services/node_attempt_replay.py models/product_definition.py models/workflow.py workflow/facts.py repositories/workflow.py workflow/definitions/vision.py workflow/requests/__init__.py workflow/handlers/__init__.py workflow/domain.py services/contracts/vision.py adapters/adk/agents/vision.py adapters/adk/prompts/vision.txt adapters/adk/recipes.py adapters/adk/runner.py adapters/adk/model_roles.py services/application.py tests/workflow/test_vision_interview_graph.py tests/workflow/test_vision_interview_transitions.py tests/services/test_vision_interview_input.py tests/services/contracts/test_vision.py tests/adapters/test_vision.py tests/adapters/test_adk_graph_recipes.py tests/adapters/test_adk_workflow_runner.py tests/adapters/test_adk_session_independence.py tests/workflow/test_node_attempts.py
git commit -m "feat: add vision interview and review"
```

---

### Task 4: Product Goal Interview, Discovery, And Specification Cycle

**Files:**
- Create: `workflow/requests/product_goal.py`
- Create: `workflow/handlers/product_goal.py`
- Create: `workflow/definitions/product_goal.py`
- Create: `services/product_goal_interview_input.py`
- Create: `services/contracts/product_goal.py`
- Create: `adapters/adk/agents/product_goal.py`
- Create: `adapters/adk/prompts/product_goal.txt`
- Create: `workflow/requests/product_discovery.py`
- Create: `workflow/handlers/product_discovery.py`
- Create: `workflow/definitions/product_discovery.py`
- Create: `services/authority_compilation_input.py`
- Modify: `models/product_definition.py`
- Modify: `models/specs.py`
- Modify: `workflow/facts.py`
- Modify: `repositories/workflow.py`
- Modify: `workflow/requests/__init__.py`
- Modify: `workflow/handlers/__init__.py`
- Modify: `workflow/domain.py`
- Modify: `services/application.py`
- Modify: `services/read_projections.py`
- Modify: `adapters/adk/recipes.py`
- Modify: `adapters/adk/runner.py`
- Modify: `adapters/adk/model_roles.py`
- Modify: `config/models.yaml`
- Modify: `config/models.test.yaml`
- Modify: `utils/agileforge_spec_profile.py`
- Test: `tests/services/contracts/test_product_goal.py`
- Test: `tests/services/test_product_goal_interview_input.py`
- Test: `tests/adapters/test_product_goal.py`
- Test: `tests/workflow/test_product_goal_graph.py`
- Test: `tests/workflow/test_product_goal_transitions.py`
- Test: `tests/workflow/test_product_discovery_graph.py`
- Test: `tests/workflow/test_product_discovery_transitions.py`
- Test: `tests/services/test_authority_compilation_input.py`
- Test: `tests/adapters/test_initial_spec_read.py`

**Interfaces:**
- Consumes: accepted Vision facts from Task 3, Goal records from Task 2, generic durable attempt replay, and staged `SpecRegistry` lineage.
- Produces: `goal.interview`, `goal.review`, `goal.fulfill`, `goal.abandon`, `discovery.record`, `specification.record`, `specification.review`, host-prepared Product Goal execution, and an approved current `SpecRegistry` row.

- [ ] **Step 1: Write the strict Product Goal interview contract tests**

Use this exact contract shape:

```python
class ProductGoalComponents(BaseModel):
    model_config = ConfigDict(extra="forbid")
    valuable_future_state: str | None = None
    beneficiary: str | None = None
    value: str | None = None
    success_signals: tuple[str, ...] = ()
    boundaries: tuple[str, ...] = ()


class ProductGoalInterviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_name: str
    accepted_vision_statement: str
    user_response: str
    prior_components: ProductGoalComponents | None


class ProductGoalInterviewOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    updated_components: ProductGoalComponents
    product_goal_statement: str
    is_complete: bool
    clarifying_questions: list[str]
```

`ProductGoalComponents.is_fully_defined()` is true only when all three scalar fields are non-blank and both tuples contain at least one non-blank item. Validators require focused questions for incomplete output, no questions for complete output, equality between `is_complete` and `is_fully_defined()`, stripped strings, and no blank collection entries.

- [ ] **Step 2: Run contract tests and verify RED**

Run: `uv run --frozen pytest tests/services/contracts/test_product_goal.py -q`

Expected: fail because the separate Goal contract and agent do not exist.

- [ ] **Step 3: Add the focused Product Goal agent and host-prepared input**

The prompt must state these exact boundaries:

```text
The accepted Project Vision is read-only context.
Ask one question or one tightly related question set per turn.
Define one valuable future state, its beneficiary, value, observable success, and boundaries.
Do not define features, technical behavior, a specification, backlog items, or implementation tasks.
Preserve answered components unless the human corrects them.
Return only the strict output schema.
```

Add the `product_goal` model role. Production resolves it to `openrouter/openai/gpt-5.6-luna`; tests resolve it to `openrouter/openai/gpt-oss-20b:free` but use fake execution with sockets disabled. `ProductGoalInterviewInputService.build(project_id, decision, user_text)` loads only Project identity, exact accepted Vision, latest valid Goal turn, latest review feedback, and current Goal outcome state. It rejects an absent Vision, an active Goal, or ambiguous turn chains and never reads repository contents, specifications, Authority, or ADK session state.

- [ ] **Step 4: Write Product Goal graph and transaction tests**

Add exact tests proving:

```text
accepted Vision exposes goal.interview while discovery remains blocked
an incomplete Goal turn persists and keeps goal.interview available
a complete Goal turn creates one immutable pending candidate
goal.review references only the exact Goal fingerprint and accepted Vision parent
feedback creates a new revision with the same goal_number
acceptance creates the only active Goal and exposes discovery.record
a second Goal cannot start while the accepted Goal lacks an outcome
goal.fulfill and goal.abandon are unavailable during an active Sprint or incomplete triage
fulfillment and abandonment require a non-blank rationale and exact active Goal
an outcome is idempotent and cannot be replaced
after an outcome, goal.interview creates goal_number + 1 under the unchanged Vision
Sprint closure or an empty backlog never creates a ProductGoalOutcome
removing ADK trace rows does not change Goal position
```

- [ ] **Step 5: Implement typed Goal requests, handlers, and application methods**

Define internal positioned requests:

```python
class RecordProductGoalInterviewTurn(PositionedRequest):
    kind: Literal["record_product_goal_interview_turn"] = "record_product_goal_interview_turn"
    node_id: ClassVar[str] = "goal.interview"
    user_text: str
    updated_components: JsonObject
    product_goal_statement: str
    is_complete: bool
    clarifying_questions: tuple[str, ...]
    attempt_id: int
    attempt_fingerprint: str


class DecideProductGoalReview(PositionedRequest):
    kind: Literal["decide_product_goal_review"] = "decide_product_goal_review"
    node_id: ClassVar[str] = "goal.review"
    product_goal_artifact_id: int
    product_goal_fingerprint: str
    decision: Literal["accepted", "rejected", "feedback"]
    rationale: str


class FulfillProductGoal(PositionedRequest):
    kind: Literal["fulfill_product_goal"] = "fulfill_product_goal"
    node_id: ClassVar[str] = "goal.fulfill"
    product_goal_artifact_id: int
    product_goal_fingerprint: str
    rationale: str


class AbandonProductGoal(PositionedRequest):
    kind: Literal["abandon_product_goal"] = "abandon_product_goal"
    node_id: ClassVar[str] = "goal.abandon"
    product_goal_artifact_id: int
    product_goal_fingerprint: str
    rationale: str
```

`RecordProductGoalInterviewTurn` carries trusted output plus attempt identity. `DecideProductGoalReview` carries the exact candidate identity/fingerprint and `accepted|rejected|feedback`. Outcome requests carry the exact active Goal identity/fingerprint and non-blank rationale. Handlers derive goal and revision numbers, canonical fingerprints, and parent Vision identity server-side; all writes use the caller-owned transaction.

Define the public host-prepared request:

```python
class ProductGoalInterviewRequest(FrozenModel):
    project_id: int
    graph_version: str
    fact_fingerprint: str
    decision_fingerprint: str
    user_text: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    correlation_id: str | None = None


class ProductGoalReviewRequest(FrozenModel):
    project_id: int
    decision: Literal["accepted", "rejected", "feedback"]
    rationale: str
    idempotency_key: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    correlation_id: str | None = None


class ProductGoalOutcomeRequest(FrozenModel):
    project_id: int
    outcome: Literal["fulfilled", "abandoned"]
    rationale: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    correlation_id: str | None = None
```

`AgileForgeApplication.run_product_goal_interview()` replays first, validates the exact `goal.interview` decision, builds input internally, selects the configured role internally, and runs the Goal recipe. Review and outcome application methods resolve the exact current candidate or active Goal internally; public callers never submit artifact fingerprints.

- [ ] **Step 6: Write and implement the discovery/specification cycle**

Add exact graph assertions:

```text
accepted Goal exposes discovery.record
discovery.record references exact accepted Vision and active Goal
recorded discovery exposes specification.record
specification.record references exact discovery fingerprint
specification.review is the only required action for a pending candidate
Authority remains blocked until specification acceptance
there is no intermediate PRD node or fact
a rejected or feedback specification can be replaced only through exact supersedes
```

Define `RecordDiscoveryArtifact`, `RecordSpecificationCandidate`, and `DecideSpecification`. Discovery input contains canonical JSON, optional content reference, and producer fixed to `grill-me-with-docs`. Specification input contains canonical JSON and optional content reference; the handler derives current base spec identity from durable state. Callers never supply base version or hash.

Task 2 already created `SpecificationDecision`; extend that existing append-only record rather than adding another decision table. Its check constraint and typed fact must permit exactly `accepted|rejected|feedback`. Load decisions into `WorkflowFactSnapshot` so graph rules and read projections can distinguish a pending candidate from terminal rejection or feedback. Each decision binds the exact candidate identity/fingerprint, is causally later than that candidate, and is unique per candidate terminal review. Feedback and rejection require a non-blank rationale.

- [ ] **Step 7: Harden specification lineage and transaction tests**

Make every Task 2 `SpecRegistry` source field non-nullable. Permit only `approved` and `superseded` status. `DecideSpecification(decision="accepted")` inserts one registry row with every exact source identity/fingerprint and supersedes the prior current row in the same transaction.

Add exact tests proving:

```text
initial specification has no base spec when the registry is empty
later specification records the exact current accepted spec as its base
wrong Vision, Goal, discovery, supersedes, or candidate fingerprint writes nothing
accepted specification atomically creates one approved SpecRegistry row
accepting a replacement marks the prior registry row superseded
registered content hash equals canonical_stored_json_hash(content)
AuthorityCompilationInputService reads only the graph-selected approved registry row
a new accepted Goal after an outcome leaves Vision unchanged
a new accepted Goal makes prior discovery, specification, Authority, and Backlog non-current
```

Use append-only selectors with exact signatures:

```python
def accepted_current_vision(snapshot: WorkflowFactSnapshot) -> VisionArtifactFact | None: ...
def accepted_current_goal(snapshot: WorkflowFactSnapshot) -> ProductGoalArtifactFact | None: ...
def current_discovery(snapshot: WorkflowFactSnapshot) -> DiscoveryArtifactFact | None: ...
def current_specification_candidate(
    snapshot: WorkflowFactSnapshot,
) -> SpecificationCandidateFact | None: ...
def accepted_current_spec(snapshot: WorkflowFactSnapshot) -> SpecVersionFact | None: ...
```

Every selector returns `None` on ambiguous chains or mismatched upstream fingerprints. Graph rules return `WORKFLOW_FACT_CONFLICT`; they never choose by timestamp.

- [ ] **Step 8: Add durable read projections**

Expose `vision_status(project_id)`, `product_goal_status(project_id)`, `discovery_status(project_id)`, `specification_status(project_id)`, and `specification_review(project_id)` from durable facts. Responses include IDs/fingerprints for machine output, human content, review state, active/outcome state, and stale reasons. They never resolve specification content from a mutable Project cache or disk path.

- [ ] **Step 9: Run focused tests and commit**

Run:

```bash
uv run --frozen pytest tests/services/contracts/test_product_goal.py tests/services/test_product_goal_interview_input.py tests/adapters/test_product_goal.py tests/workflow/test_product_goal_graph.py tests/workflow/test_product_goal_transitions.py tests/workflow/test_product_discovery_graph.py tests/workflow/test_product_discovery_transitions.py tests/services/test_authority_compilation_input.py tests/adapters/test_initial_spec_read.py -q
git diff --check
```

Commit:

```bash
git add workflow/requests/product_goal.py workflow/handlers/product_goal.py workflow/definitions/product_goal.py services/product_goal_interview_input.py services/contracts/product_goal.py adapters/adk/agents/product_goal.py adapters/adk/prompts/product_goal.txt workflow/requests/product_discovery.py workflow/handlers/product_discovery.py workflow/definitions/product_discovery.py services/authority_compilation_input.py models/product_definition.py models/specs.py workflow/facts.py repositories/workflow.py workflow/requests/__init__.py workflow/handlers/__init__.py workflow/domain.py services/application.py services/read_projections.py adapters/adk/recipes.py adapters/adk/runner.py adapters/adk/model_roles.py config/models.yaml config/models.test.yaml utils/agileforge_spec_profile.py tests/services/contracts/test_product_goal.py tests/services/test_product_goal_interview_input.py tests/adapters/test_product_goal.py tests/workflow/test_product_goal_graph.py tests/workflow/test_product_goal_transitions.py tests/workflow/test_product_discovery_graph.py tests/workflow/test_product_discovery_transitions.py tests/services/test_authority_compilation_input.py tests/adapters/test_initial_spec_read.py
git commit -m "feat: add product goal and discovery cycle"
```

---

### Task 5: Root Graph Cutover And Delivery Lineage

**Files:**
- Modify: `workflow/contracts.py`
- Modify: `workflow/definitions/root.py`
- Modify: `workflow/definitions/authority.py`
- Modify: `workflow/definitions/backlog.py`
- Modify: `workflow/definitions/planning.py`
- Modify: `workflow/definitions/execution.py`
- Modify: `workflow/facts.py`
- Modify: `models/workflow.py`
- Modify: `repositories/workflow.py`
- Modify: `workflow/requests/product_definition.py`
- Modify: `workflow/handlers/product_definition.py`
- Modify: `services/contracts/backlog.py`
- Modify: `services/contracts/roadmap.py`
- Modify: `services/agent_workbench/backlog_phase.py`
- Modify: `services/agent_workbench/roadmap_phase.py`
- Modify: `services/phases/backlog_refinement.py`
- Modify: `adapters/adk/prompts/backlog.txt`
- Modify: `adapters/adk/prompts/roadmap.txt`
- Modify: `services/authority_review_projection.py`
- Modify: `services/agent_workbench/authority_projection.py`
- Modify: `services/specs/compiler_service.py`
- Modify: `workflow/handlers/authority.py`
- Modify: `adapters/adk/recipes.py`
- Modify: `adapters/adk/model_roles.py`
- Modify: `services/application.py`
- Test: `tests/workflow/test_single_project_graph.py`
- Test: `tests/workflow/test_vision_backlog_graph.py`
- Test: `tests/workflow/test_vision_backlog_transitions.py`
- Test: `tests/workflow/test_authority_graph.py`
- Test: `tests/workflow/test_authority_transitions.py`
- Test: `tests/workflow/test_planning_graph.py`
- Test: `tests/workflow/test_planning_joins.py`
- Test: `tests/workflow/test_execution_graph.py`
- Test: `tests/workflow/test_execution_transitions.py`
- Test: `tests/test_backlog_refinement_service.py`
- Test: `tests/adapters/test_adk_graph_recipes.py`
- Test: `tests/test_specs_compiler_service.py`
- Test: `tests/adapters/test_production_runtime_cutover.py`

**Interfaces:**
- Consumes: separate Vision, Product Goal, and product-discovery graphs from Tasks 3-4; existing Authority, Backlog, Roadmap, Story, Sprint, and execution facts.
- Produces: `ROOT_GRAPH` version 2 with one lifecycle and automatic current-lineage selection.

- [ ] **Step 1: Write the root-order and retained-stage tests**

Assert this exact child graph order and key transitions:

```python
assert ROOT_GRAPH.graph_version == "agileforge.workflow.v2"
assert tuple(child.child_graph_id for child in ROOT_GRAPH.root.children) == (
    "vision",
    "product_goal",
    "product_discovery",
    "authority",
    "backlog",
    "planning",
    "execution",
)
```

Add one provider-free domain journey that records and accepts Vision, separately interviews and accepts Product Goal, then records discovery, specification, Authority, Backlog, Roadmap, Story set, dependencies, Sprint plan, Sprint start, Task completion, Story closure, Sprint review, Sprint closure, and triage. At every boundary assert the next required semantic node.

- [ ] **Step 2: Run the journey and verify RED**

Run: `uv run --frozen pytest tests/workflow/test_single_project_graph.py -q`

Expected: fail because the root still starts with setup/Authority and uses graph version 1.

- [ ] **Step 3: Recompose the root and Authority graph**

Set `GRAPH_VERSION = "agileforge.workflow.v2"`. Compose only the seven child graphs above. Remove root scope wrappers, reconciliation masking, terminal scope-start routing, and the Authority-to-Vision boundary node.

Remove the retired curator from `AgenticRecipeNodes`, `AdkRecipeRegistry`, lazy production composition, prepared-input services, and the stable agentic catalog in the same cutover. The old files may remain physically present until Task 9, but no production import or graph node may reach them after this step.

`authority.compile` must be available only for the exact current approved `SpecVersionFact`. Accepted Authority must match that spec identity and hash. Authority review snapshots must derive source content from `SpecRegistry`; remove disk-hash and resolved-path decision fields from the active workflow path.

Stop writing compiled Authority into a Project cache. `CompiledSpecAuthority` plus `SpecAuthorityAcceptance` are the only compilation and decision sources.

- [ ] **Step 4: Bind Backlog to current Goal and Authority**

Add `product_goal_artifact_id` and `product_goal_fingerprint` to `BacklogArtifact`, `BacklogArtifactFact`, `RecordBacklogDraft`, generation input, handler validation, and read projection. A Backlog is current only when both its Goal and Authority match current accepted facts.

Narrow `Backlog.InputSchema` to:

```python
class InputSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_vision_statement: str
    product_goal_statement: str
    technical_spec: str
    compiled_authority: str
    prior_backlog_state: str
    user_input: str | None
```

Remove project-wide implementation-state annotations, scope mode, Authority delta filters, and reconciliation inputs from Backlog and Roadmap contracts. Keep refinement, estimates, priorities, and review behavior.

- [ ] **Step 5: Replace explicit scope reconciliation with lineage selection**

Historical Backlog, Roadmap, Story, Sprint-plan, and execution facts remain immutable. Rules select only descendants of the current accepted Goal and Authority. A new Goal therefore makes old delivery facts non-current without mutating them.

Keep the active Goal across Sprint boundaries. After triage, remaining accepted Backlog work exposes another Sprint-planning cycle. When no Sprint is active, no review is pending, and completed Sprints have triage, `discovery.record` may reopen for another increment under the same Goal; its later Backlog replacement carries forward unresolved accepted work under exact lineage rather than creating a Goal.

Expose `goal.fulfill` and `goal.abandon` only at that quiescent boundary. After either outcome, expose `goal.interview`; never expose it while a Goal remains active. Starting a later Goal does not rerun Vision, and Sprint closure alone never resolves a Goal.

- [ ] **Step 6: Run the retained workflow suites**

Run:

```bash
uv run --frozen pytest tests/workflow/test_single_project_graph.py tests/workflow/test_vision_backlog_graph.py tests/workflow/test_vision_backlog_transitions.py tests/workflow/test_authority_graph.py tests/workflow/test_authority_transitions.py tests/workflow/test_planning_graph.py tests/workflow/test_planning_joins.py tests/workflow/test_execution_graph.py tests/workflow/test_execution_transitions.py tests/test_backlog_refinement_service.py tests/adapters/test_adk_graph_recipes.py tests/test_specs_compiler_service.py tests/adapters/test_production_runtime_cutover.py -q
git diff --check
```

Expected: all retained lifecycle tests pass under graph version 2; no reconciliation command is advertised.

- [ ] **Step 7: Commit**

```bash
git add workflow/contracts.py workflow/definitions/root.py workflow/definitions/authority.py workflow/definitions/backlog.py workflow/definitions/planning.py workflow/definitions/execution.py workflow/facts.py models/workflow.py repositories/workflow.py workflow/requests/product_definition.py workflow/handlers/product_definition.py services/contracts/backlog.py services/contracts/roadmap.py services/agent_workbench/backlog_phase.py services/agent_workbench/roadmap_phase.py services/phases/backlog_refinement.py adapters/adk/prompts/backlog.txt adapters/adk/prompts/roadmap.txt services/authority_review_projection.py services/agent_workbench/authority_projection.py services/specs/compiler_service.py workflow/handlers/authority.py adapters/adk/recipes.py adapters/adk/model_roles.py services/application.py tests/workflow/test_single_project_graph.py tests/workflow/test_vision_backlog_graph.py tests/workflow/test_vision_backlog_transitions.py tests/workflow/test_authority_graph.py tests/workflow/test_authority_transitions.py tests/workflow/test_planning_graph.py tests/workflow/test_planning_joins.py tests/workflow/test_execution_graph.py tests/workflow/test_execution_transitions.py tests/test_backlog_refinement_service.py tests/adapters/test_adk_graph_recipes.py tests/test_specs_compiler_service.py tests/adapters/test_production_runtime_cutover.py
git commit -m "feat: cut over to one product lifecycle graph"
```

---

### Task 6: Atomic Project Creation And Repository Attachment

**Files:**
- Create: `services/project_lifecycle.py`
- Create: `workflow/requests/project.py`
- Create: `workflow/handlers/project.py`
- Modify: `workflow/requests/__init__.py`
- Modify: `workflow/handlers/__init__.py`
- Modify: `workflow/domain.py`
- Modify: `models/core.py`
- Modify: `repositories/project.py`
- Modify: `services/application.py`
- Modify: `services/read_projections.py`
- Test: `tests/services/test_project_lifecycle.py`
- Test: `tests/workflow/test_project_creation.py`
- Test: `tests/workflow/test_repository_attachment.py`
- Test: `tests/test_project_repository_deletion.py`
- Test: `tests/adapters/test_api_project_deletion.py`

**Interfaces:**
- Consumes: `RepositoryProbe` and `RepositoryBinding` from Task 1; version-2 graph from Task 5.
- Produces: semantic `CreateProjectCommand`, `RepositoryAttachmentCommand`, and `RepositoryRefreshCommand`; internal `CreateProject`, `RecordRepositoryBinding`, and `RepositoryBindingInput`; `ProjectLifecycleService`; and application methods for creation and repository operations.

- [ ] **Step 1: Write application-level atomicity tests**

Add exact tests:

```text
create with name only commits one Project and returns vision.interview available
create with description stores description without generated product content
create with valid repository commits Project and binding in one transaction
probe failure creates neither Project nor binding nor receipt
post-binding injected failure rolls back Project, binding, and receipt
same idempotency and same probe result replays one Project
same idempotency and changed semantic input returns WORKFLOW_FACT_CONFLICT
attach to an existing Project leaves graph fact fingerprint and every graph decision unchanged
replace requires the exact active binding fingerprint
refresh reuses the active path and appends a new immutable observation
failed attach or refresh preserves the prior active binding pointer
delete removes the Project and all binding/product records transactionally
```

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run --frozen pytest tests/services/test_project_lifecycle.py tests/workflow/test_project_creation.py tests/workflow/test_repository_attachment.py -q`

Expected: fail because project creation still requires an origin and repository attachment is a setup transition.

- [ ] **Step 3: Add semantic application commands**

Define:

```python
class CreateProjectCommand(FrozenModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    repository_path: str | None = None
    idempotency_key: str = Field(min_length=1, max_length=200)
    actor: str = Field(min_length=1, max_length=200)
    correlation_id: str | None = None


class RepositoryAttachmentCommand(FrozenModel):
    project_id: int
    path: str
    expected_active_binding_fingerprint: str | None
    idempotency_key: str
    actor: str
    correlation_id: str | None = None


class RepositoryRefreshCommand(FrozenModel):
    project_id: int
    expected_active_binding_fingerprint: str
    idempotency_key: str
    actor: str
    correlation_id: str | None = None
```

- [ ] **Step 4: Narrow Project persistence and deletion**

Remove the Project origin constraint/field and cached `vision`, `roadmap`, `technical_spec`, `compiled_authority_json`, `spec_file_path`, and `spec_loaded_at` columns. Add only the nullable `active_repository_binding_id` foreign key for repository projection. Update `ProjectFact` and its loader so its fields are `project_id`, `name`, `description`, and `created_at`.

Delete `ProjectRepository.update_vision`, `update_technical_spec`, and `update_compiled_authority`. Keep generic list/get/delete behavior. Rewrite deletion around the fresh schema and current foreign keys; remove special-case cleanup for tables deleted in Task 9. Add a fresh-schema assertion that `Project.model_fields` contains no removed state field.

- [ ] **Step 5: Add internal prepared mutation requests**

Define `RepositoryBindingInput` with every trusted `RepositoryProbeResult` field plus `recorded_by`. Define non-public `CreateProject` and `RecordRepositoryBinding` request variants carrying idempotency and actor data. `RecordRepositoryBinding` also carries current graph version/fact fingerprint and the expected active binding fingerprint, but no decision fingerprint because repository state is orthogonal to graph nodes.

Register both variants in the closed request union and domain dispatcher. `WorkflowDomain` claims and completes the transition receipt; the caller-session handlers insert Project/binding rows, update the active pointer, and evaluate the returned graph position in that same transaction.

- [ ] **Step 6: Implement probe-before-mutation and one transaction**

`ProjectLifecycleService.create_project()` probes first when a path exists. Only after a successful probe may it open the write transaction that inserts Project, optional `RepositoryBinding`, Project active-binding pointer, and idempotency receipt. Evaluate and return the graph position after flush and before commit.

Attach/replace/refresh probes before opening the write transaction, verifies the expected active fingerprint inside the transaction, appends a binding referencing the prior observation, and updates only `Project.active_repository_binding_id`.

- [ ] **Step 7: Add repository and product read projections**

`project_show()` returns identity, counts, accepted Vision summary, accepted Goal summary, and active repository status. Add `repository_status(project_id)` returning full structured provenance and warnings. Do not include an origin field.

- [ ] **Step 8: Run focused tests and commit**

Run:

```bash
uv run --frozen pytest tests/services/test_project_lifecycle.py tests/workflow/test_project_creation.py tests/workflow/test_repository_attachment.py tests/test_project_repository_deletion.py tests/adapters/test_api_project_deletion.py -q
git diff --check
```

Commit:

```bash
git add services/project_lifecycle.py workflow/requests/project.py workflow/handlers/project.py workflow/requests/__init__.py workflow/handlers/__init__.py workflow/domain.py models/core.py repositories/project.py services/application.py services/read_projections.py tests/services/test_project_lifecycle.py tests/workflow/test_project_creation.py tests/workflow/test_repository_attachment.py tests/test_project_repository_deletion.py tests/adapters/test_api_project_deletion.py
git commit -m "feat: add atomic project and repository lifecycle"
```

---

### Task 7: Task-Specific API And Agent CLI

**Files:**
- Modify: `api.py`
- Modify: `cli/main.py`
- Modify: `cli/workflow_commands.py`
- Modify: `services/application.py`
- Test: `tests/adapters/test_api_workflow_domain.py`
- Test: `tests/adapters/test_cli_workflow_domain.py`
- Test: `tests/adapters/test_command_renderer.py`
- Test: `tests/adapters/test_production_read_surfaces.py`

**Interfaces:**
- Consumes: application commands from Tasks 3, 4, and 6.
- Produces: semantic HTTP routes, argparse commands, JSON responses, and exact `workflow next` recommendations.

- [ ] **Step 1: Write strict request-shape and parser tests**

Assert the create API rejects unknown fields and accepts exactly:

```python
class CreateProjectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    description: str | None = None
    repository_path: str | None = None
    idempotency_key: str
    actor: str
```

Add parser tests for these concrete commands:

```bash
agileforge project create --name MyFinance --description "Local household finance" --repository-path /Users/aaat/myfinance --idempotency-key create-myfinance-1 --actor acceptance-agent
agileforge vision respond --project-id 1 --text "The target user manages household finances and needs reliable movement reconciliation." --idempotency-key vision-myfinance-1 --actor acceptance-agent
agileforge vision status --project-id 1
agileforge vision review --project-id 1 --decision accepted --rationale "The product direction is accurate." --idempotency-key vision-review-myfinance-1 --actor acceptance-agent
agileforge goal respond --project-id 1 --text "The first valuable future state is reliable Beobank statement reconciliation for the household operator." --idempotency-key goal-myfinance-1 --actor acceptance-agent
agileforge goal status --project-id 1
agileforge goal review --project-id 1 --decision accepted --rationale "The outcome and success signals are correct." --idempotency-key goal-review-myfinance-1 --actor acceptance-agent
agileforge repository attach --project-id 1 --path /Users/aaat/myfinance --idempotency-key attach-myfinance-1 --actor acceptance-agent
agileforge repository status --project-id 1
agileforge repository refresh --project-id 1 --idempotency-key refresh-myfinance-1 --actor acceptance-agent
agileforge discovery record --project-id 1 --file /tmp/agileforge-acceptance/discovery.json --idempotency-key discovery-myfinance-1 --actor acceptance-agent
agileforge specification record --project-id 1 --file /tmp/agileforge-acceptance/specification.json --idempotency-key spec-myfinance-1 --actor acceptance-agent
agileforge specification review --project-id 1 --decision accepted --rationale "Desired behavior is correct." --idempotency-key spec-review-myfinance-1 --actor acceptance-agent
agileforge goal complete --project-id 1 --rationale "The accepted success signals were achieved." --idempotency-key goal-complete-myfinance-1 --actor acceptance-agent
agileforge goal abandon --project-id 1 --rationale "The outcome is no longer worth pursuing." --idempotency-key goal-abandon-myfinance-1 --actor acceptance-agent
```

- [ ] **Step 2: Run transport tests and verify RED**

Run:

```bash
uv run --frozen pytest tests/adapters/test_api_workflow_domain.py tests/adapters/test_cli_workflow_domain.py tests/adapters/test_command_renderer.py -q
```

Expected: create still requires an origin; semantic Vision/repository/Goal/specification commands are absent.

- [ ] **Step 3: Implement semantic API routes**

Provide these routes with strict models:

```text
POST   /api/projects
GET    /api/projects/{project_id}
DELETE /api/projects/{project_id}
GET    /api/projects/{project_id}/position
POST   /api/projects/{project_id}/vision/respond
GET    /api/projects/{project_id}/vision/status
POST   /api/projects/{project_id}/vision/review
POST   /api/projects/{project_id}/vision/revision
POST   /api/projects/{project_id}/goals/respond
GET    /api/projects/{project_id}/goals/status
POST   /api/projects/{project_id}/goals/review
POST   /api/projects/{project_id}/goals/complete
POST   /api/projects/{project_id}/goals/abandon
POST   /api/projects/{project_id}/discovery
GET    /api/projects/{project_id}/discovery
POST   /api/projects/{project_id}/specifications
GET    /api/projects/{project_id}/specifications/review
POST   /api/projects/{project_id}/specifications/review
POST   /api/projects/{project_id}/repository
GET    /api/projects/{project_id}/repository
POST   /api/projects/{project_id}/repository/refresh
POST   /api/projects/{project_id}/authority/compile
GET    /api/projects/{project_id}/authority/review
POST   /api/projects/{project_id}/authority/decision
```

Graph-node browser mutations may carry hidden current guards generated by the page. Repository endpoints accept only path plus transport metadata and resolve the active binding guard server-side. Unknown caller-owned compiler input, model override, repository metadata, or artifact fingerprints must return `422`.

- [ ] **Step 4: Implement CLI guard preparation**

Each graph-node CLI command reads current position once, selects the exact semantic decision, builds the internal guarded request, and then mutates. Project creation has no prior position; repository commands read the active binding projection and build the orthogonal guard internally. If no unique graph decision is available, return structured `TRANSITION_NOT_AVAILABLE`; never ask the agent to copy graph/fact/decision fingerprints.

`workflow next` renders commands containing only semantic arguments, exact IDs required for selection, idempotency key, and actor. It must advertise `vision respond` first on a new Project, `goal respond` after Vision acceptance, discovery only after Goal acceptance, and Authority compile only after specification acceptance. At the quiescent Goal boundary it may advertise completion or abandonment; after either outcome it advertises a new Goal interview without rerunning Vision.

- [ ] **Step 5: Run transport tests and commit**

Run:

```bash
uv run --frozen pytest tests/adapters/test_api_workflow_domain.py tests/adapters/test_cli_workflow_domain.py tests/adapters/test_command_renderer.py tests/adapters/test_production_read_surfaces.py -q
git diff --check
```

Commit:

```bash
git add api.py cli/main.py cli/workflow_commands.py services/application.py tests/adapters/test_api_workflow_domain.py tests/adapters/test_cli_workflow_domain.py tests/adapters/test_command_renderer.py tests/adapters/test_production_read_surfaces.py
git commit -m "feat: expose task specific workflow interfaces"
```

---

### Task 8: Human Project, Vision, Product Goal, Specification, Authority, And Repository UI

**Files:**
- Modify: `frontend/index.html`
- Modify: `frontend/app.js`
- Modify: `frontend/project.html`
- Modify: `frontend/project.js`
- Test: `tests/test_create_project_modal_required_fields.mjs`
- Test: `tests/test_workflow_position_display.mjs`
- Create: `tests/test_vision_interview_ui.mjs`
- Create: `tests/test_product_goal_interview_ui.mjs`
- Create: `tests/e2e/test_single_project_lifecycle_ui.py`

**Interfaces:**
- Consumes: task-specific API routes from Task 7.
- Produces: usable human project creation, separate Vision and Product Goal conversations/reviews, specification review, one-click Authority compilation/review, repository status/actions, and plain-language workflow navigation.

- [ ] **Step 1: Write DOM tests for the new human contract**

Assert:

```text
create modal contains Project Name, Description, and Repository Path only
only Project Name is required
successful create navigates to the new Project page
new Project page opens the Vision interview panel
Vision panel contains one response field and focused questions
Vision review panel shows only the exact Vision candidate
accepted Vision opens a separate Product Goal interview panel
Product Goal panel contains one response field and focused questions
Product Goal review shows the exact Goal candidate with accepted Vision as read-only context
each review exposes its own Accept, Feedback, and Reject controls
Goal outcome controls appear only when the graph advertises Fulfill or Abandon
recorded discovery and specification artifacts are readable without raw JSON editing
specification review exposes Accept, Feedback, and Reject for the exact candidate
Authority compile is one labeled button with no payload form
Authority review renders the exact packet and exposes Accept, Feedback, and Reject
repository status shows path, branch or detached state, short SHA, clean/dirty, inspected time, and warnings
repository controls expose Attach or Replace plus Refresh
no visible graph node ID, raw JSON textarea, fingerprint input, commit input, dirty checkbox, remote input, or model selector exists
human stage labels cover Vision, Product Goal, Discovery, Specification, Authority, Backlog, Roadmap, Stories, Sprint, Execution, and Review
```

- [ ] **Step 2: Run DOM tests and verify RED**

Run:

```bash
node --test tests/test_create_project_modal_required_fields.mjs tests/test_workflow_position_display.mjs tests/test_vision_interview_ui.mjs tests/test_product_goal_interview_ui.mjs
```

Expected: failures show the old selector, raw payload modal, and graph identifiers.

- [ ] **Step 3: Rebuild project creation and summary**

Use one compact modal with name, description, and optional local path. Keep existing Material Symbols for icon buttons. Use square or at most 8px-radius controls, stable button sizes, restrained neutral surfaces, and no nested cards.

After `201`, redirect to `/dashboard/project.html?id={project_id}`. Replace the origin summary with Product Goal and repository status summaries.

- [ ] **Step 4: Build separate Vision and Product Goal conversation/review states**

Render the current Vision questions above one multiline response input and preserve prior Vision turns in a compact transcript. When complete, replace that form with only the exact immutable Vision candidate plus Accept, Feedback, and Reject controls. After Vision acceptance, open a distinct Product Goal interview with its own transcript and one response input. Goal review renders the exact Goal candidate and the accepted Vision as read-only context; its controls decide only the Goal. Feedback opens one rationale field and returns to the corresponding interview.

At the quiescent delivery boundary, render separate Fulfill Goal and Abandon Goal commands only when advertised. Each requires a human rationale and a confirmation dialog. The UI never infers Goal completion from Sprint or Backlog state.

- [ ] **Step 5: Build specification, Authority, repository, and plain-language workflow controls**

Render discovery and specification artifacts as read-only human content after an agent records them. When specification review is available, show the exact candidate and Accept, Feedback, and Reject controls. Feedback and rejection require a rationale; acceptance may use an optional rationale.

Render `authority.compile` as one Compile button that sends no operator-authored payload. After compilation, render the existing Authority review packet as human-readable invariants and findings with Accept, Feedback, and Reject controls. No Authority action may expose compiler input, fingerprints, model selection, or raw JSON.

Attach/Replace asks only for a local path. Refresh asks for no input. Display structured errors inline, keep prior binding visible after failure, and refresh the projection after success.

Map node IDs internally to human labels and commands. The operator sees stage names, reason copy, status, and meaningful button labels; graph IDs remain in JavaScript data attributes only.

- [ ] **Step 6: Add Playwright desktop and mobile verification**

In a temporary fresh profile with fake Vision, Product Goal, and Authority execution, verify:

```text
1440x900: create without repository, answer/review Vision, answer/review Product Goal, record discovery/specification through the test API, review specification, compile/review Authority, no overlap
390x844: create with temporary Git repository, dirty warning wraps, no horizontal overflow
both: repository refresh leaves workflow stage unchanged
both: no raw JSON, compiler payload, or internal guard input is visible
```

Capture screenshots under `artifacts/ui/single-project-lifecycle/` and assert `document.documentElement.scrollWidth == document.documentElement.clientWidth` at both viewports.

- [ ] **Step 7: Run UI tests and commit**

Run:

```bash
node --test tests/test_create_project_modal_required_fields.mjs tests/test_workflow_position_display.mjs tests/test_vision_interview_ui.mjs tests/test_product_goal_interview_ui.mjs
uv run --frozen pytest tests/e2e/test_single_project_lifecycle_ui.py -q
git diff --check
```

Commit:

```bash
git add frontend/index.html frontend/app.js frontend/project.html frontend/project.js tests/test_create_project_modal_required_fields.mjs tests/test_workflow_position_display.mjs tests/test_vision_interview_ui.mjs tests/test_product_goal_interview_ui.mjs tests/e2e/test_single_project_lifecycle_ui.py artifacts/ui/single-project-lifecycle
git commit -m "feat: add human single lifecycle dashboard"
```

---

### Task 9: Delete Retired Runtime, Vocabulary, And Documentation

**Files:**
- Delete: paths listed in the "Modules deleted at cutover" section
- Delete: tests dedicated only to deleted setup and scope-extension behavior
- Delete: superseded active plans, specs, manuals, feedback artifacts, and examples containing either retired origin label
- Modify: `README.md`
- Modify: `CONTEXT.md`
- Modify: `docs/agent-cli-manual.md`
- Modify: `docs/testing/workflow-graph-acceptance-checklist.md`
- Modify: `config/models.yaml`
- Modify: `config/models.test.yaml`
- Modify: `pyproject.toml`
- Modify: `models/workflow.py`
- Modify: `models/__init__.py`
- Modify: `models/db.py`
- Modify: `agile_sqlmodel.py`
- Modify: `workflow/__init__.py`
- Modify: `workflow/facts.py`
- Modify: `workflow/domain.py`
- Modify: `workflow/requests/__init__.py`
- Modify: `workflow/handlers/__init__.py`
- Modify: `repositories/workflow.py`
- Modify: `repositories/project.py`
- Modify: `services/application.py`
- Modify: `services/agent_workbench/error_codes.py`
- Modify: `services/specs/__init__.py`
- Modify: `tools/spec_tools.py`
- Modify: `tools/export_snapshot.py`
- Modify: `adapters/adk/recipes.py`
- Modify: `tests/test_prompt_package_resources.py`
- Modify: `tests/test_task17_review_absence.py`
- Modify: `tests/test_agent_workbench_error_codes.py`
- Modify: `tests/test_project_repository_deletion.py`
- Modify: `tests/adapters/test_adk_graph_recipes.py`
- Modify: `tests/adapters/test_adk_workflow_runner.py`
- Modify: `tests/adapters/test_api_workflow_domain.py`
- Modify: `tests/adapters/test_cli_workflow_domain.py`
- Modify: `tests/adapters/test_command_renderer.py`
- Modify: `tests/dev_runtime/test_cross_worktree.py`
- Modify: `scripts/verify_distribution.py`
- Test: `tests/test_single_lifecycle_absence.py`

**Interfaces:**
- Consumes: complete replacement runtime from Tasks 1-8.
- Produces: one live vocabulary, one package resource set, and no compatibility surface.

- [ ] **Step 1: Write whole-tree and distribution absence tests**

Construct labels in code so the test does not contain either complete word:

```python
RETIRED_LABELS = ("brown" + "field", "green" + "field")


def test_retired_labels_absent_from_tracked_paths_and_content() -> None:
    completed = subprocess.run(
        ["git", "ls-files", "-z"],
        check=True,
        capture_output=True,
    )
    paths = tuple(
        Path(os.fsdecode(raw))
        for raw in completed.stdout.split(b"\0")
        if raw
    )
    offenders: list[str] = []
    for path in paths:
        folded_path = os.fsdecode(os.fsencode(path)).casefold()
        if any(label in folded_path for label in RETIRED_LABELS):
            offenders.append(str(path))
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (FileNotFoundError, UnicodeDecodeError, IsADirectoryError):
            continue
        if any(label in content.casefold() for label in RETIRED_LABELS):
            offenders.append(str(path))
    assert offenders == []
```

Extend distribution verification to scan wheel/sdist member paths and UTF-8 text content using the same computed labels.

- [ ] **Step 2: Run absence tests and verify RED**

Run: `uv run --frozen pytest tests/test_single_lifecycle_absence.py tests/test_prompt_package_resources.py -q`

Expected: fail with the complete list of remaining retired modules, tests, resources, and documents.

- [ ] **Step 3: Delete superseded runtime and specialized uncommitted work**

Delete the computed-label model, agent, prompt/contract support, full repository inventory, curation input, setup and scope-extension graph modules, handlers, requests, routes, commands, model roles, error codes, tables, and tests with `git rm`. Remove the corresponding uncommitted curation files and their tests; retain `services/authority_compilation_input.py`, `services/node_attempt_replay.py`, and project deletion tests.

Remove setup/scope artifacts from `models/workflow.py`, `workflow/facts.py`, `repositories/workflow.py`, `workflow/domain.py`, `models/db.py`, `agile_sqlmodel.py`, and package exports.

- [ ] **Step 4: Delete or rewrite every remaining tracked match**

Run:

```bash
RETIRED_ORIGIN_A="$(printf '%s%s' 'brown' 'field')"
RETIRED_ORIGIN_B="$(printf '%s%s' 'green' 'field')"
git grep -Il -i -e "$RETIRED_ORIGIN_A" -e "$RETIRED_ORIGIN_B"
git ls-files | rg -i "$RETIRED_ORIGIN_A|$RETIRED_ORIGIN_B"
```

Rewrite the four active documents named in this task around the one lifecycle. Delete every superseded plan, spec, report, feedback artifact, and test that still appears. Both commands must exit with status 1 and print no match before continuing.

- [ ] **Step 5: Remove stale package resources and model role**

Delete the retired curator key from both model YAML files, remove deleted prompts/modules from package data, and update resource tests to assert they are absent while Vision, Product Goal, Authority, Backlog, Roadmap, Story, and Sprint resources remain importable from a wheel.

Update the pytest marker description to `integration: marks environment-dependent acceptance or external-provider tests` while retaining the default `-m 'not integration'` exclusion.

- [ ] **Step 6: Run absence and retained-resource tests**

Run:

```bash
uv run --frozen pytest tests/test_single_lifecycle_absence.py tests/test_prompt_package_resources.py tests/adapters/test_agent_contract_boundaries.py tests/adapters/test_production_runtime_cutover.py -q
uv run --frozen python scripts/verify_distribution.py
git diff --check
```

Expected: no retired tracked path/content or archive member; retained runtime imports pass.

- [ ] **Step 7: Commit**

Every deletion in Steps 3-4 must already be staged by `git rm`. Stage only these retained files edited during cleanup, review the staged path list, then commit:

```bash
git add README.md CONTEXT.md docs/agent-cli-manual.md docs/testing/workflow-graph-acceptance-checklist.md config/models.yaml config/models.test.yaml pyproject.toml models/workflow.py models/__init__.py models/db.py agile_sqlmodel.py workflow/__init__.py workflow/facts.py workflow/domain.py workflow/requests/__init__.py workflow/handlers/__init__.py repositories/workflow.py repositories/project.py services/application.py services/agent_workbench/error_codes.py services/specs/__init__.py tools/spec_tools.py tools/export_snapshot.py adapters/adk/recipes.py tests/test_prompt_package_resources.py tests/test_task17_review_absence.py tests/test_agent_workbench_error_codes.py tests/test_project_repository_deletion.py tests/adapters/test_adk_graph_recipes.py tests/adapters/test_adk_workflow_runner.py tests/adapters/test_api_workflow_domain.py tests/adapters/test_cli_workflow_domain.py tests/adapters/test_command_renderer.py tests/dev_runtime/test_cross_worktree.py scripts/verify_distribution.py tests/test_single_lifecycle_absence.py
git diff --cached --name-status
git commit -m "refactor: remove retired project setup architecture"
```

The commit must include deletions and the four rewritten active documents, but no unrelated local file.

---

### Task 10: Full Gate, Fresh Distribution, And Three-Repository Acceptance

**Files:**
- Create: `artifacts/acceptance/single-project-lifecycle/acceptance-report.md`
- Create: `artifacts/acceptance/single-project-lifecycle/caRtola.json`
- Create: `artifacts/acceptance/single-project-lifecycle/asa.json`
- Create: `artifacts/acceptance/single-project-lifecycle/myfinance.json`
- Create: `tests/acceptance/test_named_repository_lifecycle.py`

**Interfaces:**
- Consumes: complete implementation and worktree-local `./agileforge-dev`.
- Produces: clean-source verification and reproducible provider-free acceptance evidence for caRtola, ASA Deep Process Control Advisory System, and MyFinance.

- [ ] **Step 1: Run the complete uv-only quality gate**

Run:

```bash
uv lock --check
uv run --frozen pyrepo-check --all
node --test tests/*.mjs
git diff --check
./agileforge-dev check --json
```

Expected: Ruff, annotations, `ty`, Bandit, pytest, Node tests, distribution checks, and whitespace checks all pass without suppressions.

- [ ] **Step 2: Prove fresh schema and clean-source archives**

Run:

```bash
ACCEPTANCE_ROOT="$(mktemp -d /tmp/agileforge-single-lifecycle.XXXXXX)"
AGILEFORGE_DB_URL="sqlite:///$ACCEPTANCE_ROOT/business.sqlite3" AGILEFORGE_ADK_EXECUTION_TRACE_DB_URL="sqlite:///$ACCEPTANCE_ROOT/trace.sqlite3" uv run --frozen python agile_sqlmodel.py
uv run --frozen python scripts/verify_distribution.py
```

Inspect SQLite metadata in a test subprocess and assert the new tables exist, removed tables/columns do not exist, and both databases are independent.

- [ ] **Step 3: Initialize three isolated acceptance profiles**

After all implementation commits, run:

```bash
HEAD_SHA="$(git rev-parse HEAD)"
PROFILE_CARTOLA="acceptance-cartola-${HEAD_SHA:0:12}"
PROFILE_ASA="acceptance-asa-${HEAD_SHA:0:12}"
PROFILE_MYFINANCE="acceptance-myfinance-${HEAD_SHA:0:12}"
./agileforge-dev init --profile "$PROFILE_CARTOLA" --mode acceptance --expect-sha "$HEAD_SHA" --json
./agileforge-dev init --profile "$PROFILE_ASA" --mode acceptance --expect-sha "$HEAD_SHA" --json
./agileforge-dev init --profile "$PROFILE_MYFINANCE" --mode acceptance --expect-sha "$HEAD_SHA" --json
```

Each JSON result must report the current branch SHA, its own business DB, its own trace DB, `config/models.yaml`, and acceptance mode.

- [ ] **Step 4: Recreate and probe the three Projects without provider calls**

Use repository-at-creation for caRtola, later attachment for ASA, and no repository during initial MyFinance creation followed by attachment:

```bash
HEAD_SHA="$(git rev-parse HEAD)"
PROFILE_CARTOLA="acceptance-cartola-${HEAD_SHA:0:12}"
PROFILE_ASA="acceptance-asa-${HEAD_SHA:0:12}"
PROFILE_MYFINANCE="acceptance-myfinance-${HEAD_SHA:0:12}"
ACCEPTANCE_OUTPUT_ROOT="/tmp/agileforge-single-lifecycle-$HEAD_SHA"
mkdir -p "$ACCEPTANCE_OUTPUT_ROOT"
./agileforge-dev cli --profile "$PROFILE_CARTOLA" --json -- project create --name caRtola --repository-path /Users/aaat/projects/caRtola --idempotency-key acceptance-cartola-create-1 --actor acceptance-agent > "$ACCEPTANCE_OUTPUT_ROOT/cartola.json"
./agileforge-dev cli --profile "$PROFILE_ASA" --json -- project create --name "ASA Deep Process Control Advisory System" --idempotency-key acceptance-asa-create-1 --actor acceptance-agent > "$ACCEPTANCE_OUTPUT_ROOT/asa-create.json"
./agileforge-dev cli --profile "$PROFILE_ASA" --json -- repository attach --project-id 1 --path /Users/aaat/projects/asa-deep-process-control-experiments --idempotency-key acceptance-asa-attach-1 --actor acceptance-agent > "$ACCEPTANCE_OUTPUT_ROOT/asa.json"
./agileforge-dev cli --profile "$PROFILE_MYFINANCE" --json -- project create --name MyFinance --idempotency-key acceptance-myfinance-create-1 --actor acceptance-agent > "$ACCEPTANCE_OUTPUT_ROOT/myfinance-create.json"
./agileforge-dev cli --profile "$PROFILE_MYFINANCE" --json -- repository attach --project-id 1 --path /Users/aaat/myfinance --idempotency-key acceptance-myfinance-attach-1 --actor acceptance-agent > "$ACCEPTANCE_OUTPUT_ROOT/myfinance.json"
```

Assert exact local path, current SHA, branch or detached state, dirty state, warnings, and `vision.interview` as the first required product action. Assert trace DBs contain no model execution after this step. Keep results under `$ACCEPTANCE_OUTPUT_ROOT` until every acceptance-mode command has finished so the checkout remains clean.

- [ ] **Step 5: Verify CLI and UI behavior with fake Vision and Goal execution**

Add a parameterized, provider-free acceptance test over these exact tuples:

```python
(
    ("caRtola", Path("/Users/aaat/projects/caRtola"), True),
    (
        "ASA Deep Process Control Advisory System",
        Path("/Users/aaat/projects/asa-deep-process-control-experiments"),
        False,
    ),
    ("MyFinance", Path("/Users/aaat/myfinance"), False),
)
```

The test composes a fresh temporary business DB, the production Git probe, one fake complete Vision recipe, and one fake complete Product Goal recipe. For each tuple it creates/attaches as indicated, accepts the exact Vision, asserts `goal.interview` is required while discovery remains blocked, separately interviews and accepts the exact Goal, and then asserts `discovery.record` is required while `authority.compile` remains blocked. Mark it `integration` because it depends on local repositories, not because it uses a provider.

Run:

```bash
uv run --frozen pytest -m integration tests/acceptance/test_named_repository_lifecycle.py -q
```

- [ ] **Step 6: Stop before paid acceptance and request explicit operator approval**

Do not call the production Vision or Product Goal model during automated implementation. Record `provider_backed_vision: not_run_pending_operator_approval` and `provider_backed_product_goal: not_run_pending_operator_approval` for each repository. A later operator-approved run may use the repository `.env` through `--secrets-file /Users/aaat/projects/agileforge/.env`; never create another secrets file or print its values.

- [ ] **Step 7: Write and review the acceptance report**

After all acceptance-mode commands finish, recompute `ACCEPTANCE_OUTPUT_ROOT="/tmp/agileforge-single-lifecycle-$(git rev-parse HEAD)"`, create `artifacts/acceptance/single-project-lifecycle/`, and copy the three final JSON results into the named artifact files. Then write the report.

The report must contain:

```text
branch and exact SHA
dirty/clean source status
all quality-gate command results
fresh schema evidence
wheel and sdist evidence
per-repository create/attach provenance
first available product action
provider-call count before Vision
fake Vision review result
fake Product Goal review result
paid acceptance status
remaining follow-up: feature-level CurrentStateAssessment only
```

- [ ] **Step 8: Commit acceptance artifacts**

```bash
git add artifacts/acceptance/single-project-lifecycle/acceptance-report.md artifacts/acceptance/single-project-lifecycle/caRtola.json artifacts/acceptance/single-project-lifecycle/asa.json artifacts/acceptance/single-project-lifecycle/myfinance.json tests/acceptance/test_named_repository_lifecycle.py
git commit -m "test: record single lifecycle acceptance"
```

Do not merge. Present the exact test evidence and branch SHA to the operator for approval first.
