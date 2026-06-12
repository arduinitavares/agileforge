# Task Close Evidence Command Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Sprint task close commands advertised by `workflow next` and `sprint task next/show` include the evidence required for `status=Done`, and enforce the same evidence contract during mutation.

**Architecture:** Add one pure Sprint task update command renderer in `services/agent_workbench/sprint_phase.py`, use it from both task tickets and Sprint workflow routing, then tighten `Done` validation to match `work_contract.done_requires`. Keep command metadata optional fields unchanged because evidence fields are conditional on `status=Done`.

**Tech Stack:** Python 3.13, SQLModel, AgileForge AgentWorkbench application facade, pytest, `uv run --frozen`, `pyrepo-check`.

---

## File Structure

- Modify `services/agent_workbench/sprint_phase.py`
  - Add `sprint_task_update_command_text()`.
  - Use it in `_build_task_ticket()`.
  - Add structured evidence validation for `Done`.

- Modify `services/agent_workbench/application.py`
  - Use `sprint_task_update_command_text()` for the generic `SPRINT_VIEW` task update command.

- Modify `tests/test_agent_workbench_application.py`
  - Update `SPRINT_VIEW` workflow-next expectation.

- Modify `tests/test_agent_workbench_sprint_phase.py`
  - Add command rendering assertions for task tickets.
  - Add validation parity tests for missing close evidence.

- Modify `tests/test_agent_workbench_command_schema.py`
  - Keep a focused compatibility assertion that task update evidence fields remain optional globally.

---

### Task 1: Workflow Next Advertises Runnable Done Command

**Files:**
- Modify: `tests/test_agent_workbench_application.py`
- Modify: `services/agent_workbench/application.py`
- Modify: `services/agent_workbench/sprint_phase.py`

- [ ] **Step 1: Write the failing workflow-next test**

Update the expected `agileforge sprint task update` command in
`tests/test_agent_workbench_application.py::test_workflow_next_routes_sprint_view_to_execution_commands`.

Replace the current task update command assertion with this exact expected command:

```python
        (
            "agileforge sprint task update --project-id 7 "
            "--task-id <task_id> --status Done "
            '--expected-status "<expected_status>" '
            "--expected-task-fingerprint <task_fingerprint> "
            "--idempotency-key <idempotency_key> "
            "--outcome-summary <outcome_summary> "
            "--validation-summary <validation_summary> "
            "--checklist-result fully_met "
            "--artifact-ref <artifact_ref>"
        ),
```

- [ ] **Step 2: Run the red test**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_application.py -q -k "workflow_next_routes_sprint_view"
```

Expected: FAIL because the command currently uses `--status <status>` and omits close-evidence flags.

- [ ] **Step 3: Add the shared command renderer**

In `services/agent_workbench/sprint_phase.py`, add `Sequence` to the typing import:

```python
from typing import TYPE_CHECKING, Any, NoReturn, Protocol, Sequence, cast
```

Add this function near the task ticket helpers, before `_build_task_ticket()`:

```python
def sprint_task_update_command_text(
    *,
    project_id: int,
    task_id: int | str,
    status: str,
    expected_status: str,
    expected_task_fingerprint: str,
    idempotency_key: str,
    include_done_evidence: bool,
    artifact_targets: Sequence[object] | None,
) -> str:
    """Return a guarded Sprint task update command for CLI agents."""
    parts = [
        f"agileforge sprint task update --project-id {project_id}",
        f"--task-id {task_id}",
        f"--status {status}",
        f'--expected-status "{expected_status}"',
        f"--expected-task-fingerprint {expected_task_fingerprint}",
        f"--idempotency-key {idempotency_key}",
    ]
    if include_done_evidence:
        parts.extend(
            [
                "--outcome-summary <outcome_summary>",
                "--validation-summary <validation_summary>",
                "--checklist-result fully_met",
            ]
        )
        targets_known = artifact_targets is not None
        has_targets = bool(list(artifact_targets or []))
        if not targets_known or has_targets:
            parts.append("--artifact-ref <artifact_ref>")
    return " ".join(parts)
```

- [ ] **Step 4: Use the renderer in workflow-next Sprint View**

In `services/agent_workbench/application.py`, inside `_sprint_command_candidates()` and the `fsm_state == "SPRINT_VIEW"` branch, add a local import immediately before the returned list:

```python
        from services.agent_workbench.sprint_phase import (  # noqa: PLC0415
            sprint_task_update_command_text,
        )
```

Replace the current hard-coded `"agileforge sprint task update"` tuple with:

```python
            (
                "agileforge sprint task update",
                sprint_task_update_command_text(
                    project_id=project_id,
                    task_id="<task_id>",
                    status="Done",
                    expected_status="<expected_status>",
                    expected_task_fingerprint="<task_fingerprint>",
                    idempotency_key="<idempotency_key>",
                    include_done_evidence=True,
                    artifact_targets=None,
                ),
            ),
```

- [ ] **Step 5: Run the green test**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_application.py -q -k "workflow_next_routes_sprint_view"
```

Expected: PASS.

- [ ] **Step 6: Commit Task 1**

Run:

```bash
git add services/agent_workbench/application.py services/agent_workbench/sprint_phase.py tests/test_agent_workbench_application.py
git commit -m "fix(workflow): advertise task close evidence command"
```

---

### Task 2: Task Ticket Next Actions Use Actual Guards And Evidence

**Files:**
- Modify: `tests/test_agent_workbench_sprint_phase.py`
- Modify: `services/agent_workbench/sprint_phase.py`

- [ ] **Step 1: Strengthen the artifact-target task ticket test**

In `tests/test_agent_workbench_sprint_phase.py::test_sprint_task_next_returns_current_ticket`, replace:

```python
    assert "agileforge sprint task update" in ticket["next_actions"]["update"]
```

with:

```python
    update_command = ticket["next_actions"]["update"]
    assert update_command == (
        f"agileforge sprint task update --project-id {product.product_id} "
        f"--task-id {first.task_id} --status Done "
        '--expected-status "In Progress" '
        f"--expected-task-fingerprint {ticket['guards']['expected_task_fingerprint']} "
        "--idempotency-key <idempotency_key> "
        "--outcome-summary <outcome_summary> "
        "--validation-summary <validation_summary> "
        "--checklist-result fully_met "
        "--artifact-ref <artifact_ref>"
    )
```

- [ ] **Step 2: Add a no-artifact-target task ticket test**

Add this test immediately after `test_sprint_task_next_returns_current_ticket`:

```python
def test_sprint_task_next_omits_artifact_ref_when_no_targets(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task ticket close command should not require artifact refs without targets."""
    product = Product(name="No Artifact Product")
    team = Team(name="No Artifact Team")
    session.add_all([product, team])
    session.flush()
    assert product.product_id is not None
    assert team.team_id is not None

    story = UserStory(
        product_id=product.product_id,
        title="Write operator note",
        story_description="As an operator, I record a note.",
        acceptance_criteria="- Note is recorded",
        story_points=1,
        rank="110",
        status=StoryStatus.IN_PROGRESS,
        is_refined=True,
    )
    session.add(story)
    session.flush()
    assert story.story_id is not None

    sprint = Sprint(
        product_id=product.product_id,
        team_id=team.team_id,
        goal="Deliver note workflow",
        start_date=date(2026, 5, 26),
        end_date=date(2026, 6, 9),
        status=SprintStatus.ACTIVE,
    )
    session.add(sprint)
    session.flush()
    assert sprint.sprint_id is not None
    session.add(SprintStory(sprint_id=sprint.sprint_id, story_id=story.story_id))

    task = Task(
        story_id=story.story_id,
        description="Document the note workflow",
        status=TaskStatus.IN_PROGRESS,
        metadata_json=serialize_task_metadata(
            TaskMetadata(
                task_kind="documentation",
                checklist_items=["Note workflow is documented"],
            )
        ),
    )
    session.add(task)
    session.commit()
    assert task.task_id is not None

    monkeypatch.setattr(sprint_phase_module, "get_engine", session.get_bind)
    runner = SprintPhaseRunner(
        product_repo=cast("Any", _FakeProductRepository()),
        workflow_service=cast("Any", _FakeWorkflowService()),
    )

    result = runner.task_next(project_id=product.product_id)

    assert result["ok"] is True
    ticket = result["data"]["task_ticket"]
    update_command = ticket["next_actions"]["update"]
    assert "--outcome-summary <outcome_summary>" in update_command
    assert "--validation-summary <validation_summary>" in update_command
    assert "--checklist-result fully_met" in update_command
    assert "--artifact-ref" not in update_command
```

- [ ] **Step 3: Run the red ticket tests**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_sprint_phase.py -q -k "task_next_returns_current_ticket or task_next_omits_artifact_ref"
```

Expected: FAIL because task ticket commands still omit evidence.

- [ ] **Step 4: Use the renderer in `_build_task_ticket()`**

In `services/agent_workbench/sprint_phase.py`, replace the current
`"next_actions": {"update": (...)}` update command inside `_build_task_ticket()`
with:

```python
        "next_actions": {
            "update": sprint_task_update_command_text(
                project_id=project_id,
                task_id=task_id,
                status=TaskStatus.DONE.value,
                expected_status=_enum_value(task.status),
                expected_task_fingerprint=fingerprint,
                idempotency_key="<idempotency_key>",
                include_done_evidence=True,
                artifact_targets=list(task_row.get("artifact_targets") or []),
            ),
            "history": (
                f"agileforge sprint task history --project-id {project_id} "
                f"--task-id {task_id}"
            ),
        },
```

- [ ] **Step 5: Run the green ticket tests**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_sprint_phase.py -q -k "task_next_returns_current_ticket or task_next_omits_artifact_ref"
```

Expected: PASS.

- [ ] **Step 6: Commit Task 2**

Run:

```bash
git add services/agent_workbench/sprint_phase.py tests/test_agent_workbench_sprint_phase.py
git commit -m "fix(sprint): include close evidence in task tickets"
```

---

### Task 3: Enforce Done Evidence Contract With Structured Details

**Files:**
- Modify: `tests/test_agent_workbench_sprint_phase.py`
- Modify: `services/agent_workbench/sprint_phase.py`

- [ ] **Step 1: Add failing validation parity tests**

Add these tests after `test_sprint_task_update_replays_done_without_duplicate_logs`:

```python
def _seed_active_task_for_evidence_tests(
    session: Session,
    *,
    artifact_targets: list[str] | None = None,
) -> tuple[Product, Task, str]:
    product = Product(name="Evidence Product")
    team = Team(name="Evidence Team")
    session.add_all([product, team])
    session.flush()
    assert product.product_id is not None
    assert team.team_id is not None

    story = UserStory(
        product_id=product.product_id,
        title="Evidence story",
        story_description="As an agent, I close with evidence.",
        acceptance_criteria="- Evidence is present",
        story_points=1,
        rank="120",
        status=StoryStatus.IN_PROGRESS,
        is_refined=True,
    )
    session.add(story)
    session.flush()
    assert story.story_id is not None

    sprint = Sprint(
        product_id=product.product_id,
        team_id=team.team_id,
        goal="Deliver evidence",
        start_date=date(2026, 5, 26),
        end_date=date(2026, 6, 9),
        status=SprintStatus.ACTIVE,
    )
    session.add(sprint)
    session.flush()
    assert sprint.sprint_id is not None
    session.add(SprintStory(sprint_id=sprint.sprint_id, story_id=story.story_id))

    task = Task(
        story_id=story.story_id,
        description="Close with evidence",
        status=TaskStatus.IN_PROGRESS,
        metadata_json=serialize_task_metadata(
            TaskMetadata(
                task_kind="implementation",
                artifact_targets=artifact_targets or [],
                checklist_items=["Evidence is recorded"],
            )
        ),
    )
    session.add(task)
    session.commit()
    assert task.task_id is not None

    runner = SprintPhaseRunner(
        product_repo=cast("Any", _FakeProductRepository()),
        workflow_service=cast("Any", _FakeWorkflowService()),
    )
    show = runner.task_show(project_id=product.product_id, task_id=task.task_id)
    fingerprint = str(
        show["data"]["task_ticket"]["guards"]["expected_task_fingerprint"]
    )
    return product, task, fingerprint
```

Then add:

```python
def test_sprint_task_update_rejects_done_without_outcome_summary(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sprint_phase_module, "get_engine", session.get_bind)
    product, task, fingerprint = _seed_active_task_for_evidence_tests(session)

    result = SprintPhaseRunner(
        product_repo=cast("Any", _FakeProductRepository()),
        workflow_service=cast("Any", _FakeWorkflowService()),
    ).task_update(
        project_id=product.product_id,
        task_id=cast("int", task.task_id),
        status="Done",
        expected_status="In Progress",
        expected_task_fingerprint=fingerprint,
        idempotency_key="done-missing-outcome",
        checklist_result="fully_met",
        validation_summary="uv run pytest tests/test_example.py -q",
        changed_by="cli-agent",
    )

    assert result["ok"] is False
    error = result["errors"][0]
    assert error["details"]["reason_code"] == "TASK_CLOSE_EVIDENCE_REQUIRED"
    assert error["details"]["missing_fields"] == ["outcome_summary"]
```

Add the same shape for missing checklist result:

```python
def test_sprint_task_update_rejects_done_without_checklist_result(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sprint_phase_module, "get_engine", session.get_bind)
    product, task, fingerprint = _seed_active_task_for_evidence_tests(session)

    result = SprintPhaseRunner(
        product_repo=cast("Any", _FakeProductRepository()),
        workflow_service=cast("Any", _FakeWorkflowService()),
    ).task_update(
        project_id=product.product_id,
        task_id=cast("int", task.task_id),
        status="Done",
        expected_status="In Progress",
        expected_task_fingerprint=fingerprint,
        idempotency_key="done-missing-checklist",
        outcome_summary="Implemented the task.",
        validation_summary="uv run pytest tests/test_example.py -q",
        changed_by="cli-agent",
    )

    assert result["ok"] is False
    error = result["errors"][0]
    assert error["details"]["reason_code"] == "TASK_CLOSE_EVIDENCE_REQUIRED"
    assert error["details"]["missing_fields"] == ["checklist_result"]
```

Add the same shape for `not_checked`:

```python
def test_sprint_task_update_rejects_done_with_not_checked_result(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sprint_phase_module, "get_engine", session.get_bind)
    product, task, fingerprint = _seed_active_task_for_evidence_tests(session)

    result = SprintPhaseRunner(
        product_repo=cast("Any", _FakeProductRepository()),
        workflow_service=cast("Any", _FakeWorkflowService()),
    ).task_update(
        project_id=product.product_id,
        task_id=cast("int", task.task_id),
        status="Done",
        expected_status="In Progress",
        expected_task_fingerprint=fingerprint,
        idempotency_key="done-not-checked",
        outcome_summary="Implemented the task.",
        checklist_result="not_checked",
        validation_summary="uv run pytest tests/test_example.py -q",
        changed_by="cli-agent",
    )

    assert result["ok"] is False
    error = result["errors"][0]
    assert error["details"]["reason_code"] == "TASK_CLOSE_EVIDENCE_REQUIRED"
    assert error["details"]["missing_fields"] == ["checklist_result"]
```

Add success without artifact targets:

```python
def test_sprint_task_update_done_accepts_complete_evidence_without_artifact_targets(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sprint_phase_module, "get_engine", session.get_bind)
    product, task, fingerprint = _seed_active_task_for_evidence_tests(session)

    result = SprintPhaseRunner(
        product_repo=cast("Any", _FakeProductRepository()),
        workflow_service=cast("Any", _FakeWorkflowService()),
    ).task_update(
        project_id=product.product_id,
        task_id=cast("int", task.task_id),
        status="Done",
        expected_status="In Progress",
        expected_task_fingerprint=fingerprint,
        idempotency_key="done-complete-evidence",
        outcome_summary="Implemented the task.",
        checklist_result="fully_met",
        validation_summary="uv run pytest tests/test_example.py -q",
        changed_by="cli-agent",
    )

    assert result["ok"] is True
    assert result["data"]["execution"]["current_status"] == "Done"
```

- [ ] **Step 2: Run the red validation tests**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_sprint_phase.py -q -k "done_without_outcome or done_without_checklist or done_with_not_checked or done_accepts_complete_evidence"
```

Expected: at least the missing outcome/checklist tests fail because current validation does not enforce them.

- [ ] **Step 3: Add structured Done evidence validation**

In `services/agent_workbench/sprint_phase.py`, add this helper near
`_task_execution_write_request()`:

```python
def _done_requires_contract() -> dict[str, object]:
    """Return the published evidence requirements for task Done transitions."""
    return {
        "outcome_summary": True,
        "checklist_result": True,
        "artifact_refs": "required_if_artifact_targets_present",
        "validation_summary": True,
    }
```

Replace the initial validation block in `_task_execution_write_request()` with:

```python
    if new_status == TaskStatus.DONE:
        missing_fields: list[str] = []
        if not (validation_summary and validation_summary.strip()):
            missing_fields.append("validation_summary")
        if not (outcome_summary and outcome_summary.strip()):
            missing_fields.append("outcome_summary")
        if acceptance_result in {None, TaskAcceptanceResult.NOT_CHECKED}:
            missing_fields.append("checklist_result")
        if missing_fields:
            message = (
                "TASK_CLOSE_EVIDENCE_REQUIRED: task Done requires close evidence."
            )
            raise _SprintTaskUpdateError(
                message,
                details={
                    "reason_code": "TASK_CLOSE_EVIDENCE_REQUIRED",
                    "missing_fields": missing_fields,
                    "done_requires": _done_requires_contract(),
                },
            )
```

- [ ] **Step 4: Run the green validation tests**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_sprint_phase.py -q -k "done_without_outcome or done_without_checklist or done_with_not_checked or done_accepts_complete_evidence"
```

Expected: PASS.

- [ ] **Step 5: Run existing idempotency and artifact-ref tests**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_sprint_phase.py -q -k "task_update_replays_done or artifact_refs"
```

Expected: PASS. Existing artifact-target guard remains intact and idempotency replay still works.

- [ ] **Step 6: Commit Task 3**

Run:

```bash
git add services/agent_workbench/sprint_phase.py tests/test_agent_workbench_sprint_phase.py
git commit -m "fix(sprint): enforce task close evidence contract"
```

---

### Task 4: Command Schema Compatibility And Feedback Status

**Files:**
- Modify: `tests/test_agent_workbench_command_schema.py`
- Modify: `docs/feedback/asa-milestone1-agileforge-feedback.md`

- [ ] **Step 1: Strengthen command schema compatibility test**

In `tests/test_agent_workbench_command_schema.py::test_sprint_commands_are_registered`, after the existing `task_update["input"]["optional"]` assertion, add:

```python
    assert {
        "outcome_summary",
        "artifact_ref",
        "checklist_result",
        "validation_summary",
    }.issubset(set(task_update["input"]["optional"]))
```

- [ ] **Step 2: Run the command schema test**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_command_schema.py -q -k "sprint_commands_are_registered"
```

Expected: PASS.

- [ ] **Step 3: Update the feedback document**

In `docs/feedback/asa-milestone1-agileforge-feedback.md`, under
`Sprint task update command omits required close-evidence arguments`, append:

```markdown
- Fix status: fixed by the task close evidence command contract branch. The
  runnable command projections now include `--outcome-summary`,
  `--validation-summary`, `--checklist-result fully_met`, and conditional
  `--artifact-ref` placeholders for `Done` transitions; mutation validation now
  returns structured `TASK_CLOSE_EVIDENCE_REQUIRED` details for missing close
  evidence.
```

- [ ] **Step 4: Commit Task 4**

Run:

```bash
git add tests/test_agent_workbench_command_schema.py docs/feedback/asa-milestone1-agileforge-feedback.md
git commit -m "docs(feedback): mark task close evidence issue fixed"
```

---

### Task 5: Final Verification

**Files:**
- Verify only.

- [ ] **Step 1: Run focused regression suite**

Run:

```bash
uv run --frozen pytest tests/test_agent_workbench_application.py -q -k "workflow_next_routes_sprint_view"
uv run --frozen pytest tests/test_agent_workbench_sprint_phase.py -q -k "task_next or task_update"
uv run --frozen pytest tests/test_agent_workbench_command_schema.py -q -k "sprint_commands_are_registered"
```

Expected: all pass.

- [ ] **Step 2: Run static frontend parse check**

Run:

```bash
node --check frontend/project.js
```

Expected: exits 0. No frontend change is expected, but this keeps the standard UI safety check in the loop.

- [ ] **Step 3: Run full gate**

Run:

```bash
pyrepo-check --all
```

Expected: ruff, annotations, ty, bandit, and pytest pass.

- [ ] **Step 4: Inspect diff and status**

Run:

```bash
git diff --check
git status --short --branch
git log --oneline --decorate -8
```

Expected: diff check exits 0; branch has only intentional commits.

---

## Plan Self-Review

- Spec coverage: Tasks 1-2 cover advertised command text; Task 3 covers validation parity and structured details; Task 4 covers command schema compatibility and feedback tracking; Task 5 covers verification.
- Red-flag scan: no unfinished implementation markers or vague deferred work.
- Type consistency: `sprint_task_update_command_text()` arguments match all planned call sites; `artifact_targets=None` means unknown targets and includes `--artifact-ref`; an empty sequence means no artifact refs are advertised.
