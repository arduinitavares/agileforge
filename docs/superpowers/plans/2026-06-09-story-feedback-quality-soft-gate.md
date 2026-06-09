# Story Feedback Quality Soft Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a soft gate that catches weak Story refinement feedback before model generation and returns actionable rewrite guidance.

**Architecture:** Add a deterministic feedback evaluator in a focused service module. Run it from the Story phase service before appending feedback or invoking the Story writer, then thread an explicit force flag through CLI, API, and the browser UI. Keep the existing Story output quality gate unchanged.

**Tech Stack:** Python, Pydantic/FastAPI request models, pytest, Ruff, existing AgileForge Story phase services, vanilla frontend HTML/JS.

---

## File Structure

- Create `services/story_feedback_quality.py`
  - Owns the Story feedback quality contract and deterministic evaluator.
  - No database or runtime-state mutation.
- Create `tests/test_story_feedback_quality.py`
  - Unit tests for vague feedback, structured feedback, and force metadata.
- Modify `services/phases/story_service.py`
  - Runs the soft gate before `append_feedback_entry()` and before `run_story_agent_from_state()`.
  - Returns a normal `STORY_INTERVIEW` payload with `generation_ran=false` when feedback needs revision.
- Modify `services/interview_runtime.py`
  - Stores optional feedback quality metadata on feedback entries when generation is forced or accepted.
- Modify `services/agent_workbench/story_phase.py`
  - Adds `force_feedback` to the Story runner boundary.
- Modify `services/agent_workbench/application.py`
  - Threads `force_feedback` through the application facade and protocol.
- Modify `cli/main.py`
  - Adds `--force-feedback` to `agileforge story generate`.
- Modify `api.py`
  - Adds `force_feedback` to `StoryGenerateRequest`.
- Modify `frontend/project.html`
  - Improves Story feedback textarea guidance.
  - Adds a feedback quality warning container.
- Modify `frontend/project.js`
  - Sends `force_feedback`.
  - Renders soft-gate response without showing a new attempt as generated.
- Test files touched:
  - `tests/test_story_phase_service.py`
  - `tests/test_agent_workbench_story_phase.py`
  - `tests/test_agent_workbench_application.py`
  - `tests/test_agent_workbench_cli.py`

---

### Task 1: Add Feedback Quality Evaluator

**Files:**
- Create: `services/story_feedback_quality.py`
- Create: `tests/test_story_feedback_quality.py`

- [ ] **Step 1: Write failing evaluator tests**

Add `tests/test_story_feedback_quality.py`:

```python
"""Tests for Story feedback quality evaluation."""

from services.story_feedback_quality import (
    STORY_FEEDBACK_QUALITY_SCHEMA_VERSION,
    evaluate_story_feedback_quality,
)


def test_vague_feedback_needs_revision() -> None:
    """Vague feedback should be soft-gated before generation."""
    result = evaluate_story_feedback_quality(
        "Make this more INVEST.",
        parent_requirement="Technology and Model Research Spike",
        force=False,
    )

    assert result["schema_version"] == STORY_FEEDBACK_QUALITY_SCHEMA_VERSION
    assert result["needs_revision"] is True
    assert result["can_force"] is True
    assert result["forced"] is False
    assert "target" in result["missing_fields"]
    assert "required_change" in result["missing_fields"]
    assert "acceptance_criteria" in result["missing_fields"]
    assert "scope_limit" in result["missing_fields"]
    assert "Target:" in result["suggested_template"]
    assert result["warnings"][0]["code"] == "FEEDBACK_TOO_VAGUE"


def test_structured_feedback_passes() -> None:
    """Structured feedback with target, evidence, change, criteria, and scope passes."""
    feedback = """
Target:
Technology and Model Research Spike, attempt-6

Issue:
Draft is partial_capacity_limited and not saveable.

Evidence:
quality.blocking_findings includes PARTIAL_CAPACITY_LIMITED.

Required change:
Refine only delay-horizon validation.

Acceptance criteria:
- Stories cover only delay-horizon validation.
- Each story has one user goal.
- Draft returns coverage_status=complete for the narrowed slice.

Scope limit:
Do not cover state-window, stack, action-set, or recovered-code work.

Priority:
Must fix.
"""

    result = evaluate_story_feedback_quality(
        feedback,
        parent_requirement="Technology and Model Research Spike",
        force=False,
    )

    assert result["needs_revision"] is False
    assert result["missing_fields"] == []
    assert result["forced"] is False
    assert result["score"] >= 80


def test_force_records_override_but_keeps_warnings() -> None:
    """Force override should not hide weak feedback warnings."""
    result = evaluate_story_feedback_quality(
        "Try again.",
        parent_requirement="Requirement A",
        force=True,
    )

    assert result["needs_revision"] is True
    assert result["forced"] is True
    assert result["can_force"] is True
    assert result["warnings"][0]["code"] == "FEEDBACK_TOO_VAGUE"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --frozen pytest tests/test_story_feedback_quality.py -q
```

Expected: fail with `ModuleNotFoundError: No module named 'services.story_feedback_quality'`.

- [ ] **Step 3: Add evaluator implementation**

Create `services/story_feedback_quality.py`:

```python
"""Deterministic quality checks for Story refinement feedback."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

STORY_FEEDBACK_QUALITY_SCHEMA_VERSION = "agileforge.story_feedback_quality.v1"

_FIELD_LABELS: Mapping[str, Sequence[str]] = {
    "target": ("target:", "story:", "requirement:", "attempt:"),
    "issue": ("issue:", "problem:", "gap:"),
    "evidence": ("evidence:", "because:", "quality.blocking_findings", "remaining_scope"),
    "required_change": (
        "required change:",
        "change:",
        "refine only",
        "split",
        "remove",
        "preserve",
        "do not cover",
    ),
    "acceptance_criteria": (
        "acceptance criteria:",
        "- stories",
        "- each story",
        "coverage_status=complete",
        "saveable",
    ),
    "scope_limit": ("scope limit:", "do not", "preserve", "out of scope"),
    "priority": ("priority:", "must fix", "should fix", "optional"),
}

_REQUIRED_FIELDS: Sequence[str] = (
    "target",
    "required_change",
    "acceptance_criteria",
    "scope_limit",
)
_ISSUE_OR_EVIDENCE: Sequence[str] = ("issue", "evidence")
_VAGUE_FEEDBACK_RE = re.compile(
    r"\b(make (this|it) better|make (this|it) more invest|fix (the )?low stories|try again|regenerate (this )?better)\b",
    flags=re.IGNORECASE,
)
_MIN_STRONG_FEEDBACK_CHARS = 80


def _normalized_text(text: str | None) -> str:
    return " ".join((text or "").strip().split())


def _present_fields(feedback: str) -> list[str]:
    normalized = feedback.casefold()
    present: list[str] = []
    for field, labels in _FIELD_LABELS.items():
        if any(label.casefold() in normalized for label in labels):
            present.append(field)
    return present


def _suggested_template(parent_requirement: str) -> str:
    return "\n".join(
        [
            "Target:",
            parent_requirement,
            "",
            "Issue:",
            "[State the observable problem, such as partial_capacity_limited or not saveable.]",
            "",
            "Evidence:",
            "[Cite quality.blocking_findings, remaining_scope, source constraint, or contradiction.]",
            "",
            "Required change:",
            "[Say exactly what to refine, split, add, remove, or preserve.]",
            "",
            "Acceptance criteria:",
            "- [Observable condition 1]",
            "- [Observable condition 2]",
            "- [Observable condition 3]",
            "",
            "Scope limit:",
            "[Say what should not change or should not be covered in this pass.]",
            "",
            "Priority:",
            "Must fix",
        ]
    )


def _warning(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def evaluate_story_feedback_quality(
    feedback: str | None,
    *,
    parent_requirement: str,
    force: bool = False,
) -> dict[str, Any]:
    """Return local quality metadata for one Story refinement feedback string."""
    text = _normalized_text(feedback)
    present_fields = _present_fields(text)
    missing_fields = [field for field in _REQUIRED_FIELDS if field not in present_fields]
    if not any(field in present_fields for field in _ISSUE_OR_EVIDENCE):
        missing_fields.append("issue_or_evidence")

    warnings: list[dict[str, str]] = []
    if not text or _VAGUE_FEEDBACK_RE.search(text):
        warnings.append(
            _warning(
                "FEEDBACK_TOO_VAGUE",
                "Feedback does not name a concrete target, issue, evidence, required change, and success criteria.",
            )
        )
    if len(text) < _MIN_STRONG_FEEDBACK_CHARS:
        warnings.append(
            _warning(
                "FEEDBACK_TOO_SHORT",
                "Feedback is too short to guide reliable Story refinement.",
            )
        )
    if missing_fields:
        warnings.append(
            _warning(
                "FEEDBACK_FIELDS_MISSING",
                f"Missing feedback fields: {', '.join(missing_fields)}.",
            )
        )

    needs_revision = bool(warnings or missing_fields)
    score = max(0, 100 - (len(set(missing_fields)) * 15) - (len(warnings) * 10))
    return {
        "schema_version": STORY_FEEDBACK_QUALITY_SCHEMA_VERSION,
        "needs_revision": needs_revision,
        "can_force": True,
        "forced": force,
        "score": score,
        "present_fields": present_fields,
        "missing_fields": sorted(set(missing_fields)),
        "warnings": warnings,
        "suggested_template": _suggested_template(parent_requirement),
        "suggested_example": None,
    }
```

- [ ] **Step 4: Run evaluator tests to verify pass**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --frozen pytest tests/test_story_feedback_quality.py -q
```

Expected: `3 passed`.

- [ ] **Step 5: Commit evaluator**

```bash
git add services/story_feedback_quality.py tests/test_story_feedback_quality.py
git commit -m "feat(story): evaluate refinement feedback quality"
```

---

### Task 2: Soft-Gate Story Service Before Generation

**Files:**
- Modify: `services/phases/story_service.py`
- Modify: `services/interview_runtime.py`
- Test: `tests/test_story_phase_service.py`

- [ ] **Step 1: Write failing service test for weak feedback**

Add this test near existing `generate_story_draft` tests in `tests/test_story_phase_service.py`:

```python
@pytest.mark.asyncio
async def test_generate_story_draft_soft_gates_weak_feedback() -> None:
    """Weak refinement feedback returns guidance without running generation."""
    parent_requirement = "Requirement A"
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
                            "classification": "quality_gate_failed",
                            "is_reusable": False,
                            "retryable": False,
                            "draft_kind": "quality_blocked_draft",
                            "output_artifact": _story_artifact(
                                parent_requirement,
                                "Broad draft",
                                is_complete=False,
                            ),
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

    async def fake_run_story_agent_from_state(*args, **kwargs) -> JsonDict:
        del args, kwargs
        calls["agent"] += 1
        return {"success": True}

    def fake_append_feedback_entry(*args, **kwargs) -> JsonDict:
        del args, kwargs
        calls["feedback"] += 1
        return {}

    payload = await generate_story_draft(
        project_id=7,
        parent_requirement=parent_requirement,
        user_input="Make this more INVEST.",
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
        mark_feedback_absorbed=lambda runtime, *, feedback_ids, attempt_id: [],
        failure_meta=lambda story_result, fallback_summary: {},
    )

    assert calls == {"agent": 0, "feedback": 0}
    assert payload["fsm_state"] == "STORY_INTERVIEW"
    assert payload["data"]["generation_ran"] is False
    assert payload["data"]["feedback_quality"]["needs_revision"] is True
    assert "required_change" in payload["data"]["feedback_quality"]["missing_fields"]
    assert len(state["interview_runtime"]["story"][parent_requirement]["attempt_history"]) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --frozen pytest tests/test_story_phase_service.py::test_generate_story_draft_soft_gates_weak_feedback -q
```

Expected: fail because `generate_story_draft()` does not accept `force_feedback`.

- [ ] **Step 3: Extend feedback entry metadata**

In `services/interview_runtime.py`, change `append_feedback_entry()` signature and entry:

```python
def append_feedback_entry(
    runtime: dict[str, Any],
    text: str,
    created_at: object,
    feedback_id: str | None = None,
    *,
    feedback_quality: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append a feedback entry and allocate a stable feedback identifier."""
    feedback_projection = _normalize_feedback_projection(runtime)
    items = _require_list(
        feedback_projection["items"],
        error=InterviewRuntimeTypeError.feedback_items_must_be_list(),
    )
    sequence = int(feedback_projection["next_feedback_sequence"]) + 1
    feedback_projection["next_feedback_sequence"] = sequence
    generated_id = feedback_id or f"feedback-{sequence}"
    entry = {
        "feedback_id": generated_id,
        "text": text,
        "created_at": created_at,
        "status": "unabsorbed",
        "absorbed_by_attempt_id": None,
    }
    if isinstance(feedback_quality, dict):
        entry["feedback_quality"] = dict(feedback_quality)
    items.append(entry)
    return entry
```

- [ ] **Step 4: Add service soft gate**

In `services/phases/story_service.py`, import evaluator:

```python
from services.story_feedback_quality import evaluate_story_feedback_quality
```

Change `generate_story_draft()` signature:

```python
async def generate_story_draft(
    *,
    project_id: int,
    parent_requirement: str,
    user_input: str | None,
    force_feedback: bool = False,
    load_state: Callable[[], Awaitable[dict[str, Any]]],
    save_state: Callable[[dict[str, Any]], None],
    now_iso: Callable[[], str],
    run_story_agent_from_state: Callable,
    append_feedback_entry: Callable,
    set_request_projection: Callable,
    append_attempt: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]],
    promote_reusable_draft: Callable,
    mark_feedback_absorbed: Callable,
    failure_meta: Callable,
) -> dict[str, Any]:
```

After `normalized_user_input` and before the feedback append call, add:

```python
    feedback_quality: dict[str, Any] | None = None
    if normalized_user_input:
        feedback_quality = evaluate_story_feedback_quality(
            normalized_user_input,
            parent_requirement=normalized_parent_requirement,
            force=force_feedback,
        )
        if feedback_quality["needs_revision"] and not force_feedback:
            state["fsm_state"] = OrchestratorState.STORY_INTERVIEW.value
            return {
                "fsm_state": OrchestratorState.STORY_INTERVIEW.value,
                "parent_requirement": normalized_parent_requirement,
                "data": {
                    "generation_ran": False,
                    "feedback_quality": feedback_quality,
                    **story_interview_summary(runtime),
                },
            }
```

Then update feedback append call:

```python
    if normalized_user_input:
        append_feedback_entry(
            runtime,
            normalized_user_input,
            now_iso(),
            feedback_quality=feedback_quality,
        )
```

In normal return data, include generation flag and feedback quality:

```python
        "data": {
            "generation_ran": True,
            "feedback_quality": feedback_quality,
            "output_artifact": story_result.get("output_artifact"),
            **story_interview_summary(runtime),
        },
```

- [ ] **Step 5: Run weak-feedback service test**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --frozen pytest tests/test_story_phase_service.py::test_generate_story_draft_soft_gates_weak_feedback -q
```

Expected: pass.

- [ ] **Step 6: Add force-feedback service test**

Add:

```python
@pytest.mark.asyncio
async def test_generate_story_draft_force_feedback_runs_generation() -> None:
    """Forced weak feedback records quality metadata and still runs generation."""
    parent_requirement = "Requirement A"
    artifact = _story_artifact(parent_requirement, "Forced draft")
    state: JsonDict = {"roadmap_releases": [{"items": [parent_requirement]}]}
    captured_feedback: dict[str, Any] = {}

    async def fake_run_story_agent_from_state(*args, **kwargs) -> JsonDict:
        del args, kwargs
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

    def fake_append_feedback_entry(runtime, text, created_at, **kwargs):
        del runtime, text, created_at
        captured_feedback.update(kwargs)
        return {"feedback_id": "feedback-1"}

    payload = await generate_story_draft(
        project_id=7,
        parent_requirement=parent_requirement,
        user_input="Try again.",
        force_feedback=True,
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
        ).update(
            {
                "latest_reusable_attempt_id": kwargs["attempt_id"],
                "kind": kwargs["kind"],
                "is_complete": kwargs["is_complete"],
                "updated_at": kwargs["updated_at"],
            }
        ),
        mark_feedback_absorbed=lambda runtime, *, feedback_ids, attempt_id: [],
        failure_meta=lambda story_result, fallback_summary: {},
    )

    assert payload["data"]["generation_ran"] is True
    assert payload["data"]["feedback_quality"]["forced"] is True
    assert captured_feedback["feedback_quality"]["forced"] is True
```

- [ ] **Step 7: Run service tests**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --frozen pytest tests/test_story_phase_service.py::test_generate_story_draft_soft_gates_weak_feedback tests/test_story_phase_service.py::test_generate_story_draft_force_feedback_runs_generation -q
```

Expected: `2 passed`.

- [ ] **Step 8: Commit service gate**

```bash
git add services/phases/story_service.py services/interview_runtime.py tests/test_story_phase_service.py
git commit -m "feat(story): soft gate weak refinement feedback"
```

---

### Task 3: Thread Force Flag Through CLI, API, and Application

**Files:**
- Modify: `services/agent_workbench/story_phase.py`
- Modify: `services/agent_workbench/application.py`
- Modify: `services/agent_workbench/command_registry.py`
- Modify: `cli/main.py`
- Modify: `api.py`
- Test: `tests/test_agent_workbench_story_phase.py`
- Test: `tests/test_agent_workbench_application.py`
- Test: `tests/test_agent_workbench_cli.py`

- [ ] **Step 1: Add failing application facade test**

In `tests/test_agent_workbench_application.py`, update the fake Story runner to capture `force_feedback`, then add:

```python
def test_story_generate_threads_force_feedback() -> None:
    """Verify application facade forwards Story force-feedback override."""
    runner = _FakeStoryRunner()
    app = AgentWorkbenchApplication(story_runner=runner)

    result = app.story_generate(
        project_id=7,
        parent_requirement="REQ.checkout",
        user_input="Try again.",
        force_feedback=True,
    )

    assert result["ok"] is True
    assert runner.calls[-1]["force_feedback"] is True
```

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --frozen pytest tests/test_agent_workbench_application.py::test_story_generate_threads_force_feedback -q
```

Expected: fail because `story_generate()` does not accept `force_feedback`.

- [ ] **Step 2: Update application protocols and facade**

In `services/agent_workbench/application.py`, update `_StoryPhaseRunner.generate()` protocol:

```python
    def generate(
        self,
        *,
        project_id: int,
        parent_requirement: str,
        user_input: str | None = None,
        force_feedback: bool = False,
    ) -> dict[str, Any]:
        """Generate or refine a Story draft."""
        raise NotImplementedError
```

Update `AgentWorkbenchApplication.story_generate()`:

```python
    def story_generate(
        self,
        *,
        project_id: int,
        parent_requirement: str,
        user_input: str | None = None,
        force_feedback: bool = False,
    ) -> dict[str, Any]:
        """Generate or refine a Story draft."""
        return self._get_story_runner().generate(
            project_id=project_id,
            parent_requirement=parent_requirement,
            user_input=user_input,
            force_feedback=force_feedback,
        )
```

- [ ] **Step 3: Update Story runner**

In `services/agent_workbench/story_phase.py`, update public and async methods:

```python
    def generate(
        self,
        *,
        project_id: int,
        parent_requirement: str,
        user_input: str | None = None,
        force_feedback: bool = False,
    ) -> dict[str, Any]:
        """Generate or refine a Story draft."""
        return anyio.run(
            self._generate,
            project_id,
            parent_requirement,
            user_input,
            force_feedback,
        )
```

```python
    async def _generate(
        self,
        project_id: int,
        parent_requirement: str,
        user_input: str | None,
        force_feedback: bool,
    ) -> dict[str, Any]:
```

Pass into `generate_story_draft()`:

```python
                force_feedback=force_feedback,
```

- [ ] **Step 4: Update command registry**

In `services/agent_workbench/command_registry.py`, update `agileforge story generate` optional inputs:

```python
        input_optional=("input", "force_feedback"),
```

- [ ] **Step 5: Add CLI parser flag and handler threading**

In `cli/main.py`, add:

```python
    story_generate.add_argument(
        "--force-feedback",
        action="store_true",
        help="Run Story generation even when feedback quality needs revision.",
    )
```

Update `_Application.story_generate()` protocol in `cli/main.py`:

```python
    def story_generate(
        self,
        *,
        project_id: int,
        parent_requirement: str,
        user_input: str | None = None,
        force_feedback: bool = False,
    ) -> JsonObject:
        raise NotImplementedError
```

Update `_story_generate()`:

```python
    return "agileforge story generate", application.story_generate(
        project_id=args.project_id,
        parent_requirement=args.parent_requirement,
        user_input=args.user_input,
        force_feedback=bool(args.force_feedback),
    )
```

- [ ] **Step 6: Update API request model and route**

In `api.py`, update request body:

```python
class StoryGenerateRequest(BaseModel):
    """Request body for generating user stories."""

    user_input: str | None = None
    force_feedback: bool = False
```

Pass into service:

```python
            force_feedback=req.force_feedback,
```

- [ ] **Step 7: Run facade/API/CLI tests**

Run targeted tests:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --frozen pytest tests/test_agent_workbench_application.py::test_story_generate_threads_force_feedback tests/test_agent_workbench_story_phase.py tests/test_agent_workbench_cli.py -q
```

Expected: tests pass after fake runners and protocol call sites are updated for the new optional argument.

- [ ] **Step 8: Commit plumbing**

```bash
git add services/agent_workbench/story_phase.py services/agent_workbench/application.py services/agent_workbench/command_registry.py cli/main.py api.py tests/test_agent_workbench_story_phase.py tests/test_agent_workbench_application.py tests/test_agent_workbench_cli.py
git commit -m "feat(story): expose feedback quality override"
```

---

### Task 4: Add UI Feedback Guidance and Soft-Gate Rendering

**Files:**
- Modify: `frontend/project.html`
- Modify: `frontend/project.js`

- [ ] **Step 1: Update textarea guidance**

In `frontend/project.html`, replace the Story refinement paragraph and placeholder:

```html
<p class="text-[11px] text-slate-500 mb-3 leading-relaxed">
    Use specific feedback: target, issue, evidence, required change,
    acceptance criteria, and scope limit.
</p>
<textarea id="story-user-input" rows="6"
    class="w-full text-sm rounded-xl border border-slate-300 dark:border-slate-600 px-4 py-3 bg-white dark:bg-slate-900 text-slate-800 dark:text-slate-200 focus:ring-2 focus:ring-orange-500 focus:border-orange-500 outline-none transition-all placeholder:text-slate-400 resize-none shadow-inner"
    placeholder="Target: [requirement, attempt, or story]&#10;Issue: [observable problem]&#10;Evidence: [quality finding or source]&#10;Required change: [exact refinement]&#10;Acceptance criteria: [observable success]&#10;Scope limit: [what not to change]"></textarea>
<div id="story-feedback-quality-panel"
    class="hidden mt-3 rounded-lg border border-amber-300 bg-amber-50 p-3 text-xs text-amber-900 dark:border-amber-700 dark:bg-amber-950/40 dark:text-amber-100"></div>
```

- [ ] **Step 2: Add JS renderer**

In `frontend/project.js`, add helper near other Story rendering helpers:

```javascript
function renderStoryFeedbackQuality(feedbackQuality) {
    const panel = document.getElementById('story-feedback-quality-panel');
    if (!panel) return;
    if (!feedbackQuality || !feedbackQuality.needs_revision) {
        panel.classList.add('hidden');
        panel.replaceChildren();
        return;
    }

    const missing = safeArray(feedbackQuality.missing_fields).join(', ') || 'none';
    const template = feedbackQuality.suggested_template || '';
    const title = document.createElement('div');
    title.className = 'font-bold mb-1';
    title.textContent = 'Feedback needs more structure before generation.';

    const detail = document.createElement('div');
    detail.className = 'mb-2';
    detail.textContent = `Missing: ${missing}`;

    const pre = document.createElement('pre');
    pre.className = 'whitespace-pre-wrap rounded bg-white/70 p-2 text-[11px] dark:bg-slate-900/60';
    pre.textContent = template;

    panel.replaceChildren(title, detail, pre);
    panel.classList.remove('hidden');
}
```

- [ ] **Step 3: Render soft-gate response after generate**

In `generateStoryDraft()`, after `const data = await response.json();`, add:

```javascript
        const payloadData = data.data || {};
        renderStoryFeedbackQuality(payloadData.feedback_quality);
        if (payloadData.generation_ran === false) {
            return;
        }
```

Keep the existing reload calls for actual generation.

- [ ] **Step 4: Clear feedback warning when selecting/reloading requirement**

Where Story requirement selection clears the textarea or loads history, add:

```javascript
renderStoryFeedbackQuality(null);
```

- [ ] **Step 5: Manual UI check**

Run local backend normally, open:

```text
http://127.0.0.1:8001/dashboard/project.html?id=3
```

Enter:

```text
Make this more INVEST.
```

Expected:

- Warning panel shows missing fields.
- No new generation run appears.
- Existing Story history does not add a new attempt.

- [ ] **Step 6: Commit UI guidance**

```bash
git add frontend/project.html frontend/project.js
git commit -m "feat(ui): guide Story refinement feedback"
```

---

### Task 5: Regression and Full Validation

**Files:**
- No new files unless tests reveal missing coverage.

- [ ] **Step 1: Run feedback evaluator tests**

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --frozen pytest tests/test_story_feedback_quality.py -q
```

Expected: all tests pass.

- [ ] **Step 2: Run Story service/runtime tests**

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --frozen pytest tests/test_story_runtime.py tests/test_story_phase_service.py tests/test_user_story_writer_schemas.py -q
```

Expected: all tests pass.

- [ ] **Step 3: Run workbench Story/CLI tests**

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --frozen pytest tests/test_agent_workbench_story_phase.py tests/test_agent_workbench_application.py tests/test_agent_workbench_cli.py -q
```

Expected: all tests pass.

- [ ] **Step 4: Run focused regression suite used by prior Story fixes**

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --frozen pytest tests/test_agent_workbench_application.py tests/test_agent_workbench_authority_decision.py tests/test_agent_workbench_authority_projection.py tests/test_agent_workbench_authority_regenerate.py tests/test_db_migrations_authority_decision.py tests/test_spec_authority_compiler_normalizer.py tests/test_specs_compiler_service.py tests/test_user_story_writer_schemas.py tests/test_story_runtime.py tests/test_story_phase_service.py tests/test_agent_workbench_story_phase.py tests/test_user_story_writer_tools.py tests/test_user_story_writer_agent.py tests/test_user_story_writer_integration.py tests/test_create_user_story.py tests/test_story_pipeline_batch.py tests/test_story_dependencies.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Run Ruff**

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --frozen ruff check services/story_feedback_quality.py services/phases/story_service.py services/interview_runtime.py services/agent_workbench/story_phase.py services/agent_workbench/application.py services/agent_workbench/command_registry.py cli/main.py api.py tests/test_story_feedback_quality.py tests/test_story_phase_service.py tests/test_agent_workbench_story_phase.py tests/test_agent_workbench_application.py tests/test_agent_workbench_cli.py
```

Expected: `All checks passed!`

- [ ] **Step 6: Run whitespace check**

```bash
git diff --check
```

Expected: no output and exit code 0.

- [ ] **Step 7: Final commit if validation edits were needed**

If validation required follow-up fixes after Task 4, commit them:

```bash
git add services tests cli api.py frontend
git commit -m "test(story): cover feedback quality soft gate"
```

If no files changed, skip this commit.

---

## Self-Review Notes

- Spec coverage: evaluator, soft gate, force override, CLI/API, UI, tests, and non-ASA behavior are covered.
- Scope: this plan does not alter Story output quality gate, story cap, batching, or saved Story persistence.
- Type consistency: `force_feedback` is the single flag name across CLI, API, application, runner, and phase service.
- State behavior: weak feedback does not append a feedback entry, does not append a Story attempt, and keeps `STORY_INTERVIEW`.
