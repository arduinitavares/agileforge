# Story CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Install Story CLI commands so AgileForge can move caRtola from `ROADMAP_PERSISTENCE` through story generation, review, guarded persistence, and Sprint handoff without manual interviews.

**Architecture:** Follow the installed Vision, Backlog, and Roadmap CLI pattern: CLI -> application facade -> phase runner -> phase service -> runtime/tool boundary. Story generation is per Roadmap requirement, so the CLI must expose pending work, generate/refine one requirement at a time, save only a reviewed attempt with exact guards, and complete the Story phase only when every Roadmap item has canonical persisted story coverage.

**Tech Stack:** Python 3.13, SQLModel, Pydantic, AnyIO, existing AgileForge CLI envelopes, existing `services/story_runtime.py`, existing `services/phases/story_service.py`, existing `orchestrator_agent/agent_tools/user_story_writer_tool`.

---

## File Structure

- Create `services/agent_workbench/story_phase.py`: CLI-facing Story runner. It hydrates project state, delegates to `services/phases/story_service.py`, converts domain errors to CLI envelopes, and mirrors the failure behavior used by Vision, Backlog, and Roadmap runners.
- Modify `services/phases/story_service.py`: add artifact fingerprints, attempt-aware save guards, FSM transitions, idempotency replay, story completion coverage checks, and complete-phase hard blocks.
- Modify `services/story_runtime.py`: force `is_complete: false` when `clarifying_questions` is non-empty, even if the model returns `is_complete: true`.
- Modify `orchestrator_agent/agent_tools/user_story_writer_tool/tools.py`: make story persistence idempotency-aware and block unsafe replacement when a requirement's existing stories have progressed downstream.
- Modify `services/agent_workbench/application.py`: add Story phase protocol/facade methods and workflow-next routing.
- Modify `services/agent_workbench/command_registry.py`: register Story command contracts for generate, retry, history, pending, save, and complete.
- Modify `cli/main.py`: add installed Story CLI commands.
- Modify `api.py`: keep HTTP Story endpoints behaviorally aligned with guarded CLI saves.
- Modify `docs/agent-cli-manual.md`: document the complete project -> authority -> vision -> backlog -> roadmap -> story flow using direct `agileforge` commands.
- Tests:
  - `tests/test_story_runtime.py`
  - `tests/test_story_phase_service.py`
  - `tests/test_save_stories_tool.py`
  - `tests/test_agent_workbench_story_phase.py`
  - `tests/test_agent_workbench_application.py`
  - `tests/test_agent_workbench_cli.py`
  - `tests/test_agent_workbench_command_schema.py`
  - `tests/test_api_story_flow.py`

## Core Behavior

- `agileforge story pending --project-id <id>` lists every Roadmap requirement grouped by milestone, with `Pending`, `Attempted`, `Saved`, or `Merged` status.
- `agileforge story generate --project-id <id> --parent-requirement "<requirement>"` creates the first draft for one Roadmap requirement.
- `agileforge story generate --project-id <id> --parent-requirement "<requirement>" --input "<review feedback>"` refines an existing draft or answers blocking questions.
- `agileforge story retry --project-id <id> --parent-requirement "<requirement>"` replays the last retryable provider/transport failure without inventing reviewer input.
- `agileforge story history --project-id <id> --parent-requirement "<requirement>"` shows attempts and guard fields.
- `agileforge story save --project-id <id> --parent-requirement "<requirement>" --attempt-id <attempt_id> --expected-artifact-fingerprint <fingerprint> --expected-state STORY_REVIEW --idempotency-key <key>` persists exactly the reviewed draft for one Roadmap requirement.
- `agileforge story complete --project-id <id> --expected-state STORY_PERSISTENCE --idempotency-key <key>` moves to `SPRINT_SETUP` only when every Roadmap requirement is saved or explicitly merged through existing Story resolution state.
- Downstream Sprint commands must only see active canonical story rows; superseded or unsafe duplicate rows must not count as active backlog/story structure.

## Task 1: Runtime Completion Consistency

**Files:**
- Modify: `services/story_runtime.py`
- Test: `tests/test_story_runtime.py`

- [ ] **Step 1: Write failing runtime test**

Add this test to `tests/test_story_runtime.py` near the existing successful draft tests:

```python
@pytest.mark.asyncio
async def test_story_runtime_forces_incomplete_when_clarifying_questions_remain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_invoke_story_agent(payload: UserStoryWriterInput) -> str:
        return json.dumps(
            {
                "parent_requirement": payload.parent_requirement,
                "user_stories": [
                    {
                        "story_title": "Live lineup decision",
                        "statement": "As a Cartola manager, I want a recommended lineup so that I can act before market lock.",
                        "acceptance_criteria": [
                            "Given eligible players exist, when the recommendation is generated, then a lineup is returned with player names and positions."
                        ],
                        "invest_score": "High",
                        "estimated_effort": "M",
                        "produced_artifacts": ["lineup_recommendation"],
                    }
                ],
                "is_complete": True,
                "clarifying_questions": ["Which live-lock cutoff should the story use?"],
            }
        )

    monkeypatch.setattr(
        "services.story_runtime._invoke_story_agent",
        fake_invoke_story_agent,
    )

    result = await run_story_agent_from_state(
        {
            "roadmap_releases": [{"items": ["Live weekly recommendation MVP"]}],
            "pending_spec_content": "{}",
            "compiled_authority_cached": "{}",
        },
        project_id=2,
        parent_requirement="Live weekly recommendation MVP",
        user_input=None,
    )

    assert result["success"] is True
    assert result["is_complete"] is False
    assert result["draft_kind"] == "incomplete_draft"
    assert result["output_artifact"]["is_complete"] is False
```

- [ ] **Step 2: Run the failing runtime test**

Run:

```bash
uv run --frozen pytest tests/test_story_runtime.py::test_story_runtime_forces_incomplete_when_clarifying_questions_remain -q
```

Expected: fails because Story runtime currently trusts `is_complete: true` even when blocking questions remain.

- [ ] **Step 3: Add runtime consistency helper**

In `services/story_runtime.py`, add:

```python
def _has_clarifying_questions(output: UserStoryWriterOutput) -> bool:
    return any(question.strip() for question in output.clarifying_questions)
```

In `run_story_agent_request`, after `output = UserStoryWriterOutput.model_validate(parsed)` and before returning success, compute:

```python
effective_is_complete = output.is_complete and not _has_clarifying_questions(output)
artifact = output.model_dump()
artifact["is_complete"] = effective_is_complete
```

Return `artifact` as `output_artifact`, set `is_complete` to `effective_is_complete`, and set `draft_kind` to:

```python
"complete_draft" if effective_is_complete else "incomplete_draft"
```

- [ ] **Step 4: Verify runtime test passes**

Run:

```bash
uv run --frozen pytest tests/test_story_runtime.py::test_story_runtime_forces_incomplete_when_clarifying_questions_remain -q
```

Expected: pass.

## Task 2: Story Phase Attempt Guards

**Files:**
- Modify: `services/phases/story_service.py`
- Test: `tests/test_story_phase_service.py`

- [ ] **Step 1: Write failing tests for attempt IDs and fingerprints**

Add to `tests/test_story_phase_service.py`:

```python
@pytest.mark.asyncio
async def test_generate_story_draft_returns_attempt_guards() -> None:
    state: dict[str, Any] = {
        "fsm_state": "ROADMAP_PERSISTENCE",
        "roadmap_releases": [{"items": ["Live weekly recommendation MVP"]}],
    }

    async def load_state() -> dict[str, Any]:
        return state

    saved_states: list[dict[str, Any]] = []

    async def fake_run_story_agent_from_state(**_: Any) -> dict[str, Any]:
        return {
            "success": True,
            "is_reusable": True,
            "is_complete": True,
            "draft_kind": "complete_draft",
            "classification": "reusable_content_result",
            "input_context": {"parent_requirement": "Live weekly recommendation MVP"},
            "request_payload": {"parent_requirement": "Live weekly recommendation MVP"},
            "output_artifact": {
                "parent_requirement": "Live weekly recommendation MVP",
                "user_stories": [
                    {
                        "story_title": "Live lineup decision",
                        "statement": "As a Cartola manager, I want a recommended lineup so that I can act before market lock.",
                        "acceptance_criteria": [
                            "Given eligible players exist, when a recommendation is generated, then the lineup contains selected player names and positions."
                        ],
                        "invest_score": "High",
                        "estimated_effort": "M",
                        "produced_artifacts": ["lineup_recommendation"],
                    }
                ],
                "is_complete": True,
                "clarifying_questions": [],
            },
        }

    payload = await generate_story_draft(
        project_id=2,
        parent_requirement="Live weekly recommendation MVP",
        user_input=None,
        load_state=load_state,
        save_state=lambda next_state: saved_states.append(next_state),
        now_iso=lambda: "2026-05-22T00:00:00Z",
        run_story_agent_from_state=fake_run_story_agent_from_state,
        append_feedback_entry=append_feedback_entry,
        set_request_projection=set_request_projection,
        append_attempt=append_attempt,
        promote_reusable_draft=promote_reusable_draft,
        mark_feedback_absorbed=mark_feedback_absorbed,
        failure_meta=failure_meta_from_runtime_result,
    )

    data = payload["data"]
    assert payload["fsm_state"] == "STORY_REVIEW"
    assert data["current_draft"]["attempt_id"] == "attempt-1"
    assert data["current_draft"]["artifact_fingerprint"].startswith("sha256:")
    assert data["save"] == {
        "available": True,
        "attempt_id": "attempt-1",
        "artifact_fingerprint": data["current_draft"]["artifact_fingerprint"],
        "expected_state": "STORY_REVIEW",
    }
    assert saved_states[-1]["fsm_state"] == "STORY_REVIEW"
```

- [ ] **Step 2: Run the focused failing test**

Run:

```bash
uv run --frozen pytest tests/test_story_phase_service.py::test_generate_story_draft_returns_attempt_guards -q
```

Expected: fails because Story drafts do not yet expose artifact fingerprints or update FSM state.

- [ ] **Step 3: Add fingerprint helpers and guarded summary**

In `services/phases/story_service.py`, import:

```python
from services.agent_workbench.fingerprints import canonical_hash
```

Add:

```python
def _story_artifact_fingerprint(
    *,
    parent_requirement: str,
    output_artifact: dict[str, Any],
) -> str:
    return canonical_hash(
        {
            "phase": "story",
            "parent_requirement": parent_requirement,
            "output_artifact": output_artifact,
        }
    )


def _attach_story_attempt_guards(
    runtime: dict[str, Any],
    *,
    attempt_id: str,
    parent_requirement: str,
) -> str | None:
    attempt = _find_attempt_by_id(runtime, attempt_id)
    if not isinstance(attempt, dict):
        return None

    artifact = attempt.get("output_artifact")
    if not isinstance(artifact, dict):
        return None

    fingerprint = _story_artifact_fingerprint(
        parent_requirement=parent_requirement,
        output_artifact=artifact,
    )
    attempt["artifact_fingerprint"] = fingerprint
    artifact["attempt_id"] = attempt_id
    artifact["artifact_fingerprint"] = fingerprint

    draft_projection = runtime.get("draft_projection")
    if isinstance(draft_projection, dict):
        draft_projection["artifact_fingerprint"] = fingerprint

    return fingerprint
```

Update `story_interview_summary` so `current_draft` includes `artifact_fingerprint`, and `save` includes:

```python
save_payload = story_save_payload(runtime)
save_attempt_id = draft_projection.get("latest_reusable_attempt_id")
artifact_fingerprint = draft_projection.get("artifact_fingerprint")
save_available = bool(
    save_payload
    and isinstance(save_attempt_id, str)
    and isinstance(artifact_fingerprint, str)
)
```

Return:

```python
"save": {
    "available": save_available,
    "attempt_id": save_attempt_id if save_available else None,
    "artifact_fingerprint": artifact_fingerprint if save_available else None,
    "expected_state": OrchestratorState.STORY_REVIEW.value if save_available else None,
}
```

- [ ] **Step 4: Set FSM transitions in generation**

In `generate_story_draft`, immediately after the existing `promote_reusable_draft` call, call:

```python
_attach_story_attempt_guards(
    runtime,
    attempt_id=attempt_id,
    parent_requirement=normalized_parent_requirement,
)
```

Then before `save_state(state)`, set:

```python
state["fsm_state"] = (
    OrchestratorState.STORY_REVIEW.value
    if story_save_payload(runtime)
    else OrchestratorState.STORY_INTERVIEW.value
)
```

At the top-level return, add:

```python
"fsm_state": state["fsm_state"],
```

- [ ] **Step 5: Verify focused test passes**

Run:

```bash
uv run --frozen pytest tests/test_story_phase_service.py::test_generate_story_draft_returns_attempt_guards -q
```

Expected: pass.

## Task 3: Guarded Story Save

**Files:**
- Modify: `services/phases/story_service.py`
- Test: `tests/test_story_phase_service.py`

- [ ] **Step 1: Write failing save guard tests**

Add:

```python
@pytest.mark.asyncio
async def test_save_story_draft_requires_attempt_guards() -> None:
    state = _state_with_complete_story_draft()

    with pytest.raises(StoryPhaseError, match="attempt-id"):
        await save_story_draft(
            project_id=2,
            parent_requirement="Live weekly recommendation MVP",
            load_state=lambda: _async_value(state),
            save_state=lambda _state: None,
            hydrate_context=lambda _state: {"product_id": 2},
            build_tool_context=lambda _state: None,
            save_stories_tool=lambda _input_data, _tool_context: {"success": True},
            attempt_id=None,
            expected_artifact_fingerprint=None,
            expected_state=None,
            idempotency_key=None,
        )
```

Add:

```python
@pytest.mark.asyncio
async def test_save_story_draft_rejects_stale_fingerprint() -> None:
    state = _state_with_complete_story_draft()

    with pytest.raises(StoryPhaseError, match="artifact fingerprint"):
        await save_story_draft(
            project_id=2,
            parent_requirement="Live weekly recommendation MVP",
            load_state=lambda: _async_value(state),
            save_state=lambda _state: None,
            hydrate_context=lambda _state: {"product_id": 2},
            build_tool_context=lambda _state: None,
            save_stories_tool=lambda _input_data, _tool_context: {"success": True},
            attempt_id="attempt-1",
            expected_artifact_fingerprint="sha256:stale",
            expected_state="STORY_REVIEW",
            idempotency_key="story-save-2-live",
        )
```

Add:

```python
@pytest.mark.asyncio
async def test_save_story_draft_replays_same_idempotency_key() -> None:
    state = _state_with_complete_story_draft()
    save_calls: list[SaveStoriesInput] = []

    first = await save_story_draft(
        project_id=2,
        parent_requirement="Live weekly recommendation MVP",
        load_state=lambda: _async_value(state),
        save_state=lambda next_state: state.update(next_state),
        hydrate_context=lambda _state: {"product_id": 2},
        build_tool_context=lambda _state: None,
        save_stories_tool=lambda input_data, _tool_context: (
            save_calls.append(input_data)
            or {"success": True, "saved_count": 1, "story_ids": [7]}
        ),
        attempt_id="attempt-1",
        expected_artifact_fingerprint=state["interview_runtime"]["story"]["Live weekly recommendation MVP"]["draft_projection"]["artifact_fingerprint"],
        expected_state="STORY_REVIEW",
        idempotency_key="story-save-2-live",
    )

    second = await save_story_draft(
        project_id=2,
        parent_requirement="Live weekly recommendation MVP",
        load_state=lambda: _async_value(state),
        save_state=lambda next_state: state.update(next_state),
        hydrate_context=lambda _state: {"product_id": 2},
        build_tool_context=lambda _state: None,
        save_stories_tool=lambda input_data, _tool_context: (
            save_calls.append(input_data)
            or {"success": True, "saved_count": 1, "story_ids": [8]}
        ),
        attempt_id="attempt-1",
        expected_artifact_fingerprint=state["interview_runtime"]["story"]["Live weekly recommendation MVP"]["draft_projection"]["artifact_fingerprint"],
        expected_state="STORY_REVIEW",
        idempotency_key="story-save-2-live",
    )

    assert first == second
    assert len(save_calls) == 1
```

- [ ] **Step 2: Run save guard tests and verify failure**

Run:

```bash
uv run --frozen pytest \
  tests/test_story_phase_service.py::test_save_story_draft_requires_attempt_guards \
  tests/test_story_phase_service.py::test_save_story_draft_rejects_stale_fingerprint \
  tests/test_story_phase_service.py::test_save_story_draft_replays_same_idempotency_key \
  -q
```

Expected: fail because `save_story_draft` does not accept guard fields yet.

- [ ] **Step 3: Extend `save_story_draft` signature and validate guards**

Change the `save_story_draft` signature to include:

```python
attempt_id: str | None,
expected_artifact_fingerprint: str | None,
expected_state: str | None,
idempotency_key: str | None,
```

Add this validation near the start after state and runtime are loaded:

```python
if attempt_id is None or not attempt_id.strip():
    raise StoryPhaseError("story save requires --attempt-id", status_code=400)
if expected_artifact_fingerprint is None or not expected_artifact_fingerprint.strip():
    raise StoryPhaseError(
        "story save requires --expected-artifact-fingerprint",
        status_code=400,
    )
if expected_state != OrchestratorState.STORY_REVIEW.value:
    raise StoryPhaseError(
        "story save requires --expected-state STORY_REVIEW",
        status_code=400,
    )
if idempotency_key is None or not idempotency_key.strip():
    raise StoryPhaseError("story save requires --idempotency-key", status_code=400)
if state.get("fsm_state") != OrchestratorState.STORY_REVIEW.value:
    raise StoryPhaseError(
        f"story save requires FSM state STORY_REVIEW, got {state.get('fsm_state')}",
        status_code=409,
    )
```

Then verify selected attempt:

```python
runtime = ensure_story_runtime(
    state,
    parent_requirement=normalized_parent_requirement,
)
artifact = story_save_payload(runtime)
if not artifact:
    raise StoryPhaseError("No complete story draft is available to save.")

draft_projection = runtime.get("draft_projection") or {}
current_attempt_id = draft_projection.get("latest_reusable_attempt_id")
current_fingerprint = draft_projection.get("artifact_fingerprint")

if current_attempt_id != attempt_id:
    raise StoryPhaseError(
        f"story save attempt mismatch: expected current attempt {current_attempt_id}, got {attempt_id}",
        status_code=409,
    )
if current_fingerprint != expected_artifact_fingerprint:
    raise StoryPhaseError(
        "story save artifact fingerprint mismatch; refresh history and review the current draft",
        status_code=409,
    )
```

- [ ] **Step 4: Add idempotency replay state**

In `save_story_draft`, before calling `save_stories_tool`, add:

```python
idempotency_registry = state.setdefault("story_save_idempotency", {})
if not isinstance(idempotency_registry, dict):
    idempotency_registry = {}
    state["story_save_idempotency"] = idempotency_registry

existing = idempotency_registry.get(idempotency_key)
if isinstance(existing, dict):
    return dict(existing)
```

After successful save, build:

```python
result = {
    "parent_requirement": normalized_parent_requirement,
    "attempt_id": attempt_id,
    "artifact_fingerprint": expected_artifact_fingerprint,
    "save_result": save_result,
    "fsm_state": OrchestratorState.STORY_PERSISTENCE.value,
}
```

Persist:

```python
state["story_saved"][normalized_parent_requirement] = True
state["fsm_state"] = OrchestratorState.STORY_PERSISTENCE.value
idempotency_registry[idempotency_key] = result
```

- [ ] **Step 5: Pass idempotency into persistence tool**

When constructing `SaveStoriesInput`, include `idempotency_key=idempotency_key` after Task 4 extends that schema.

- [ ] **Step 6: Verify save guard tests pass**

Run:

```bash
uv run --frozen pytest \
  tests/test_story_phase_service.py::test_save_story_draft_requires_attempt_guards \
  tests/test_story_phase_service.py::test_save_story_draft_rejects_stale_fingerprint \
  tests/test_story_phase_service.py::test_save_story_draft_replays_same_idempotency_key \
  -q
```

Expected: pass.

## Task 4: Story Persistence Tool Safety

**Files:**
- Modify: `orchestrator_agent/agent_tools/user_story_writer_tool/tools.py`
- Test: `tests/test_save_stories_tool.py`

- [ ] **Step 1: Add failing tests for idempotency and unsafe replacement**

Add:

```python
def test_save_stories_tool_replays_idempotency_key(
    session: Session,
    product: Product,
) -> None:
    input_data = SaveStoriesInput(
        product_id=product.product_id,
        parent_requirement="Live weekly recommendation MVP",
        idempotency_key="story-save-2-live",
        stories=[_story_item("Live lineup decision")],
    )

    first = save_stories_tool(input_data)
    second = save_stories_tool(input_data)

    assert first["success"] is True
    assert second["success"] is True
    assert second["idempotent_replay"] is True

    active = session.exec(
        select(UserStory)
        .where(UserStory.product_id == product.product_id)
        .where(UserStory.source_requirement == normalize_requirement_key("Live weekly recommendation MVP"))
        .where(UserStory.is_superseded == False)
    ).all()
    assert len(active) == 1
```

Add:

```python
def test_save_stories_tool_blocks_replacement_after_sprint_link(
    session: Session,
    product: Product,
) -> None:
    progressed = UserStory(
        product_id=product.product_id,
        title="Existing story",
        story_description="As a manager, I want an existing story.",
        acceptance_criteria="- Existing criterion",
        source_requirement=normalize_requirement_key("Live weekly recommendation MVP"),
        refinement_slot=1,
        story_origin="refined",
        is_refined=True,
        is_superseded=False,
        sprint_id=44,
    )
    session.add(progressed)
    session.commit()

    result = save_stories_tool(
        SaveStoriesInput(
            product_id=product.product_id,
            parent_requirement="Live weekly recommendation MVP",
            idempotency_key="story-save-2-live",
            stories=[_story_item("Replacement story")],
        )
    )

    assert result["success"] is False
    assert result["error_code"] == "STORY_REPLACEMENT_UNSAFE"
    assert "sprint" in result["error"].lower()
```

- [ ] **Step 2: Run persistence tests and verify failure**

Run:

```bash
uv run --frozen pytest \
  tests/test_save_stories_tool.py::test_save_stories_tool_replays_idempotency_key \
  tests/test_save_stories_tool.py::test_save_stories_tool_blocks_replacement_after_sprint_link \
  -q
```

Expected: fail because `SaveStoriesInput` does not include `idempotency_key`, and unsafe replacement is not blocked.

- [ ] **Step 3: Extend `SaveStoriesInput`**

Add to `SaveStoriesInput`:

```python
idempotency_key: Annotated[
    str,
    Field(
        min_length=1,
        description="Caller-provided key that makes story persistence retry-safe.",
    ),
]
```

- [ ] **Step 4: Add event lookup helper**

Add:

```python
def _find_story_save_event(
    session: Session,
    *,
    product_id: int,
    idempotency_key: str,
) -> dict[str, Any] | None:
    events = session.exec(
        select(WorkflowEvent)
        .where(WorkflowEvent.product_id == product_id)
        .where(WorkflowEvent.event_type == WorkflowEventType.STORIES_SAVED)
        .order_by(WorkflowEvent.created_at.desc())
    ).all()

    for event in events:
        try:
            metadata = json.loads(event.event_metadata or "{}")
        except json.JSONDecodeError:
            continue
        if metadata.get("idempotency_key") == idempotency_key:
            return metadata
    return None
```

At the start of the session block after product validation, call it:

```python
previous = _find_story_save_event(
    session,
    product_id=input_data.product_id,
    idempotency_key=input_data.idempotency_key,
)
if previous:
    return {
        "success": True,
        "idempotent_replay": True,
        "saved_count": previous.get("saved_count", 0),
        "updated_count": previous.get("updated_count", 0),
        "created_count": previous.get("created_count", 0),
        "story_ids": previous.get("story_ids", []),
    }
```

- [ ] **Step 5: Add unsafe replacement guard**

Add:

```python
def _story_replacement_blockers(stories: list[UserStory]) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    for story in stories:
        if getattr(story, "sprint_id", None):
            blockers.append(
                {
                    "story_id": story.story_id,
                    "reason": "linked_to_sprint",
                    "sprint_id": story.sprint_id,
                }
            )
        status = getattr(story, "status", None)
        status_value = getattr(status, "value", status)
        if isinstance(status_value, str) and status_value not in {"TO_DO", "BACKLOG"}:
            blockers.append(
                {
                    "story_id": story.story_id,
                    "reason": "status_progressed",
                    "status": status_value,
                }
            )
    return blockers
```

Before upserting slots, load active rows:

```python
existing_active = session.exec(
    select(UserStory)
    .where(UserStory.product_id == input_data.product_id)
    .where(UserStory.source_requirement == normalized_req)
    .where(UserStory.is_superseded == False)  # noqa: E712
).all()
blockers = _story_replacement_blockers(list(existing_active))
if blockers:
    return {
        "success": False,
        "error_code": "STORY_REPLACEMENT_UNSAFE",
        "error": (
            "Cannot replace stories for this requirement because at least one "
            "existing story has progressed downstream."
        ),
        "blockers": blockers,
    }
```

- [ ] **Step 6: Supersede overflow active stories**

After upserting all validated slots, supersede any existing active row whose `refinement_slot` is greater than the number of validated stories:

```python
valid_slots = set(range(1, len(validated) + 1))
superseded_ids: list[int] = []
for story in existing_active:
    if story.refinement_slot in valid_slots:
        continue
    story.is_superseded = True
    session.add(story)
    if story.story_id is not None:
        superseded_ids.append(story.story_id)
```

This keeps exactly one active story set for the requirement while retaining audit history in superseded rows.

- [ ] **Step 7: Store idempotency metadata in event**

Include these fields in `event_metadata`:

```python
{
    "idempotency_key": input_data.idempotency_key,
    "parent_requirement": input_data.parent_requirement,
    "saved_count": len(updated_ids) + len(created_ids),
    "updated_count": len(updated_ids),
    "created_count": len(created_ids),
    "superseded_count": len(superseded_ids),
    "story_ids": [*updated_ids, *created_ids],
    "superseded_story_ids": superseded_ids,
}
```

Return those same counts.

- [ ] **Step 8: Verify persistence tests pass**

Run:

```bash
uv run --frozen pytest tests/test_save_stories_tool.py -q
```

Expected: pass.

## Task 5: Story Completion Gate

**Files:**
- Modify: `services/phases/story_service.py`
- Test: `tests/test_story_phase_service.py`

- [ ] **Step 1: Write failing completion tests**

Add:

```python
@pytest.mark.asyncio
async def test_complete_story_phase_blocks_until_all_roadmap_requirements_saved() -> None:
    state = {
        "fsm_state": "STORY_PERSISTENCE",
        "roadmap_releases": [{"items": ["A", "B"]}],
        "story_saved": {"A": True},
    }

    with pytest.raises(StoryPhaseError, match="1 of 2"):
        await complete_story_phase(
            load_state=lambda: _async_value(state),
            save_state=lambda _state: None,
            now_iso=lambda: "2026-05-22T00:00:00Z",
            expected_state="STORY_PERSISTENCE",
            idempotency_key="story-complete-2",
        )
```

Add:

```python
@pytest.mark.asyncio
async def test_complete_story_phase_moves_to_sprint_setup_when_all_saved() -> None:
    state = {
        "fsm_state": "STORY_PERSISTENCE",
        "roadmap_releases": [{"items": ["A", "B"]}],
        "story_saved": {"A": True, "B": True},
    }
    saved_states: list[dict[str, Any]] = []

    result = await complete_story_phase(
        load_state=lambda: _async_value(state),
        save_state=lambda next_state: saved_states.append(next_state),
        now_iso=lambda: "2026-05-22T00:00:00Z",
        expected_state="STORY_PERSISTENCE",
        idempotency_key="story-complete-2",
    )

    assert result["fsm_state"] == "SPRINT_SETUP"
    assert result["coverage"] == {"saved": 2, "merged": 0, "total": 2}
    assert saved_states[-1]["fsm_state"] == "SPRINT_SETUP"
```

- [ ] **Step 2: Run completion tests and verify failure**

Run:

```bash
uv run --frozen pytest \
  tests/test_story_phase_service.py::test_complete_story_phase_blocks_until_all_roadmap_requirements_saved \
  tests/test_story_phase_service.py::test_complete_story_phase_moves_to_sprint_setup_when_all_saved \
  -q
```

Expected: fail because `complete_story_phase` currently accepts partial story coverage and does not require guarded completion fields.

- [ ] **Step 3: Harden `complete_story_phase`**

Change the signature:

```python
async def complete_story_phase(
    *,
    load_state: Callable[[], Awaitable[dict[str, Any]]],
    save_state: Callable[[dict[str, Any]], None],
    now_iso: Callable[[], str],
    expected_state: str | None,
    idempotency_key: str | None,
) -> dict[str, Any]:
```

At the start:

```python
state = await load_state()
if expected_state != OrchestratorState.STORY_PERSISTENCE.value:
    raise StoryPhaseError(
        "story complete requires --expected-state STORY_PERSISTENCE",
        status_code=400,
    )
if idempotency_key is None or not idempotency_key.strip():
    raise StoryPhaseError("story complete requires --idempotency-key", status_code=400)
if state.get("fsm_state") != OrchestratorState.STORY_PERSISTENCE.value:
    raise StoryPhaseError(
        f"story complete requires FSM state STORY_PERSISTENCE, got {state.get('fsm_state')}",
        status_code=409,
    )
```

Compute coverage:

```python
requirements = get_all_roadmap_requirements(state)
saved = state.get("story_saved") if isinstance(state.get("story_saved"), dict) else {}
saved_count = sum(1 for req in requirements if saved.get(req))
merged_count = 0
for req in requirements:
    runtime = ensure_story_runtime(state, parent_requirement=req)
    if story_current_resolution(runtime):
        merged_count += 1
covered_count = saved_count + merged_count
if covered_count != len(requirements):
    raise StoryPhaseError(
        f"Story phase cannot complete: {covered_count} of {len(requirements)} roadmap requirements are saved or merged.",
        status_code=409,
    )
```

Then set:

```python
state["fsm_state"] = OrchestratorState.SPRINT_SETUP.value
state["story_completed_at"] = now_iso()
state.setdefault("story_complete_idempotency", {})[idempotency_key] = {
    "fsm_state": OrchestratorState.SPRINT_SETUP.value,
    "coverage": {
        "saved": saved_count,
        "merged": merged_count,
        "total": len(requirements),
    },
}
save_state(state)
return state["story_complete_idempotency"][idempotency_key]
```

- [ ] **Step 4: Verify completion tests pass**

Run:

```bash
uv run --frozen pytest \
  tests/test_story_phase_service.py::test_complete_story_phase_blocks_until_all_roadmap_requirements_saved \
  tests/test_story_phase_service.py::test_complete_story_phase_moves_to_sprint_setup_when_all_saved \
  -q
```

Expected: pass.

## Task 6: Story Runner Hydration

**Files:**
- Create: `services/agent_workbench/story_phase.py`
- Test: `tests/test_agent_workbench_story_phase.py`

- [ ] **Step 1: Write failing runner tests**

Create `tests/test_agent_workbench_story_phase.py` with:

```python
from __future__ import annotations

import pytest

from services.agent_workbench.story_phase import StoryPhaseRunner


def test_story_runner_pending_hydrates_project_state(fake_session_factory) -> None:
    runner = StoryPhaseRunner(session_factory=fake_session_factory)

    result = runner.pending(project_id=2)

    assert result["project_id"] == 2
    assert "grouped_items" in result


def test_story_runner_generate_returns_mutation_failed_on_runtime_error(
    fake_session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def failing_generate_story_draft(**_: object) -> dict[str, object]:
        raise RuntimeError("provider failed")

    monkeypatch.setattr(
        "services.agent_workbench.story_phase.generate_story_draft",
        failing_generate_story_draft,
    )
    runner = StoryPhaseRunner(session_factory=fake_session_factory)

    result = runner.generate(
        project_id=2,
        parent_requirement="Live weekly recommendation MVP",
        user_input=None,
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "MUTATION_FAILED"
```

- [ ] **Step 2: Run runner tests and verify failure**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_story_phase.py -q
```

Expected: fail because `services/agent_workbench/story_phase.py` does not exist.

- [ ] **Step 3: Create `StoryPhaseRunner`**

Create `services/agent_workbench/story_phase.py` with this structure:

```python
"""CLI-facing Story phase runner."""

from __future__ import annotations

from typing import Any

import anyio

from orchestrator_agent.agent_tools.user_story_writer_tool.tools import save_stories_tool
from services.agent_workbench.application import SessionFactory
from services.agent_workbench.errors import mutation_failed_envelope
from services.agent_workbench.phase_context import (
    build_tool_context_for_project,
    hydrate_project_context,
    load_project_state,
    save_project_state,
)
from services.phases.story_service import (
    StoryPhaseError,
    complete_story_phase,
    generate_story_draft,
    get_story_history,
    get_story_pending,
    retry_story_draft,
    save_story_draft,
)
from services.story_runtime import run_story_agent_from_state
from services.interview_runtime import (
    append_attempt,
    append_feedback_entry,
    failure_meta_from_runtime_result,
    mark_feedback_absorbed,
    promote_reusable_draft,
    set_request_projection,
)
from utils.time import utc_now_iso


class StoryPhaseRunner:
    """Runs Story phase commands behind the agent-facing CLI."""

    def __init__(self, *, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def pending(self, *, project_id: int) -> dict[str, Any]:
        async def run() -> dict[str, Any]:
            return await get_story_pending(
                load_state=lambda: load_project_state(
                    self._session_factory,
                    project_id=project_id,
                ),
            )

        data = anyio.run(run)
        return {"project_id": project_id, **data}
```

Then add `generate`, `retry`, `history`, `save`, and `complete` methods using the same pattern as `BacklogPhaseRunner` and `RoadmapPhaseRunner`: call the corresponding service function, pass `run_story_agent_from_state`, pass interview runtime helpers, pass `hydrate_project_context`, pass `build_tool_context_for_project`, and catch `StoryPhaseError` plus unexpected exceptions into the same `MUTATION_FAILED` envelope style already used by phase runners.

- [ ] **Step 4: Verify runner tests pass**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_story_phase.py -q
```

Expected: pass.

## Task 7: CLI, Application, Registry, and Workflow Routing

**Files:**
- Modify: `services/agent_workbench/application.py`
- Modify: `services/agent_workbench/command_registry.py`
- Modify: `cli/main.py`
- Test: `tests/test_agent_workbench_application.py`
- Test: `tests/test_agent_workbench_cli.py`
- Test: `tests/test_agent_workbench_command_schema.py`

- [ ] **Step 1: Write failing CLI parser tests**

Add to `tests/test_agent_workbench_cli.py`:

```python
def test_story_generate_cli_routes_to_application() -> None:
    result = invoke_cli(
        [
            "story",
            "generate",
            "--project-id",
            "2",
            "--parent-requirement",
            "Live weekly recommendation MVP",
        ],
        application=FakeApplication(),
    )

    assert result.command == "agileforge story generate"
    assert result.payload["called"] == "story_generate"
```

```python
def test_story_save_cli_requires_guard_fields() -> None:
    result = invoke_cli(
        [
            "story",
            "save",
            "--project-id",
            "2",
            "--parent-requirement",
            "Live weekly recommendation MVP",
        ],
        application=FakeApplication(),
        expect_system_exit=True,
    )

    assert result.exit_code == 2
```

- [ ] **Step 2: Write failing workflow-next test**

Add to `tests/test_agent_workbench_application.py`:

```python
def test_workflow_next_routes_roadmap_persistence_to_story_pending(
    application: AgentWorkbenchApplication,
    project_with_state,
) -> None:
    project = project_with_state(
        {
            "fsm_state": "ROADMAP_PERSISTENCE",
            "roadmap_releases": [{"items": ["Live weekly recommendation MVP"]}],
        }
    )

    payload = application.workflow_next(project_id=project.project_id)

    commands = [item["command"] for item in payload["next_actions"]]
    assert f"agileforge story pending --project-id {project.project_id}" in commands
```

Add:

```python
def test_workflow_next_routes_story_persistence_to_complete_when_all_saved(
    application: AgentWorkbenchApplication,
    project_with_state,
) -> None:
    project = project_with_state(
        {
            "fsm_state": "STORY_PERSISTENCE",
            "roadmap_releases": [{"items": ["A"]}],
            "story_saved": {"A": True},
        }
    )

    payload = application.workflow_next(project_id=project.project_id)

    commands = [item["command"] for item in payload["next_actions"]]
    assert any(command.startswith("agileforge story complete") for command in commands)
```

- [ ] **Step 3: Run CLI/application tests and verify failure**

Run:

```bash
uv run --frozen pytest \
  tests/test_agent_workbench_cli.py::test_story_generate_cli_routes_to_application \
  tests/test_agent_workbench_cli.py::test_story_save_cli_requires_guard_fields \
  tests/test_agent_workbench_application.py::test_workflow_next_routes_roadmap_persistence_to_story_pending \
  tests/test_agent_workbench_application.py::test_workflow_next_routes_story_persistence_to_complete_when_all_saved \
  -q
```

Expected: fail because Story CLI generate/save/complete are not installed.

- [ ] **Step 4: Add application facade methods**

In `services/agent_workbench/application.py`, add protocol methods matching:

```python
def story_pending(self, *, project_id: int) -> dict[str, Any]:
    raise NotImplementedError


def story_generate(
    self,
    *,
    project_id: int,
    parent_requirement: str,
    user_input: str | None,
) -> dict[str, Any]:
    raise NotImplementedError


def story_retry(self, *, project_id: int, parent_requirement: str) -> dict[str, Any]:
    raise NotImplementedError


def story_history(self, *, project_id: int, parent_requirement: str) -> dict[str, Any]:
    raise NotImplementedError


def story_save(
    self,
    *,
    project_id: int,
    parent_requirement: str,
    attempt_id: str,
    expected_artifact_fingerprint: str,
    expected_state: str,
    idempotency_key: str,
) -> dict[str, Any]:
    raise NotImplementedError


def story_complete(
    self,
    *,
    project_id: int,
    expected_state: str,
    idempotency_key: str,
) -> dict[str, Any]:
    raise NotImplementedError
```

Add `_get_story_runner()` and delegate each method to `StoryPhaseRunner`.

- [ ] **Step 5: Add CLI parsers and handlers**

In `cli/main.py`, keep `story show` and add:

```python
story_pending = story_sub.add_parser("pending", help="Show Story phase pending Roadmap requirements.")
story_pending.add_argument("--project-id", type=int, required=True)
story_pending.set_defaults(command_handler=_story_pending)

story_generate = story_sub.add_parser("generate", help="Generate or refine stories for one Roadmap requirement.")
story_generate.add_argument("--project-id", type=int, required=True)
story_generate.add_argument("--parent-requirement", required=True)
story_generate.add_argument("--input", dest="user_input", default=None)
story_generate.set_defaults(command_handler=_story_generate)

story_retry = story_sub.add_parser("retry", help="Retry the last retryable Story runtime failure.")
story_retry.add_argument("--project-id", type=int, required=True)
story_retry.add_argument("--parent-requirement", required=True)
story_retry.set_defaults(command_handler=_story_retry)

story_history = story_sub.add_parser("history", help="Show Story attempt history for one Roadmap requirement.")
story_history.add_argument("--project-id", type=int, required=True)
story_history.add_argument("--parent-requirement", required=True)
story_history.set_defaults(command_handler=_story_history)

story_save = story_sub.add_parser("save", help="Persist the reviewed Story draft for one Roadmap requirement.")
story_save.add_argument("--project-id", type=int, required=True)
story_save.add_argument("--parent-requirement", required=True)
story_save.add_argument("--attempt-id", required=True)
story_save.add_argument("--expected-artifact-fingerprint", required=True)
story_save.add_argument("--expected-state", required=True)
story_save.add_argument("--idempotency-key", required=True)
story_save.set_defaults(command_handler=_story_save)

story_complete = story_sub.add_parser("complete", help="Complete Story phase after all Roadmap requirements are saved.")
story_complete.add_argument("--project-id", type=int, required=True)
story_complete.add_argument("--expected-state", required=True)
story_complete.add_argument("--idempotency-key", required=True)
story_complete.set_defaults(command_handler=_story_complete)
```

Add handlers that return exact command names:

```python
def _story_generate(args: argparse.Namespace, application: _Application) -> CommandResult:
    return "agileforge story generate", application.story_generate(
        project_id=args.project_id,
        parent_requirement=args.parent_requirement,
        user_input=args.user_input,
    )
```

Repeat the same direct routing for pending, retry, history, save, and complete.

- [ ] **Step 6: Register Story commands**

In `services/agent_workbench/command_registry.py`, register:

```python
CommandContract(
    name="agileforge story pending",
    phase="story",
    mutates=False,
    input_required=("project_id",),
)
CommandContract(
    name="agileforge story generate",
    phase="story",
    mutates=True,
    input_required=("project_id", "parent_requirement"),
    input_optional=("input",),
)
CommandContract(
    name="agileforge story retry",
    phase="story",
    mutates=True,
    input_required=("project_id", "parent_requirement"),
)
CommandContract(
    name="agileforge story history",
    phase="story",
    mutates=False,
    input_required=("project_id", "parent_requirement"),
)
CommandContract(
    name="agileforge story save",
    phase="story",
    mutates=True,
    input_required=(
        "project_id",
        "parent_requirement",
        "attempt_id",
        "expected_artifact_fingerprint",
        "expected_state",
        "idempotency_key",
    ),
)
CommandContract(
    name="agileforge story complete",
    phase="story",
    mutates=True,
    input_required=("project_id", "expected_state", "idempotency_key"),
)
```

- [ ] **Step 7: Update workflow-next routing**

In `services/agent_workbench/application.py`, route:

```python
if fsm_state == OrchestratorState.ROADMAP_PERSISTENCE.value:
    next_actions.append(
        {
            "command": f"agileforge story pending --project-id {project_id}",
            "installed": command_is_available("agileforge story pending"),
            "reason": "Inspect Roadmap requirements that still need user stories.",
            "requires_cli_installation": not command_is_available("agileforge story pending"),
        }
    )
```

For `STORY_INTERVIEW`, route to `story generate` with the first pending requirement from `_story_pending_items`.

For `STORY_REVIEW`, route to `story history`, `story generate --input`, and guarded `story save` using the current draft guard fields.

For `STORY_PERSISTENCE`, route to the next unsaved requirement if coverage is incomplete; route to guarded `story complete` if coverage is complete.

For `SPRINT_SETUP`, preserve existing `agileforge sprint candidates --project-id {project_id}` routing.

- [ ] **Step 8: Verify CLI/application/registry tests pass**

Run:

```bash
uv run --frozen pytest \
  tests/test_agent_workbench_cli.py \
  tests/test_agent_workbench_application.py \
  tests/test_agent_workbench_command_schema.py \
  -q
```

Expected: pass.

## Task 8: API Parity

**Files:**
- Modify: `api.py`
- Test: `tests/test_api_story_flow.py`

- [ ] **Step 1: Write failing API guarded-save test**

Create or extend `tests/test_api_story_flow.py`:

```python
def test_story_save_api_requires_attempt_guards(client) -> None:
    response = client.post(
        "/api/projects/2/story/save",
        params={"parent_requirement": "Live weekly recommendation MVP"},
        json={},
    )

    assert response.status_code == 422
```

Add:

```python
def test_story_save_api_accepts_attempt_guards(client, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_save_story_draft(**kwargs: object) -> dict[str, object]:
        assert kwargs["attempt_id"] == "attempt-1"
        assert kwargs["expected_artifact_fingerprint"] == "sha256:abc"
        assert kwargs["expected_state"] == "STORY_REVIEW"
        assert kwargs["idempotency_key"] == "story-save-2-live"
        return {"success": True, "fsm_state": "STORY_PERSISTENCE"}

    monkeypatch.setattr("api.save_story_draft_service", fake_save_story_draft)

    response = client.post(
        "/api/projects/2/story/save",
        params={"parent_requirement": "Live weekly recommendation MVP"},
        json={
            "attempt_id": "attempt-1",
            "expected_artifact_fingerprint": "sha256:abc",
            "expected_state": "STORY_REVIEW",
            "idempotency_key": "story-save-2-live",
        },
    )

    assert response.status_code == 200
```

- [ ] **Step 2: Run API tests and verify failure**

Run:

```bash
uv run --frozen pytest tests/test_api_story_flow.py -q
```

Expected: fail because Story save API does not use guard request fields yet.

- [ ] **Step 3: Add request model and pass guards**

In `api.py`, add:

```python
class StorySaveRequest(BaseModel):
    attempt_id: str
    expected_artifact_fingerprint: str
    expected_state: str
    idempotency_key: str
```

Change the save endpoint:

```python
async def save_story(
    project_id: int,
    parent_requirement: str,
    request: StorySaveRequest,
) -> dict[str, Any]:
```

Pass:

```python
attempt_id=request.attempt_id,
expected_artifact_fingerprint=request.expected_artifact_fingerprint,
expected_state=request.expected_state,
idempotency_key=request.idempotency_key,
```

- [ ] **Step 4: Verify API tests pass**

Run:

```bash
uv run --frozen pytest tests/test_api_story_flow.py -q
```

Expected: pass.

## Task 9: Documentation and Live caRtola Verification

**Files:**
- Modify: `docs/agent-cli-manual.md`

- [ ] **Step 1: Update manual feature list**

In `docs/agent-cli-manual.md`, change the installed phase list from:

```markdown
- Generating or saving story or sprint drafts.
```

to:

```markdown
- Sprint draft generation and persistence.
```

Add:

```markdown
- Story pending, generate, retry, history, save, and complete.
```

- [ ] **Step 2: Add Story phase command section**

Add a Story section after Roadmap:

```markdown
## Story Phase

Story generation is per Roadmap requirement. Always inspect pending work first:

```bash
agileforge story pending --project-id "$PROJECT_ID" | python -m json.tool
```

Generate the first draft for one requirement:

```bash
agileforge story generate \
  --project-id "$PROJECT_ID" \
  --parent-requirement "Live weekly recommendation MVP" | python -m json.tool
```

Use reviewer feedback only after a draft exists or the Story runtime asks blocking questions:

```bash
agileforge story generate \
  --project-id "$PROJECT_ID" \
  --parent-requirement "Live weekly recommendation MVP" \
  --input "Keep this story focused on the usable live MVP. Do not include model promotion gates here." | python -m json.tool
```

Inspect the exact reviewed attempt:

```bash
agileforge story history \
  --project-id "$PROJECT_ID" \
  --parent-requirement "Live weekly recommendation MVP" | python -m json.tool
```

Save only the reviewed attempt and fingerprint returned by generate/history:

```bash
agileforge story save \
  --project-id "$PROJECT_ID" \
  --parent-requirement "Live weekly recommendation MVP" \
  --attempt-id "attempt-1" \
  --expected-artifact-fingerprint "<artifact_fingerprint from history>" \
  --expected-state STORY_REVIEW \
  --idempotency-key "story-save-$PROJECT_ID-live-weekly-recommendation-mvp" | python -m json.tool
```

After every Roadmap requirement is saved or merged, complete Story phase:

```bash
agileforge story complete \
  --project-id "$PROJECT_ID" \
  --expected-state STORY_PERSISTENCE \
  --idempotency-key "story-complete-$PROJECT_ID" | python -m json.tool
```
```

- [ ] **Step 3: Run documentation grep checks**

Run:

```bash
rg -n "story generate|story save|story complete|Sprint draft generation" docs/agent-cli-manual.md
```

Expected: all Story commands are documented, and the manual no longer says Story CLI is uninstalled.

- [ ] **Step 4: Run focused test suite**

Run:

```bash
uv run --frozen pytest \
  tests/test_story_runtime.py \
  tests/test_story_phase_service.py \
  tests/test_save_stories_tool.py \
  tests/test_agent_workbench_story_phase.py \
  tests/test_agent_workbench_application.py \
  tests/test_agent_workbench_cli.py \
  tests/test_agent_workbench_command_schema.py \
  tests/test_api_story_flow.py \
  -q
```

Expected: pass.

- [ ] **Step 5: Run full regression checks**

Run:

```bash
uv run --frozen pytest -q
uv run --frozen ruff check .
```

Expected: pytest passes, ruff check passes.

- [ ] **Step 6: Verify live caRtola flow**

Run in `/Users/aaat/projects/caRtola`:

```bash
cd /Users/aaat/projects/caRtola
PROJECT_ID=2

agileforge workflow next --project-id "$PROJECT_ID" | python -m json.tool
agileforge story pending --project-id "$PROJECT_ID" | python -m json.tool
```

Pick the first pending requirement from the output, then run:

```bash
agileforge story generate \
  --project-id "$PROJECT_ID" \
  --parent-requirement "Live weekly recommendation MVP" | python -m json.tool
```

If the draft is strategically wrong or asks clarifying questions, refine:

```bash
agileforge story generate \
  --project-id "$PROJECT_ID" \
  --parent-requirement "Live weekly recommendation MVP" \
  --input "Keep this requirement limited to the live weekly recommendation MVP. Exclude M009 promotion gates and governance hardening." | python -m json.tool
```

Inspect guard fields:

```bash
agileforge story history \
  --project-id "$PROJECT_ID" \
  --parent-requirement "Live weekly recommendation MVP" | python -m json.tool
```

Save the reviewed attempt:

```bash
agileforge story save \
  --project-id "$PROJECT_ID" \
  --parent-requirement "Live weekly recommendation MVP" \
  --attempt-id "<attempt_id from history>" \
  --expected-artifact-fingerprint "<artifact_fingerprint from history>" \
  --expected-state STORY_REVIEW \
  --idempotency-key "story-save-2-live-weekly-recommendation-mvp" | python -m json.tool
```

Repeat generate/review/save for each pending Roadmap requirement. After `story pending` reports all requirements saved or merged:

```bash
agileforge story complete \
  --project-id "$PROJECT_ID" \
  --expected-state STORY_PERSISTENCE \
  --idempotency-key "story-complete-2" | python -m json.tool

agileforge workflow next --project-id "$PROJECT_ID" | python -m json.tool
agileforge sprint candidates --project-id "$PROJECT_ID" | python -m json.tool
```

Expected: `story complete` moves the FSM to `SPRINT_SETUP`, and `workflow next` exposes the installed Sprint candidate command.

## Self-Review

- Spec coverage: this plan installs every command needed to execute the Story phase via CLI, preserves fail-closed behavior, requires attempt-aware saves, blocks unsafe persistence replacement, and hands off to the already-installed Sprint candidate command.
- Placeholder scan: the plan contains no deferred placeholders; every task names concrete files, commands, tests, and expected results.
- Type consistency: command names, guard field names, and FSM states match the existing Vision/Backlog/Roadmap pattern and the current `OrchestratorState` naming style.
