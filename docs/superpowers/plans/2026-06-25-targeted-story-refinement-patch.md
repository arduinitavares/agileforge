# Targeted Story Patch Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement Issue #160 with an explicit targeted story patch draft contract so one To Do story can be refined without requiring the agent to return progressed sibling stories.

**Architecture:** Keep full parent decomposition on `UserStoryWriterOutput` with `user_stories`. Add `UserStoryPatchOutput` with `artifact_kind="story_patch"` and one `story`, route targeted generation through a fresh ADK agent contract using that schema, store patch attempts as `draft_kind="story_patch"`, and make `story save-patch` read `output_artifact["story"]` directly. Preserve the existing host-level targeted persistence work, but remove any dependency on `user_stories[target_refinement_slot - 1]` for patch saves.

**Tech Stack:** Python, Pydantic v2, Google ADK Python Agent structured outputs, SQLModel, FastAPI, argparse CLI, pytest, Ruff.

---

## Files

- Modify: `orchestrator_agent/agent_tools/user_story_writer_tool/schemes.py`
- Modify: `orchestrator_agent/agent_tools/user_story_writer_tool/agent.py`
- Create: `orchestrator_agent/agent_tools/user_story_writer_tool/patch_instructions.txt`
- Modify: `orchestrator_agent/agent_tools/user_story_writer_tool/tools.py`
- Modify: `services/story_runtime.py`
- Modify: `services/phases/story_service.py`
- Modify: `services/agent_workbench/story_phase.py`
- Modify: `services/agent_workbench/application.py`
- Modify: `services/agent_workbench/command_registry.py`
- Modify: `cli/main.py`
- Modify: `api.py`
- Test: `tests/test_user_story_writer_agent.py`
- Test: `tests/test_story_phase_service.py`
- Test: `tests/test_agent_workbench_story_phase.py`
- Test: `tests/test_agent_workbench_cli.py`
- Test: `tests/test_agent_workbench_command_schema.py`
- Test: `tests/test_api_story_interview_flow.py`
- Test: `tests/test_save_stories_tool.py`

## Source Notes

- ADK Python 2.0 structured output is configured on `Agent(output_schema=...)`.
- Do not mutate the existing module-level `root_agent.output_schema`.
- Implement either a separate `create_user_story_patch_agent()` or a parameterized factory that creates a fresh `Agent`; prefer the separate factory unless code duplication grows materially.

## Task 1: Patch Output Schema And ADK Agent

**Files:**
- Modify: `orchestrator_agent/agent_tools/user_story_writer_tool/schemes.py`
- Modify: `orchestrator_agent/agent_tools/user_story_writer_tool/agent.py`
- Create: `orchestrator_agent/agent_tools/user_story_writer_tool/patch_instructions.txt`
- Test: `tests/test_user_story_writer_agent.py`

- [ ] **Step 1: Write failing schema and agent tests**

Add tests that prove:

```python
from orchestrator_agent.agent_tools.user_story_writer_tool.agent import (
    create_user_story_patch_agent,
    create_user_story_writer_agent,
)
from orchestrator_agent.agent_tools.user_story_writer_tool.schemes import (
    UserStoryPatchOutput,
    UserStoryWriterOutput,
)


def test_user_story_patch_output_rejects_user_stories_field():
    payload = {
        "artifact_kind": "story_patch",
        "parent_requirement": "Requirement A",
        "target_refinement_slot": 2,
        "story": {
            "story_title": "Refined Story",
            "statement": "As a user, I want a refined behavior so that value is clear.",
            "acceptance_criteria": ["Given context, When action, Then result"],
            "estimated_effort": "3",
            "invest_score": "Good",
            "dependencies": [],
        },
        "user_stories": [],
        "is_complete": True,
        "clarifying_questions": [],
    }

    with pytest.raises(ValidationError):
        UserStoryPatchOutput.model_validate(payload)


def test_user_story_patch_agent_uses_patch_output_schema():
    patch_agent = create_user_story_patch_agent()
    full_agent = create_user_story_writer_agent()

    assert patch_agent.output_schema is UserStoryPatchOutput
    assert full_agent.output_schema is UserStoryWriterOutput
    assert patch_agent is not full_agent
```

Run:

```bash
uv run --frozen pytest tests/test_user_story_writer_agent.py -k "patch_output or patch_agent" -q
```

Expected: fail because `UserStoryPatchOutput` and `create_user_story_patch_agent` do not exist.

- [ ] **Step 2: Implement schema and agent factory**

Add `UserStoryPatchOutput` beside `UserStoryWriterOutput`:

```python
class UserStoryPatchOutput(BaseModel):
    """Structured output payload for one targeted Story refinement patch."""

    model_config = ConfigDict(extra="forbid")

    artifact_kind: Literal["story_patch"] = "story_patch"
    parent_requirement: Annotated[
        str,
        Field(description="Copied verbatim from input for traceability."),
    ]
    target_refinement_slot: Annotated[
        int,
        Field(ge=1, description="Canonical 1-based refinement slot being patched."),
    ]
    target_story_id: int | None = Field(
        default=None,
        description="Existing story ID when host-side target resolution knows it.",
    )
    story: Annotated[
        UserStoryItem,
        Field(description="The only story item included in a targeted patch artifact."),
    ]
    quality_schema_version: Literal["agileforge.story_quality.v1"] = Field(
        default=STORY_QUALITY_SCHEMA_VERSION,
        description="Version of the Story draft quality contract.",
    )
    coverage_status: Literal["complete", "needs_clarification"] = Field(
        default="complete",
        description="Whether this targeted patch fully resolves the requested refinement.",
    )
    remaining_scope: list[str] = Field(default_factory=list)
    quality_findings: list[StoryQualityFinding] = Field(default_factory=list)
    is_complete: bool
    clarifying_questions: list[str] = Field(default_factory=list)
```

Add `patch_instructions.txt` with direct instructions:

```text
You refine exactly one existing user story.
Return UserStoryPatchOutput only.
Do not return user_stories.
Do not invent sibling stories.
Copy parent_requirement and target_refinement_slot from the input context.
The story field must contain the refined target story only.
```

Add a fresh factory:

```python
PATCH_INSTRUCTIONS_PATH: Path = Path(__file__).parent / "patch_instructions.txt"
USER_STORY_PATCH_INSTRUCTIONS = load_instruction(PATCH_INSTRUCTIONS_PATH)


def create_user_story_patch_agent() -> Agent:
    """Factory: create a fresh targeted User Story patch agent instance."""
    model: LiteLlm = _create_story_writer_model()
    return Agent(
        name="user_story_patch_tool",
        description="Refines exactly one existing Scrum user story.",
        model=model,
        input_schema=UserStoryWriterInput,
        output_schema=UserStoryPatchOutput,
        output_key="story_output",
        instruction=USER_STORY_PATCH_INSTRUCTIONS,
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
    )
```

Extract `_create_story_writer_model()` only if needed to keep the two factories short.

- [ ] **Step 3: Run green check**

Run:

```bash
uv run --frozen pytest tests/test_user_story_writer_agent.py -k "patch_output or patch_agent" -q
```

Expected: pass.

## Task 2: Patch-Mode Story Generation Contract

**Files:**
- Modify: `services/story_runtime.py`
- Modify: `services/phases/story_service.py`
- Modify: `services/agent_workbench/story_phase.py`
- Test: `tests/test_story_phase_service.py`
- Test: `tests/test_agent_workbench_story_phase.py`

- [ ] **Step 1: Write failing service tests for targeted generation**

Add tests that prove:

```python
async def test_generate_story_draft_with_target_slot_stores_story_patch_attempt():
    result = await generate_story_draft(
        project_id=1,
        parent_requirement="Requirement A",
        user_input="Refine slot 2 only",
        force_feedback=False,
        target_refinement_slot=2,
        target_story_id=None,
        load_state=load_state,
        save_state=save_state,
        now_iso=lambda: "2026-06-25T12:00:00Z",
        run_story_agent_from_state=run_patch_agent,
        dependency_preflight=no_dependency_blocker,
        append_feedback_entry=append_feedback_entry,
        set_request_projection=set_request_projection,
        append_attempt=append_attempt,
        promote_reusable_draft=promote_reusable_draft,
        mark_feedback_absorbed=mark_feedback_absorbed,
        failure_meta=failure_meta,
    )

    assert result["current_draft"]["kind"] == "story_patch"
    assert result["current_draft"]["target_refinement_slot"] == 2
    attempt = state["story_runtime"]["requirements"]["Requirement A"]["attempts"][-1]
    assert attempt["draft_kind"] == "story_patch"
    assert attempt["output_artifact"]["artifact_kind"] == "story_patch"
    assert "story" in attempt["output_artifact"]
    assert "user_stories" not in attempt["output_artifact"]
```

Add a negative test:

```python
async def test_generate_story_draft_rejects_both_patch_targets():
    with pytest.raises(StoryPhaseError, match="Exactly one"):
        await generate_story_draft(
            project_id=1,
            parent_requirement="Requirement A",
            target_story_id=29,
            target_refinement_slot=2,
            ...
        )
```

Run:

```bash
uv run --frozen pytest tests/test_story_phase_service.py -k "target_slot_stores_story_patch or rejects_both_patch_targets" -q
```

Expected: fail because `generate_story_draft` does not accept target selectors and runtime stores only full drafts.

- [ ] **Step 2: Implement generation target plumbing**

Update service and runner signatures:

```python
async def generate_story_draft(
    *,
    project_id: int,
    parent_requirement: str,
    user_input: str | None,
    force_feedback: bool,
    target_story_id: int | None = None,
    target_refinement_slot: int | None = None,
    ...
) -> dict[str, Any]:
```

Rules:

- Neither target selector means existing full-list mode.
- Both target selectors means `StoryPhaseError(..., status_code=400)`.
- One target selector means patch mode.
- Resolve `target_story_id` to canonical `target_refinement_slot` before storing a reusable draft.
- Reject progressed, accepted, superseded, sprint-linked, wrong-parent, or wrong-project target before agent call when repository data is available.
- Patch mode calls a patch runtime path and receives `UserStoryPatchOutput` shape.
- Patch attempts store `draft_kind="story_patch"`, top-level `target_refinement_slot`, optional `target_story_id`, and `output_artifact["story"]`.

- [ ] **Step 3: Update draft summaries**

Make `story_interview_summary()` and any current-draft projection expose target metadata:

```python
current_draft = {
    "attempt_id": draft_projection.get("latest_reusable_attempt_id"),
    "kind": draft_projection.get("kind"),
    "is_complete": bool(draft_projection.get("is_complete", False)),
    "target_story_id": draft_projection.get("target_story_id"),
    "target_refinement_slot": draft_projection.get("target_refinement_slot"),
}
```

Keep full-list summaries unchanged except for harmless absent target fields.

- [ ] **Step 4: Run green check**

Run:

```bash
uv run --frozen pytest tests/test_story_phase_service.py -k "target_slot_stores_story_patch or rejects_both_patch_targets" -q
uv run --frozen pytest tests/test_agent_workbench_story_phase.py -k "generate" -q
```

Expected: pass.

## Task 3: Save-Patch Reads Patch Artifact Directly

**Files:**
- Modify: `services/phases/story_service.py`
- Modify: `orchestrator_agent/agent_tools/user_story_writer_tool/tools.py`
- Test: `tests/test_story_phase_service.py`
- Test: `tests/test_save_stories_tool.py`

- [ ] **Step 1: Write failing save-patch contract tests**

Add a service test with a `STORY_REVIEW` state whose reusable attempt is:

```python
{
    "attempt_id": "story-attempt-1",
    "draft_kind": "story_patch",
    "artifact_fingerprint": "sha256:patch",
    "is_reusable": True,
    "output_artifact": {
        "artifact_kind": "story_patch",
        "parent_requirement": "Requirement A",
        "target_refinement_slot": 2,
        "story": {
            "story_title": "Refined Story B",
            "statement": "As a user, I want Story B refined so that it is actionable.",
            "acceptance_criteria": ["Given B, When refined, Then it is actionable"],
            "estimated_effort": "3",
            "invest_score": "Good",
            "dependencies": [],
        },
        "is_complete": True,
        "clarifying_questions": [],
    },
}
```

Assert `save_story_patch(...)` passes exactly `output_artifact["story"]` to `save_story_patch_tool` and does not require `output_artifact["user_stories"]`.

Add a negative test proving `save_story_patch` rejects `draft_kind="complete_draft"` with a 409 conflict.

Run:

```bash
uv run --frozen pytest tests/test_story_phase_service.py -k "patch_artifact_directly or rejects_complete_draft" -q
```

Expected: fail because current code reads `assessment["user_stories"]`.

- [ ] **Step 2: Refactor save-patch extraction**

Remove `_story_patch_item_from_artifact(stories, target_refinement_slot=...)` from the patch save path.

Add:

```python
def _story_patch_artifact(runtime: dict[str, Any], *, attempt_id: str) -> dict[str, Any]:
    attempt = _find_attempt_by_id(runtime, attempt_id)
    artifact = _attempt_output_artifact(attempt)
    if not isinstance(attempt, dict) or attempt.get("draft_kind") != "story_patch":
        raise StoryPhaseError("story save-patch requires a story_patch draft", status_code=409)
    if not isinstance(artifact, dict) or artifact.get("artifact_kind") != "story_patch":
        raise StoryPhaseError("story save-patch artifact is not a story_patch", status_code=409)
    story = artifact.get("story")
    if not isinstance(story, dict):
        raise StoryPhaseError("story save-patch target story is invalid", status_code=409)
    return artifact
```

Validate command selector against artifact target:

```python
if int(artifact.get("target_refinement_slot") or 0) != target_refinement_slot:
    raise StoryPhaseError("story save-patch target mismatch; refresh history", status_code=409)
```

Pass `artifact["story"]` to `SaveStoryPatchInput`.

- [ ] **Step 3: Refactor merged output**

Change `_story_patch_merged_output(...)` to accept `patch_story: dict[str, Any]` instead of `assessment`.

Required behavior:

- Read existing `story_outputs[parent_requirement]["user_stories"]`.
- Replace only `target_refinement_slot - 1` with `patch_story`.
- Preserve every non-target sibling object exactly.
- Return a full `story_outputs[parent_requirement]` compatible object with `user_stories`.
- If no existing output exists and slot is `1`, create a one-story output.
- If no existing output exists and slot is greater than `1`, reject with a clear 409 because siblings cannot be reconstructed.

- [ ] **Step 4: Keep tool-level target safety tests green**

Run:

```bash
uv run --frozen pytest tests/test_save_stories_tool.py -k "patch or progressed or sibling" -q
uv run --frozen pytest tests/test_story_phase_service.py -k "patch" -q
```

Expected: pass.

## Task 4: Full Save Rejects Patch Drafts

**Files:**
- Modify: `services/phases/story_service.py`
- Test: `tests/test_story_phase_service.py`

- [ ] **Step 1: Write failing full-save rejection test**

Add a test:

```python
async def test_save_story_draft_rejects_story_patch_current_draft():
    state = story_review_state_with_patch_attempt()

    with pytest.raises(StoryPhaseError, match="story save requires a complete draft"):
        await save_story_draft(
            project_id=1,
            parent_requirement="Requirement A",
            attempt_id="story-attempt-1",
            expected_artifact_fingerprint="sha256:patch",
            expected_state="STORY_REVIEW",
            idempotency_key="key",
            ...
        )
```

Run:

```bash
uv run --frozen pytest tests/test_story_phase_service.py -k "rejects_story_patch_current_draft" -q
```

Expected: fail if full save accepts the patch draft or errors for the wrong reason.

- [ ] **Step 2: Add explicit guard in full save**

In `save_story_draft(...)`, after current-attempt/fingerprint validation and before building the save payload, reject patch attempts:

```python
current_attempt = _find_attempt_by_id(runtime, attempt_id)
if isinstance(current_attempt, dict) and current_attempt.get("draft_kind") == "story_patch":
    raise StoryPhaseError("story save requires a complete draft; use story save-patch for story_patch drafts", status_code=409)
```

- [ ] **Step 3: Run green check**

Run:

```bash
uv run --frozen pytest tests/test_story_phase_service.py -k "rejects_story_patch_current_draft or save_story_patch" -q
```

Expected: pass.

## Task 5: CLI, API, And Command Registry Targeted Generation

**Files:**
- Modify: `cli/main.py`
- Modify: `api.py`
- Modify: `services/agent_workbench/application.py`
- Modify: `services/agent_workbench/command_registry.py`
- Modify: `services/agent_workbench/story_phase.py`
- Test: `tests/test_agent_workbench_cli.py`
- Test: `tests/test_api_story_interview_flow.py`
- Test: `tests/test_agent_workbench_command_schema.py`
- Test: `tests/test_agent_workbench_story_phase.py`

- [ ] **Step 1: Write failing CLI/API routing tests**

Add CLI test:

```python
def test_story_generate_accepts_target_refinement_slot(cli_runner):
    result = cli_runner.invoke(
        [
            "story",
            "generate",
            "--project-id",
            "1",
            "--parent-requirement",
            "Requirement A",
            "--target-refinement-slot",
            "2",
            "--input",
            "Refine only this story",
        ]
    )

    assert result.exit_code == 0
    assert fake_app.calls[-1]["target_refinement_slot"] == 2
```

Add API test:

```python
response = client.post(
    "/api/projects/1/story/generate",
    json={
        "parent_requirement": "Requirement A",
        "user_input": "Refine only this story",
        "target_refinement_slot": 2,
    },
)
assert response.status_code == 200
```

Add command schema test that `story.generate` exposes optional `target_story_id` and `target_refinement_slot`.

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_cli.py -k "story_generate_accepts_target" -q
uv run --frozen pytest tests/test_api_story_interview_flow.py -k "story_generate_target" -q
uv run --frozen pytest tests/test_agent_workbench_command_schema.py -k "story_generate" -q
```

Expected: fail because generate accepts no target selectors.

- [ ] **Step 2: Implement routing**

Add optional target selectors to:

- `StoryPhaseRunner.generate(...)`
- `StoryPhaseRunner._generate(...)`
- `Application.story_generate(...)`
- CLI `story generate`
- API `StoryGenerateRequest`
- command registry schema

Boundary rules:

- API/CLI reject both target selectors.
- API/CLI allow neither target selector for full mode.
- Save-patch still requires exactly one selector.

- [ ] **Step 3: Run green check**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_cli.py -k "story_generate or save_patch" -q
uv run --frozen pytest tests/test_api_story_interview_flow.py -k "story_generate or save_patch" -q
uv run --frozen pytest tests/test_agent_workbench_command_schema.py -k "story_generate or save_patch" -q
uv run --frozen pytest tests/test_agent_workbench_story_phase.py -k "generate or save_patch" -q
```

Expected: pass.

## Task 6: Verification And Cleanup

**Files:**
- No production behavior changes unless verification finds a defect.

- [ ] **Step 1: Run focused #160 suite**

Run:

```bash
uv run --frozen pytest \
  tests/test_user_story_writer_agent.py \
  tests/test_save_stories_tool.py \
  tests/test_story_phase_service.py \
  tests/test_agent_workbench_story_phase.py \
  tests/test_agent_workbench_cli.py \
  tests/test_api_story_interview_flow.py \
  tests/test_agent_workbench_command_schema.py \
  -k "patch or story_generate or save_patch or targeted" -q
```

Expected: pass.

- [ ] **Step 2: Run Ruff on touched Python files**

Run:

```bash
uv run --frozen ruff check \
  api.py \
  cli/main.py \
  orchestrator_agent/agent_tools/user_story_writer_tool/agent.py \
  orchestrator_agent/agent_tools/user_story_writer_tool/schemes.py \
  orchestrator_agent/agent_tools/user_story_writer_tool/tools.py \
  services/story_runtime.py \
  services/phases/story_service.py \
  services/agent_workbench/application.py \
  services/agent_workbench/command_registry.py \
  services/agent_workbench/story_phase.py \
  tests/test_user_story_writer_agent.py \
  tests/test_save_stories_tool.py \
  tests/test_story_phase_service.py \
  tests/test_agent_workbench_story_phase.py \
  tests/test_agent_workbench_cli.py \
  tests/test_api_story_interview_flow.py \
  tests/test_agent_workbench_command_schema.py
```

Expected: pass.

- [ ] **Step 3: Run full test suite**

Run:

```bash
uv run --frozen pytest
```

Expected: pass.

- [ ] **Step 4: Check whitespace and summarize diff**

Run:

```bash
git diff --check
git status --short --branch
git diff --stat
```

Expected: no whitespace errors; branch remains `dev/issue-160-targeted-story-refinement`; diff only includes #160 spec/plan/code/tests.

## Self-Review

- Spec FR-001 through FR-010 are covered by Tasks 1-5.
- The plan rejects the list-length heuristic and direct slot indexing from full-list artifacts.
- The ADK schema decision is explicit: fresh agent contract with `output_schema=UserStoryPatchOutput`.
- Existing full-list behavior remains compatible because no target selector keeps the existing `UserStoryWriterOutput` path.
- The current partial host-level persistence work is preserved where useful, but patch save must be refactored to consume `output_artifact["story"]`.
