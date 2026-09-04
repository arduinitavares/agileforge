# Guided accepted Backlog correction implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an exact, guided correction flow for an accepted Backlog during Roadmap-stage review and ensure a replacement Backlog starts a clean downstream planning lineage.

**Architecture:** Reuse the existing `backlog.generate` agentic node and immutable Backlog review lifecycle. Add a dedicated application, CLI, and API request that binds guidance to one accepted Backlog and one correction decision, including correction-specific recovery. Filter attempt overlays and recovery references by current business facts. Permit correction only before Story or Sprint planning begins, and pause all downstream planning until the correction chain is accepted.

**Tech Stack:** Python >=3.13.15,<3.14; uv; Pydantic; SQLModel; SQLite; Google ADK 2.2.0; pytest with pytest-socket; argparse; FastAPI.

**Spec:** [`docs/superpowers/specs/2026-09-04-guided-backlog-correction-design.md`](../specs/2026-09-04-guided-backlog-correction-design.md)

## Global constraints

- Begin from commit `56caf1cd6c36fcc43f174329c4dbeefac268da44` on `fix/issue-245-specification-validation` and preserve unrelated work if the checkout has advanced.
- Use `uv run --no-sync --offline` for Python tests and checks. Keep network sockets disabled during tests.
- Use synthetic fixtures and temporary databases only. Do not read from or mutate `pid-verification`, `pid-verification-terra`, the original P&ID databases, or the registered P&ID source.
- Do not call OpenRouter or any other provider. All model execution tests use deterministic test doubles.
- Do not change `config/models.yaml`, provider privacy policy, provider retry behavior, model prompts, prompt hashes, or completion-token settings.
- Do not add a workflow node, database table, migration, dependency, or automatic acceptance path.
- Keep the browser action list and `frontend/project.js` unchanged in this implementation. The controlled public paths are CLI and direct API.
- Human guidance is trimmed, nonblank, and limited to 32,768 characters. This is a request-size bound, not a model completion-token limit.
- The provider never chooses Backlog item IDs or supersession metadata. Existing host canonicalization continues to mint IDs after sorting unique priorities.
- The accepted source Backlog remains authoritative until a human accepts the exact successor. Never edit or delete historical artifacts.
- This first version is available only before Story or Sprint planning has begun. A terminal Roadmap may exist; a pending Roadmap review, active downstream provider attempt, or any Story/Sprint planning state closes the correction boundary.
- Reusing one idempotency key with changed actor, correlation ID, decision fingerprint, accepted artifact identity, or guidance must conflict before provider execution.
- Local commits in the task steps require separate operator authorization. An implementation agent must leave changes uncommitted when its delegation prompt says to stop for review.

---

## Verified baseline and failure

- `workflow/definitions/backlog.py::_backlog_generate_rule` already emits `BACKLOG_CORRECTION_AVAILABLE` as `OPTIONAL_REENTRY` for an accepted Backlog.
- `services/application.py::_backlog_input` loads exact canonical prior state, but it supplies `user_input` only for `feedback` or `rejected` review decisions.
- `services/application.py::_run_delivery_action` blocks optional Story correction through generic generation but does not block optional Backlog correction.
- `services/contracts/backlog.py::BacklogAgentOutput` is ID-free. `canonicalize_backlog_items()` sorts unique priorities and mints durable `PBI-NNNNNN` IDs.
- `services/contracts/roadmap.py::validate_roadmap_backlog_coverage` rejects repeated Backlog IDs. Roadmap Attempt 18 correctly asked for the overloaded Backlog items to be split.
- `workflow/graph.py::_overlay_agentic_attempt` filters attempts by node and instance key, but only checks `business_fact_fingerprint` for successful attempts. `_decision_fact_references()` independently appends stale failed or expired attempts. Either path can contaminate a new Backlog lineage.
- After an optional correction fails, the generic agentic overlay currently changes its reason to `BACKLOG_GENERATION_FAILED`; that makes the dedicated correction unavailable and lets generic generation bypass the retained guidance. Backlog optional re-entry needs correction-specific active, failure, and recovery reasons.
- Roadmap artifacts are already selected by exact Backlog identity. Story, dependency, and Sprint selectors are not all isolated for a same-Specification Backlog replacement, so this version closes correction once any Story or Sprint planning state exists instead of attempting a broad lineage migration.

## File map

| File | Responsibility |
| --- | --- |
| Modify `services/contracts/backlog.py` | Shared 32,768-character guidance limit. |
| Modify `services/application.py` | Correction request, exact durable target resolution, input construction, generic-generation refusal, and application operation. |
| Modify `services/node_attempt_replay.py` | Closed replay semantics for `backlog_correction`. |
| Modify `adapters/adk/recipes.py` | Closed host-only correction envelope beside the existing provider input. |
| Modify `workflow/handlers/product_definition.py` | Recheck the correction boundary in-transaction and reject byte-equivalent output before insert. |
| Modify `workflow/graph.py` | Scope every agentic attempt overlay and recovery reference to current business facts; support optional-reentry reason overrides. |
| Modify `workflow/definitions/backlog.py` | Correction-specific attempt reasons, reusable stage-boundary check, and correction-in-progress predicate. |
| Modify `workflow/definitions/planning.py` | Block all downstream planning while a correction chain is unresolved. |
| Modify `cli/main.py` | `backlog correct` parser and application forwarding. |
| Modify `cli/workflow_commands.py` | Render the exact artifact-bound correction command from `workflow next`. |
| Modify `api.py` | Closed correction request and direct API route. Keep browser action discovery unchanged. |
| Modify `tests/workflow/test_graph_kernel.py` | Stale attempt overlay regressions. |
| Modify `tests/workflow/test_vision_backlog_graph.py` | Correction-specific recovery and stage-boundary graph rules. |
| Modify `tests/workflow/test_vision_backlog_transitions.py` | In-transaction correction boundary and acceptance race checks. |
| Modify `tests/workflow/test_node_attempts.py` | Exact correction replay and changed-input conflicts. |
| Modify `tests/workflow/test_planning_graph.py` | Planning pause rules for every unresolved correction state. |
| Modify `tests/workflow/test_planning_transitions.py` | Pending correction pause and clean Roadmap lineage after acceptance. |
| Modify `tests/adapters/test_adk_workflow_runner.py` | Real runner with deterministic Backlog correction output and duplicate-output rejection. |
| Modify `tests/adapters/test_api_workflow_domain.py` | Application input resolution and HTTP transport. |
| Modify `tests/adapters/test_cli_workflow_domain.py` | CLI parser and forwarding. |
| Modify `tests/adapters/test_command_renderer.py` | Exact `workflow next` command rendering and parser compatibility. |
| Create `docs/testing/GUIDED-BACKLOG-CORRECTION.md` | Operator contract and controlled verification procedure. |

---

### Task 1: Scope attempts to current facts and retain correction recovery

**Files:**
- Modify: `workflow/graph.py:75-126`
- Modify: `workflow/definitions/backlog.py:257-278`
- Test: `tests/workflow/test_graph_kernel.py`
- Test: `tests/workflow/test_vision_backlog_graph.py`

**Interfaces:**
- Consumes: `business_fact_fingerprint(snapshot: WorkflowFactSnapshot) -> str`.
- Produces: `_overlay_agentic_attempt()` and `_decision_fact_references()` behavior that ignores attempts from any prior business-fact snapshot for success, failure, obsolete, expired, and active outcomes.
- Extends: `AgenticExecutionSpec` with optional-reentry active, failure, and recovery reason overrides. Backlog sets them; nodes without overrides retain their current behavior.

- [ ] **Step 1: Add failing stale-attempt tests**

Add a parametrized kernel regression that uses one available agentic node and one
attempt whose `business_fact_fingerprint` differs from the current snapshot:

```python
@pytest.mark.parametrize("outcome", [None, "failure", "obsolete", "success"])
def test_agentic_overlay_ignores_attempt_from_prior_business_facts(
    outcome: str | None,
) -> None:
    snapshot = _snapshot()
    stale_attempt = NodeAttemptFact(
        attempt_id=19,
        node_id="test.execute",
        instance_key=None,
        graph_version=GRAPH_VERSION,
        input_fingerprint="sha256:input",
        fact_fingerprint="sha256:facts",
        business_fact_fingerprint=canonical_hash({"prior": True}),
        decision_fingerprint="sha256:decision",
        attempt_fingerprint="sha256:attempt",
        model_id="fixed-model",
        lease_expires_at=EVALUATED_AT + timedelta(minutes=5),
        outcome=outcome,
    )
    graph = _agentic_graph(reason_code="READY")

    decision = graph.evaluate(
        snapshot.model_copy(update={"node_attempts": (stale_attempt,)}),
        EVALUATED_AT,
    ).decisions[0]

    assert decision.category is NodeCategory.AVAILABLE
    assert decision.reason_code == "READY"
    assert decision.recommendation_kind is RecommendationKind.REQUIRED
```

Also add one same-business-facts test for an active attempt and one for a failed
attempt. These tests must retain `WAITING` and `RECOVERY` behavior respectively.
Add an intrinsic recovery rule whose own fact references are valid but whose
snapshot contains a newer stale failed attempt; assert
`_decision_fact_references()` does not append that stale `node_attempt`.

In `tests/workflow/test_vision_backlog_graph.py`, cover an accepted Backlog with
a same-current-facts correction attempt. Assert active, failed, and expired or
obsolete outcomes use `BACKLOG_CORRECTION_ACTIVE`,
`BACKLOG_CORRECTION_FAILED`, and `BACKLOG_CORRECTION_RECOVERY_REQUIRED`.
The two recovery decisions must contain exactly one current `node_attempt`
reference. Add a control proving Story optional-reentry reasons stay unchanged
when its `AgenticExecutionSpec` does not define overrides.

- [ ] **Step 2: Run the focused tests and capture the expected failure**

Run:

```powershell
uv run --no-sync --offline pytest tests/workflow/test_graph_kernel.py tests/workflow/test_vision_backlog_graph.py -k "agentic_overlay or backlog_correction" -q
```

Expected before implementation: stale active, failed, and obsolete cases do not
remain `AVAILABLE` with reason `READY`; the intrinsic recovery decision gains a
stale attempt reference; and Backlog correction falls through to generic reasons.

- [ ] **Step 3: Filter before selecting the latest attempt**

Change `_overlay_agentic_attempt()` so it computes the current business-fact
fingerprint once and includes it in the attempt selection:

```python
current_business_facts = business_fact_fingerprint(snapshot)
attempts = tuple(
    attempt
    for attempt in snapshot.node_attempts
    if attempt.node_id == node.node_id
    and attempt.instance_key == evaluation.instance_key
    and attempt.business_fact_fingerprint == current_business_facts
)
```

Apply the same fingerprint predicate inside `_decision_fact_references()` before
selecting a failed, obsolete, or expired attempt. With this filter in place, the
successful-attempt branch can return the existing fact conflict directly because
every selected attempt already matches current business facts.

- [ ] **Step 4: Add optional-reentry reason overrides**

Extend `AgenticExecutionSpec` with optional fields:

```python
optional_reentry_active_reason: str | None = None
optional_reentry_failure_reason: str | None = None
optional_reentry_recovery_reason: str | None = None
```

When the unoverlaid rule evaluation has recommendation kind
`OPTIONAL_REENTRY`, `_overlay_agentic_attempt()` uses these values when present
and otherwise falls back to the existing generic reasons. Set the Backlog node's
overrides to:

```python
BACKLOG_CORRECTION_ACTIVE
BACKLOG_CORRECTION_FAILED
BACKLOG_CORRECTION_RECOVERY_REQUIRED
```

Do not change generic generation or Story correction reason codes.

- [ ] **Step 5: Run graph tests**

Run:

```powershell
uv run --no-sync --offline pytest tests/workflow/test_graph_kernel.py tests/workflow/test_graph_properties.py tests/workflow/test_vision_backlog_graph.py -q
```

Expected: all tests pass, including same-lineage active and failure overlays.

- [ ] **Step 6: Create the optional review commit**

After explicit commit authorization:

```powershell
git add workflow/graph.py workflow/definitions/backlog.py tests/workflow/test_graph_kernel.py tests/workflow/test_vision_backlog_graph.py
git commit -m "fix(workflow): scope attempts to current facts"
```

---

### Task 2: Add the exact application correction contract

**Files:**
- Modify: `services/contracts/backlog.py:22-23,170-178`
- Modify: `services/application.py:348-369,844-974,1281-1352,2378-2556,3566-3630,4487-4547`
- Test: `tests/adapters/test_api_workflow_domain.py`

**Interfaces:**
- Produces: `MAX_BACKLOG_CORRECTION_GUIDANCE_CHARS: Final[int] = 32_768`.
- Produces: `BacklogCorrectionRequest(FrozenModel)` with the exact fields from the design.
- Produces: `DeliveryActionInputService.build_backlog_correction(*, project_id: int, decision: NodeDecision, request: BacklogCorrectionRequest) -> JsonObject | WorkflowError | None`.
- Produces: `AgileForgeApplication.correct_backlog(request: BacklogCorrectionRequest) -> TransitionResult`.
- Consumes later: normalized input key `backlog_correction` with accepted artifact ID, fingerprint, and guidance.

- [ ] **Step 1: Add request validation tests**

Add tests that validate a complete request and reject blank, whitespace-only, and
32,769-character guidance before any input-service call:

```python
def test_backlog_correction_request_rejects_invalid_guidance() -> None:
    base = {
        "project_id": 41,
        "expected_decision_fingerprint": "sha256:" + "a" * 64,
        "accepted_backlog_artifact_id": 3,
        "accepted_backlog_artifact_fingerprint": "sha256:" + "b" * 64,
        "idempotency_key": "backlog-correct-41-01",
        "actor": "operator",
    }
    for guidance in ("", "   ", "x" * 32_769):
        with pytest.raises(ValidationError):
            BacklogCorrectionRequest.model_validate({**base, "guidance": guidance})
```

Add strict tests for a boolean or nonpositive artifact ID, malformed
fingerprints, blank or whitespace-only actor and idempotency key, and an extra
field.

- [ ] **Step 2: Add failing exact-target and input-composition tests**

Use the existing accepted-Backlog fixtures in
`tests/adapters/test_api_workflow_domain.py`. Read the real optional correction
decision from `planning_domain(engine).position(project_id)` and assert:

```python
prepared = DeliveryActionInputService(engine=engine).build_backlog_correction(
    project_id=project_id,
    decision=decision,
    request=request,
)
assert isinstance(prepared, dict)
builder_input = BacklogBuilderInput.model_validate(prepared["builder_input"])
assert builder_input.prior_backlog_state == accepted.canonical_content_json
assert builder_input.user_input == "Split consent audit from gold publication."
assert prepared["supersedes_backlog_artifact_id"] == accepted.backlog_artifact_id
assert prepared["backlog_correction"] == {
    "accepted_backlog_artifact_id": accepted.backlog_artifact_id,
    "accepted_backlog_artifact_fingerprint": accepted.content_fingerprint,
    "guidance": "Split consent audit from gold publication.",
}
```

Add negative cases for wrong project, wrong artifact ID, wrong artifact
fingerprint, nonaccepted review, a child artifact already superseding the target,
changed Specification lineage, and changed Product Goal lineage. Assert each
case returns no prepared input or a `WORKFLOW_FACT_CONFLICT` and creates no
`WorkflowNodeAttempt`.

Add decision-shape cases. Initial correction accepts only available,
`OPTIONAL_REENTRY`, `BACKLOG_CORRECTION_AVAILABLE`, with no `node_attempt`.
Correction retry accepts only available, `RECOVERY`,
`BACKLOG_CORRECTION_FAILED` or
`BACKLOG_CORRECTION_RECOVERY_REQUIRED`, with exactly one positive-integer
`node_attempt` reference. Every mixed, duplicate, missing, or extra reference
shape fails before an attempt.

- [ ] **Step 3: Run the application tests and capture failure**

Run:

```powershell
uv run --no-sync --offline pytest tests/adapters/test_api_workflow_domain.py -k "backlog_correction_request or build_backlog_correction or generic_backlog_generation_refuses_correction" -q
```

Expected before implementation: missing request class, method, and generic
refusal assertions fail.

- [ ] **Step 4: Add the shared guidance bound and request model**

In `services/contracts/backlog.py` add and export:

```python
MAX_BACKLOG_CORRECTION_GUIDANCE_CHARS: Final[int] = 32_768
```

In `services/application.py` add:

```python
class BacklogCorrectionRequest(FrozenModel):
    project_id: int
    expected_decision_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    accepted_backlog_artifact_id: Annotated[int, Field(strict=True, gt=0)]
    accepted_backlog_artifact_fingerprint: str = Field(
        pattern=r"^sha256:[0-9a-f]{64}$"
    )
    guidance: SemanticText = Field(max_length=MAX_BACKLOG_CORRECTION_GUIDANCE_CHARS)
    idempotency_key: SemanticText
    actor: SemanticText
    correlation_id: str | None = None
```

Add `build_backlog_correction()` to `_DeliveryActionInputPort` and
`DeliveryActionInputService`, add the request to `services.application.__all__`,
and import `Annotated` and `Final` where the new annotations require them.

- [ ] **Step 5: Resolve the accepted physical leaf inside one database session**

The builder must call the existing `_delivery_lineage()` and `_backlog_input()`
inside its session, then perform these checks before replacing `user_input`:

```python
target_reference = _single_fact_reference(decision, "backlog")
target = session.get(BacklogArtifact, request.accepted_backlog_artifact_id)
review = session.exec(
    select(BacklogArtifactDecision).where(
        col(BacklogArtifactDecision.project_id) == project_id,
        col(BacklogArtifactDecision.backlog_artifact_id)
        == request.accepted_backlog_artifact_id,
    )
).one_or_none()
successor = session.exec(
    select(BacklogArtifact).where(
        col(BacklogArtifact.project_id) == project_id,
        col(BacklogArtifact.supersedes_backlog_artifact_id)
        == request.accepted_backlog_artifact_id,
    )
).first()
```

Require exact project, artifact and reference fingerprints, canonical bytes,
current Specification and Product Goal lineage, `review.decision == "accepted"`,
`review.artifact_fingerprint == target.content_fingerprint`, and
`successor is None`. Convert the existing builder payload through
`BacklogBuilderInput.model_validate()`, set `user_input` with `model_copy()`, and
append only the closed `backlog_correction` object.

- [ ] **Step 6: Implement replay-first `correct_backlog()`**

The method first calls replay with:

```python
semantic_input = {
    "backlog_correction": {
        "accepted_backlog_artifact_id": request.accepted_backlog_artifact_id,
        "accepted_backlog_artifact_fingerprint": (
            request.accepted_backlog_artifact_fingerprint
        ),
        "guidance": request.guidance,
    }
}
```

For a new request, read the current position and require the exact initial or
recovery tuple defined by `_backlog_correction_decision_is_valid()`, the expected
decision fingerprint, and the supplied artifact identity. Pass the prepared payload to
`run_agentic_action()` with
`model_id=get_model_id(AGENTIC_MODEL_ROLES["backlog.generate"])`, preserving the
configured `backlog_primer` role.

Add this guard to `_run_delivery_action()` beside the Story correction guard:

```python
if (
    node_id == "backlog.generate"
    and decision.reason_code
    in {
        "BACKLOG_CORRECTION_AVAILABLE",
        "BACKLOG_CORRECTION_FAILED",
        "BACKLOG_CORRECTION_RECOVERY_REQUIRED",
    }
):
    return _transition_not_available(position, node_id)
```

- [ ] **Step 7: Run the focused application tests**

Run:

```powershell
uv run --no-sync --offline pytest tests/adapters/test_api_workflow_domain.py -k "backlog_correction or delivery_input_service" -q
```

Expected: all selected tests pass and every negative case records zero attempts.

- [ ] **Step 8: Create the optional review commit**

After explicit commit authorization:

```powershell
git add services/contracts/backlog.py services/application.py tests/adapters/test_api_workflow_domain.py
git commit -m "feat(backlog): add guided accepted correction"
```

---

### Task 3: Bind replay and ADK persistence to the correction

**Files:**
- Modify: `services/node_attempt_replay.py:26-29,69-131,180-270`
- Modify: `adapters/adk/recipes.py:136-144,488-513`
- Modify: `workflow/handlers/product_definition.py:193-262`
- Test: `tests/workflow/test_node_attempts.py`
- Test: `tests/adapters/test_adk_workflow_runner.py`

**Interfaces:**
- Produces: `_BacklogCorrectionRecipeInput` with accepted artifact ID, fingerprint, and guidance.
- Extends: `_BacklogRecipePayload.backlog_correction: _BacklogCorrectionRecipeInput | None = None`.
- Produces: stable unchanged-output conflict message `Backlog correction did not change the accepted artifact.`

- [ ] **Step 1: Add replay identity regressions**

Persist one completed `StartNodeAttempt` whose normalized input contains:

```python
"backlog_correction": {
    "accepted_backlog_artifact_id": 3,
    "accepted_backlog_artifact_fingerprint": "sha256:" + "b" * 64,
    "guidance": "Split the overloaded items.",
}
```

Assert that an exact `NodeAttemptReplayQuery` replays. Parametrize changed
guidance, artifact ID, artifact fingerprint, decision fingerprint, actor, and
correlation ID. Each changed case must return `WORKFLOW_FACT_CONFLICT` with no
new attempt. Add malformed semantic shapes with extra or missing nested keys and
assert the same conflict. Add both cross-operation cases: a generic generation
query reusing a correction key and a correction query reusing a generic
generation key. These correction-to-generic and generic-to-correction collisions
must both conflict rather than replay.

- [ ] **Step 2: Run replay tests and capture failure**

Run:

```powershell
uv run --no-sync --offline pytest tests/workflow/test_node_attempts.py -k "backlog_correction" -q
```

Expected before implementation: the new closed-shape assertions fail.

- [ ] **Step 3: Add a closed replay-shape check**

Before reconstructing the expected start request, reject malformed explicit
Backlog correction semantics:

```python
_BACKLOG_CORRECTION_FIELDS = frozenset(
    {
        "accepted_backlog_artifact_id",
        "accepted_backlog_artifact_fingerprint",
        "guidance",
    }
)

def _backlog_correction_replay_conflicts(
    stored: StartNodeAttempt,
    query: NodeAttemptReplayQuery,
) -> bool:
    requested_semantics = query.semantic_input
    requested = (
        None
        if requested_semantics is None
        else requested_semantics.get("backlog_correction")
    )
    persisted = stored.normalized_input.get("backlog_correction")
    if requested is None and persisted is None:
        return False
    return (
        query.node_id != "backlog.generate"
        or stored.target_node_id != "backlog.generate"
        or requested_semantics is None
        or set(requested_semantics) != {"backlog_correction"}
        or not isinstance(requested, dict)
        or set(requested) != _BACKLOG_CORRECTION_FIELDS
        or not isinstance(persisted, dict)
        or set(persisted) != _BACKLOG_CORRECTION_FIELDS
        or persisted != requested
    )
```

Call it beside `_sprint_replay_conflicts()`. Keep the existing canonical request
fingerprint comparison as the final replay authority.

- [ ] **Step 4: Add real-runner correction tests with a deterministic leaf**

Build an accepted Backlog, obtain its optional correction decision, and run a
`CountingLeafAgent` that returns a changed, complete `BacklogAgentOutput`. Assert:

```python
assert result.ok is True
assert leaf.calls == ["provider"]
assert captured_provider_input["prior_backlog_state"] == source.canonical_content_json
assert captured_provider_input["user_input"] == guidance
assert "backlog_correction" not in captured_provider_input
assert successor.supersedes_backlog_artifact_id == source.backlog_artifact_id
assert successor.content_fingerprint != source.content_fingerprint
assert successor_decision_count == 0
```

Then replay the exact request and assert `leaf.calls` remains `['provider']` and
only one successor exists.

Add a two-response regression where the first correction call fails before an
artifact is written. Assert an exact replay of that key returns the same failure
without another provider call. Re-evaluate the graph and prove it advertises the
dedicated correction command with `BACKLOG_CORRECTION_FAILED` or
`BACKLOG_CORRECTION_RECOVERY_REQUIRED` and one exact attempt reference. Submit a
new correction key against that recovery decision, return changed valid output,
and assert one successor is persisted. Assert generic `backlog generate` cannot
execute either recovery decision.

- [ ] **Step 5: Add the recipe envelope**

Add:

```python
class _BacklogCorrectionRecipeInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    accepted_backlog_artifact_id: Annotated[int, Field(strict=True, gt=0)]
    accepted_backlog_artifact_fingerprint: str = Field(
        pattern=r"^sha256:[0-9a-f]{64}$"
    )
    guidance: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=32_768),
    ]
```

Add the optional field to `_BacklogRecipePayload`. Do not pass it to the leaf.
The existing Backlog workflow already passes only `envelope.builder_input`.

- [ ] **Step 6: Reject unchanged accepted correction before insert**

In `execute_record_backlog_draft()`, after exact reference validation and before
`record_backlog_draft_in_session()`, compare the correction decision's Backlog
reference fingerprint with `request.content_fingerprint`:

```python
parent_reference = next(
    (
        reference
        for reference in decision.fact_references
        if reference.fact_type == "backlog"
    ),
    None,
)
if (
    decision.reason_code
    in {
        "BACKLOG_CORRECTION_AVAILABLE",
        "BACKLOG_CORRECTION_FAILED",
        "BACKLOG_CORRECTION_RECOVERY_REQUIRED",
    }
    and parent_reference is not None
    and parent_reference.fingerprint == request.content_fingerprint
):
    return _conflict("Backlog correction did not change the accepted artifact.")
```

Add a real-runner test whose leaf returns content canonically equal to the parent.
Assert `WORKFLOW_FACT_CONFLICT`, zero new Backlog rows, one failed attempt outcome,
and no raw `IntegrityError`.

- [ ] **Step 7: Run replay and runner tests**

Run:

```powershell
uv run --no-sync --offline pytest tests/workflow/test_node_attempts.py -k "backlog_correction" -q
uv run --no-sync --offline pytest tests/adapters/test_adk_workflow_runner.py -k "backlog_correction" -q
```

Expected: all selected tests pass, replay never adds a provider call, and each
new correction attempt dispatches at most once.

- [ ] **Step 8: Create the optional review commit**

After explicit commit authorization:

```powershell
git add services/node_attempt_replay.py adapters/adk/recipes.py workflow/handlers/product_definition.py tests/workflow/test_node_attempts.py tests/adapters/test_adk_workflow_runner.py
git commit -m "fix(backlog): bind correction replay and output"
```

---

### Task 4: Add CLI, command-renderer, and direct API transport

**Files:**
- Modify: `services/application.py:5271-5309`
- Modify: `cli/main.py:139-220,513-530,777-950,1212-1230`
- Modify: `cli/workflow_commands.py:243-390`
- Modify: `api.py:108-132,207-226,486-573,1370-1410`
- Test: `tests/adapters/test_cli_workflow_domain.py`
- Test: `tests/adapters/test_command_renderer.py`
- Test: `tests/adapters/test_api_workflow_domain.py`

**Interfaces:**
- Produces CLI: `backlog correct` with the seven required semantic and binding flags plus optional correlation ID.
- Produces HTTP: `POST /api/projects/{project_id}/backlog/correct` with `X-AgileForge-Expected-Decision`.
- Preserves: `_workflow_actions()` does not advertise accepted Backlog correction to the browser in this patch.

- [ ] **Step 1: Add failing CLI parser and forwarding tests**

Parse this exact synthetic command and assert every field:

```text
backlog correct --project-id 41 --guidance "Split consent audit from gold publication." --accepted-backlog-artifact-id 3 --accepted-backlog-artifact-fingerprint sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb --expected-decision-fingerprint sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa --idempotency-key backlog-correct-41-01 --actor operator --correlation-id corr-backlog-correct-41-01
```

Use the fake application to assert `_backlog_correct()` forwards one exact
`BacklogCorrectionRequest`. Add parser rejection for missing guidance and a
noninteger artifact ID. Add handler cases proving whitespace-only actor,
idempotency key, or guidance fails request validation before the application is
called.

- [ ] **Step 2: Add failing `workflow next` rendering tests**

Build a well-formed initial correction decision with one Backlog,
Specification, and Product Goal reference. Build correction recovery decisions
for both recovery reason codes with the same references plus one current
`node_attempt`. Assert each rendered command starts with
`agileforge backlog correct`, includes exact decision and Backlog identity, uses
`<guidance>`, and parses through `build_parser()` after placeholder replacement.

Add malformed cases for missing, duplicate, noninteger, and extra Backlog
references, an initial decision with an attempt reference, and a recovery
decision with zero or multiple attempt references. Assert no command is rendered.

- [ ] **Step 3: Add failing API tests**

POST a valid closed body and expected-decision header. Assert the fake
application receives the same `BacklogCorrectionRequest`. Assert HTTP 422 for
blank or oversized guidance, bool or nonpositive artifact ID, malformed
fingerprints, blank or whitespace-only actor and idempotency key, missing
expected-decision header, and extra JSON fields.

Add one action-projection regression confirming `_workflow_actions()` still does
not advertise `BACKLOG_CORRECTION_AVAILABLE` until browser support exists.

- [ ] **Step 4: Run the adapter tests and capture failure**

Run:

```powershell
uv run --no-sync --offline pytest tests/adapters/test_cli_workflow_domain.py tests/adapters/test_command_renderer.py tests/adapters/test_api_workflow_domain.py -k "backlog_correct or backlog_correction" -q
```

Expected before implementation: the parser, renderer, and endpoint tests fail.

- [ ] **Step 5: Implement CLI parsing and forwarding**

Add `correct_backlog()` to the CLI `_Application` protocol. Install the command:

```python
backlog_correct = _semantic_leaf(
    branches[("backlog",)],
    "correct",
    _backlog_correct,
)
backlog_correct.add_argument("--guidance", required=True)
backlog_correct.add_argument("--expected-decision-fingerprint", required=True)
backlog_correct.add_argument("--accepted-backlog-artifact-id", type=int, required=True)
backlog_correct.add_argument("--accepted-backlog-artifact-fingerprint", required=True)
```

The handler constructs `BacklogCorrectionRequest` and calls
`application.correct_backlog()`.

- [ ] **Step 6: Render only exact correction commands**

Add the Backlog correction branch before generic delivery rendering in
`_render_semantic_command()`. Add the exact initial and correction-recovery
decisions to the renderer's candidate allowlist. Extend
`planning_action_decision_is_transportable()` so the initial shape requires no
instance key and one positive-integer Backlog reference, while either recovery
shape additionally requires exactly one positive-integer `node_attempt`
reference. Both shapes require exactly one Specification and Product Goal
reference and reject all extras.

The rendered tuple contains the exact values in this order:

```python
(
    "agileforge", "backlog", "correct",
    "--project-id", str(position.project_id),
    "--guidance", "<guidance>",
    "--accepted-backlog-artifact-id", backlog_reference.fact_id,
    "--accepted-backlog-artifact-fingerprint", backlog_reference.fingerprint,
    "--expected-decision-fingerprint", decision.decision_fingerprint,
    "--idempotency-key", "<idempotency-key>",
    "--actor", "<actor>",
)
```

- [ ] **Step 7: Implement the closed API route**

Add `BacklogCorrectionApiRequest(MutationApiRequest)` with exact guidance and
artifact fields. Type the artifact ID as
`Annotated[int, Field(strict=True, gt=0)]` so JSON `true` is rejected. Add the
endpoint using the same typed Header contract as Story set correction:

Redeclare `idempotency_key` and `actor` on this API request as strict semantic
strings with `StringConstraints(strip_whitespace=True, min_length=1,
max_length=200)`. This gives the direct HTTP boundary the same blank rejection
as the application request instead of relying on `Field(min_length=1)`.

```python
@app.post("/api/projects/{project_id}/backlog/correct")
def correct_project_backlog(
    project_id: int,
    req: BacklogCorrectionApiRequest,
    expected_decision: Annotated[
        str,
        Header(
            alias="X-AgileForge-Expected-Decision",
            min_length=1,
            pattern=r"^sha256:[0-9a-f]{64}$",
        ),
    ],
) -> dict[str, object]:
    return _result_payload(
        _application().correct_backlog(
            BacklogCorrectionRequest(
                project_id=project_id,
                expected_decision_fingerprint=expected_decision,
                accepted_backlog_artifact_id=req.accepted_backlog_artifact_id,
                accepted_backlog_artifact_fingerprint=(
                    req.accepted_backlog_artifact_fingerprint
                ),
                guidance=req.guidance,
                **_metadata(req),
            )
        )
    )
```

Do not add Backlog correction to `_workflow_actions()` in this task.

- [ ] **Step 8: Run transport and renderer tests**

Run:

```powershell
uv run --no-sync --offline pytest tests/adapters/test_cli_workflow_domain.py tests/adapters/test_command_renderer.py tests/adapters/test_api_workflow_domain.py -k "backlog_correct or backlog_correction" -q
```

Expected: all selected tests pass and the browser action projection remains
unchanged.

- [ ] **Step 9: Create the optional review commit**

After explicit commit authorization:

```powershell
git add services/application.py cli/main.py cli/workflow_commands.py api.py tests/adapters/test_cli_workflow_domain.py tests/adapters/test_command_renderer.py tests/adapters/test_api_workflow_domain.py
git commit -m "feat(backlog): expose guarded correction command"
```

---

### Task 5: Enforce the Roadmap-stage boundary and pause planning

**Files:**
- Modify: `workflow/definitions/backlog.py:114-227`
- Modify: `workflow/definitions/planning.py:140-157,558-605,645-816,1470-1686`
- Modify: `workflow/handlers/product_definition.py:193-314`
- Test: `tests/workflow/test_vision_backlog_graph.py`
- Test: `tests/workflow/test_vision_backlog_transitions.py`
- Test: `tests/workflow/test_planning_graph.py`
- Test: `tests/workflow/test_planning_transitions.py`

**Interfaces:**
- Produces: `backlog_correction_boundary_problem(snapshot, evaluated_at) -> Blocker | None`.
- Produces: `backlog_correction_in_progress(snapshot, evaluated_at) -> bool`.
- Produces stable blockers `BACKLOG_CORRECTION_STAGE_CLOSED`, `BACKLOG_CORRECTION_DOWNSTREAM_ACTIVE`, and `BACKLOG_CORRECTION_IN_PROGRESS`.
- Preserves: terminal Roadmap history and exact Roadmap-to-Backlog selection. This version deliberately does not migrate Story, dependency, Sprint-plan, or execution lineage.

- [ ] **Step 1: Add failing correction-boundary graph tests**

Start with one accepted physical-leaf Backlog. Prove correction remains available
when the project contains only terminal Roadmap history, Roadmap feedback, and a
failed or expired Roadmap generation attempt. This is the P&ID Attempt 18 shape.

Then add one case for every stage-closing class:

1. a pending-review Roadmap;
2. any Story planning artifact or Story fact;
3. any dependency row or dependency-review row;
4. any Sprint-plan artifact, including accepted, feedback, or rejected state;
5. any planned, active, or completed Sprint, Sprint-start fact, or Task;
6. any same-current-business-facts Story or Sprint-plan attempt, including a
   failed, obsolete, expired, active, or successful attempt; and
7. an active, unexpired same-current-business-facts Roadmap attempt.

Cases 1 through 6 return `BACKLOG_CORRECTION_STAGE_CLOSED` with the message
`Guided Backlog correction is available only before Story or Sprint planning begins.`
An active Roadmap attempt returns `BACKLOG_CORRECTION_DOWNSTREAM_ACTIVE` and says
to wait for the current downstream operation to finish. Assert no optional
correction decision is emitted. Old attempts whose business-fact fingerprint
does not match the current snapshot must not close the boundary.

- [ ] **Step 2: Add failing unresolved-correction planning tests**

Create snapshots for each unresolved state:

1. active, failed, obsolete, or expired same-current-facts `backlog.generate`
   correction attempt; and
2. a physical Backlog child different from the accepted leaf, with
   `pending_review`, `feedback`, or `rejected` status.

For every state, assert all nine downstream planning nodes are blocked with
`BACKLOG_CORRECTION_IN_PROGRESS`: Roadmap generate/review, Story generate/review,
dependencies, readiness, Sprint plan/review/start. Feedback or rejection does
not release planning because the replacement chain remains unresolved. Control
cases prove no blocker before correction and after exact successor acceptance.

- [ ] **Step 3: Run graph tests and capture failure**

Run:

```powershell
uv run --no-sync --offline pytest tests/workflow/test_vision_backlog_graph.py tests/workflow/test_planning_graph.py -k "backlog_correction" -q
```

Expected before implementation: the accepted Backlog always advertises optional
correction, and downstream planning remains available during failed or pending
correction states.

- [ ] **Step 4: Implement the pure boundary and in-progress helpers**

Place both helpers beside `current_backlog_lineage()` in
`workflow/definitions/backlog.py`.

`backlog_correction_boundary_problem()` must inspect durable project facts. It
allows terminal Roadmap artifacts and terminal Roadmap attempts. It rejects a
pending Roadmap, any Story/dependency/Sprint state listed in Step 1, any
same-current-facts Story or Sprint-plan attempt regardless of outcome, and any
active same-current-facts Roadmap attempt. Use
`business_fact_fingerprint(snapshot)` for attempt scoping.

Call this helper from `_backlog_generate_rule()` immediately before emitting
`BACKLOG_CORRECTION_AVAILABLE`. Do not apply it to initial Backlog generation or
to feedback/rejected Backlog revision.

`backlog_correction_in_progress()` returns true when an accepted Backlog exists
and either:

- the physical Backlog leaf differs from the accepted leaf, regardless of
  whether that successor is pending, feedback, or rejected; or
- a same-current-business-facts `backlog.generate` correction attempt exists and
  has not produced a successful successor under the current facts. This includes
  active, failed, obsolete, and expired correction attempts.

- [ ] **Step 5: Add the shared planning guard**

Add `_pause_during_backlog_correction(rule: NodeRule) -> NodeRule` and wrap each
of the nine `PLANNING_NODES` rules. Before invoking the wrapped rule, it returns:

```python
_blocked(
    "BACKLOG_CORRECTION_IN_PROGRESS",
    "New planning waits until the accepted Backlog correction chain is resolved.",
)
```

Do not change execution graph rules. Under this version's entry boundary,
execution state cannot exist when correction begins.

- [ ] **Step 6: Add transactional boundary and race regressions**

In `tests/workflow/test_vision_backlog_transitions.py`, obtain a valid correction
decision, then inject each of these changes before the transition executes:

- a current Story or Sprint boundary fact; and
- an active current Roadmap, Story, or Sprint-plan agentic attempt.

Assert `RecordBacklogDraft` fails with `WORKFLOW_FACT_CONFLICT`, writes no Backlog
successor, and records no success outcome. Add both start-order interleavings:
downstream attempt first prevents correction start; correction attempt first
causes every downstream start to fail its graph/decision revalidation.

Create a pending correction successor, inject a stage-closing fact, and attempt
an `accepted` Backlog review. Assert the in-transaction recheck refuses the
acceptance and inserts no decision. Feedback and rejection do not switch
authority, but their unresolved successor still keeps planning paused.

- [ ] **Step 7: Recheck the boundary inside persistence transactions**

In `execute_record_backlog_draft()`, for all three correction reason codes, load
the current `WorkflowFactSnapshot` through `WorkflowFactRepository(session)` and
call `backlog_correction_boundary_problem()` before inserting the successor.
Return a stable conflict if the boundary closed after decision selection.

In `execute_decide_backlog()`, when accepting a physical successor of an already
accepted Backlog, perform the same in-session boundary check before writing the
decision. This closes the race between provider completion and human acceptance.
Do not apply this acceptance guard to the initial Backlog or to feedback/rejected
reviews.

- [ ] **Step 8: Add the accepted-successor lineage regression**

Create this provider-free history:

1. accepted Backlog A;
2. Roadmap A with feedback;
3. failed Roadmap generation Attempt 18 under A;
4. pending Backlog B that supersedes A; and
5. accepted review for Backlog B.

Assert planning is blocked after step 4. After step 5 assert:

```python
roadmap = _decision(domain.position(project_id), "planning.roadmap.generate")
assert roadmap.category is NodeCategory.AVAILABLE
assert roadmap.reason_code == "ROADMAP_GENERATION_REQUIRED"
assert roadmap.recommendation_kind is RecommendationKind.REQUIRED
assert {ref.fact_type for ref in roadmap.fact_references} == {
    "backlog",
    "product_goal",
    "specification",
}
assert all(ref.fact_type != "node_attempt" for ref in roadmap.fact_references)
assert next(ref for ref in roadmap.fact_references if ref.fact_type == "backlog").fact_id == str(backlog_b_id)
```

Assert Roadmap A, its feedback, and Attempt 18 remain immutable history. A new
Roadmap under B has `supersedes_roadmap_artifact_id is None`. Story and Sprint
state is absent by the correction entry contract; do not add selector or
repository migrations in this task.

- [ ] **Step 9: Run planning and transition tests**

Run:

```powershell
uv run --no-sync --offline pytest tests/workflow/test_vision_backlog_graph.py tests/workflow/test_vision_backlog_transitions.py tests/workflow/test_planning_graph.py tests/workflow/test_planning_transitions.py -q
```

Expected: all tests pass. Terminal Roadmap evidence remains queryable, every
unresolved correction pauses downstream planning, stage-closing facts fail
closed, and successor acceptance produces a clean Roadmap decision.

- [ ] **Step 10: Create the optional review commit**

After explicit commit authorization:

```powershell
git add workflow/definitions/backlog.py workflow/definitions/planning.py workflow/handlers/product_definition.py tests/workflow/test_vision_backlog_graph.py tests/workflow/test_vision_backlog_transitions.py tests/workflow/test_planning_graph.py tests/workflow/test_planning_transitions.py
git commit -m "fix(planning): isolate backlog correction lineage"
```

---

### Task 6: Document and verify the complete feature

**Files:**
- Create: `docs/testing/GUIDED-BACKLOG-CORRECTION.md`
- Verify all files in the file map.

**Interfaces:**
- Documents: operator preflight, exact command fields, replay rules, pending-review authority, downstream pause, unchanged-output conflict, and post-acceptance Roadmap behavior.
- Produces: one review packet containing diff, exact test results, and any remaining failures.

- [ ] **Step 1: Write the operator guide**

The guide must state all of the following in direct language:

- Use the checkout-local `./agileforge-dev`; run `info --json` before a runtime mutation.
- Read `workflow next` and use the exact rendered `backlog correct` binding.
- Guidance is human semantic input. It cannot override the accepted Specification
  or Product Goal.
- A successful call creates a pending successor. It does not accept it.
- The old Backlog remains authoritative until exact successor acceptance.
- Exact retries reuse the same key. Changed guidance or target uses a new key.
- A failed or expired correction is retried only from the dedicated correction
  recovery decision with a new key; generic Backlog generation cannot bypass it.
- An unchanged generated Backlog fails with `WORKFLOW_FACT_CONFLICT` and creates
  no artifact.
- Correction is available during terminal Roadmap review history and closes once
  Story or Sprint planning begins. Pending Roadmap review and active downstream
  provider work must finish first.
- All downstream planning pauses while a correction attempt or unaccepted
  successor remains unresolved, including feedback and rejection states.
- After successor acceptance, generate and review a fresh Roadmap before Stories.
- The P&ID split remains a separate authorized live operation and human review.

- [ ] **Step 2: Run focused tests with socket blocking**

Run:

```powershell
uv run --no-sync --offline pytest tests/workflow/test_graph_kernel.py tests/workflow/test_vision_backlog_graph.py tests/workflow/test_vision_backlog_transitions.py tests/workflow/test_node_attempts.py tests/workflow/test_planning_graph.py tests/workflow/test_planning_transitions.py tests/adapters/test_adk_workflow_runner.py tests/adapters/test_api_workflow_domain.py tests/adapters/test_cli_workflow_domain.py tests/adapters/test_command_renderer.py -q
```

Expected: all selected tests pass with no network access.

- [ ] **Step 3: Run static checks**

Run:

```powershell
uv run --no-sync --offline ruff check workflow/graph.py workflow/definitions/backlog.py workflow/definitions/planning.py workflow/handlers/product_definition.py services/contracts/backlog.py services/application.py services/node_attempt_replay.py adapters/adk/recipes.py cli/main.py cli/workflow_commands.py api.py tests/workflow/test_graph_kernel.py tests/workflow/test_vision_backlog_graph.py tests/workflow/test_vision_backlog_transitions.py tests/workflow/test_node_attempts.py tests/workflow/test_planning_graph.py tests/workflow/test_planning_transitions.py tests/adapters/test_adk_workflow_runner.py tests/adapters/test_api_workflow_domain.py tests/adapters/test_cli_workflow_domain.py tests/adapters/test_command_renderer.py
uv run --no-sync --offline ty check workflow/ services/ adapters/adk/ cli/ api.py
git diff --check
```

Expected: zero Ruff errors, zero type diagnostics, and zero whitespace errors.

- [ ] **Step 4: Run the full offline Python and Node suites**

Run:

```powershell
uv run --no-sync --offline pytest -q
node --test tests/*.mjs
```

Expected: all repository tests pass or the implementation report names each
pre-existing unrelated failure with exact evidence. Do not weaken tests or enable
network access to obtain a green result.

- [ ] **Step 5: Review the final diff**

Run:

```powershell
git status --short
git diff --stat
git diff -- docs/superpowers/specs/2026-09-04-guided-backlog-correction-design.md docs/superpowers/plans/2026-09-04-guided-backlog-correction.md docs/testing/GUIDED-BACKLOG-CORRECTION.md
git diff -- workflow/graph.py workflow/definitions/backlog.py workflow/definitions/planning.py workflow/handlers/product_definition.py services/contracts/backlog.py services/application.py services/node_attempt_replay.py adapters/adk/recipes.py cli/main.py cli/workflow_commands.py api.py
```

Confirm the patch contains no model configuration, prompt, frontend, database,
profile, or P&ID source changes.

- [ ] **Step 6: Return the review handoff**

Report:

- files changed and why;
- before-and-after evidence for stale attempt contamination;
- exact request, symmetric replay, correction recovery, stage-boundary,
  concurrency, lineage, unchanged-output, and planning-pause tests;
- focused and full-suite results;
- working-tree status;
- any failure or design conflict that remains.

Stop before commit, profile mutation, or live provider execution unless the
operator separately authorizes those actions.

- [ ] **Step 7: Create the optional final documentation commit**

After explicit commit authorization:

```powershell
git add docs/testing/GUIDED-BACKLOG-CORRECTION.md
git commit -m "docs(backlog): explain guided correction"
```

---

## Post-implementation live sequence

These steps are outside implementation authorization. They occur only after code
review and a new runtime commit:

1. Review and commit the generic correction patch.
2. Initialize or advance an isolated successor profile under the reviewed commit.
3. Seed it from `pid-verification-terra` during a verified quiet window.
4. Run a provider-free preflight and confirm the exact
   `BACKLOG_CORRECTION_AVAILABLE` decision for Backlog Artifact 3.
5. Submit one `backlog correct` call with the approved five-way partition guidance.
6. Review the pending twelve-item Backlog against accepted Specification Version 1.
7. Accept only the exact reviewed successor fingerprint.
8. Confirm a clean `ROADMAP_GENERATION_REQUIRED` decision with no Attempt 18
   reference.
9. Generate one fresh Roadmap, review it, then proceed to Story generation.
