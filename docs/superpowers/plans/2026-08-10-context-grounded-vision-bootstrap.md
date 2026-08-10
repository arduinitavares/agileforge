# Context-Grounded Vision Bootstrap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the first Project Vision action generate an evidence-grounded draft without requiring human text, while preserving explicit human review, durable provenance, replay safety, and repository-drift protection.

**Architecture:** Add a deterministic, bounded host evidence collector and strict Vision generation contracts. Introduce `vision.bootstrap` as the first agentic graph node, retain `vision.interview` for ordinary-language clarification, and persist the exact successful evidence snapshot with each Vision lineage. Keep the workflow graph as the sole routing authority; provider calls remain execution-only ADK recipes and every paid input is persisted before dispatch.

**Tech Stack:** Python 3.13, uv, Pydantic v2, SQLModel/SQLite, Google ADK 2.x graphs, FastAPI, Typer, vanilla JavaScript, Node test runner, pytest, Ruff, ty, Bandit.

## Global Constraints

- Use uv only. Do not add another package manager or direct environment mutation instructions.
- This is a hard break for experimental databases and profiles. Add no migration, compatibility alias, fallback schema, or dual-write path.
- The human remains the only authority for Vision acceptance, rejection, and feedback.
- Automated tests use fakes and temporary repositories only. They must not touch the manual acceptance profile, String Calculator Lab, caRtola, ASA, MyFinance, or any other user repository.
- `GET` and status/projection paths must never invoke a model or mutate workflow state.
- Repository evidence is bounded to the approved seven relative paths, eight items, 32 KiB per item, and 96 KiB total.
- Never send absolute paths, Git common directories, status-entry paths, URL credentials, query strings, fragments, environment files, source code, or arbitrary repository files to a model.
- Persist normalized attempt input before every provider call. Persist a `VisionEvidenceSnapshot`, Vision turn, and optional Vision artifact only after strict output validation succeeds.
- Permit at most one compact semantic-repair call. Never retry paid Vision generation in an unbounded loop.
- Fix typing errors without suppressions.
- Keep `config/models.yaml` unchanged: production Vision continues to use the reviewed `product_vision` role.
- Before each task, verify `git status --short --branch` in this worktree. Preserve unrelated edits.
- After each task, run the task tests, `uv lock --check`, and `git diff --check` before committing.

## File And Interface Map

- `services/contracts/vision_evidence.py`: strict evidence item, warning, and bundle contracts plus byte-limit constants.
- `services/contracts/vision.py`: discriminated bootstrap/clarification/revision model inputs and the provenance-rich Vision draft output.
- `services/vision_output_validation.py`: deterministic cross-reference and completion validation independent of ADK.
- `services/vision_evidence.py`: deterministic project/repository evidence collection and freshness checks.
- `services/vision_input.py`: replay-safe host preparation for bootstrap, clarification, and revision attempts.
- `models/product_definition.py`: immutable Vision evidence snapshot plus expanded turn/artifact provenance.
- `workflow/facts.py` and `repositories/workflow.py`: load exact Vision evidence and attempt-failure facts into the pure graph.
- `workflow/definitions/vision.py`: route bootstrap, clarification, review, and accepted-Vision revision.
- `workflow/requests/vision.py` and `workflow/handlers/vision.py`: typed requests and atomic persistence.
- `adapters/adk/recipes.py`, `adapters/adk/agents/vision.py`, and `adapters/adk/prompts/vision.txt`: bounded generation and one semantic repair.
- `services/application.py`, `api.py`, and `cli/main.py`: semantic application, HTTP, and CLI entry points.
- The existing concrete Vision projection, located with `rg -n "def vision_status" services repositories`, remains the only read projection.
- `frontend/project.js`: human bootstrap, clarification, provenance, and review controls.

---

### Task 1: Strict Evidence And Vision Contracts

**Files:**
- Create: `services/contracts/vision_evidence.py`
- Modify: `services/contracts/vision.py`
- Create: `services/vision_output_validation.py`
- Create: `tests/services/contracts/test_vision_evidence.py`
- Modify: `tests/services/contracts/test_vision.py`
- Create: `tests/services/test_vision_output_validation.py`

**Interfaces:**
- Produces: `VisionEvidenceItem`, `VisionEvidenceWarning`, `VisionEvidenceBundle`, `VisionBootstrapInput`, `VisionClarificationInput`, `VisionRevisionInput`, `VisionAgentInput`, `VisionDraftOutput`, `VisionRepairInput`, and `validate_vision_draft(output, input_payload) -> None`.
- Consumes: `workflow.contracts.JsonObject` and the existing seven-field `VisionComponents` vocabulary.

- [ ] **Step 1: Write failing evidence-contract tests**

Add tests proving strict extra-field rejection, canonical fingerprint validation, allowed trust/kind values, POSIX-relative paths, and the approved constants:

```python
def test_evidence_contract_exposes_approved_bounds() -> None:
    assert MAX_EVIDENCE_ITEMS == 8
    assert MAX_EVIDENCE_ITEM_BYTES == 32 * 1024
    assert MAX_EVIDENCE_TOTAL_BYTES == 96 * 1024


def test_evidence_item_rejects_absolute_path() -> None:
    with pytest.raises(ValidationError, match="relative_path"):
        VisionEvidenceItem(
            evidence_id="file:README.md",
            kind="readme",
            relative_path="/private/repository/README.md",
            content_fingerprint="sha256:" + "0" * 64,
            trust="unreviewed_repository_evidence",
            content="Example",
            truncated=False,
        )
```

- [ ] **Step 2: Write failing Vision semantic tests**

Cover complete/incomplete drafts, duplicate IDs, unknown references, missing basis rows, human basis without human input, evidence basis without IDs, inference basis without assumptions, unresolved conflicts without questions, and forbidden extra Product Goal/delivery fields.

```python
def test_complete_draft_requires_no_open_questions() -> None:
    output = complete_draft_output()
    output.clarifying_questions = (
        VisionClarifyingQuestion(
            question_id="question:audience",
            text="Who benefits first?",
            affected_components=("target_user",),
        ),
    )

    with pytest.raises(VisionDraftValidationError, match="complete"):
        validate_vision_draft(output, bootstrap_input())


def test_human_basis_requires_human_input_in_lineage() -> None:
    with pytest.raises(VisionDraftValidationError, match="human"):
        validate_vision_draft(
            complete_draft_output_with_human_basis(),
            bootstrap_input(),
        )
```

- [ ] **Step 3: Run the tests and confirm failure**

```bash
uv run --frozen pytest \
  tests/services/contracts/test_vision_evidence.py \
  tests/services/contracts/test_vision.py \
  tests/services/test_vision_output_validation.py -q
```

Expected: collection/import failures for the new contracts and validator.

- [ ] **Step 4: Implement the strict contracts**

Use `ConfigDict(extra="forbid")` on every model. Define one ADK-compatible envelope around the discriminated union:

```python
type VisionOperationInput = Annotated[
    VisionBootstrapInput | VisionClarificationInput | VisionRevisionInput,
    Field(discriminator="operation"),
]


class VisionAgentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request: VisionOperationInput
```

Use these exact component names everywhere:

```python
type VisionComponentName = Literal[
    "project_name",
    "target_user",
    "problem",
    "product_category",
    "key_benefit",
    "competitors",
    "differentiator",
]
```

Define structured basis, assumptions, conflicts, and questions. `VisionDraftOutput` exposes only `schema_version`, `components`, `component_basis`, `draft_statement`, `assumptions`, `conflicts`, `clarifying_questions`, and `is_complete`.

- [ ] **Step 5: Implement deterministic semantic validation**

`validate_vision_draft` collects all findings and raises one `VisionDraftValidationError(findings: tuple[str, ...])`. Validate the eleven design invariants without model calls or database reads. Treat human input as available only for clarification or a revision reason. Do not implement a text classifier for feature leakage; strict output fields plus the prompt are the enforceable boundary.

- [ ] **Step 6: Run tests and commit**

```bash
uv run --frozen pytest \
  tests/services/contracts/test_vision_evidence.py \
  tests/services/contracts/test_vision.py \
  tests/services/test_vision_output_validation.py -q
uv lock --check
git diff --check
```

Expected: all selected tests pass.

```bash
git add services/contracts/vision.py services/contracts/vision_evidence.py \
  services/vision_output_validation.py tests/services/contracts/test_vision.py \
  tests/services/contracts/test_vision_evidence.py \
  tests/services/test_vision_output_validation.py
git commit -m "feat: define grounded vision contracts"
```

---

### Task 2: Deterministic Bounded Evidence Collector

**Files:**
- Create: `services/vision_evidence.py`
- Create: `tests/services/test_vision_evidence.py`
- Modify: `tests/services/test_repository_probe.py`

**Interfaces:**
- Consumes: `RepositoryProbe`, `RepositoryBinding`, `TechnicalSpecArtifact`, and Task 1 evidence contracts.
- Produces: `VisionEvidenceCollector(engine: Engine, repository_probe: RepositoryProbe)` with `collect(project_id: int) -> VisionEvidenceBundle` and typed `VisionEvidenceCollectionError(code, message)`.

- [ ] **Step 1: Create temporary-repository fixtures and failing collector tests**

Build repositories only under pytest temporary paths. Cover project-only collection and the complete allowlist. Assert exact item order and that `.env`, `src/private.py`, and `notes.txt` are absent.

```python
def test_project_without_repository_collects_only_project_metadata(
    collector: VisionEvidenceCollector,
    project_id: int,
) -> None:
    bundle = collector.collect(project_id)

    assert [item.evidence_id for item in bundle.items] == ["project:metadata"]
    assert bundle.items[0].trust == "operator_provided"
    assert "/Users/" not in bundle.model_dump_json()
```

- [ ] **Step 2: Add failing safety and bounds tests**

Cover sanitized remotes, no status paths, symlink escape, invalid UTF-8, invalid TOML, invalid `TechnicalSpecArtifact`, both valid JSON spec locations, Markdown fallback order, oversized structured omission, UTF-8-safe text truncation, eight-item cap, 96 KiB total cap, deterministic warnings, and repeated fingerprint equality.

```python
def test_remote_credentials_query_and_fragment_are_removed(
    collector: VisionEvidenceCollector,
    project_id: int,
    repository: Path,
) -> None:
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "remote",
            "set-url",
            "origin",
            "https://user:secret@example.test/repo.git?token=x#fragment",
        ],
        check=True,
    )
    bundle = collector.collect(project_id)
    serialized = bundle.model_dump_json()

    assert "user:secret" not in serialized
    assert "token=x" not in serialized
    assert "fragment" not in serialized
    assert "https://example.test/repo.git" in serialized
```

- [ ] **Step 3: Run tests and confirm failure**

```bash
uv run --frozen pytest tests/services/test_vision_evidence.py -q
```

Expected: import failure for `VisionEvidenceCollector`.

- [ ] **Step 4: Implement collection with small source helpers**

Use this exact priority and collector boundary:

```python
_ALLOWED_PATHS: tuple[str, ...] = (
    "docs/spec/spec.json",
    "specs/spec.json",
    "CONTEXT.md",
    "README.md",
    "pyproject.toml",
    "docs/spec/spec.md",
    "specs/spec.md",
)


@dataclass(frozen=True)
class VisionEvidenceCollector:
    engine: Engine
    repository_probe: RepositoryProbe

    def collect(self, project_id: int) -> VisionEvidenceBundle:
        context = self._load_context(project_id)
        items = [self._project_item(context)]
        if context.binding is not None:
            observed = self._verify_binding(context.binding)
            items.extend(self._repository_items(context.binding, observed))
        return self._bounded_bundle(items)
```

Read eligible files through descriptors and compare identity, size, and nanosecond modification time before/after each read. Resolve symlinks and reject targets outside the worktree. Re-probe after all reads and compare exact provenance fields with the pre-read probe. Parse TOML with `tomllib`; validate JSON specs with `TechnicalSpecArtifact.model_validate_json`.

- [ ] **Step 5: Implement canonical IDs, warnings, and limits**

Use `project:metadata`, `repository:provenance`, and `file:<relative-path>` evidence IDs. Compute item/bundle fingerprints with `canonical_hash` over structured content excluding timestamps. Truncate text by UTF-8 byte count; omit oversized structured values with `STRUCTURED_EVIDENCE_TOO_LARGE`.

- [ ] **Step 6: Run tests and commit**

```bash
uv run --frozen pytest \
  tests/services/test_vision_evidence.py \
  tests/services/test_repository_probe.py -q
uv lock --check
git diff --check
```

Expected: all selected tests pass.

```bash
git add services/vision_evidence.py tests/services/test_vision_evidence.py \
  tests/services/test_repository_probe.py
git commit -m "feat: collect bounded vision evidence"
```

---

### Task 3: Durable Vision Evidence And Provenance Facts

**Files:**
- Modify: `models/product_definition.py`
- Modify: `agile_sqlmodel.py`
- Modify: `workflow/facts.py`
- Modify: `repositories/workflow.py`
- Modify: `repositories/project.py`
- Modify: `tests/workflow/test_vision_interview_transitions.py`
- Modify: `tests/test_project_repository_deletion.py`
- Create: `tests/workflow/test_vision_evidence_persistence.py`

**Interfaces:**
- Consumes: Task 1 contract JSON and existing `WorkflowNodeAttempt` identity.
- Produces: `VisionEvidenceSnapshot`, expanded `VisionInterviewTurn`, expanded `VisionArtifact`, `VisionEvidenceSnapshotFact`, and expanded Vision turn/artifact facts.

- [ ] **Step 1: Write failing schema and deletion tests**

Assert fresh schema creation includes `vision_evidence_snapshots`; bootstrap `user_text` is nullable; snapshot, turn, and artifact foreign keys reject cross-Project identities; project deletion removes evidence snapshots in FK-safe order.

```python
def test_vision_snapshot_references_same_project_attempt(session: Session) -> None:
    snapshot = VisionEvidenceSnapshot(
        project_id=PROJECT_ID,
        repository_binding_id=None,
        workflow_node_attempt_id=OTHER_PROJECT_ATTEMPT_ID,
        evidence_json=canonical_json(EVIDENCE_BUNDLE),
        evidence_fingerprint=EVIDENCE_FINGERPRINT,
        warnings_json="[]",
        created_at=NOW,
    )
    session.add(snapshot)

    with pytest.raises(IntegrityError):
        session.commit()
```

- [ ] **Step 2: Run tests and confirm failure**

```bash
uv run --frozen pytest \
  tests/workflow/test_vision_evidence_persistence.py \
  tests/test_project_repository_deletion.py -q
```

Expected: missing model/table/field failures.

- [ ] **Step 3: Add the fresh-schema models**

Add `VisionEvidenceSnapshot` with the approved fields and composite same-Project foreign keys. Change `VisionInterviewTurn` to `operation IN ('bootstrap', 'clarification', 'revision')`, make `user_text` nullable, and add `vision_evidence_snapshot_id`, `component_basis_json`, `assumptions_json`, and `conflicts_json`. Add the same snapshot/basis/assumption/conflict provenance to `VisionArtifact`.

Do not retain a `mode` compatibility column. Update all current constructors/tests to the new `operation` field in this task so the repository remains runnable after the commit.

- [ ] **Step 4: Load strict graph facts**

Add exact fields to facts:

```python
class VisionEvidenceSnapshotFact(FrozenModel):
    vision_evidence_snapshot_id: int
    repository_binding_id: int | None
    workflow_node_attempt_id: int
    evidence: JsonObject
    evidence_fingerprint: str
    warnings: tuple[JsonObject, ...]
    created_at: _DATETIME
```

Parse persisted JSON with existing strict loader helpers. Reject missing attempt/snapshot identities, cross-Project references, invalid canonical JSON, or fingerprint mismatches as `WorkflowFactLoadError`.

- [ ] **Step 5: Update schema exports and deletion ordering**

Import the new table through `agile_sqlmodel.py`. Delete artifact decisions/artifacts before turns, then snapshots, then attempts. Keep project deletion atomic and add no migration code.

- [ ] **Step 6: Run tests and commit**

```bash
uv run --frozen pytest \
  tests/workflow/test_vision_evidence_persistence.py \
  tests/workflow/test_vision_interview_transitions.py \
  tests/test_project_repository_deletion.py -q
uv lock --check
git diff --check
```

Expected: all selected tests pass.

```bash
git add models/product_definition.py agile_sqlmodel.py workflow/facts.py \
  repositories/workflow.py repositories/project.py \
  tests/workflow/test_vision_evidence_persistence.py \
  tests/workflow/test_vision_interview_transitions.py \
  tests/test_project_repository_deletion.py
git commit -m "feat: persist vision evidence provenance"
```

---

### Task 4: Bootstrap And Clarification Graph Transitions

**Files:**
- Modify: `workflow/requests/vision.py`
- Modify: `workflow/requests/__init__.py`
- Modify: `workflow/domain.py`
- Modify: `workflow/handlers/vision.py`
- Modify: `workflow/definitions/vision.py`
- Modify: `workflow/definitions/root.py`
- Create: `tests/workflow/test_vision_bootstrap_graph.py`
- Create: `tests/workflow/test_vision_bootstrap_transitions.py`
- Modify: `tests/workflow/test_vision_interview_graph.py`

**Interfaces:**
- Consumes: Task 3 snapshot/turn/artifact facts and Task 1 output structures.
- Produces: positioned `GenerateVisionBootstrap` and revised `RecordVisionInterviewTurn`; graph node `vision.bootstrap`; atomic `_persist_vision_generation(session, request, decision, evaluated_at) -> TransitionResult` handler.

- [ ] **Step 1: Write failing graph-route tests**

Prove these exact routes:

```text
no Vision lineage                              -> vision.bootstrap available
incomplete bootstrap with open questions      -> vision.interview available
complete bootstrap                            -> vision.review waiting
feedback or rejection                         -> vision.interview available
accepted Vision plus eligible revision intent -> vision.bootstrap available
accepted Vision with active Product Goal       -> revision start unavailable
```

Assert `vision.bootstrap.required_inputs == ()`. Assert the initial graph does not expose `mode` or `user_text`.

- [ ] **Step 2: Write failing transition tests**

Test that bootstrap persists snapshot/turn atomically, a complete result also persists an artifact, clarification reuses the same snapshot, revision supersedes the accepted artifact, cross-Project snapshots/attempts fail, invalid basis references fail, and a failed transaction leaves no snapshot/turn/artifact.

- [ ] **Step 3: Run tests and confirm failure**

```bash
uv run --frozen pytest \
  tests/workflow/test_vision_bootstrap_graph.py \
  tests/workflow/test_vision_bootstrap_transitions.py \
  tests/workflow/test_vision_interview_graph.py -q
```

Expected: `vision.bootstrap` and `GenerateVisionBootstrap` are absent.

- [ ] **Step 4: Add typed positioned requests**

Define `GenerateVisionBootstrap` with request kind `generate_vision_bootstrap`, node `vision.bootstrap`, trusted evidence bundle, operation `bootstrap | revision`, output provenance, and attempt identity. Revise `RecordVisionInterviewTurn` to require operation `clarification`, selected snapshot ID/fingerprint, host-derived addressed question IDs, ordinary `user_text`, output provenance, and attempt identity.

- [ ] **Step 5: Implement one atomic persistence path**

Extract `_persist_vision_generation` in `workflow/handlers/vision.py`. It must:

1. Validate the positioned decision and same-Project attempt.
2. For bootstrap/revision, validate the trusted bundle fingerprint and create `VisionEvidenceSnapshot`.
3. For clarification, load and verify the selected existing snapshot.
4. Validate output semantics again at the domain boundary.
5. Append one turn linked to the prior turn and snapshot.
6. Materialize one artifact only when `is_complete` is true.
7. Flush and return IDs only after all related rows are valid.

- [ ] **Step 6: Implement pure routing**

Add `vision.bootstrap` before `vision.interview` in `VISION_INTERVIEW_NODES`. Bootstrap is the only required initial action. Clarification is available only for a current incomplete turn or a reviewed artifact with feedback/rejection. A revision intent with no revision turn returns bootstrap; the host later selects `operation=revision`.

- [ ] **Step 7: Run tests and commit**

```bash
uv run --frozen pytest \
  tests/workflow/test_vision_bootstrap_graph.py \
  tests/workflow/test_vision_bootstrap_transitions.py \
  tests/workflow/test_vision_interview_graph.py \
  tests/workflow/test_vision_interview_transitions.py -q
uv lock --check
git diff --check
```

Expected: all selected tests pass.

```bash
git add workflow/requests/vision.py workflow/requests/__init__.py \
  workflow/domain.py workflow/handlers/vision.py workflow/definitions/vision.py \
  workflow/definitions/root.py tests/workflow/test_vision_bootstrap_graph.py \
  tests/workflow/test_vision_bootstrap_transitions.py \
  tests/workflow/test_vision_interview_graph.py \
  tests/workflow/test_vision_interview_transitions.py
git commit -m "feat: add vision bootstrap graph route"
```

---

### Task 5: Replay-Safe Input Preparation And Drift Recovery

**Files:**
- Create: `services/vision_input.py`
- Delete: `services/vision_interview_input.py`
- Modify: `workflow/contracts.py`
- Modify: `workflow/facts.py`
- Modify: `repositories/workflow.py`
- Create: `adapters/adk/errors.py`
- Modify: `adapters/adk/runner.py`
- Create: `tests/services/test_vision_input.py`
- Delete: `tests/services/test_vision_interview_input.py`
- Modify: `tests/adapters/test_adk_workflow_runner.py`
- Modify: `docs/superpowers/specs/2026-08-10-context-grounded-vision-bootstrap-design.md`

**Interfaces:**
- Consumes: `VisionEvidenceCollector`, current Vision facts, durable replay services, and positioned decisions.
- Produces: `VisionInputService.build_bootstrap(project_id, decision) -> JsonObject`, `VisionInputService.build_clarification(project_id, decision, user_text) -> JsonObject`, `VisionAgenticPreflightError`, and typed failure codes `REPOSITORY_PROVENANCE_STALE`, `REPOSITORY_CHANGED_DURING_EVIDENCE_COLLECTION`, and `VISION_EVIDENCE_STALE`.

- [ ] **Step 1: Write failing input-selection tests**

Test project-only bootstrap, repository bootstrap, revision bootstrap, clarification with host-derived question IDs, complete-candidate feedback with empty addressed-question IDs, snapshot reuse, same-key replay, and different-input idempotency conflict.

```python
def test_bootstrap_requires_no_human_response(
    service: VisionInputService,
    bootstrap_decision: NodeDecision,
) -> None:
    payload = service.build_bootstrap(
        project_id=PROJECT_ID,
        decision=bootstrap_decision,
    )

    request = VisionAgentInput.model_validate(payload).request
    assert request.operation == "bootstrap"
    assert "user_response" not in request.model_dump()
```

- [ ] **Step 2: Write failing drift and paid-call prevention tests**

Cover stale binding vs fresh probe, file mutation during collection, clarification after evidence content changes with an unchanged status path set, unchanged evidence after repository refresh, changed evidence after refresh, and two concurrent requests for the same node/instance/decision. Fake provider call count remains zero for every failed preflight or competing request.

- [ ] **Step 3: Run tests and confirm failure**

```bash
uv run --frozen pytest \
  tests/services/test_vision_input.py \
  tests/adapters/test_adk_workflow_runner.py -q
```

Expected: missing `VisionInputService` and typed preflight handling.

- [ ] **Step 4: Implement host input preparation**

`build_bootstrap` collects the current bundle and returns `VisionAgentInput` with operation `bootstrap` or `revision` selected from graph facts. `build_clarification` loads the exact lineage snapshot, derives addressed question IDs from the latest turn, preserves stored evidence as model input, recollects current evidence for comparison, and includes only expected/observed fingerprints in a host-owned preflight envelope.

The caller supplies only ordinary response text. It never supplies mode, question IDs, snapshot IDs, fingerprints, or repository-derived values.

- [ ] **Step 5: Preserve typed preflight failures durably**

Add `failure_code: str | None` to `NodeAttemptFact` from `WorkflowNodeAttemptOutcome`. Define `VisionAgenticPreflightError(code: WorkflowErrorCode, message: str)` in `adapters/adk/errors.py`. The Vision recipe raises it before `context.run_node` when expected/observed evidence differ. Update `AdkWorkflowRunner` to catch it before the generic execution block, record its exact `failure_code`, and return that typed `WorkflowError`.

The graph treats the latest `VISION_EVIDENCE_STALE` failure for the current clarification instance as recovery evidence and advertises `vision.bootstrap`. This uses the existing append-only attempt/outcome system; do not add mutable session state. An unchanged recollected fingerprint proceeds with the existing snapshot even after repository refresh.

- [ ] **Step 6: Document the durable-preflight implementation note**

Add one paragraph to the design's replay/failure section: evidence-stale recovery is represented by the existing durable node-attempt failure outcome, allowing the pure graph to advertise bootstrap without mutable workflow session state.

- [ ] **Step 7: Run tests and commit**

```bash
uv run --frozen pytest \
  tests/services/test_vision_input.py \
  tests/adapters/test_adk_workflow_runner.py \
  tests/workflow/test_vision_bootstrap_graph.py -q
uv lock --check
git diff --check
```

Expected: all selected tests pass and fake provider call count is zero on preflight errors.

```bash
git add services/vision_input.py services/vision_interview_input.py \
  workflow/contracts.py workflow/facts.py repositories/workflow.py \
  adapters/adk/errors.py adapters/adk/runner.py tests/services/test_vision_input.py \
  tests/services/test_vision_interview_input.py \
  tests/adapters/test_adk_workflow_runner.py \
  docs/superpowers/specs/2026-08-10-context-grounded-vision-bootstrap-design.md
git commit -m "feat: prepare replay-safe vision inputs"
```

---

### Task 6: Bounded ADK Vision Generation And One Repair

**Files:**
- Modify: `adapters/adk/prompts/vision.txt`
- Create: `adapters/adk/prompts/vision_repair.txt`
- Modify: `adapters/adk/agents/vision.py`
- Modify: `adapters/adk/recipes.py`
- Modify: `adapters/adk/model_roles.py`
- Modify: `services/application.py`
- Modify: `tests/adapters/test_vision.py`
- Create: `tests/adapters/test_vision_recipe.py`
- Modify: `tests/adapters/test_adk_workflow_runner.py`

**Interfaces:**
- Consumes: Task 1 contracts/validator, Task 4 positioned requests, and Task 5 preflight envelope.
- Produces: `vision.bootstrap` and `vision.interview` recipes, trusted output adapters, and a maximum of one compact repair call.

- [ ] **Step 1: Write failing recipe tests with fake ADK leaves**

Cover valid bootstrap, valid clarification, incomplete output, complete output, semantic failure followed by successful repair, semantic failure followed by failed repair, schema failure with no business facts, preflight failure with zero leaf calls, and output adapters binding trusted evidence/question IDs from persisted attempt input.

```python
def test_semantic_repair_runs_once() -> None:
    primary = FakeLeaf(outputs=[semantically_invalid_output()])
    repair = FakeLeaf(outputs=[valid_repaired_output()])
    recipe = build_vision_workflow(
        primary_leaf=primary,
        repair_leaf=repair,
        execution_settings=TEST_EXECUTION_SETTINGS,
    )

    result = run_recipe(recipe, bootstrap_recipe_input())

    assert result.payload == valid_repaired_output()
    assert primary.call_count == 1
    assert repair.call_count == 1
```

- [ ] **Step 2: Run tests and confirm failure**

```bash
uv run --frozen pytest \
  tests/adapters/test_vision.py \
  tests/adapters/test_vision_recipe.py \
  tests/adapters/test_adk_workflow_runner.py -q
```

Expected: missing grounded schemas, recipe, and repair behavior.

- [ ] **Step 3: Replace prompt and agent schemas**

The Vision prompt directs the model to propose a durable direction from supplied evidence, distinguish evidence/human/inference, expose assumptions/conflicts, ask only material questions, preserve human corrections, and produce no Product Goal or delivery scope. Repository evidence is unreviewed context, not authority.

Configure the Vision agent with `VisionAgentInput` and `VisionDraftOutput`. Configure a separate repair agent with `VisionRepairInput` and `VisionDraftOutput`; use the same `product_vision` model role and existing runtime token/settings helpers.

- [ ] **Step 4: Build the dedicated bounded workflow**

Do not use generic retry for Vision semantic repair. `build_vision_workflow` performs these exact steps:

1. Validate host preflight before any leaf call.
2. Run the primary leaf once.
3. Parse `VisionDraftOutput` and run `validate_vision_draft`.
4. On semantic failure only, create `VisionRepairInput` with findings, invalid structured output, allowed evidence IDs, and `human_input_available`.
5. Run the repair leaf once.
6. Reparse/revalidate once; propagate failure after that.

Set `RetryConfig(max_attempts=1)` for paid Vision leaf nodes so ADK cannot duplicate the explicit repair policy.

- [ ] **Step 5: Bind trusted input in output adapters**

The `vision.bootstrap` adapter creates `GenerateVisionBootstrap`; the `vision.interview` adapter creates `RecordVisionInterviewTurn`. Both load operation, bundle/snapshot, human response, and question IDs only from `AttemptCompletionContext.normalized_input`. Model output supplies only `VisionDraftOutput` fields.

- [ ] **Step 6: Run tests and commit**

```bash
uv run --frozen pytest \
  tests/adapters/test_vision.py \
  tests/adapters/test_vision_recipe.py \
  tests/adapters/test_adk_workflow_runner.py -q
uv lock --check
git diff --check
```

Expected: all selected tests pass; repair call count never exceeds one.

```bash
git add adapters/adk/prompts/vision.txt adapters/adk/prompts/vision_repair.txt \
  adapters/adk/agents/vision.py adapters/adk/recipes.py \
  adapters/adk/model_roles.py services/application.py \
  tests/adapters/test_vision.py tests/adapters/test_vision_recipe.py \
  tests/adapters/test_adk_workflow_runner.py
git commit -m "feat: run bounded grounded vision generation"
```

---

### Task 7: Semantic Application, API, And CLI Surfaces

**Files:**
- Modify: `services/application.py`
- Modify: `api.py`
- Modify: `cli/main.py`
- Create: `tests/adapters/test_vision_bootstrap_api.py`
- Create: `tests/adapters/test_vision_bootstrap_cli.py`
- Modify: `tests/adapters/test_api_workflow_domain.py`
- Modify: `tests/adapters/test_cli_workflow_domain.py`
- Modify: `tests/adapters/test_command_renderer.py`

**Interfaces:**
- Consumes: `VisionInputService`, graph decisions, ADK recipes, and current read projection.
- Produces: `VisionBootstrapRequest`, `AgileForgeApplication.bootstrap_vision`, `POST /api/projects/{project_id}/vision/bootstrap`, and `agileforge vision bootstrap`.

- [ ] **Step 1: Write failing application/API tests**

Assert bootstrap body contains transport metadata only, calls the model only on POST, replays the same key, maps typed preflight failures without a paid call, and keeps `GET /vision/status`, project show, workflow position, and workflow next pure.

```python
def test_vision_status_get_does_not_invoke_runner(client, fake_runner) -> None:
    response = client.get(f"/api/projects/{PROJECT_ID}/vision/status")

    assert response.status_code == 200
    assert fake_runner.call_count == 0
```

- [ ] **Step 2: Write failing CLI tests**

Assert exact semantic command rendering:

```text
agileforge vision bootstrap --project-id 1 --idempotency-key KEY --actor ACTOR
```

Assert no generated command exposes graph/fact/decision fingerprints, evidence/snapshot IDs, mode, or repository-derived fields. Retain `vision respond --text`, `vision review`, and `vision status`.

- [ ] **Step 3: Run tests and confirm failure**

```bash
uv run --frozen pytest \
  tests/adapters/test_vision_bootstrap_api.py \
  tests/adapters/test_vision_bootstrap_cli.py \
  tests/adapters/test_api_workflow_domain.py \
  tests/adapters/test_cli_workflow_domain.py \
  tests/adapters/test_command_renderer.py -q
```

Expected: bootstrap method, route, and command are absent.

- [ ] **Step 4: Implement replay-safe orchestration**

Add:

```python
class VisionBootstrapRequest(FrozenModel):
    project_id: int
    idempotency_key: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    correlation_id: str | None = None
```

`bootstrap_vision` replays first, reads one position, selects exactly one `vision.bootstrap` decision, builds host input, and calls `run_agentic_action`. Refactor shared Vision dispatch helpers without changing Product Goal or other agentic nodes. Rename the injected option from `vision_interview_input` to `vision_input`; retain no old constructor key.

- [ ] **Step 5: Implement HTTP and CLI entry points**

Add the POST route and Typer command using existing idempotency/actor conventions. Update workflow command-template mapping so `workflow next` emits bootstrap or respond from the graph decision. CLI transport must not derive internal identity itself.

- [ ] **Step 6: Run tests and commit**

```bash
uv run --frozen pytest \
  tests/adapters/test_vision_bootstrap_api.py \
  tests/adapters/test_vision_bootstrap_cli.py \
  tests/adapters/test_api_workflow_domain.py \
  tests/adapters/test_cli_workflow_domain.py \
  tests/adapters/test_command_renderer.py -q
uv lock --check
git diff --check
```

Expected: all selected tests pass.

```bash
git add services/application.py api.py cli/main.py \
  tests/adapters/test_vision_bootstrap_api.py \
  tests/adapters/test_vision_bootstrap_cli.py \
  tests/adapters/test_api_workflow_domain.py \
  tests/adapters/test_cli_workflow_domain.py \
  tests/adapters/test_command_renderer.py
git commit -m "feat: expose vision bootstrap commands"
```

---

### Task 8: Human Vision Bootstrap And Provenance UI

**Files:**
- Modify: `services/read_projections.py`
- Modify: `frontend/project.js`
- Modify: `adapters/adk/recipes.py`
- Modify: `tests/adapters/test_api_workflow_domain.py`
- Modify: `tests/adapters/test_command_renderer.py`
- Modify: `tests/adapters/test_vision.py`
- Modify: `tests/services/contracts/test_vision.py`
- Modify: `tests/workflow/test_vision_interview_graph.py`
- Modify: `tests/workflow/test_vision_interview_transitions.py`
- Modify: `frontend/styles.css` only if existing classes cannot express the approved states
- Modify: `tests/test_vision_projection.py`
- Modify: `tests/test_vision_interview_ui.mjs`

**Interfaces:**
- Consumes: snapshot/turn/artifact facts and semantic HTTP routes.
- Produces: safe Vision status projection and human controls for bootstrap, clarification, provenance inspection, and review.

- [ ] **Step 1: Write failing projection tests**

Assert initial status says bootstrap is available; incomplete status includes draft components, statement, basis, assumptions, conflicts, and structured questions; complete status includes the same review material. Projection omits absolute paths, raw evidence JSON, editable snapshot/evidence IDs, graph fingerprints, and attempt IDs.

- [ ] **Step 2: Write failing UI behavior tests**

Test initial generation, disabled/loading double-submit protection, ordinary-language response after an incomplete draft, visible provenance labels, complete review controls, and refresh purity. Remove tests for fallback questions and the initial required textarea.

```javascript
test("initial vision state offers generation without a response textarea", () => {
  const html = visionPanelMarkup(initialVisionStatus());

  assert.match(html, /Generate Vision draft/);
  assert.doesNotMatch(html, /Your response/);
  assert.doesNotMatch(html, /Who should benefit/);
});
```

- [ ] **Step 3: Run tests and confirm failure**

```bash
uv run --frozen pytest tests/test_vision_projection.py -q
node --test tests/test_vision_interview_ui.mjs
```

Expected: initial UI still renders fallback questions and lacks bootstrap.

- [ ] **Step 4: Extend the safe read projection**

Return display-safe provenance: component name/value, basis source kinds, assumption/conflict text, and question ID/text needed for rendering. Replace internal IDs with display ordering where browser actions do not need an ID. The browser never submits snapshot/evidence/question identity; the host derives it.

- [ ] **Step 5: Implement the human flow**

Render:

1. Initial context summary and `Generate Vision draft` button.
2. One stable generation loading state with button disabled.
3. Incomplete draft plus basis/assumptions/conflicts/questions and one response textarea.
4. Complete draft plus the same provenance and Accept/Feedback/Reject controls.

Use existing icon library/classes and established dashboard layout. Do not add feature-explainer copy, nested cards, raw JSON editors, or internal workflow fields.

- [ ] **Step 6: Run tests and commit**

```bash
uv run --frozen pytest tests/test_vision_projection.py -q
node --test tests/test_vision_interview_ui.mjs
uv lock --check
git diff --check
```

Expected: all selected tests pass.

```bash
git add services/read_projections.py frontend/project.js frontend/styles.css \
  tests/test_vision_projection.py tests/test_vision_interview_ui.mjs
git commit -m "feat: add human vision bootstrap flow"
```

---

### Task 9: Hard-Break Cleanup And Retained Lifecycle Regression

**Files:**
- Modify: `services/contracts/vision.py`
- Modify: `services/application.py`
- Modify: `workflow/definitions/vision.py`
- Modify: `adapters/adk/agents/vision.py`
- Modify: `adapters/adk/prompts/vision.txt`
- Modify: `frontend/project.js`
- Modify: `tests/workflow/test_vision_backlog_graph.py`
- Modify: `tests/workflow/test_vision_backlog_transitions.py`
- Modify: `tests/services/test_durable_product_definition_projections.py`
- Modify: `docs/agent-cli-manual.md`

**Interfaces:**
- Consumes: completed Tasks 1-8.
- Produces: one Vision path with no human-first bootstrap remnants and retained accepted-Vision-to-Product-Goal behavior.

- [ ] **Step 1: Run hard-break absence scans**

```bash
rg -n "VisionInterviewInput|VisionInterviewOutput|vision_interview_input|VISION_INTERVIEW_REQUIRED" \
  --glob '*.py' --glob '*.js' --glob '*.md' .
rg -n "Do not infer Vision from repository contents|Who should benefit from this product first" \
  adapters frontend tests
```

Expected: only deliberate historical design references remain; runtime references fail this task.

- [ ] **Step 2: Add retained lifecycle tests**

Prove only a human-accepted Vision unlocks Product Goal, feedback/rejection returns to clarification, accepted Vision revision remains blocked by an active Product Goal, and repository attachment is optional for bootstrap.

- [ ] **Step 3: Delete obsolete runtime and tests**

Remove the human-first input service, old contracts, prompt prohibition, initial textarea/fallback questions, exposed `mode`/`user_text` graph requirements, and tests asserting repository/spec evidence is absent. Leave no aliases or deprecation paths.

- [ ] **Step 4: Update the branch-testing guide**

Document semantic development commands only: initialize a fresh profile/database from the current branch with `./agileforge-dev init --profile vision-bootstrap-manual --json`, start with `./agileforge-dev ui --profile vision-bootstrap-manual --port auto`, inspect provenance with `./agileforge-dev info --profile vision-bootstrap-manual --json`, and invoke semantic CLI commands through `./agileforge-dev cli --profile vision-bootstrap-manual -- vision bootstrap|respond|status|review`. State that the operator performs manual acceptance and automated tests use only temporary fixtures.

- [ ] **Step 5: Run focused retained regression**

```bash
uv run --frozen pytest \
  tests/workflow/test_vision_backlog_graph.py \
  tests/workflow/test_vision_backlog_transitions.py \
  tests/services/test_durable_product_definition_projections.py -q
node --test tests/test_vision_interview_ui.mjs
uv lock --check
git diff --check
```

Expected: all selected tests pass and runtime absence scans are clean.

- [ ] **Step 6: Commit**

```bash
git add services/contracts/vision.py services/application.py \
  workflow/definitions/vision.py adapters/adk/agents/vision.py \
  adapters/adk/prompts/vision.txt adapters/adk/recipes.py frontend/project.js \
  tests/adapters/test_api_workflow_domain.py \
  tests/adapters/test_command_renderer.py tests/adapters/test_vision.py \
  tests/services/contracts/test_vision.py \
  tests/workflow/test_vision_interview_graph.py \
  tests/workflow/test_vision_interview_transitions.py \
  tests/workflow/test_vision_backlog_graph.py \
  tests/workflow/test_vision_backlog_transitions.py \
  tests/services/test_durable_product_definition_projections.py \
  docs/agent-cli-manual.md
git commit -m "refactor: remove human-first vision bootstrap"
```

---

### Task 10: Full Verification And Manual-Test Handoff

**Files:**
- Modify only files required to fix failures caused by Tasks 1-9; no unrelated cleanup.

**Interfaces:**
- Consumes: complete implementation.
- Produces: verified branch evidence and operator instructions. It does not produce an automated acceptance verdict.

- [ ] **Step 1: Run the full uv-only gate**

```bash
uv lock --check
uv run --frozen pyrepo-check --all
git diff --check
```

Expected: Ruff, annotation checks, ty, Bandit, and all tests pass with no typing suppressions.

- [ ] **Step 2: Run clean-source and absence checks**

```bash
git status --short
rg -n "VisionInterviewInput|VisionInterviewOutput|vision_interview_input" \
  --glob '*.py' --glob '*.js' .
rg -n "pip install|poetry" docs README.md agileforge-dev
```

Expected: no runtime legacy names exist and development instructions remain uv-only. Historical design/plan text is allowed.

- [ ] **Step 3: Perform fresh-context review gates**

Dispatch one specification-compliance reviewer and one code-quality reviewer against the plan base through current `HEAD`. Resolve only concrete findings, rerun affected tests after each fix, then rerun the full gate.

- [ ] **Step 4: Record verification without running acceptance**

Capture branch name, final commit SHA, full gate summary, and exact fresh-profile launcher commands. Do not initialize, mutate, or judge the operator's Manual Test 1 profile or fixture project.

- [ ] **Step 5: Commit verification corrections only when needed**

When review/full-gate corrections changed tracked files, list them with `git status --short`, stage only those exact task-owned paths using the corresponding Task 1-9 `git add` command, and commit with `git commit -m "fix: close grounded vision review findings"`. When no tracked correction is needed, do not create an empty commit.
