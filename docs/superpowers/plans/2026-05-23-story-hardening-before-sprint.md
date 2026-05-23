# Story Hardening Before Sprint Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden the Story phase before Sprint planning by fixing CLI envelope shape, rejecting unusable incomplete drafts, and adding a guarded Story correction path for saved stories that have not progressed downstream.

**Architecture:** Keep the existing Story pipeline intact: CLI -> `AgentWorkbenchApplication` -> `StoryPhaseRunner` -> `services/phases/story_service.py` -> runtime/tool boundary. Fix the CLI-only envelope nesting at the `StoryPhaseRunner` success-envelope boundary, enforce Story draft usability inside `services/story_runtime.py`, and add a safe correction command that reopens one saved Story requirement only when no Sprint downstream links exist. API response shape remains stable unless a test proves it already exposes the same defect.

**Tech Stack:** Python 3.13, Pydantic, SQLModel, AnyIO, existing AgileForge CLI envelopes, existing Story runtime/services, existing `save_stories_tool` replacement safety.

---

## File Structure

- Modify `services/agent_workbench/story_phase.py`: flatten Story phase service payloads before the CLI envelope wraps them; add runner method for guarded correction/reopen.
- Modify `services/agent_workbench/application.py`: expose `story_reopen` and route `workflow next` away from Sprint if a reopened Story is under review.
- Modify `services/agent_workbench/command_registry.py`: register the Story correction command contract.
- Modify `cli/main.py`: add `agileforge story reopen` command with explicit guards.
- Modify `services/story_runtime.py`: reject `is_complete=false` outputs that do not include at least one useful clarifying question.
- Modify `services/phases/story_service.py`: add reopen/correction service that clears saved projection and resets the specific Story runtime only if replacement is safe.
- Modify `orchestrator_agent/agent_tools/user_story_writer_tool/instructions.txt`: instruct the model that incomplete output must ask concrete, answerable questions, and that generic/non-actionable questions are invalid.
- Modify `docs/agent-cli-manual.md`: document Story correction before Sprint planning and the corrected Story CLI output shape.
- Tests:
  - `tests/test_story_runtime.py`
  - `tests/test_story_phase_service.py`
  - `tests/test_agent_workbench_story_phase.py`
  - `tests/test_agent_workbench_application.py`
  - `tests/test_agent_workbench_cli.py`
  - `tests/test_agent_workbench_command_schema.py`
  - `tests/test_api_story_interview_flow.py` only if API parity needs explicit coverage.

## Behavior Contract

- `agileforge story generate` success output must expose `data.output_artifact`, `data.current_draft`, `data.save`, `data.retry`, and `data.resolution`.
- `agileforge story history` success output must expose `data.items`, `data.count`, `data.current_draft`, `data.save`, `data.retry`, and `data.resolution`.
- `agileforge story save` success output must expose `data.save_result`, `data.attempt_id`, `data.artifact_fingerprint`, `data.parent_requirement`, and `data.fsm_state`.
- Story runtime must not return a reusable incomplete draft unless it includes at least one actionable clarifying question.
- Generic questions such as "Please clarify requirements", "What should happen?", or empty/whitespace questions are not actionable.
- Reopening a saved Story is allowed only before Sprint work exists for that Story.
- Reopening a Story clears `story_saved[parent_requirement]`, resets that requirement's Story runtime, leaves persisted old rows replaceable, and moves FSM back to `STORY_INTERVIEW`.
- Existing `story save` replacement logic remains the canonical atomic replacement path.

## Task 1: Flatten Story CLI Success Envelopes

**Files:**
- Modify: `services/agent_workbench/story_phase.py`
- Test: `tests/test_agent_workbench_story_phase.py`
- Test: `tests/test_agent_workbench_cli.py`

- [ ] **Step 1: Write failing runner tests**

In `tests/test_agent_workbench_story_phase.py`, update the existing generate assertion that currently expects `result["data"]["data"]["output_artifact"]`.

Use this assertion shape:

```python
assert result["ok"] is True
assert result["data"]["output_artifact"]["parent_requirement"] == (
    "Review match result"
)
assert "data" not in result["data"]
```

Add a save assertion to `test_story_save_passes_guard_fields`:

```python
assert result["ok"] is True
assert result["data"]["save_result"] == {"success": True}
assert result["data"]["attempt_id"] == "attempt-1"
assert result["data"]["artifact_fingerprint"] == "sha256:abc"
assert result["data"]["fsm_state"] == "STORY_PERSISTENCE"
assert "data" not in result["data"]
```

- [ ] **Step 2: Write failing CLI envelope tests**

In `tests/test_agent_workbench_cli.py`, add CLI tests using the fake application object already used by Story CLI tests:

```python
def test_story_generate_cli_flattens_phase_data(capsys: pytest.CaptureFixture[str]) -> None:
    app = _FakeApplication()
    app.results["story_generate"] = {
        "ok": True,
        "data": {
            "fsm_state": "STORY_REVIEW",
            "parent_requirement": "Requirement A",
            "output_artifact": {"parent_requirement": "Requirement A"},
            "current_draft": {"attempt_id": "attempt-1"},
            "save": {"available": True},
            "retry": {"available": False},
            "resolution": {"available": False},
        },
        "warnings": [],
        "errors": [],
    }

    exit_code = main(
        [
            "story",
            "generate",
            "--project-id",
            "7",
            "--parent-requirement",
            "Requirement A",
        ],
        application=app,
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["output_artifact"]["parent_requirement"] == "Requirement A"
    assert "data" not in payload["data"]
```

Add a similar save test:

```python
def test_story_save_cli_flattens_save_result(capsys: pytest.CaptureFixture[str]) -> None:
    app = _FakeApplication()
    app.results["story_save"] = {
        "ok": True,
        "data": {
            "parent_requirement": "Requirement A",
            "attempt_id": "attempt-1",
            "artifact_fingerprint": "sha256:abc",
            "fsm_state": "STORY_PERSISTENCE",
            "save_result": {"success": True, "saved_count": 1},
        },
        "warnings": [],
        "errors": [],
    }

    exit_code = main(
        [
            "story",
            "save",
            "--project-id",
            "7",
            "--parent-requirement",
            "Requirement A",
            "--attempt-id",
            "attempt-1",
            "--expected-artifact-fingerprint",
            "sha256:abc",
            "--expected-state",
            "STORY_REVIEW",
            "--idempotency-key",
            "story-save-7-a",
        ],
        application=app,
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["save_result"]["saved_count"] == 1
    assert "data" not in payload["data"]
```

- [ ] **Step 3: Run failing tests**

Run:

```bash
uv run --frozen pytest \
  tests/test_agent_workbench_story_phase.py::test_story_generate_hydrates_spec_authority_and_roadmap \
  tests/test_agent_workbench_story_phase.py::test_story_save_passes_guard_fields \
  tests/test_agent_workbench_cli.py::test_story_generate_cli_flattens_phase_data \
  tests/test_agent_workbench_cli.py::test_story_save_cli_flattens_save_result \
  -q
```

Expected: fail because `StoryPhaseRunner._data_envelope` currently preserves the nested service `data` object.

- [ ] **Step 4: Implement CLI-only flatten helper**

In `services/agent_workbench/story_phase.py`, replace `_data_envelope` with:

```python
def _flatten_phase_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Flatten phase service payloads for CLI consumers."""
    payload: dict[str, Any] = {
        str(key): value for key, value in data.items() if key != "data"
    }
    inner = data.get("data")
    if isinstance(inner, dict):
        payload.update({str(key): value for key, value in inner.items()})
    return payload


def _data_envelope(data: dict[str, Any]) -> dict[str, Any]:
    """Return application facade success envelope."""
    return {"ok": True, "data": _flatten_phase_payload(data), "warnings": [], "errors": []}
```

- [ ] **Step 5: Verify Story CLI flattening tests pass**

Run the same command from Step 3.

Expected: pass.

## Task 2: Reject Incomplete Story Drafts Without Useful Questions

**Files:**
- Modify: `services/story_runtime.py`
- Modify: `orchestrator_agent/agent_tools/user_story_writer_tool/instructions.txt`
- Test: `tests/test_story_runtime.py`

- [ ] **Step 1: Write failing tests for unusable incomplete drafts**

In `tests/test_story_runtime.py`, add:

```python
@pytest.mark.asyncio
async def test_story_runtime_rejects_incomplete_without_questions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_invoke_story_agent(_payload: UserStoryWriterInput) -> str:
        return json.dumps(
            {
                "parent_requirement": "Budget-bound live workflow",
                "user_stories": [_valid_story("Budget story")],
                "is_complete": False,
                "clarifying_questions": [],
            }
        )

    monkeypatch.setattr(
        "services.story_runtime._invoke_story_agent",
        fake_invoke_story_agent,
    )

    result = await run_story_agent_from_state(
        {
            "roadmap_releases": [{"items": ["Budget-bound live workflow"]}],
            "pending_spec_content": "{}",
            "compiled_authority_cached": "{}",
        },
        project_id=2,
        parent_requirement="Budget-bound live workflow",
        user_input=None,
    )

    assert result["success"] is False
    assert result["classification"] == "nonreusable_schema_failure"
    assert result["failure_stage"] == "output_validation"
    assert "clarifying question" in result["failure_summary"].lower()
```

Add:

```python
@pytest.mark.asyncio
async def test_story_runtime_rejects_generic_clarifying_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_invoke_story_agent(_payload: UserStoryWriterInput) -> str:
        return json.dumps(
            {
                "parent_requirement": "Budget-bound live workflow",
                "user_stories": [_valid_story("Budget story")],
                "is_complete": False,
                "clarifying_questions": ["Please clarify the requirements."],
            }
        )

    monkeypatch.setattr(
        "services.story_runtime._invoke_story_agent",
        fake_invoke_story_agent,
    )

    result = await run_story_agent_from_state(
        {
            "roadmap_releases": [{"items": ["Budget-bound live workflow"]}],
            "pending_spec_content": "{}",
            "compiled_authority_cached": "{}",
        },
        project_id=2,
        parent_requirement="Budget-bound live workflow",
        user_input=None,
    )

    assert result["success"] is False
    assert result["classification"] == "nonreusable_schema_failure"
    assert result["failure_stage"] == "output_validation"
    assert "actionable" in result["failure_summary"].lower()
```

If `_valid_story` does not exist, add this helper near the top of `tests/test_story_runtime.py`:

```python
def _valid_story(title: str) -> dict[str, Any]:
    return {
        "story_title": title,
        "statement": (
            "As a Cartola operator, I want a validated live recommendation, "
            "so that I can review it before market lock."
        ),
        "acceptance_criteria": [
            "Verify that the recommendation artifact records the selected squad."
        ],
        "invest_score": "High",
        "estimated_effort": "M",
        "produced_artifacts": ["recommendation_artifact"],
    }
```

- [ ] **Step 2: Run failing runtime tests**

Run:

```bash
uv run --frozen pytest \
  tests/test_story_runtime.py::test_story_runtime_rejects_incomplete_without_questions \
  tests/test_story_runtime.py::test_story_runtime_rejects_generic_clarifying_question \
  -q
```

Expected: fail because the runtime currently accepts reusable incomplete drafts with empty or generic questions.

- [ ] **Step 3: Implement actionable-question validation**

In `services/story_runtime.py`, add:

```python
_GENERIC_CLARIFYING_QUESTIONS = {
    "please clarify the requirements",
    "please clarify the requirement",
    "what should happen",
    "what is expected",
    "need more details",
    "clarify requirements",
    "clarify the requirements",
}


def _normalized_question_text(question: str) -> str:
    return " ".join(question.strip().rstrip(".?").lower().split())


def _actionable_clarifying_questions(questions: list[str]) -> list[str]:
    actionable: list[str] = []
    for question in questions:
        if not isinstance(question, str):
            continue
        stripped = question.strip()
        if not stripped:
            continue
        normalized = _normalized_question_text(stripped)
        if normalized in _GENERIC_CLARIFYING_QUESTIONS:
            continue
        if len(stripped.split()) < 5:
            continue
        actionable.append(stripped)
    return actionable


def _validate_story_output_consistency(
    output: UserStoryWriterOutput,
    *,
    raw_text: str,
    project_id: int,
    parent_requirement: str,
    input_context: StoryInputContext,
) -> dict[str, Any] | None:
    if output.is_complete:
        return None

    actionable_questions = _actionable_clarifying_questions(
        output.clarifying_questions
    )
    if actionable_questions:
        return None

    return _with_failure_metadata(
        _failure(
            project_id=project_id,
            parent_requirement=parent_requirement,
            input_context=input_context,
            failure_stage="output_validation",
            details=_FailureDetails(
                message=(
                    "Story output validation failed: incomplete drafts must include "
                    "at least one actionable clarifying question."
                ),
                raw_text=raw_text,
            ),
        ),
        classification="nonreusable_schema_failure",
        draft_kind=None,
        is_reusable=False,
        request_payload=input_context,
    )
```

After `output_model = UserStoryWriterOutput.model_validate(parsed)`, call:

```python
consistency_failure = _validate_story_output_consistency(
    output_model,
    raw_text=raw_text,
    project_id=project_id,
    parent_requirement=parent_requirement,
    input_context=request_payload,
)
if consistency_failure is not None:
    return consistency_failure
```

- [ ] **Step 4: Update Story writer prompt**

In `orchestrator_agent/agent_tools/user_story_writer_tool/instructions.txt`, under `### 5. Failure Handling`, add:

```text
* If output is incomplete, set `is_complete` to false and include at least one concrete clarifying question that the reviewer can answer directly.
* Do NOT use generic questions such as "Please clarify the requirements" or "What should happen?".
* A valid clarifying question must name the missing decision, artifact, command, contract, threshold, or source of truth needed to finish the story safely.
```

- [ ] **Step 5: Verify runtime validation tests pass**

Run:

```bash
uv run --frozen pytest \
  tests/test_story_runtime.py::test_story_runtime_rejects_incomplete_without_questions \
  tests/test_story_runtime.py::test_story_runtime_rejects_generic_clarifying_question \
  tests/test_story_runtime.py::test_story_runtime_forces_incomplete_when_clarifying_questions_remain \
  -q
```

Expected: pass.

## Task 3: Add Guarded Story Reopen Command

**Files:**
- Modify: `services/phases/story_service.py`
- Modify: `services/agent_workbench/story_phase.py`
- Modify: `services/agent_workbench/application.py`
- Modify: `services/agent_workbench/command_registry.py`
- Modify: `cli/main.py`
- Test: `tests/test_story_phase_service.py`
- Test: `tests/test_agent_workbench_story_phase.py`
- Test: `tests/test_agent_workbench_application.py`
- Test: `tests/test_agent_workbench_cli.py`
- Test: `tests/test_agent_workbench_command_schema.py`

- [ ] **Step 1: Choose command name and contract**

Use this command:

```bash
agileforge story reopen \
  --project-id <project_id> \
  --parent-requirement <parent_requirement> \
  --expected-state SPRINT_SETUP \
  --idempotency-key <idempotency_key>
```

The command reopens exactly one saved Story requirement before Sprint planning. It does not edit the story itself. After reopen, the normal flow is:

```bash
agileforge story generate --project-id <project_id> --parent-requirement <parent_requirement> --input "<correction>"
agileforge story save --project-id <project_id> --parent-requirement <parent_requirement> --attempt-id <attempt_id> --expected-artifact-fingerprint <fingerprint> --expected-state STORY_REVIEW --idempotency-key <idempotency_key>
agileforge story complete --project-id <project_id> --expected-state STORY_PERSISTENCE --idempotency-key <idempotency_key>
```

- [ ] **Step 2: Write failing service test for safe reopen**

In `tests/test_story_phase_service.py`, add:

```python
@pytest.mark.asyncio
async def test_reopen_story_requirement_clears_saved_projection_before_sprint_work() -> None:
    parent_requirement = "Live Pre-Lock Recommendation Workflow with Risk-Audited Artifact"
    state: dict[str, Any] = {
        "fsm_state": "SPRINT_SETUP",
        "roadmap_releases": [{"items": [parent_requirement]}],
        "story_saved": {parent_requirement: True},
        "story_outputs": {parent_requirement: _story_artifact(parent_requirement, "Old story")},
        "interview_runtime": {
            "story": {
                parent_requirement: {
                    "draft_projection": {"latest_reusable_attempt_id": "attempt-1"},
                    "attempt_history": [
                        {
                            "attempt_id": "attempt-1",
                            "trigger": "manual_refine",
                            "output_artifact": _story_artifact(parent_requirement, "Old story"),
                        }
                    ],
                }
            }
        },
    }
    saved_states: list[dict[str, Any]] = []

    payload = await reopen_story_requirement(
        parent_requirement=parent_requirement,
        expected_state="SPRINT_SETUP",
        idempotency_key="reopen-story-live-budget",
        load_state=lambda: _async_value(state),
        save_state=lambda updated: saved_states.append(dict(updated)),
        now_iso=lambda: "2026-05-23T12:00:00Z",
        assert_reopen_safe=lambda normalized_requirement: None,
        reset_subject_working_set=reset_subject_working_set,
    )

    assert payload == {
        "parent_requirement": parent_requirement,
        "fsm_state": "STORY_INTERVIEW",
        "idempotency_key": "reopen-story-live-budget",
    }
    assert state["fsm_state"] == "STORY_INTERVIEW"
    assert parent_requirement not in state["story_saved"]
    assert parent_requirement not in state["story_outputs"]
    runtime = state["interview_runtime"]["story"][parent_requirement]
    assert runtime["draft_projection"] == {}
    assert saved_states
```

- [ ] **Step 3: Write failing service test for unsafe downstream work**

Add:

```python
@pytest.mark.asyncio
async def test_reopen_story_requirement_blocks_when_downstream_work_exists() -> None:
    parent_requirement = "Live Pre-Lock Recommendation Workflow with Risk-Audited Artifact"
    state: dict[str, Any] = {
        "fsm_state": "SPRINT_SETUP",
        "roadmap_releases": [{"items": [parent_requirement]}],
        "story_saved": {parent_requirement: True},
    }

    with pytest.raises(StoryPhaseError) as excinfo:
        await reopen_story_requirement(
            parent_requirement=parent_requirement,
            expected_state="SPRINT_SETUP",
            idempotency_key="reopen-story-live-budget",
            load_state=lambda: _async_value(state),
            save_state=lambda _updated: None,
            now_iso=lambda: "2026-05-23T12:00:00Z",
            assert_reopen_safe=lambda _normalized_requirement: (_ for _ in ()).throw(
                StoryPhaseError(
                    "Story correction is unsafe: story has sprint links.",
                    status_code=409,
                )
            ),
            reset_subject_working_set=reset_subject_working_set,
        )

    assert excinfo.value.status_code == 409
    assert "unsafe" in excinfo.value.detail.lower()
    assert state["fsm_state"] == "SPRINT_SETUP"
```

- [ ] **Step 4: Implement service function**

In `services/phases/story_service.py`, add:

```python
async def reopen_story_requirement(
    *,
    parent_requirement: str,
    expected_state: str | None,
    idempotency_key: str | None,
    load_state: Callable[[], Awaitable[dict[str, Any]]],
    save_state: Callable[[dict[str, Any]], None],
    now_iso: Callable[[], str],
    assert_reopen_safe: Callable[[str], None],
    reset_subject_working_set: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    if expected_state != OrchestratorState.SPRINT_SETUP.value:
        raise StoryPhaseError(
            "story reopen requires --expected-state SPRINT_SETUP",
            status_code=400,
        )
    if idempotency_key is None or not idempotency_key.strip():
        raise StoryPhaseError("story reopen requires --idempotency-key", status_code=400)

    state = await load_state()
    normalized_idempotency_key = idempotency_key.strip()
    idempotency_registry = state.get("story_reopen_idempotency")
    if isinstance(idempotency_registry, dict):
        existing = idempotency_registry.get(normalized_idempotency_key)
        if isinstance(existing, dict):
            return dict(existing)

    current_state = _normalize_fsm_state(state.get("fsm_state"))
    if current_state != OrchestratorState.SPRINT_SETUP.value:
        raise StoryPhaseError(
            "Story correction can reopen only from SPRINT_SETUP.",
            status_code=409,
        )

    normalized_parent_requirement = _normalize_story_requirement(
        state,
        parent_requirement,
    )
    assert_reopen_safe(normalized_parent_requirement)

    story_saved = state.get("story_saved")
    if isinstance(story_saved, dict):
        story_saved.pop(normalized_parent_requirement, None)

    story_outputs = state.get("story_outputs")
    if isinstance(story_outputs, dict):
        story_outputs.pop(normalized_parent_requirement, None)

    runtime = ensure_story_runtime(
        state,
        parent_requirement=normalized_parent_requirement,
    )
    reset_subject_working_set(
        runtime,
        created_at=now_iso(),
        summary="Story reopened for correction before Sprint planning.",
    )
    sync_story_legacy_mirrors(
        state,
        parent_requirement=normalized_parent_requirement,
        runtime=runtime,
    )

    state["fsm_state"] = OrchestratorState.STORY_INTERVIEW.value
    state["fsm_state_entered_at"] = now_iso()
    payload = {
        "parent_requirement": normalized_parent_requirement,
        "fsm_state": OrchestratorState.STORY_INTERVIEW.value,
        "idempotency_key": normalized_idempotency_key,
    }
    if not isinstance(idempotency_registry, dict):
        idempotency_registry = {}
        state["story_reopen_idempotency"] = idempotency_registry
    idempotency_registry[normalized_idempotency_key] = payload
    save_state(state)
    return payload
```

- [ ] **Step 5: Add downstream safety adapter in StoryPhaseRunner**

In `services/agent_workbench/story_phase.py`, import:

```python
from sqlmodel import Session, select
from models.core import SprintStory, UserStory
from orchestrator_agent.agent_tools.story_linkage import normalize_requirement_key
from database import get_engine
from services.interview_runtime import reset_subject_working_set
from services.phases.story_service import reopen_story_requirement
```

Add a runner method:

```python
def reopen(
    self,
    *,
    project_id: int,
    parent_requirement: str,
    expected_state: str,
    idempotency_key: str,
) -> dict[str, Any]:
    """Reopen one saved Story requirement before Sprint work exists."""
    return anyio.run(
        self._reopen,
        project_id,
        parent_requirement,
        expected_state,
        idempotency_key,
    )
```

Add `_reopen`:

```python
async def _reopen(
    self,
    project_id: int,
    parent_requirement: str,
    expected_state: str,
    idempotency_key: str,
) -> dict[str, Any]:
    product = self._load_project(project_id)
    if isinstance(product, dict):
        return product

    try:
        data = await reopen_story_requirement(
            parent_requirement=parent_requirement,
            expected_state=expected_state,
            idempotency_key=idempotency_key,
            load_state=lambda: self._load_story_state(str(project_id), project_id, product),
            save_state=lambda state: self._save_session_state(str(project_id), state),
            now_iso=_now_iso,
            assert_reopen_safe=lambda normalized_requirement: _assert_reopen_safe(
                project_id=project_id,
                normalized_requirement=normalized_requirement,
            ),
            reset_subject_working_set=reset_subject_working_set,
        )
    except StoryPhaseError as exc:
        return _phase_error(exc)
    except RuntimeError as exc:
        return _workflow_error(exc)
    return _data_envelope(data)
```

Add module helper:

```python
def _assert_reopen_safe(*, project_id: int, normalized_requirement: str) -> None:
    normalized_key = normalize_requirement_key(normalized_requirement)
    with Session(get_engine()) as session:
        story_ids = [
            story_id
            for story_id in session.exec(
                select(UserStory.story_id).where(
                    UserStory.product_id == project_id,
                    UserStory.source_requirement == normalized_key,
                    UserStory.is_superseded.is_(False),
                )
            ).all()
            if story_id is not None
        ]
        if not story_ids:
            return

        sprint_link = session.exec(
            select(SprintStory.story_id).where(SprintStory.story_id.in_(story_ids))
        ).first()
        if sprint_link is not None:
            raise StoryPhaseError(
                "Story correction is unsafe: active story already has Sprint links.",
                status_code=409,
            )
```

If SQLModel typing rejects `.in_`, use the same cast pattern already used in `repositories/story.py`.

- [ ] **Step 6: Add application facade and CLI parser**

In `services/agent_workbench/application.py`, add:

```python
def story_reopen(
    self,
    *,
    project_id: int,
    parent_requirement: str,
    expected_state: str,
    idempotency_key: str,
) -> dict[str, Any]:
    """Reopen one saved Story requirement before Sprint planning."""
    return self._get_story_runner().reopen(
        project_id=project_id,
        parent_requirement=parent_requirement,
        expected_state=expected_state,
        idempotency_key=idempotency_key,
    )
```

In `cli/main.py`, add parser:

```python
story_reopen = story_sub.add_parser(
    "reopen",
    help="Reopen a saved Story requirement for correction before Sprint work exists.",
)
story_reopen.add_argument("--project-id", type=int, required=True)
story_reopen.add_argument("--parent-requirement", required=True)
story_reopen.add_argument("--expected-state", required=True)
story_reopen.add_argument("--idempotency-key", required=True)
story_reopen.set_defaults(command_handler=_story_reopen)
```

Add handler:

```python
def _story_reopen(
    args: argparse.Namespace,
    application: _Application,
) -> CommandResult:
    """Route Story reopen to the application facade."""
    return "agileforge story reopen", application.story_reopen(
        project_id=args.project_id,
        parent_requirement=args.parent_requirement,
        expected_state=args.expected_state,
        idempotency_key=args.idempotency_key,
    )
```

- [ ] **Step 7: Register command contract**

In `_PHASE_2D_COMMANDS` in `services/agent_workbench/command_registry.py`, add this `CommandMetadata` entry immediately after `agileforge story complete`:

```python
CommandMetadata(
    name="agileforge story reopen",
    mutates=True,
    phase="phase_2d",
    requires_idempotency_key=True,
    input_required=(
        "project_id",
        "parent_requirement",
        "expected_state",
        "idempotency_key",
    ),
    errors=(
        ErrorCode.PROJECT_NOT_FOUND.value,
        ErrorCode.AUTHORITY_NOT_ACCEPTED.value,
        ErrorCode.INVALID_COMMAND.value,
        ErrorCode.WORKFLOW_SESSION_FAILED.value,
        ErrorCode.MUTATION_FAILED.value,
    ),
)
```

- [ ] **Step 8: Write runner/application/CLI tests**

Add tests:

```python
def test_story_reopen_runner_passes_guards(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_reopen_story_requirement(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {
            "parent_requirement": kwargs["parent_requirement"],
            "fsm_state": "STORY_INTERVIEW",
            "idempotency_key": kwargs["idempotency_key"],
        }

    monkeypatch.setattr(
        "services.agent_workbench.story_phase.reopen_story_requirement",
        fake_reopen_story_requirement,
    )
    runner = StoryPhaseRunner(
        product_repo=_FakeProductRepo(),
        workflow_service=_FakeWorkflowService(),
    )

    result = runner.reopen(
        project_id=PROJECT_ID,
        parent_requirement="Review match result",
        expected_state="SPRINT_SETUP",
        idempotency_key="reopen-story-review-match",
    )

    assert result["ok"] is True
    assert result["data"]["fsm_state"] == "STORY_INTERVIEW"
    assert captured["expected_state"] == "SPRINT_SETUP"
    assert captured["idempotency_key"] == "reopen-story-review-match"
```

Add CLI test:

```python
def test_story_reopen_cli_routes_guard_fields(capsys: pytest.CaptureFixture[str]) -> None:
    app = _FakeApplication()

    exit_code = main(
        [
            "story",
            "reopen",
            "--project-id",
            "7",
            "--parent-requirement",
            "Requirement A",
            "--expected-state",
            "SPRINT_SETUP",
            "--idempotency-key",
            "reopen-story-7-a",
        ],
        application=app,
    )

    assert exit_code == 0
    assert app.calls[-1] == (
        "story_reopen",
        {
            "project_id": 7,
            "parent_requirement": "Requirement A",
            "expected_state": "SPRINT_SETUP",
            "idempotency_key": "reopen-story-7-a",
        },
    )
```

Add command schema test asserting `agileforge story reopen` is installed and requires `project_id`, `parent_requirement`, `expected_state`, and `idempotency_key`.

- [ ] **Step 9: Verify Story reopen tests pass**

Run:

```bash
uv run --frozen pytest \
  tests/test_story_phase_service.py::test_reopen_story_requirement_clears_saved_projection_before_sprint_work \
  tests/test_story_phase_service.py::test_reopen_story_requirement_blocks_when_downstream_work_exists \
  tests/test_agent_workbench_story_phase.py::test_story_reopen_runner_passes_guards \
  tests/test_agent_workbench_cli.py::test_story_reopen_cli_routes_guard_fields \
  tests/test_agent_workbench_command_schema.py \
  -q
```

Expected: pass.

## Task 4: Route Workflow Correctly During Story Correction

**Files:**
- Modify: `services/agent_workbench/application.py`
- Test: `tests/test_agent_workbench_application.py`

- [ ] **Step 1: Write failing workflow-next test**

In `tests/test_agent_workbench_application.py`, add a read projection fake whose workflow state is `STORY_INTERVIEW` with `story_saved` missing one Roadmap requirement after a reopen.

Add:

```python
def test_workflow_next_routes_reopened_story_to_generate_not_sprint() -> None:
    app = AgentWorkbenchApplication(
        read_projection=_StoryReopenedReadProjection(),
        authority_projection=_CurrentAuthorityProjection(),
    )

    result = app.workflow_next(project_id=PROJECT_ID)

    assert result["ok"] is True
    assert result["data"]["next_valid_commands"] == [
        "agileforge story pending --project-id 7",
        (
            "agileforge story generate --project-id 7 "
            "--parent-requirement <parent_requirement>"
        ),
    ]
```

Implement `_StoryReopenedReadProjection` like the existing Story projection fakes, with:

```python
"state": {
    "fsm_state": "STORY_INTERVIEW",
    "roadmap_releases": [{"items": ["Requirement A", "Requirement B"]}],
    "story_saved": {"Requirement B": True},
}
```

- [ ] **Step 2: Run failing workflow test**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_application.py::test_workflow_next_routes_reopened_story_to_generate_not_sprint -q
```

Expected: pass if existing Story routing already handles `STORY_INTERVIEW`; fail only if current routing regressed.

- [ ] **Step 3: Adjust routing only if needed**

If the test fails, update `_story_command_candidates` in `services/agent_workbench/application.py` so `STORY_INTERVIEW` always returns:

```python
return [pending_command, generate_command]
```

Do not expose `sprint candidates` until the workflow state is `SPRINT_SETUP`.

- [ ] **Step 4: Verify workflow routing**

Run the test command from Step 2 again.

Expected: pass.

## Task 5: Document Corrected Agent CLI Flow

**Files:**
- Modify: `docs/agent-cli-manual.md`

- [ ] **Step 1: Update Story output examples**

In `docs/agent-cli-manual.md`, update Story sections to show these JSON paths:

```text
Generate:
- attempt id: data.current_draft.attempt_id
- fingerprint: data.current_draft.artifact_fingerprint
- draft artifact: data.output_artifact
- save guard: data.save

Save:
- save result: data.save_result
- saved attempt: data.attempt_id
- saved fingerprint: data.artifact_fingerprint
```

Remove any examples that require `data.data`.

- [ ] **Step 2: Add Story correction flow before Sprint**

Add:

```markdown
### Correcting a Saved Story Before Sprint

Use this only when Story is complete but Sprint work has not started.

```sh
agileforge story reopen \
  --project-id 2 \
  --parent-requirement "Live Pre-Lock Recommendation Workflow with Risk-Audited Artifact" \
  --expected-state SPRINT_SETUP \
  --idempotency-key reopen-story-2-live-budget-001
```

Then regenerate with explicit feedback:

```sh
agileforge story generate \
  --project-id 2 \
  --parent-requirement "Live Pre-Lock Recommendation Workflow with Risk-Audited Artifact" \
  --input "Correct the budget contract: accepted spec REQ.budget-bound says missing available budget must require an explicit operator-provided budget. Do not preserve a default budget fallback for live recommendation." \
  > story-generate-corrected-live-budget.json
```

Review `data.output_artifact`, then save with the returned `data.save.attempt_id`
and `data.save.artifact_fingerprint`.
```

- [ ] **Step 3: Check docs for stale nested paths**

Run:

```bash
rg -n "data\\.data|save_result.*data\\.data|output_artifact.*data\\.data" docs/agent-cli-manual.md
```

Expected: no output.

## Task 6: Live caRtola Correction Smoke Test

**Files:**
- No source edits.
- Live project: `/Users/aaat/projects/caRtola`, project id `2`.

- [ ] **Step 1: Confirm current state**

Run:

```bash
cd /Users/aaat/projects/caRtola
agileforge workflow next --project-id 2 > /tmp/cartola-workflow-next-before-story-reopen.json
uv run --project /Users/aaat/projects/agileforge --frozen python - <<'PY'
import json
from pathlib import Path
payload = json.loads(Path("/tmp/cartola-workflow-next-before-story-reopen.json").read_text())
print(json.dumps(payload["data"], indent=2))
PY
```

Expected: `next_valid_commands` includes `agileforge sprint candidates --project-id 2`.

- [ ] **Step 2: Reopen the conflicting budget story**

Run:

```bash
agileforge story reopen \
  --project-id 2 \
  --parent-requirement "Live Pre-Lock Recommendation Workflow with Risk-Audited Artifact" \
  --expected-state SPRINT_SETUP \
  --idempotency-key reopen-story-2-live-budget-001
```

Expected: `ok: true`, `data.fsm_state: STORY_INTERVIEW`.

- [ ] **Step 3: Regenerate with explicit budget correction**

Run:

```bash
agileforge story generate \
  --project-id 2 \
  --parent-requirement "Live Pre-Lock Recommendation Workflow with Risk-Audited Artifact" \
  --input "Correct the budget contract: accepted spec REQ.budget-bound says missing available budget must require an explicit operator-provided budget. Do not preserve a default budget fallback for live recommendation." \
  > story-generate-corrected-live-budget.json
```

Expected: `ok: true`, output has `data.output_artifact`, not `data.data.output_artifact`.

- [ ] **Step 4: Review and save corrected Story**

Extract guards from generate output:

```bash
uv run --project /Users/aaat/projects/agileforge --frozen python - <<'PY'
import json
from pathlib import Path
payload = json.loads(Path("story-generate-corrected-live-budget.json").read_text())
save = payload["data"]["save"]
print(save["attempt_id"])
print(save["artifact_fingerprint"])
PY
```

Save:

```bash
agileforge story save \
  --project-id 2 \
  --parent-requirement "Live Pre-Lock Recommendation Workflow with Risk-Audited Artifact" \
  --attempt-id <attempt_id> \
  --expected-artifact-fingerprint <artifact_fingerprint> \
  --expected-state STORY_REVIEW \
  --idempotency-key save-story-2-live-budget-corrected-001
```

Expected: `ok: true`, `data.save_result.success: true`.

- [ ] **Step 5: Complete Story again**

Run:

```bash
agileforge story pending --project-id 2
agileforge story complete \
  --project-id 2 \
  --expected-state STORY_PERSISTENCE \
  --idempotency-key complete-story-2-after-budget-correction-001
agileforge workflow next --project-id 2
```

Expected:
- `story pending` shows `10/10`.
- `story complete` returns `fsm_state: SPRINT_SETUP`.
- `workflow next` returns `agileforge sprint candidates --project-id 2`.

## Task 7: Verification

**Files:**
- All changed files from Tasks 1-5.

- [ ] **Step 1: Run focused Story tests**

Run:

```bash
uv run --frozen pytest \
  tests/test_story_runtime.py \
  tests/test_story_phase_service.py \
  tests/test_agent_workbench_story_phase.py \
  tests/test_agent_workbench_application.py \
  tests/test_agent_workbench_cli.py \
  tests/test_agent_workbench_command_schema.py \
  tests/test_api_story_interview_flow.py \
  -q
```

Expected: all pass.

- [ ] **Step 2: Run focused Ruff**

Run:

```bash
uv run --frozen ruff check \
  services/story_runtime.py \
  services/phases/story_service.py \
  services/agent_workbench/story_phase.py \
  services/agent_workbench/application.py \
  services/agent_workbench/command_registry.py \
  cli/main.py \
  tests/test_story_runtime.py \
  tests/test_story_phase_service.py \
  tests/test_agent_workbench_story_phase.py \
  tests/test_agent_workbench_application.py \
  tests/test_agent_workbench_cli.py \
  tests/test_agent_workbench_command_schema.py \
  tests/test_api_story_interview_flow.py
```

Expected: `All checks passed!`

- [ ] **Step 3: Run bounded full suite**

Run:

```bash
uv run --frozen pytest -q
uv run --frozen ruff check .
```

Expected:
- pytest passes.
- Ruff passes.

If full pytest takes longer than 5 minutes, stop and report the focused Story results plus the timeout.

## Self-Review

- Spec coverage: the plan covers all three accepted fixes: CLI envelope flattening, incomplete draft validation, and safe Story correction before Sprint.
- Placeholder scan: no placeholder markers are intentionally left.
- Type consistency: new service uses existing `StoryPhaseError`, `OrchestratorState`, `reset_subject_working_set`, `sync_story_legacy_mirrors`, and `normalize_requirement_key` patterns already present in the Story phase code.
- Scope check: Sprint generation is intentionally out of scope. This plan stops at restoring a corrected, trusted Story phase and verifying `sprint candidates` is the next command.
