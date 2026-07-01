# Issue 180 Story Generate Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix issue #180 so Story generation can recover schema-valid JSON from noisy model output, avoid retry lockouts after hard runtime failures, and expose feedback soft-gate warnings clearly.

**Architecture:** Keep JSON extraction in `utils/adk_runner.py`, but add schema-key-aware recovery so Story runtime can select a real `UserStoryWriterOutput` object instead of a nested or unrelated JSON snippet. Keep Story phase state rules in `services/phases/story_service.py`; failed runtime artifacts must be reported as runtime failures, not empty clarification drafts. Keep CLI envelope warning projection in `services/agent_workbench/story_phase.py`.

**Tech Stack:** Python 3.13, Pydantic, pytest, Ruff, ty, AgileForge CLI via `uv run --frozen`.

---

## File Structure

- Modify `utils/adk_runner.py`
  - Add optional `required_keys` support to `parse_json_payload`.
  - Add small private helpers for candidate parsing and key checks.
- Create `tests/test_adk_runner.py`
  - Unit-test multi-object JSON recovery without invoking agents.
- Modify `services/story_runtime.py`
  - Pass Story schema keys into `parse_json_payload` for Story and Story patch outputs.
- Modify `services/phases/story_service.py`
  - Do not feedback-soft-gate retries after a hard schema/provider failure with no working draft.
  - Project failed runtime artifacts as blocking quality findings.
- Modify `tests/test_story_phase_service.py`
  - Add service-level regressions for weak feedback after hard schema failure and failure-artifact quality projection.
  - Keep existing quality-gate draft soft-gate behavior intact.
- Modify `services/agent_workbench/story_phase.py`
  - Propagate feedback soft-gate warnings into the outer CLI/API envelope.
- Modify `tests/test_agent_workbench_story_phase.py`
  - Verify `warnings` exposes missing feedback fields when generation does not run.

---

### Task 1: Parser Recovery

**Files:**
- Modify: `utils/adk_runner.py`
- Create: `tests/test_adk_runner.py`

- [ ] **Step 1: Write failing parser tests**

Create `tests/test_adk_runner.py`:

```python
"""Tests for ADK JSON response parsing."""

from __future__ import annotations

import json

from utils.adk_runner import parse_json_payload


STORY_OUTPUT_KEYS = {
    "parent_requirement",
    "user_stories",
    "is_complete",
    "clarifying_questions",
}


def test_parse_json_payload_selects_schema_object_from_multi_object_text() -> None:
    """Recover the first schema-shaped JSON object from prose plus two objects."""
    first = {
        "parent_requirement": "First Model Baseline Evaluation and Reporting",
        "user_stories": [{"story_title": "Compare model to baselines"}],
        "is_complete": True,
        "clarifying_questions": [],
        "coverage_status": "complete",
    }
    second = {
        "parent_requirement": "First Model Baseline Evaluation and Reporting",
        "user_stories": [{"story_title": "Publish baseline report"}],
        "is_complete": True,
        "clarifying_questions": [],
        "coverage_status": "complete",
    }
    raw_text = (
        "Reasoning before JSON.\n"
        f"{json.dumps(first)}\n"
        "More prose between candidate objects.\n"
        f"{json.dumps(second)}\n"
    )

    parsed = parse_json_payload(raw_text, required_keys=STORY_OUTPUT_KEYS)

    assert parsed is not None
    assert parsed["user_stories"][0]["story_title"] == "Compare model to baselines"


def test_parse_json_payload_skips_unrelated_json_before_schema_object() -> None:
    """Do not return a small example object when required keys are supplied."""
    raw_text = """
Example: {"not_the_payload": true}

{
  "parent_requirement": "Requirement A",
  "user_stories": [{"story_title": "Generate report"}],
  "is_complete": true,
  "clarifying_questions": [],
  "coverage_status": "complete"
}
"""

    parsed = parse_json_payload(raw_text, required_keys=STORY_OUTPUT_KEYS)

    assert parsed is not None
    assert parsed["parent_requirement"] == "Requirement A"
    assert "not_the_payload" not in parsed


def test_parse_json_payload_preserves_fenced_json_behavior() -> None:
    """Existing fenced JSON behavior stays intact."""
    raw_text = """```json
{"ok": true}
```"""

    assert parse_json_payload(raw_text) == {"ok": True}
```

- [ ] **Step 2: Run parser tests and confirm failure**

Run:

```bash
uv run --frozen pytest tests/test_adk_runner.py -q
```

Expected: failure because `parse_json_payload()` does not accept `required_keys`.

- [ ] **Step 3: Implement schema-key-aware JSON candidate recovery**

In `utils/adk_runner.py`, import `Collection` under `TYPE_CHECKING` or directly from `collections.abc`, then replace `parse_json_payload` and add helpers:

```python
from collections.abc import Collection, Iterator
```

```python
def parse_json_payload(
    raw_text: str,
    *,
    required_keys: Collection[str] | None = None,
) -> dict[str, Any] | None:
    """Parse a JSON object from raw model text or a fenced JSON block."""
    candidate = (raw_text or "").strip()
    if not candidate:
        return None

    fenced = re.search(
        r"```(?:json)?\s*(.*?)\s*```",
        candidate,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if fenced:
        candidate = fenced.group(1).strip()

    parsed = _parse_json_dict(candidate)
    if _json_dict_matches_required_keys(parsed, required_keys):
        return parsed

    for parsed in _iter_json_dict_candidates(candidate):
        if _json_dict_matches_required_keys(parsed, required_keys):
            return parsed

    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    parsed = _parse_json_dict(candidate[start : end + 1])
    return parsed if _json_dict_matches_required_keys(parsed, required_keys) else None


def _parse_json_dict(candidate: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _iter_json_dict_candidates(candidate: str) -> Iterator[dict[str, Any]]:
    decoder = json.JSONDecoder()
    cursor = 0
    while cursor < len(candidate):
        start = candidate.find("{", cursor)
        if start == -1:
            return
        try:
            parsed, offset = decoder.raw_decode(candidate[start:])
        except json.JSONDecodeError:
            cursor = start + 1
            continue
        if isinstance(parsed, dict):
            yield parsed
        cursor = start + max(offset, 1)


def _json_dict_matches_required_keys(
    parsed: dict[str, Any] | None,
    required_keys: Collection[str] | None,
) -> bool:
    if parsed is None:
        return False
    if required_keys is None:
        return True
    return all(key in parsed for key in required_keys)
```

- [ ] **Step 4: Run parser tests and focused parser consumers**

Run:

```bash
uv run --frozen pytest tests/test_adk_runner.py tests/test_user_story_writer_integration.py -q
```

Expected: parser tests pass; existing Story integration parser tests still pass.

- [ ] **Step 5: Commit parser recovery**

```bash
git add utils/adk_runner.py tests/test_adk_runner.py
git commit -m "fix: recover schema json from noisy agent output"
```

---

### Task 2: Story Runtime Uses Schema Keys

**Files:**
- Modify: `services/story_runtime.py`
- Test: `tests/test_adk_runner.py`, `tests/test_user_story_writer_integration.py`

- [ ] **Step 1: Add Story output key constants**

In `services/story_runtime.py`, near the imports/constants, add:

```python
_USER_STORY_WRITER_OUTPUT_KEYS = frozenset(
    {
        "parent_requirement",
        "user_stories",
        "is_complete",
        "clarifying_questions",
    }
)
_USER_STORY_PATCH_OUTPUT_KEYS = frozenset(
    {
        "parent_requirement",
        "story",
        "is_complete",
        "clarifying_questions",
    }
)
```

- [ ] **Step 2: Pass required keys into parser calls**

Change the Story generation parser call:

```python
parsed = parse_json_payload(
    raw_text,
    required_keys=_USER_STORY_WRITER_OUTPUT_KEYS,
)
```

Change the Story patch parser call:

```python
parsed = parse_json_payload(
    raw_text,
    required_keys=_USER_STORY_PATCH_OUTPUT_KEYS,
)
```

- [ ] **Step 3: Run Story runtime-focused tests**

Run:

```bash
uv run --frozen pytest tests/test_user_story_writer_integration.py tests/test_user_story_writer_schemas.py tests/test_story_runtime.py -q
```

Expected: pass.

- [ ] **Step 4: Commit Story parser usage**

```bash
git add services/story_runtime.py
git commit -m "fix: select story schema json during parsing"
```

---

### Task 3: Retry After Hard Failure Should Run Generation

**Files:**
- Modify: `services/phases/story_service.py`
- Test: `tests/test_story_phase_service.py`

- [ ] **Step 1: Write failing service regression**

Add this test near `test_generate_story_draft_soft_gates_weak_feedback` in `tests/test_story_phase_service.py`:

```python
@pytest.mark.asyncio
async def test_generate_story_draft_weak_feedback_after_schema_failure_runs_generation() -> None:
    """Weak feedback after a hard schema failure should start a new generation."""
    parent_requirement = "Requirement A"
    artifact = _story_artifact(parent_requirement, "Recovered draft")
    state: JsonDict = {
        "roadmap_releases": [{"items": [parent_requirement]}],
        "interview_runtime": {
            "story": {
                parent_requirement: {
                    "phase": "story",
                    "subject_key": parent_requirement,
                    "attempt_history": [
                        {
                            "attempt_id": "attempt-1",
                            "classification": "nonreusable_schema_failure",
                            "failure_stage": "invalid_json",
                            "failure_summary": "Story response is not valid JSON",
                            "is_reusable": False,
                            "retryable": False,
                            "draft_kind": None,
                            "output_artifact": {
                                "error": "STORY_GENERATION_FAILED",
                                "message": "Story response is not valid JSON",
                                "failure_stage": "invalid_json",
                                "failure_summary": "Story response is not valid JSON",
                                "is_complete": False,
                            },
                        }
                    ],
                    "draft_projection": {},
                    "feedback_projection": {"items": [], "next_feedback_sequence": 0},
                    "request_projection": {},
                }
            }
        },
    }
    calls = {"agent": 0, "feedback": 0}

    async def fake_run_story_agent_from_state(
        *args: object,
        **kwargs: object,
    ) -> JsonDict:
        del args, kwargs
        calls["agent"] += 1
        return {
            "success": True,
            "input_context": {"requirement_context": "assembled"},
            "output_artifact": artifact,
            "classification": "reusable_content_result",
            "draft_kind": "complete_draft",
            "is_reusable": True,
            "is_complete": True,
            "request_payload": {"parent_requirement": parent_requirement},
            "error": None,
        }

    def fake_append_feedback_entry(*args: object, **kwargs: object) -> JsonDict:
        del args, kwargs
        calls["feedback"] += 1
        return {"feedback_id": "feedback-1"}

    payload = await generate_story_draft(
        project_id=7,
        parent_requirement=parent_requirement,
        user_input="Try again with this requirement only.",
        force_feedback=False,
        load_state=lambda: _async_value(state),
        save_state=lambda _updated: None,
        now_iso=lambda: "2026-06-09T00:00:00Z",
        run_story_agent_from_state=fake_run_story_agent_from_state,
        append_feedback_entry=fake_append_feedback_entry,
        set_request_projection=lambda runtime, **kwargs: (
            runtime.setdefault("request_projection", {}).update(kwargs)
            or runtime["request_projection"]
        ),
        append_attempt=lambda runtime, attempt: runtime.setdefault(
            "attempt_history", []
        ).append(attempt),
        promote_reusable_draft=lambda runtime, **kwargs: runtime.setdefault(
            "draft_projection", {}
        ).update(kwargs),
        mark_feedback_absorbed=lambda _runtime, **_kwargs: [],
        failure_meta=lambda *_args, **_kwargs: {},
    )

    assert calls == {"agent": 1, "feedback": 1}
    assert payload["fsm_state"] == "STORY_REVIEW"
    assert payload["data"]["generation_ran"] is True
    assert payload["data"]["feedback_quality"]["needs_revision"] is True
    runtime = state["interview_runtime"]["story"][parent_requirement]
    assert len(runtime["attempt_history"]) == 2
```

- [ ] **Step 2: Run regression and confirm failure**

Run:

```bash
uv run --frozen pytest tests/test_story_phase_service.py::test_generate_story_draft_weak_feedback_after_schema_failure_runs_generation -q
```

Expected: fail because current soft gate returns before agent execution.

- [ ] **Step 3: Implement gated soft-gate helper**

In `services/phases/story_service.py`, add helper near `story_retryable`:

```python
def _should_soft_gate_story_feedback(
    *,
    feedback_quality: dict[str, Any],
    force_feedback: bool,
    has_working_state: bool,
    latest_attempt: dict[str, Any] | None,
) -> bool:
    if not feedback_quality.get("needs_revision") or force_feedback:
        return False
    if has_working_state:
        return True
    latest_classification = (
        latest_attempt.get("classification")
        if isinstance(latest_attempt, dict)
        else None
    )
    return latest_classification == "quality_gate_failed"
```

Then change the check inside `generate_story_draft`:

```python
latest_attempt = _latest_story_attempt(runtime)
if _should_soft_gate_story_feedback(
    feedback_quality=feedback_quality,
    force_feedback=force_feedback,
    has_working_state=has_working_state,
    latest_attempt=latest_attempt,
):
    state["fsm_state"] = OrchestratorState.STORY_INTERVIEW.value
    save_state(state)
    return {
        "fsm_state": OrchestratorState.STORY_INTERVIEW.value,
        "parent_requirement": normalized_parent_requirement,
        "data": {
            "generation_ran": False,
            "feedback_quality": feedback_quality,
            **story_interview_summary(
                runtime,
                extension_metadata=extension_metadata,
            ),
        },
    }
```

- [ ] **Step 4: Run soft-gate Story tests**

Run:

```bash
uv run --frozen pytest \
  tests/test_story_phase_service.py::test_generate_story_draft_soft_gates_weak_feedback \
  tests/test_story_phase_service.py::test_generate_story_draft_force_feedback_runs_generation \
  tests/test_story_phase_service.py::test_generate_story_draft_weak_feedback_after_schema_failure_runs_generation \
  -q
```

Expected: all pass.

- [ ] **Step 5: Commit hard-failure retry behavior**

```bash
git add services/phases/story_service.py tests/test_story_phase_service.py
git commit -m "fix: allow story retry after schema failure"
```

---

### Task 4: Failed Runtime Artifacts Must Be Actionable

**Files:**
- Modify: `services/phases/story_service.py`
- Test: `tests/test_story_phase_service.py`

- [ ] **Step 1: Write failing quality projection test**

Add this unit test near existing `story_quality_summary` tests, or near the Story history tests if no direct block exists:

```python
def test_story_quality_summary_projects_runtime_failure_as_blocking_finding() -> None:
    """Runtime failures should not look like empty clarification drafts."""
    summary = story_quality_summary(
        {
            "error": "STORY_GENERATION_FAILED",
            "message": "Story response is not valid JSON",
            "failure_stage": "invalid_json",
            "failure_summary": "Story response is not valid JSON",
            "failure_artifact_id": "story-failure-1",
            "is_complete": False,
        }
    )

    assert summary["saveable"] is False
    assert summary["story_count"] == 0
    assert summary["coverage_status"] == "needs_clarification"
    assert summary["blocking_findings"] == [
        {
            "code": "STORY_RUNTIME_FAILURE",
            "severity": "blocking",
            "message": "Story response is not valid JSON",
            "failure_stage": "invalid_json",
            "failure_artifact_id": "story-failure-1",
        }
    ]
```

- [ ] **Step 2: Run regression and confirm failure**

Run:

```bash
uv run --frozen pytest tests/test_story_phase_service.py::test_story_quality_summary_projects_runtime_failure_as_blocking_finding -q
```

Expected: fail because current summary has empty findings.

- [ ] **Step 3: Implement runtime failure finding**

In `services/phases/story_service.py`, add helper near `_quality_findings_from_artifact`:

```python
def _runtime_failure_finding_from_artifact(
    artifact: dict[str, Any],
) -> dict[str, Any] | None:
    if not (
        artifact.get("error") == "STORY_GENERATION_FAILED"
        or artifact.get("failure_stage")
    ):
        return None
    message = str(
        artifact.get("failure_summary")
        or artifact.get("message")
        or artifact.get("error")
        or "Story generation failed."
    )
    finding: dict[str, Any] = {
        "code": "STORY_RUNTIME_FAILURE",
        "severity": "blocking",
        "message": message,
    }
    failure_stage = artifact.get("failure_stage")
    if isinstance(failure_stage, str) and failure_stage:
        finding["failure_stage"] = failure_stage
    failure_artifact_id = artifact.get("failure_artifact_id")
    if isinstance(failure_artifact_id, str) and failure_artifact_id:
        finding["failure_artifact_id"] = failure_artifact_id
    return finding
```

Then modify `_quality_findings_from_artifact`:

```python
def _quality_findings_from_artifact(artifact: dict[str, Any]) -> list[dict[str, Any]]:
    quality = artifact.get("quality")
    findings = (
        quality.get("quality_findings")
        if isinstance(quality, dict)
        else artifact.get("quality_findings")
    )
    normalized = findings if isinstance(findings, list) else []
    result = [finding for finding in normalized if isinstance(finding, dict)]
    runtime_failure = _runtime_failure_finding_from_artifact(artifact)
    if runtime_failure is not None:
        result.append(runtime_failure)
    return result
```

- [ ] **Step 4: Run Story summary/history tests**

Run:

```bash
uv run --frozen pytest tests/test_story_phase_service.py::test_story_quality_summary_projects_runtime_failure_as_blocking_finding tests/test_story_phase_service.py::test_get_story_history_returns_attempts_and_projection_summary -q
```

Expected: pass.

- [ ] **Step 5: Commit failure projection**

```bash
git add services/phases/story_service.py tests/test_story_phase_service.py
git commit -m "fix: expose story runtime failures in quality summary"
```

---

### Task 5: Propagate Feedback Soft-Gate Warnings

**Files:**
- Modify: `services/agent_workbench/story_phase.py`
- Test: `tests/test_agent_workbench_story_phase.py`

- [ ] **Step 1: Write failing warning propagation test**

Add this test near Story generate facade tests in `tests/test_agent_workbench_story_phase.py`:

```python
def test_story_generate_exposes_feedback_soft_gate_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Soft-gated feedback should appear in outer warnings."""

    async def fake_generate_story_draft(**kwargs: object) -> dict[str, Any]:
        return {
            "fsm_state": "STORY_INTERVIEW",
            "parent_requirement": kwargs["parent_requirement"],
            "data": {
                "generation_ran": False,
                "feedback_quality": {
                    "needs_revision": True,
                    "forced": False,
                    "missing_fields": ["target", "required_change"],
                    "warnings": [
                        {
                            "code": "FEEDBACK_FIELDS_MISSING",
                            "message": "Missing feedback fields: target, required_change.",
                        }
                    ],
                    "suggested_template": "Target:\n...\nRequired change:\n...",
                },
                "story_count": 0,
            },
        }

    monkeypatch.setattr(
        "services.agent_workbench.story_phase.generate_story_draft",
        fake_generate_story_draft,
    )
    runner = StoryPhaseRunner(
        product_repo=_FakeProductRepo(),
        workflow_service=_FakeWorkflowService(),
    )

    result = runner.generate(
        project_id=PROJECT_ID,
        parent_requirement="Review match result",
        user_input="Try again.",
    )

    assert result["ok"] is True
    assert result["data"]["generation_ran"] is False
    assert result["warnings"] == [
        {
            "code": "FEEDBACK_FIELDS_MISSING",
            "message": "Missing feedback fields: target, required_change.",
            "remediation": [
                "Use structured feedback with Target, Issue/Evidence, Required change, Acceptance criteria, and Scope limit sections.",
                "Missing fields: target, required_change.",
                "Use --force-feedback to bypass this check when regenerating after a non-draft runtime failure.",
            ],
        }
    ]
```

- [ ] **Step 2: Run regression and confirm failure**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_story_phase.py::test_story_generate_exposes_feedback_soft_gate_warning -q
```

Expected: fail because `warnings` is empty.

- [ ] **Step 3: Implement warning extraction**

In `services/agent_workbench/story_phase.py`, change `_data_envelope`:

```python
def _data_envelope(data: dict[str, Any]) -> dict[str, Any]:
    """Return application facade success envelope."""
    return {
        "ok": True,
        "data": _flatten_phase_payload(data),
        "warnings": _warnings_from_feedback_quality(data),
        "errors": [],
    }
```

Add helper before `_data_envelope`:

```python
def _warnings_from_feedback_quality(data: dict[str, Any]) -> list[dict[str, Any]]:
    inner = data.get("data")
    if not isinstance(inner, dict) or inner.get("generation_ran") is not False:
        return []
    feedback_quality = inner.get("feedback_quality")
    if not isinstance(feedback_quality, dict):
        return []
    if feedback_quality.get("needs_revision") is not True:
        return []
    raw_warnings = feedback_quality.get("warnings")
    if not isinstance(raw_warnings, list):
        return []

    missing_fields = feedback_quality.get("missing_fields")
    missing_summary = (
        ", ".join(str(item) for item in missing_fields if isinstance(item, str))
        if isinstance(missing_fields, list)
        else ""
    )
    remediation = [
        "Use structured feedback with Target, Issue/Evidence, Required change, Acceptance criteria, and Scope limit sections.",
    ]
    if missing_summary:
        remediation.append(f"Missing fields: {missing_summary}.")
    remediation.append(
        "Use --force-feedback to bypass this check when regenerating after a non-draft runtime failure."
    )

    warnings: list[dict[str, Any]] = []
    for raw_warning in raw_warnings:
        if not isinstance(raw_warning, dict):
            continue
        warnings.append(
            {
                "code": str(raw_warning.get("code") or "FEEDBACK_NEEDS_REVISION"),
                "message": str(raw_warning.get("message") or "Feedback needs revision."),
                "remediation": list(remediation),
            }
        )
    return warnings
```

- [ ] **Step 4: Run agent workbench Story tests**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_story_phase.py::test_story_generate_exposes_feedback_soft_gate_warning tests/test_agent_workbench_story_phase.py::test_story_generate_passes_target_slot_to_service -q
```

Expected: pass.

- [ ] **Step 5: Commit warning propagation**

```bash
git add services/agent_workbench/story_phase.py tests/test_agent_workbench_story_phase.py
git commit -m "fix: expose story feedback gate warnings"
```

---

### Task 6: Verification and Live Read-Only Audit

**Files:**
- No new code files.

- [ ] **Step 1: Run focused regression suites**

Run:

```bash
uv run --frozen pytest \
  tests/test_adk_runner.py \
  tests/test_user_story_writer_integration.py \
  tests/test_user_story_writer_schemas.py \
  tests/test_story_runtime.py \
  tests/test_story_phase_service.py \
  tests/test_agent_workbench_story_phase.py \
  -q
```

Expected: pass.

- [ ] **Step 2: Run lint/type checks on touched Python files**

Run:

```bash
uv run --frozen ruff check \
  utils/adk_runner.py \
  services/story_runtime.py \
  services/phases/story_service.py \
  services/agent_workbench/story_phase.py \
  tests/test_adk_runner.py \
  tests/test_story_phase_service.py \
  tests/test_agent_workbench_story_phase.py
```

Run:

```bash
uv run --frozen python -m ty check \
  utils/adk_runner.py \
  services/story_runtime.py \
  services/phases/story_service.py \
  services/agent_workbench/story_phase.py \
  tests/test_adk_runner.py \
  tests/test_story_phase_service.py \
  tests/test_agent_workbench_story_phase.py
```

Expected: both pass.

- [ ] **Step 3: Run full gate**

Run:

```bash
tmp_log="$(mktemp)"
uv run --frozen pyrepo-check --all >"$tmp_log" 2>&1
rc=$?
tail -n 80 "$tmp_log"
exit "$rc"
```

Expected: pass.

- [ ] **Step 4: Run read-only live ASA route checks**

From `/Users/aaat/projects/asa-deep-process-control-experiments`, run only read-only commands:

```bash
agileforge story history --project-id 3 --parent-requirement "First Model Baseline Evaluation and Reporting"
agileforge workflow next --project-id 3
```

Summarize selected scalar fields only. Do not run `story generate` live unless the user explicitly approves the mutation/model call.

- [ ] **Step 5: Final commit if previous tasks were not committed separately**

If tasks were not committed incrementally, commit once:

```bash
git add \
  utils/adk_runner.py \
  services/story_runtime.py \
  services/phases/story_service.py \
  services/agent_workbench/story_phase.py \
  tests/test_adk_runner.py \
  tests/test_story_phase_service.py \
  tests/test_agent_workbench_story_phase.py
git commit -m "fix: recover story generation after invalid json"
```

---

## Self-Review

### Spec Coverage

- Parser recovery is covered by Task 1 and Task 2.
- Soft-gate bypass after hard schema failure is covered by Task 3.
- Warning propagation is covered by Task 5.
- Hard failure should not look like empty `needs_clarification` is covered by Task 4.
- Verification and read-only ASA checks are covered by Task 6.

### Placeholder Scan

No `TBD`, `TODO`, "similar to", or unspecified test commands remain.

### Type Consistency

- `parse_json_payload()` keeps existing one-argument callers working because `required_keys` is keyword-only and optional.
- Story runtime uses `frozenset[str]` constants compatible with `Collection[str]`.
- Warning payloads remain `list[dict[str, Any]]`, matching current envelope shape.
