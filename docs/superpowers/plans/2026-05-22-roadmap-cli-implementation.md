# Roadmap CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Install Roadmap CLI commands so AgileForge can move caRtola from `BACKLOG_PERSISTENCE` through Roadmap generation, review, and persistence without manual interviews.

**Architecture:** Follow the installed Vision and Backlog CLI pattern: application facade -> phase runner -> phase service -> runtime/tool boundary. Roadmap save must be attempt-aware and fail-closed, and the Roadmap artifact must cover the canonical active backlog exactly once before it can become persisted project structure.

**Tech Stack:** Python 3.13, SQLModel, Pydantic, AnyIO, existing AgileForge CLI envelopes, existing `roadmap_runtime.py`, existing `services/phases/roadmap_service.py`, existing `orchestrator_agent/agent_tools/roadmap_builder`.

---

## File Structure

- Create `services/agent_workbench/roadmap_phase.py`: CLI-facing Roadmap runner, mirroring `BacklogPhaseRunner`.
- Modify `services/phases/roadmap_service.py`: attempt IDs, artifact fingerprints, guarded save, idempotency replay, runtime consistency checks, exact active backlog coverage checks.
- Modify `services/roadmap_runtime.py`: force `is_complete: false` when clarifying questions remain.
- Modify `orchestrator_agent/agent_tools/roadmap_builder/tools.py`: persist idempotency metadata and return saved roadmap/fingerprint-friendly data.
- Modify `services/agent_workbench/application.py`: add Roadmap protocol/facade methods and workflow-next routing.
- Modify `cli/main.py`: add `agileforge roadmap generate/history/save`.
- Modify `services/agent_workbench/command_registry.py`: register Roadmap command contracts.
- Modify `api.py`: update Roadmap save route/request to use the same save guards as CLI.
- Modify `docs/agent-cli-manual.md`: replace “Roadmap not installed” with exact Roadmap commands.
- Tests:
  - `tests/test_roadmap_phase_service.py`
  - `tests/test_runtime_failure_artifacts.py`
  - `tests/test_agent_workbench_roadmap_phase.py`
  - `tests/test_agent_workbench_application.py`
  - `tests/test_agent_workbench_cli.py`
  - `tests/test_agent_workbench_command_schema.py`
  - `tests/test_api_roadmap_flow.py`

## Task 1: Harden Roadmap Phase Service

**Files:**
- Modify: `services/phases/roadmap_service.py`
- Test: `tests/test_roadmap_phase_service.py`

- [ ] **Step 1: Write failing tests for attempt guards and completion consistency**

Add tests equivalent to Backlog:

```python
@pytest.mark.asyncio
async def test_generate_roadmap_draft_attaches_attempt_guards() -> None:
    state: JsonDict = {"fsm_state": "BACKLOG_PERSISTENCE", "backlog_items": [{"requirement": "A"}]}

    async def load_state() -> JsonDict:
        return state

    async def fake_run_roadmap_agent_from_state(
        state: object, *, project_id: int, user_input: str | None
    ) -> JsonDict:
        return {
            "success": True,
            "input_context": {"user_input": user_input or ""},
            "output_artifact": {
                "roadmap_releases": [
                    {
                        "release_name": "Milestone 1",
                        "theme": "Foundation",
                        "focus_area": "Technical Foundation",
                        "items": ["A"],
                        "reasoning": "Start with A",
                    }
                ],
                "roadmap_summary": "Ship A first.",
                "is_complete": True,
                "clarifying_questions": [],
            },
            "is_complete": True,
            "error": None,
        }

    payload = await generate_roadmap_draft(
        project_id=7,
        load_state=load_state,
        save_state=lambda _state: None,
        now_iso=lambda: "2026-05-22T00:00:00Z",
        run_roadmap_agent=fake_run_roadmap_agent_from_state,
        user_input=None,
    )

    assert payload["attempt_id"] == "roadmap-attempt-1"
    assert payload["artifact_fingerprint"].startswith("sha256:")
    assert state["product_roadmap_assessment"]["attempt_id"] == "roadmap-attempt-1"
```

```python
@pytest.mark.asyncio
async def test_generate_roadmap_draft_forces_incomplete_when_questions_remain() -> None:
    state: JsonDict = {"fsm_state": "BACKLOG_PERSISTENCE", "backlog_items": [{"requirement": "A"}]}

    async def fake_run_roadmap_agent_from_state(
        state: object, *, project_id: int, user_input: str | None
    ) -> JsonDict:
        return {
            "success": True,
            "input_context": {},
            "output_artifact": {
                "roadmap_releases": [],
                "roadmap_summary": "Draft",
                "is_complete": True,
                "clarifying_questions": ["Which milestone boundary?"],
            },
            "is_complete": True,
            "error": None,
        }

    payload = await generate_roadmap_draft(
        project_id=7,
        load_state=lambda: _async_value(state),
        save_state=lambda _state: None,
        now_iso=lambda: "2026-05-22T00:00:00Z",
        run_roadmap_agent=fake_run_roadmap_agent_from_state,
        user_input=None,
    )

    assert payload["is_complete"] is False
    assert payload["fsm_state"] == "ROADMAP_INTERVIEW"
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
uv run --frozen pytest \
  tests/test_roadmap_phase_service.py::test_generate_roadmap_draft_attaches_attempt_guards \
  tests/test_roadmap_phase_service.py::test_generate_roadmap_draft_forces_incomplete_when_questions_remain \
  -q
```

Expected: fail because `attempt_id`, `artifact_fingerprint`, and forced-incomplete behavior do not exist.

- [ ] **Step 3: Implement minimal attempt guard helpers**

Add to `services/phases/roadmap_service.py`:

```python
from services.agent_workbench.fingerprints import canonical_hash
```

```python
def _has_clarifying_questions(artifact: dict[str, Any]) -> bool:
    questions = artifact.get("clarifying_questions")
    return isinstance(questions, list) and any(
        isinstance(question, str) and bool(question.strip()) for question in questions
    )


def _effective_roadmap_completion(
    roadmap_result: dict[str, Any],
    output_artifact: dict[str, Any],
) -> bool:
    if not roadmap_result.get("success"):
        return False
    if _has_clarifying_questions(output_artifact):
        return False
    return bool(roadmap_result.get("is_complete"))


def _roadmap_artifact_fingerprint(output_artifact: dict[str, Any]) -> str:
    return canonical_hash({"phase": "roadmap", "output_artifact": output_artifact})


def _attach_attempt_guards(
    state: dict[str, Any],
    *,
    attempt_id: str,
    artifact_fingerprint: str,
) -> None:
    attempts = ensure_roadmap_attempts(state)
    if attempts:
        attempts[-1]["attempt_id"] = attempt_id
        attempts[-1]["artifact_fingerprint"] = artifact_fingerprint
        output_artifact = attempts[-1].get("output_artifact")
        if isinstance(output_artifact, dict):
            output_artifact["attempt_id"] = attempt_id
            output_artifact["artifact_fingerprint"] = artifact_fingerprint

    assessment = state.get("product_roadmap_assessment")
    if isinstance(assessment, dict):
        assessment["attempt_id"] = attempt_id
        assessment["artifact_fingerprint"] = artifact_fingerprint
```

Update `generate_roadmap_draft`:

```python
output_artifact = dict(roadmap_result.get("output_artifact") or {})
is_complete = _effective_roadmap_completion(roadmap_result, output_artifact)
output_artifact["is_complete"] = is_complete
artifact_fingerprint = _roadmap_artifact_fingerprint(output_artifact)
attempt_count = record_roadmap_attempt(..., output_artifact=output_artifact, ...)
attempt_id = f"roadmap-attempt-{attempt_count}"
_attach_attempt_guards(
    state,
    attempt_id=attempt_id,
    artifact_fingerprint=artifact_fingerprint,
)
```

Return `attempt_id` and `artifact_fingerprint`.

- [ ] **Step 4: Run tests and verify they pass**

Run the same focused command. Expected: pass.

## Task 2: Add Roadmap Save Guard Rails

**Files:**
- Modify: `services/phases/roadmap_service.py`
- Modify: `api.py`
- Test: `tests/test_roadmap_phase_service.py`
- Test: `tests/test_api_roadmap_flow.py`

- [ ] **Step 1: Write failing guarded-save tests**

Add tests:

```python
@pytest.mark.asyncio
async def test_save_roadmap_draft_requires_expected_state_attempt_and_fingerprint() -> None:
    state: JsonDict = {
        "fsm_state": "ROADMAP_REVIEW",
        "roadmap_attempts": [{"attempt_id": "roadmap-attempt-1", "artifact_fingerprint": "sha256:" + "a" * 64}],
        "product_roadmap_assessment": {
            "attempt_id": "roadmap-attempt-1",
            "artifact_fingerprint": "sha256:" + "a" * 64,
            "roadmap_releases": [
                {
                    "release_name": "Milestone 1",
                    "theme": "Foundation",
                    "focus_area": "Technical Foundation",
                    "items": ["A"],
                    "reasoning": "Start with A",
                }
            ],
            "roadmap_summary": "Final",
            "is_complete": True,
            "clarifying_questions": [],
        },
        "backlog_items": [{"requirement": "A"}],
    }

    payload = await save_roadmap_draft(
        project_id=7,
        attempt_id="roadmap-attempt-1",
        expected_artifact_fingerprint="sha256:" + "a" * 64,
        expected_state="ROADMAP_REVIEW",
        idempotency_key="save-roadmap-1",
        save_state=lambda _state: None,
        now_iso=lambda: "2026-05-22T00:00:00Z",
        hydrate_context=lambda: _async_value(SimpleNamespace(state=dict(state), session_id="7")),
        build_tool_context=lambda context: context,
        save_roadmap_tool=lambda roadmap_input, tool_context: {"success": True, "product_id": 7},
    )

    assert payload["attempt_id"] == "roadmap-attempt-1"
    assert payload["artifact_fingerprint"] == "sha256:" + "a" * 64
    assert payload["idempotency_key"] == "save-roadmap-1"
```

```python
@pytest.mark.asyncio
async def test_save_roadmap_draft_rejects_stale_attempt_guard() -> None:
    state: JsonDict = {
        "fsm_state": "ROADMAP_REVIEW",
        "roadmap_attempts": [{"attempt_id": "roadmap-attempt-2", "artifact_fingerprint": "sha256:" + "b" * 64}],
        "product_roadmap_assessment": {"attempt_id": "roadmap-attempt-2", "artifact_fingerprint": "sha256:" + "b" * 64},
    }

    with pytest.raises(RoadmapPhaseError, match="Roadmap save guard mismatch"):
        await save_roadmap_draft(
            project_id=7,
            attempt_id="roadmap-attempt-1",
            expected_artifact_fingerprint="sha256:" + "a" * 64,
            expected_state="ROADMAP_REVIEW",
            idempotency_key="save-roadmap-1",
            save_state=lambda _state: None,
            now_iso=lambda: "2026-05-22T00:00:00Z",
            hydrate_context=lambda: _async_value(SimpleNamespace(state=dict(state))),
            build_tool_context=lambda context: context,
            save_roadmap_tool=_fake_save_roadmap_tool,
        )
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
uv run --frozen pytest tests/test_roadmap_phase_service.py -q
```

Expected: fail on new `save_roadmap_draft` arguments.

- [ ] **Step 3: Implement guarded save**

Change `save_roadmap_draft` signature:

```python
async def save_roadmap_draft(
    *,
    project_id: int,
    attempt_id: str,
    expected_artifact_fingerprint: str,
    expected_state: str,
    idempotency_key: str,
    ...
) -> dict[str, Any]:
```

Add helpers:

```python
def _assert_save_expected_state(state: dict[str, Any], expected_state: str) -> None:
    if expected_state != OrchestratorState.ROADMAP_REVIEW.value:
        raise RoadmapPhaseError("Roadmap save expected_state must be ROADMAP_REVIEW")
    fsm_state = _normalize_fsm_state(state.get("fsm_state"))
    if fsm_state != expected_state:
        raise RoadmapPhaseError(
            f"Roadmap save stale state: expected {expected_state}, got {fsm_state}",
        )


def _find_roadmap_attempt(
    state: dict[str, Any],
    attempt_id: str,
) -> dict[str, Any] | None:
    for attempt in ensure_roadmap_attempts(state):
        if attempt.get("attempt_id") == attempt_id:
            return attempt
    return None


def _assert_save_guards(
    *,
    state: dict[str, Any],
    assessment: dict[str, Any],
    attempt_id: str,
    expected_artifact_fingerprint: str,
) -> None:
    selected_attempt = _find_roadmap_attempt(state, attempt_id)
    if (
        assessment.get("attempt_id") != attempt_id
        or assessment.get("artifact_fingerprint") != expected_artifact_fingerprint
        or selected_attempt is None
        or selected_attempt.get("artifact_fingerprint") != expected_artifact_fingerprint
    ):
        raise RoadmapPhaseError(
            "Roadmap save guard mismatch: draft attempt or artifact fingerprint "
            "does not match the reviewed Roadmap draft.",
        )
```

Add state-level idempotency replay:

```python
def _roadmap_save_replay(
    state: dict[str, Any],
    idempotency_key: str,
) -> dict[str, Any] | None:
    saves = state.get("roadmap_save_idempotency_keys")
    if not isinstance(saves, dict):
        return None
    payload = saves.get(idempotency_key)
    return dict(payload) if isinstance(payload, dict) else None


def _record_roadmap_save_replay(
    state: dict[str, Any],
    idempotency_key: str,
    payload: dict[str, Any],
) -> None:
    saves = state.get("roadmap_save_idempotency_keys")
    if not isinstance(saves, dict):
        saves = {}
    saves[idempotency_key] = dict(payload)
    state["roadmap_save_idempotency_keys"] = saves
```

- [ ] **Step 4: Update API Roadmap save request**

In `api.py`, add:

```python
class RoadmapSaveRequest(BaseModel):
    attempt_id: str
    expected_artifact_fingerprint: str
    expected_state: str
    idempotency_key: str
```

Change route:

```python
@app.post("/api/projects/{project_id}/roadmap/save")
async def save_project_roadmap(
    project_id: int,
    req: RoadmapSaveRequest,
) -> dict[str, Any]:
```

Pass the four guard fields to `save_roadmap_draft_service`.

- [ ] **Step 5: Run phase/API tests**

Run:

```bash
uv run --frozen pytest tests/test_roadmap_phase_service.py tests/test_api_roadmap_flow.py -q
```

Expected: pass.

## Task 3: Enforce Exact Backlog Coverage Before Roadmap Save

**Files:**
- Modify: `services/phases/roadmap_service.py`
- Test: `tests/test_roadmap_phase_service.py`

- [ ] **Step 1: Write failing coverage tests**

Add tests:

```python
@pytest.mark.asyncio
async def test_save_roadmap_draft_blocks_unknown_backlog_items() -> None:
    state: JsonDict = {
        "fsm_state": "ROADMAP_REVIEW",
        "roadmap_attempts": [{"attempt_id": "roadmap-attempt-1", "artifact_fingerprint": "sha256:" + "a" * 64}],
        "backlog_items": [{"requirement": "A"}],
        "product_roadmap_assessment": {
            "attempt_id": "roadmap-attempt-1",
            "artifact_fingerprint": "sha256:" + "a" * 64,
            "roadmap_releases": [
                {
                    "release_name": "Milestone 1",
                    "theme": "Foundation",
                    "focus_area": "Technical Foundation",
                    "items": ["A", "Invented item"],
                    "reasoning": "Bad item",
                }
            ],
            "roadmap_summary": "Final",
            "is_complete": True,
            "clarifying_questions": [],
        },
    }

    with pytest.raises(RoadmapPhaseError, match="Roadmap coverage mismatch"):
        await save_roadmap_draft(
            project_id=7,
            attempt_id="roadmap-attempt-1",
            expected_artifact_fingerprint="sha256:" + "a" * 64,
            expected_state="ROADMAP_REVIEW",
            idempotency_key="save-roadmap-1",
            save_state=lambda _state: None,
            now_iso=lambda: "2026-05-22T00:00:00Z",
            hydrate_context=lambda: _async_value(SimpleNamespace(state=dict(state))),
            build_tool_context=lambda context: context,
            save_roadmap_tool=_fake_save_roadmap_tool,
        )
```

```python
@pytest.mark.asyncio
async def test_save_roadmap_draft_blocks_missing_or_duplicate_backlog_items() -> None:
    state: JsonDict = {
        "fsm_state": "ROADMAP_REVIEW",
        "roadmap_attempts": [{"attempt_id": "roadmap-attempt-1", "artifact_fingerprint": "sha256:" + "a" * 64}],
        "backlog_items": [{"requirement": "A"}, {"requirement": "B"}],
        "product_roadmap_assessment": {
            "attempt_id": "roadmap-attempt-1",
            "artifact_fingerprint": "sha256:" + "a" * 64,
            "roadmap_releases": [
                {
                    "release_name": "Milestone 1",
                    "theme": "Foundation",
                    "focus_area": "Technical Foundation",
                    "items": ["A", "A"],
                    "reasoning": "Duplicate A, missing B",
                }
            ],
            "roadmap_summary": "Final",
            "is_complete": True,
            "clarifying_questions": [],
        },
    }

    with pytest.raises(RoadmapPhaseError) as exc_info:
        await save_roadmap_draft(...)

    assert "missing" in exc_info.value.detail
    assert "duplicate" in exc_info.value.detail
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
uv run --frozen pytest tests/test_roadmap_phase_service.py -q
```

Expected: fail because Roadmap save does not enforce coverage.

- [ ] **Step 3: Implement coverage validator**

Add:

```python
def _expected_backlog_requirements(state: dict[str, Any]) -> list[str]:
    items = state.get("backlog_items")
    if not isinstance(items, list):
        return []
    requirements: list[str] = []
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("requirement"), str):
            requirement = item["requirement"].strip()
            if requirement:
                requirements.append(requirement)
    return requirements


def _roadmap_release_items(assessment: dict[str, Any]) -> list[str]:
    releases = assessment.get("roadmap_releases")
    if not isinstance(releases, list):
        return []
    selected: list[str] = []
    for release in releases:
        if not isinstance(release, dict):
            continue
        items = release.get("items")
        if not isinstance(items, list):
            continue
        selected.extend(item.strip() for item in items if isinstance(item, str) and item.strip())
    return selected


def _assert_exact_backlog_coverage(
    *,
    state: dict[str, Any],
    assessment: dict[str, Any],
) -> None:
    expected = _expected_backlog_requirements(state)
    actual = _roadmap_release_items(assessment)
    expected_set = set(expected)
    actual_set = set(actual)
    duplicates = sorted({item for item in actual if actual.count(item) > 1})
    missing = sorted(expected_set - actual_set)
    unknown = sorted(actual_set - expected_set)
    if missing or unknown or duplicates:
        raise RoadmapPhaseError(
            "Roadmap coverage mismatch: "
            f"missing={missing}, unknown={unknown}, duplicate={duplicates}",
            status_code=409,
        )
```

Call this before `RoadmapBuilderOutput.model_validate(assessment)` in `save_roadmap_draft`.

- [ ] **Step 4: Run tests and verify pass**

Run:

```bash
uv run --frozen pytest tests/test_roadmap_phase_service.py -q
```

Expected: pass.

## Task 4: Add RoadmapPhaseRunner

**Files:**
- Create: `services/agent_workbench/roadmap_phase.py`
- Test: `tests/test_agent_workbench_roadmap_phase.py`

- [ ] **Step 1: Write failing runner tests**

Create `tests/test_agent_workbench_roadmap_phase.py`:

```python
"""Tests for agent workbench Roadmap phase runner."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from services.agent_workbench.roadmap_phase import RoadmapPhaseRunner


class _FakeProductRepo:
    def get_by_id(self, product_id: int) -> SimpleNamespace:
        return SimpleNamespace(
            product_id=product_id,
            name="Cartola",
            vision="A clear saved vision.",
            spec_file_path="specs/spec.json",
            compiled_authority_json='{"authority": true}',
        )


class _FakeWorkflowService:
    def __init__(self) -> None:
        self.state: dict[str, Any] = {
            "fsm_state": "BACKLOG_PERSISTENCE",
            "setup_status": "passed",
            "product_vision_assessment": {
                "product_vision_statement": "A clear saved vision.",
                "is_complete": True,
            },
            "backlog_items": [{"priority": 1, "requirement": "Choose squad", "value_driver": "Strategic", "justification": "Core", "estimated_effort": "M"}],
        }

    def get_session_status(self, session_id: str) -> dict[str, Any]:
        return dict(self.state)

    async def initialize_session(self, session_id: str) -> str:
        return session_id

    def update_session_status(self, session_id: str, partial_update: dict[str, Any]) -> None:
        self.state.update(partial_update)


def test_roadmap_generate_hydrates_vision_backlog_spec_and_authority(monkeypatch: object) -> None:
    captured: dict[str, Any] = {}

    def fake_select_project(product_id: int, tool_context: object) -> dict[str, Any]:
        tool_context.state["pending_spec_content"] = "SPEC CONTENT"
        tool_context.state["compiled_authority_cached"] = "AUTHORITY JSON"
        return {"success": True, "project_id": product_id}

    async def fake_run_roadmap_agent_from_state(
        state: dict[str, Any],
        *,
        project_id: int,
        user_input: str | None,
    ) -> dict[str, Any]:
        captured["state"] = dict(state)
        return {
            "success": True,
            "input_context": {
                "product_vision": state["product_vision_assessment"]["product_vision_statement"],
                "backlog_items": state["backlog_items"],
                "technical_spec": state["pending_spec_content"],
                "compiled_authority": state["compiled_authority_cached"],
            },
            "output_artifact": {
                "roadmap_releases": [],
                "roadmap_summary": "Need more detail",
                "is_complete": False,
                "clarifying_questions": ["Which milestone boundary?"],
            },
            "is_complete": False,
            "error": None,
        }

    monkeypatch.setattr("services.agent_workbench.roadmap_phase.select_project", fake_select_project)
    monkeypatch.setattr("services.agent_workbench.roadmap_phase.run_roadmap_agent_from_state", fake_run_roadmap_agent_from_state)

    runner = RoadmapPhaseRunner(
        product_repo=_FakeProductRepo(),
        workflow_service=_FakeWorkflowService(),
    )

    result = runner.generate(project_id=2)

    assert result["ok"] is True
    assert captured["state"]["pending_spec_content"] == "SPEC CONTENT"
    assert captured["state"]["compiled_authority_cached"] == "AUTHORITY JSON"
    assert captured["state"]["backlog_items"][0]["requirement"] == "Choose squad"
```

Also add a runtime failure test expecting `ok: false`, error code `MUTATION_FAILED`, and details with `roadmap_run_success: false`.

- [ ] **Step 2: Run tests and verify fail**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_roadmap_phase.py -q
```

Expected: import fails because `roadmap_phase.py` does not exist.

- [ ] **Step 3: Implement runner**

Create `services/agent_workbench/roadmap_phase.py` by mirroring `BacklogPhaseRunner`, with:

```python
class RoadmapPhaseRunner:
    def generate(self, *, project_id: int, user_input: str | None = None) -> dict[str, Any]: ...
    def history(self, *, project_id: int) -> dict[str, Any]: ...
    def save(
        self,
        *,
        project_id: int,
        attempt_id: str,
        expected_artifact_fingerprint: str,
        expected_state: str,
        idempotency_key: str,
    ) -> dict[str, Any]: ...
```

Use:

```python
from orchestrator_agent.agent_tools.roadmap_builder.tools import save_roadmap_tool
from services.roadmap_runtime import run_roadmap_agent_from_state
from services.phases.roadmap_service import (
    RoadmapPhaseError,
    generate_roadmap_draft,
    get_roadmap_history,
    save_roadmap_draft,
)
```

Hydration rules:

```python
def _assert_required_context(state: dict[str, Any]) -> None:
    missing: list[str] = []
    if not state.get("pending_spec_content"):
        missing.append("pending_spec_content")
    if not state.get("compiled_authority_cached"):
        missing.append("compiled_authority_cached")
    if not _vision_text(state):
        missing.append("product_vision_assessment.product_vision_statement")
    backlog_items = state.get("backlog_items")
    if not isinstance(backlog_items, list) or not backlog_items:
        missing.append("backlog_items")
    if missing:
        raise RoadmapPhaseError(
            "Setup required: Roadmap context hydration missing " + ", ".join(missing)
        )
```

Map runtime failures:

```python
def _roadmap_runtime_error(*, project_id: int, data: dict[str, Any]) -> dict[str, Any]:
    return _error_envelope(
        ErrorCode.MUTATION_FAILED,
        str(data.get("failure_summary") or data.get("error") or "Roadmap generation failed."),
        details={
            "project_id": project_id,
            "roadmap_run_success": False,
            "failure_stage": data.get("failure_stage"),
            "failure_artifact_id": data.get("failure_artifact_id"),
            "attempt_count": data.get("attempt_count"),
            "fsm_state": data.get("fsm_state"),
        },
        remediation=[
            "Inspect agileforge roadmap history --project-id <project_id>.",
            "Fix the Roadmap runtime/provider configuration or refine the input.",
        ],
    )
```

- [ ] **Step 4: Run tests and verify pass**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_roadmap_phase.py -q
```

Expected: pass.

## Task 5: Install Roadmap CLI and Command Contracts

**Files:**
- Modify: `services/agent_workbench/application.py`
- Modify: `cli/main.py`
- Modify: `services/agent_workbench/command_registry.py`
- Test: `tests/test_agent_workbench_application.py`
- Test: `tests/test_agent_workbench_cli.py`
- Test: `tests/test_agent_workbench_command_schema.py`

- [ ] **Step 1: Write failing facade/CLI/schema tests**

In `tests/test_agent_workbench_application.py`, add `_FakeRoadmapRunner` and assert:

```python
app.roadmap_generate(project_id=PROJECT_ID)["data"]["is_complete"] is False
app.roadmap_history(project_id=PROJECT_ID)["data"]["items"] == []
app.roadmap_save(
    project_id=PROJECT_ID,
    attempt_id="roadmap-attempt-1",
    expected_artifact_fingerprint="sha256:" + "a" * 64,
    expected_state="ROADMAP_REVIEW",
    idempotency_key="save-roadmap-1",
)["data"]["fsm_state"] == "ROADMAP_PERSISTENCE"
```

In workflow-next tests, change `test_application_workflow_next_reports_roadmap_after_backlog_save` expected result from blocked future command to:

```python
assert result["data"]["next_valid_commands"] == [
    "agileforge roadmap generate --project-id 7"
]
assert result["data"]["blocked_future_commands"] == []
```

Add tests for `ROADMAP_INTERVIEW`, `ROADMAP_REVIEW`, and `ROADMAP_PERSISTENCE`:

```python
ROADMAP_REVIEW -> [
    "agileforge roadmap save --project-id 7 --attempt-id <attempt_id> --expected-artifact-fingerprint <artifact_fingerprint> --expected-state ROADMAP_REVIEW --idempotency-key <idempotency_key>",
    "agileforge roadmap generate --project-id 7 --input <feedback>",
]
ROADMAP_PERSISTENCE -> blocked future "agileforge story generate --project-id 7"
```

In CLI tests, add parametrized routes for:

```bash
agileforge roadmap generate --project-id 7 --input "group MVP"
agileforge roadmap history --project-id 7
agileforge roadmap save --project-id 7 --attempt-id roadmap-attempt-1 --expected-artifact-fingerprint sha256:<64 a> --expected-state ROADMAP_REVIEW --idempotency-key save-roadmap-1
```

In command schema tests, require:

```python
"agileforge roadmap generate"
"agileforge roadmap history"
"agileforge roadmap save"
```

Roadmap save required fields:

```python
["project_id", "attempt_id", "expected_artifact_fingerprint", "expected_state", "idempotency_key"]
```

- [ ] **Step 2: Run tests and verify fail**

Run:

```bash
uv run --frozen pytest \
  tests/test_agent_workbench_application.py \
  tests/test_agent_workbench_cli.py \
  tests/test_agent_workbench_command_schema.py \
  -q
```

Expected: fail because Roadmap facade/CLI/registry are missing.

- [ ] **Step 3: Implement facade and workflow routing**

In `application.py`, add `_RoadmapPhaseRunner` protocol and constructor field `roadmap_runner`. Add methods:

```python
def roadmap_generate(...): ...
def roadmap_history(...): ...
def roadmap_save(...): ...
```

Add lazy loader:

```python
def _get_roadmap_runner(self) -> _RoadmapPhaseRunner:
    if self._roadmap_runner is None:
        from services.agent_workbench.roadmap_phase import RoadmapPhaseRunner
        self._roadmap_runner = RoadmapPhaseRunner()
    return self._roadmap_runner
```

Add `_roadmap_workflow_next` after `_backlog_workflow_next`:

```python
if fsm_state == "BACKLOG_PERSISTENCE":
    return roadmap generate
if fsm_state == "ROADMAP_INTERVIEW":
    return roadmap generate
if fsm_state == "ROADMAP_REVIEW":
    return roadmap save + roadmap generate --input
if fsm_state == "ROADMAP_PERSISTENCE":
    return blocked future story generate
```

- [ ] **Step 4: Implement CLI parser/routes**

In `cli/main.py`, add `roadmap` subparser with `generate`, `history`, `save`.

Add protocol methods and route functions:

```python
def _roadmap_generate(args, application): ...
def _roadmap_history(args, application): ...
def _roadmap_save(args, application): ...
```

- [ ] **Step 5: Register command metadata**

In `command_registry.py`, add:

```python
CommandMetadata(
    name="agileforge roadmap generate",
    mutates=True,
    phase="phase_2d",
    input_required=("project_id",),
    input_optional=("input",),
    errors=(ErrorCode.PROJECT_NOT_FOUND.value, ErrorCode.AUTHORITY_NOT_ACCEPTED.value, ErrorCode.INVALID_COMMAND.value, ErrorCode.WORKFLOW_SESSION_FAILED.value, ErrorCode.MUTATION_FAILED.value),
)
CommandMetadata(
    name="agileforge roadmap history",
    mutates=False,
    phase="phase_2d",
    input_required=("project_id",),
)
CommandMetadata(
    name="agileforge roadmap save",
    mutates=True,
    phase="phase_2d",
    requires_idempotency_key=True,
    input_required=("project_id", "attempt_id", "expected_artifact_fingerprint", "expected_state", "idempotency_key"),
    errors=(ErrorCode.PROJECT_NOT_FOUND.value, ErrorCode.AUTHORITY_NOT_ACCEPTED.value, ErrorCode.INVALID_COMMAND.value, ErrorCode.WORKFLOW_SESSION_FAILED.value, ErrorCode.MUTATION_FAILED.value),
)
```

- [ ] **Step 6: Run tests and verify pass**

Run the same focused tests. Expected: pass.

## Task 6: Runtime and Tool Consistency

**Files:**
- Modify: `services/roadmap_runtime.py`
- Modify: `orchestrator_agent/agent_tools/roadmap_builder/tools.py`
- Test: `tests/test_runtime_failure_artifacts.py`
- Test: `tests/test_roadmap_builder_tools.py` if present, otherwise `tests/test_api_roadmap_flow.py`

- [ ] **Step 1: Write failing runtime consistency test**

In `tests/test_runtime_failure_artifacts.py`, add:

```python
@pytest.mark.asyncio
async def test_roadmap_runtime_forces_incomplete_when_questions_remain(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_invoke(_payload: object) -> str:
        return (
            '{"roadmap_releases":[],"roadmap_summary":"Draft",'
            '"is_complete":true,"clarifying_questions":["Which release boundary?"]}'
        )

    monkeypatch.setattr(roadmap_runtime, "_invoke_roadmap_agent", fake_invoke)

    result = await roadmap_runtime.run_roadmap_agent_from_state(
        _roadmap_state(),
        project_id=1,
        user_input="",
    )

    assert result["success"] is True
    assert result["is_complete"] is False
    assert result["output_artifact"]["is_complete"] is False
```

- [ ] **Step 2: Run and verify fail**

Run:

```bash
uv run --frozen pytest tests/test_runtime_failure_artifacts.py::test_roadmap_runtime_forces_incomplete_when_questions_remain -q
```

Expected: fail because runtime trusts `is_complete`.

- [ ] **Step 3: Implement runtime consistency**

In `roadmap_runtime.py`, after `output_artifact`:

```python
if _has_clarifying_questions(output_artifact):
    output_artifact["is_complete"] = False
```

Add local helper:

```python
def _has_clarifying_questions(artifact: dict[str, Any]) -> bool:
    questions = artifact.get("clarifying_questions")
    return isinstance(questions, list) and any(
        isinstance(question, str) and bool(question.strip()) for question in questions
    )
```

- [ ] **Step 4: Add idempotency metadata to save tool**

Extend `SaveRoadmapToolInput`:

```python
idempotency_key: str | None = Field(default=None)
```

In `save_roadmap_tool`, include metadata:

```python
metadata = json.dumps(
    {
        "action": "roadmap_saved",
        "idempotency_key": input_data.idempotency_key,
        "releases_count": len(input_data.roadmap_data.roadmap_releases),
    }
)
```

Return:

```python
"saved_roadmap": input_data.roadmap_data.model_dump(mode="json"),
```

- [ ] **Step 5: Run runtime/API tests**

Run:

```bash
uv run --frozen pytest tests/test_runtime_failure_artifacts.py tests/test_api_roadmap_flow.py -q
```

Expected: pass.

## Task 7: Update CLI Documentation

**Files:**
- Modify: `docs/agent-cli-manual.md`

- [ ] **Step 1: Replace “Roadmap not installed” section**

Change the Backlog-save section from blocked Roadmap to installed Roadmap:

```md
After Backlog save, the next installed command should be Roadmap generation:

```sh
agileforge roadmap generate --project-id "$PROJECT_ID" > roadmap-generate.json
```
```

Add save command:

```sh
agileforge roadmap save \
  --project-id "$PROJECT_ID" \
  --attempt-id "$ATTEMPT_ID" \
  --expected-artifact-fingerprint "$ARTIFACT_FINGERPRINT" \
  --expected-state ROADMAP_REVIEW \
  --idempotency-key "save-roadmap-$PROJECT_ID-$(date +%Y%m%d%H%M%S)" \
  > roadmap-save.json
```

Document that Roadmap save requires exact backlog coverage and blocks missing, duplicate, or invented backlog items.

- [ ] **Step 2: Run docs grep**

Run:

```bash
rg -n "Roadmap Commands Not Yet Installed|Do not call roadmap commands|roadmap generate" docs/agent-cli-manual.md
```

Expected: no remaining instruction says Roadmap CLI is not installed.

## Task 8: Verification and caRtola Live CLI Run

**Files:**
- No code edits unless verification exposes a bug.

- [ ] **Step 1: Run focused tests**

```bash
uv run --frozen pytest \
  tests/test_roadmap_phase_service.py \
  tests/test_agent_workbench_roadmap_phase.py \
  tests/test_agent_workbench_application.py \
  tests/test_agent_workbench_cli.py \
  tests/test_agent_workbench_command_schema.py \
  tests/test_api_roadmap_flow.py \
  tests/test_runtime_failure_artifacts.py \
  -q
```

Expected: pass.

- [ ] **Step 2: Run full test suite and lint**

```bash
uv run --frozen pytest -q
uv run --frozen ruff check .
```

Expected: pass.

Use touched-file formatting check instead of whole-repo formatting unless unrelated formatting drift is resolved:

```bash
uv run --frozen ruff format --check \
  services/agent_workbench/roadmap_phase.py \
  services/phases/roadmap_service.py \
  services/roadmap_runtime.py \
  orchestrator_agent/agent_tools/roadmap_builder/tools.py \
  services/agent_workbench/application.py \
  cli/main.py \
  services/agent_workbench/command_registry.py \
  api.py \
  tests/test_agent_workbench_roadmap_phase.py \
  tests/test_roadmap_phase_service.py \
  tests/test_api_roadmap_flow.py
```

- [ ] **Step 3: Verify installed CLI contracts**

```bash
agileforge roadmap --help
agileforge command schema "agileforge roadmap save" | python -m json.tool
```

Expected: `generate`, `history`, and guarded `save` exist.

- [ ] **Step 4: Run caRtola Roadmap generate**

```bash
cd /Users/aaat/projects/caRtola
PROJECT_ID=2
agileforge workflow next --project-id "$PROJECT_ID" | python -m json.tool
agileforge roadmap generate --project-id "$PROJECT_ID" > roadmap-generate.json
python -m json.tool roadmap-generate.json >/dev/null
```

If provider/ZDR fails, stop and report the model/provider error. That is a valid fail-closed result.

- [ ] **Step 5: Save only a complete reviewed Roadmap**

If `roadmap-generate.json` has `ok: true` and `data.is_complete: true`:

```bash
ATTEMPT_ID="$(
  python - <<'PY'
import json
from pathlib import Path
payload = json.loads(Path("roadmap-generate.json").read_text())
print(payload["data"]["attempt_id"])
PY
)"

ARTIFACT_FINGERPRINT="$(
  python - <<'PY'
import json
from pathlib import Path
payload = json.loads(Path("roadmap-generate.json").read_text())
print(payload["data"]["artifact_fingerprint"])
PY
)"

agileforge roadmap save \
  --project-id "$PROJECT_ID" \
  --attempt-id "$ATTEMPT_ID" \
  --expected-artifact-fingerprint "$ARTIFACT_FINGERPRINT" \
  --expected-state ROADMAP_REVIEW \
  --idempotency-key "save-roadmap-$PROJECT_ID-$(date +%Y%m%d%H%M%S)" \
  > roadmap-save.json
```

- [ ] **Step 6: Confirm next phase**

```bash
agileforge status --project-id "$PROJECT_ID" | python - <<'PY'
import json, sys
payload = json.load(sys.stdin)
project = payload["data"]["project"]
print({
    "roadmap_present": project["roadmap_present"],
    "active_user_stories": project["structure_counts"]["user_stories"],
})
PY

agileforge workflow next --project-id "$PROJECT_ID" | python -m json.tool
```

Expected after save:

```text
roadmap_present: true
active_user_stories: 10
workflow next blocks on story CLI not installed, not sprint planning
```

---

## Self-Review

- The plan installs Roadmap CLI without skipping Roadmap.
- The plan keeps fail-closed behavior for runtime/provider errors and invalid saves.
- The plan applies the Backlog safety standard to Roadmap: attempt-aware save, artifact fingerprint guard, expected FSM state, idempotency key.
- The plan adds a Roadmap-specific semantic guard: every canonical active backlog item must appear exactly once in Roadmap releases.
- The plan avoids manual interviews and keeps the caRtola validation path CLI-only.
