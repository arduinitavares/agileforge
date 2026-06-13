# Sprint Generation Retryable Errors Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Normalize Sprint generation model-response failures as structured retryable errors instead of generic mutation failures.

**Architecture:** Preserve existing runtime attempt recording and FSM behavior. Add a registered retryable error code, propagate it on failed generation payloads for model-response failure stages, and map CLI errors through that code with attempt persistence and retry details.

**Tech Stack:** Python 3.13, pytest, AgileForge agent workbench services.

---

## File Structure

- Modify `services/agent_workbench/error_codes.py`: register `SPRINT_GENERATION_MODEL_RESPONSE_INVALID`.
- Modify `services/agent_workbench/command_registry.py`: list the new error for `agileforge sprint generate`.
- Modify `services/phases/sprint_service.py`: add `error_code` and `attempt_persisted` to failed generation payloads for retryable model-response stages.
- Modify `services/agent_workbench/sprint_phase.py`: map retryable Sprint runtime failures to the new error code and include retry metadata.
- Modify `tests/test_agent_workbench_sprint_phase.py`: add CLI facade regression.
- Modify `tests/test_api_sprint_flow.py`: add API payload regression.
- Modify `tests/test_agent_workbench_command_schema.py`: add command schema regression.

## Task 1: Add RED CLI Facade Regression

**Files:**
- Modify `tests/test_agent_workbench_sprint_phase.py`

- [ ] **Step 1: Add failing test**

Add a test near existing Sprint runner generate tests:

```python
def test_sprint_runner_generate_normalizes_invalid_model_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Model-response Sprint failures should be retryable structured errors."""

    async def fake_run_sprint_agent(_state: object, **_kwargs: object) -> JsonDict:
        return {
            "success": False,
            "input_context": {"available_stories": []},
            "output_artifact": {
                "error": "SPRINT_GENERATION_FAILED",
                "message": "Sprint response is not valid JSON",
                "is_complete": False,
            },
            "is_complete": None,
            "error": "Sprint response is not valid JSON",
            "failure_artifact_id": "sprint-failure-001",
            "failure_stage": "invalid_json",
            "failure_summary": "Sprint response is not valid JSON",
            "raw_output_preview": "",
            "has_full_artifact": True,
        }

    monkeypatch.setattr(
        sprint_phase_module,
        "run_sprint_agent_from_state",
        fake_run_sprint_agent,
    )
    monkeypatch.setattr(
        sprint_service,
        "load_sprint_candidates",
        lambda _project_id, **_kwargs: {
            "success": True,
            "count": 1,
            "stories": [{"story_id": 1}],
            "readiness": {"status": "ready"},
        },
    )
    monkeypatch.setattr(
        SprintPhaseRunner,
        "_current_planned_sprint_id",
        lambda _self, _project_id: None,
    )
    runner = SprintPhaseRunner(
        product_repo=cast("Any", _FakeProductRepository()),
        workflow_service=cast("Any", _FakeWorkflowService()),
    )

    result = runner.generate(project_id=7, max_story_points=9)

    assert result["ok"] is False
    error = result["errors"][0]
    assert error["code"] == "SPRINT_GENERATION_MODEL_RESPONSE_INVALID"
    assert error["message"] == "Sprint response is not valid JSON"
    assert error["details"]["failure_stage"] == "invalid_json"
    assert error["details"]["failure_artifact_id"] == "sprint-failure-001"
    assert error["details"]["attempt_id"] == "sprint-attempt-1"
    assert error["details"]["attempt_count"] == 1
    assert error["details"]["attempt_persisted"] is True
    assert error["details"]["fsm_state"] == "SPRINT_SETUP"
    assert (
        error["details"]["safe_retry_command"]
        == "agileforge sprint generate --project-id 7"
    )
    assert "Retry agileforge sprint generate" in error["remediation"][-1]
```

- [ ] **Step 2: Run RED test**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_sprint_phase.py -q -k "normalizes_invalid_model_response"
```

Expected: FAIL because the current code returns `MUTATION_FAILED`.

## Task 2: Add RED API And Command Schema Regressions

**Files:**
- Modify `tests/test_api_sprint_flow.py`
- Modify `tests/test_agent_workbench_command_schema.py`

- [ ] **Step 1: Add API payload assertion**

Extend `test_sprint_generate_failure_stays_in_setup_and_records_attempt`:

```python
    assert payload["data"]["error_code"] == "SPRINT_GENERATION_MODEL_RESPONSE_INVALID"
    assert payload["data"]["attempt_persisted"] is True
```

- [ ] **Step 2: Add command schema assertion**

In the Sprint command schema test, assert:

```python
    assert "SPRINT_GENERATION_MODEL_RESPONSE_INVALID" in generate["errors"]
```

- [ ] **Step 3: Run RED tests**

Run:

```bash
uv run --frozen pytest tests/test_api_sprint_flow.py -q -k "sprint_generate_failure_stays_in_setup"
uv run --frozen pytest tests/test_agent_workbench_command_schema.py -q -k "sprint"
```

Expected: FAIL for the new API/schema assertions.

## Task 3: Implement Error Code And Payload Propagation

**Files:**
- Modify `services/agent_workbench/error_codes.py`
- Modify `services/agent_workbench/command_registry.py`
- Modify `services/phases/sprint_service.py`
- Modify `services/agent_workbench/sprint_phase.py`

- [ ] **Step 1: Register error code**

Add `SPRINT_GENERATION_MODEL_RESPONSE_INVALID` to `ErrorCode` and `_ERROR_REGISTRY`
with `default_exit_code=1`, `retryable=True`, and description
`Sprint generation produced an invalid model response.`

- [ ] **Step 2: Register command schema**

Add `ErrorCode.SPRINT_GENERATION_MODEL_RESPONSE_INVALID.value` to the
`agileforge sprint generate` command metadata errors.

- [ ] **Step 3: Add shared stage predicate**

In `services/phases/sprint_service.py`, add:

```python
RETRYABLE_SPRINT_MODEL_FAILURE_STAGES = {
    "invalid_json",
    "output_validation",
    "invocation_exception",
}
```

- [ ] **Step 4: Propagate error metadata**

After `attempt_id` is computed in `generate_sprint_plan()`, set:

```python
model_response_error_code = (
    "SPRINT_GENERATION_MODEL_RESPONSE_INVALID"
    if not bool(sprint_result.get("success"))
    and failure_meta.get("failure_stage") in RETRYABLE_SPRINT_MODEL_FAILURE_STAGES
    else None
)
attempt_persisted = attempt_count > 0
```

Include `error_code=model_response_error_code` only when non-null and include
`attempt_persisted` in the returned payload.

- [ ] **Step 5: Map facade error**

In `_sprint_runtime_error()`, choose
`ErrorCode.SPRINT_GENERATION_MODEL_RESPONSE_INVALID` when `data["error_code"]`
matches it. Add `attempt_id`, `attempt_persisted`, and
`safe_retry_command` to details. Add remediation ending with:

```python
f"Retry agileforge sprint generate --project-id {project_id} from the current workflow next route."
```

- [ ] **Step 6: Run GREEN tests**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_sprint_phase.py -q -k "normalizes_invalid_model_response"
uv run --frozen pytest tests/test_api_sprint_flow.py -q -k "sprint_generate_failure_stays_in_setup"
uv run --frozen pytest tests/test_agent_workbench_command_schema.py -q -k "sprint"
```

Expected: PASS.

## Task 4: Verify And Commit

**Files:**
- Verify all touched areas.

- [ ] **Step 1: Run focused tests**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_sprint_phase.py -q -k "generate"
uv run --frozen pytest tests/test_api_sprint_flow.py -q -k "sprint_generate"
uv run --frozen pytest tests/test_agent_workbench_command_schema.py -q -k "sprint"
```

Expected: PASS.

- [ ] **Step 2: Run full gate**

Run:

```bash
pyrepo-check --all
```

Expected: PASS.

- [ ] **Step 3: Commit**

Run:

```bash
git add services/agent_workbench/error_codes.py services/agent_workbench/command_registry.py services/phases/sprint_service.py services/agent_workbench/sprint_phase.py tests/test_agent_workbench_sprint_phase.py tests/test_api_sprint_flow.py tests/test_agent_workbench_command_schema.py docs/superpowers/specs/2026-06-13-sprint-generation-retryable-errors-design.md docs/superpowers/plans/2026-06-13-sprint-generation-retryable-errors.md
git commit -m "fix(sprint): normalize retryable generation failures"
```

Expected: commit succeeds on `dev/sprint-generation-retryable-errors`.

## Self-Review

- Spec coverage: tasks cover error registration, CLI mapping, API payload, schema, tests, and full verification.
- Placeholder scan: no TODO/TBD placeholders.
- Type consistency: error-code string, detail keys, and command names match the design.
