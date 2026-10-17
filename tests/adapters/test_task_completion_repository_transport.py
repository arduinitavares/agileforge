# tests/adapters/test_task_completion_repository_transport.py
"""Real completion transports preserve repository semantics and old receipts."""

from __future__ import annotations

import json
from http import HTTPStatus
from typing import TYPE_CHECKING, Literal, cast

import pytest
from fastapi.testclient import TestClient
from git import Repo
from pydantic import ValidationError
from sqlmodel import Session, col, select

import api as api_module
from adapters.git.repository_probe import GitPythonRepositoryProbe
from cli.main import main
from cli.workflow_commands import render_workflow_next
from models.core import Task
from models.db import ensure_business_db_ready
from models.enums import TaskStatus
from models.sprint_retry import SprintRetryTaskEvidence
from models.workflow import TaskCompletionEvidence, WorkflowTransitionReceipt
from services.application import (
    AgileForgeApplication,
    CompleteTaskRequest,
    ExecutionActionSelectionService,
)
from services.node_attempt_replay import (
    DurableTransitionReplayService,
    TransitionReplayQuery,
)
from services.read_projections import DurableReadProjectionService
from services.repository_probe import RepositoryProbeError, RepositoryProbeErrorCode
from tests.adapters.sprint_retry_fixtures import durable_rows
from tests.services.test_task_completion_repository_evidence import (
    _bind,
    _deny_target_preflight,
    _policy_command,
    _repository,
)
from tests.workflow.execution_fixtures import seed_started_execution
from tests.workflow.test_sprint_retry_schema import _frozen_completed_history
from workflow.clock import SystemClock
from workflow.definitions.execution import execution_graph
from workflow.domain import WorkflowDomain
from workflow.fingerprints import canonical_hash, canonical_json
from workflow.requests import CompleteTask

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine

    from services.repository_probe import (
        RepositoryProbeResult,
        RepositoryRevisionProbeResult,
    )
    from workflow.contracts import JsonObject


class _TransportProbe:
    """Exercise real Git, or forbid every probe during durable receipt replay."""

    def __init__(self) -> None:
        self.adapter = GitPythonRepositoryProbe()
        self.forbid = False
        self.unavailable = False

    def _check(self, path: Path | str) -> None:
        if self.forbid:
            message = "Receipt replay must not inspect repository state."
            pytest.fail(message)  # ty: ignore[invalid-argument-type]
        if self.unavailable:
            raise RepositoryProbeError(
                RepositoryProbeErrorCode.GIT_METADATA_UNREADABLE, str(path)
            )

    def inspect_common_git_dir(self, path: Path | str) -> str:
        self._check(path)
        return self.adapter.inspect_common_git_dir(path)

    def inspect(self, path: Path | str) -> RepositoryProbeResult:
        self._check(path)
        return self.adapter.inspect(path)

    def inspect_revision(self, path: Path | str) -> RepositoryRevisionProbeResult:
        self._check(path)
        return self.adapter.inspect_revision(path)

    def has_other_worktrees(self, path: Path | str) -> bool:
        self._check(path)
        return self.adapter.has_other_worktrees(path)


def _application(engine: Engine, probe: _TransportProbe) -> AgileForgeApplication:
    """Inject disposable durable state into both production transports."""
    return AgileForgeApplication(
        workflow_domain=WorkflowDomain(
            engine=engine,
            graph=execution_graph(),
            clock=SystemClock(),
            _task_repository_probe=probe,
        ),
        execution_action_selection=ExecutionActionSelectionService(engine=engine),
        read_projection=DurableReadProjectionService(engine=engine),
    )


def _payload(task_id: int, key: str = "repository-transport") -> JsonObject:
    return {
        "instance_key": f"task:{task_id}",
        "outcome_summary": "Implemented the delivery.",
        "artifact_refs": ["tracked.txt"],
        "acceptance_result": "fully_met",
        "checklist_result": {"Run focused tests": "passed"},
        "idempotency_key": key,
        "actor": "synthetic-operator",
    }


def _send(  # noqa: PLR0913  # Explicit transport fixtures keep I/O ownership visible.
    transport: Literal["api", "cli"],
    application: AgileForgeApplication,
    project_id: int,
    payload: JsonObject,
    *,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> tuple[int, JsonObject]:
    """Use real FastAPI and CLI parsing without substituting their adapters."""
    if transport == "api":
        monkeypatch.setattr(api_module, "_application", lambda: application)
        response = TestClient(api_module.app).post(
            f"/api/projects/{project_id}/sprint/task/complete", json=payload
        )
        envelope = response.json()
        result = envelope.get("data", envelope.get("detail", envelope))
        return response.status_code, cast("JsonObject", result)
    checklist_file = tmp_path / "checklist.json"
    checklist_file.write_text(json.dumps(payload["checklist_result"]), encoding="utf-8")
    argv = [
        "sprint",
        "task",
        "complete",
        "--project-id",
        str(project_id),
        "--instance-key",
        str(payload["instance_key"]),
        "--outcome-summary",
        str(payload["outcome_summary"]),
        "--artifact-ref",
        "tracked.txt",
        "--acceptance-result",
        str(payload["acceptance_result"]),
        "--checklist-file",
        str(checklist_file),
        "--idempotency-key",
        str(payload["idempotency_key"]),
        "--actor",
        str(payload["actor"]),
    ]
    if payload.get("uncommitted") is True:
        argv.append("--uncommitted")
    if payload.get("worktree_path") is not None:
        argv.extend(["--worktree", str(payload["worktree_path"])])
    exit_code = main(argv, application=application)
    output = capsys.readouterr().out
    return exit_code, cast("JsonObject", json.loads(output))


def _assert_refusal(result: JsonObject, rule: str) -> None:
    assert result["ok"] is False
    serialized = json.dumps(result)
    assert "WORKFLOW_FACT_CONFLICT" in serialized
    assert rule in serialized
    assert "A changed request requires a new idempotency key." in serialized


@pytest.mark.parametrize("transport", ["api", "cli"])
@pytest.mark.parametrize("explicit", [False, True])
def test_linked_worktree_selection_and_acknowledgement_are_semantic(  # noqa: PLR0913, PLR0915
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    transport: Literal["api", "cli"],
    *,
    explicit: bool,
) -> None:
    """Ignoring the selected path would silently accept the clean main checkout."""
    project_id, _sprint_id, _story_id, task_id = seed_started_execution(engine)
    root = _repository(tmp_path)
    _bind(engine, project_id, root)
    linked = tmp_path / "linked "
    with Repo(root) as repo:
        repo.git.worktree("add", "--detach", str(linked), "HEAD")
    dirty_file = linked / "untracked.txt"
    dirty_file.write_text("linked delivery\n", encoding="utf-8")
    probe = _TransportProbe()
    application = _application(engine, probe)
    next_command = render_workflow_next(application.position(project_id=project_id))
    assert "--uncommitted" not in next_command
    assert "--worktree" not in next_command
    before = GitPythonRepositoryProbe().inspect(linked)
    payload = _payload(task_id)
    if explicit:
        payload["worktree_path"] = str(linked)
    code, result = _send(
        transport,
        application,
        project_id,
        payload,
        monkeypatch=monkeypatch,
        capsys=capsys,
        tmp_path=tmp_path,
    )
    if explicit:
        assert code == (HTTPStatus.CONFLICT if transport == "api" else 1)
        _assert_refusal(result, "UNCOMMITTED_ACKNOWLEDGEMENT_REQUIRED")
        assert "--uncommitted" in json.dumps(result)
        assert "uncommitted=true" in json.dumps(result)
        rows = durable_rows(engine)
        probe.forbid = True
        _, replay = _send(
            transport,
            application,
            project_id,
            payload,
            monkeypatch=monkeypatch,
            capsys=capsys,
            tmp_path=tmp_path,
        )
        assert replay["replayed"] is True
        assert replay["output"] == result["output"]
        payload["uncommitted"] = True
        _, conflict = _send(
            transport,
            application,
            project_id,
            payload,
            monkeypatch=monkeypatch,
            capsys=capsys,
            tmp_path=tmp_path,
        )
        _assert_refusal(conflict, "different input")
        assert durable_rows(engine) == rows
        probe.forbid = False
        payload["idempotency_key"] = "repository-transport-acknowledged"
        code, result = _send(
            transport,
            application,
            project_id,
            payload,
            monkeypatch=monkeypatch,
            capsys=capsys,
            tmp_path=tmp_path,
        )
    assert code == (HTTPStatus.OK if transport == "api" else 0)
    assert result["ok"] is True
    output = cast("JsonObject", result["output"])
    evidence = cast("JsonObject", output["repository_evidence"])
    assert evidence["dirty"] is explicit
    assert evidence["worktree_path"] == str(linked if explicit else root)
    assert evidence["probed_path_matches_binding"] is not explicit
    assert evidence["other_worktrees_present"] is not explicit
    assert evidence["uncommitted_acknowledged"] is explicit
    expected_warnings = (
        ["UNCOMMITTED_WORKTREE", "DETACHED_HEAD"]
        if explicit
        else ["OTHER_WORKTREES_PRESENT"]
    )
    assert output["repository_warnings"] == expected_warnings
    assert output["repository_warning_messages"]
    after = GitPythonRepositoryProbe().inspect(linked)
    assert after.model_dump(exclude={"inspected_at"}) == before.model_dump(
        exclude={"inspected_at"}
    )
    assert dirty_file.read_text(encoding="utf-8") == "linked delivery\n"
    with Session(engine) as session:
        task = session.get(Task, task_id)
        assert task is not None
        assert task.status is TaskStatus.DONE
        stored = session.exec(select(TaskCompletionEvidence)).one()
        assert json.loads(stored.repository_evidence_json or "null") == evidence


@pytest.mark.parametrize("transport", ["api", "cli"])
@pytest.mark.parametrize("foreign_state", ["unborn", "committed"])
def test_foreign_repository_refuses_even_with_acknowledgement(  # noqa: PLR0913
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    transport: Literal["api", "cli"],
    foreign_state: str,
) -> None:
    """An unborn foreign repository must not become an acknowledged unavailable one."""
    project_id, _sprint_id, _story_id, task_id = seed_started_execution(engine)
    root = _repository(tmp_path)
    _bind(engine, project_id, root)
    parent = tmp_path / "foreign"
    parent.mkdir()
    if foreign_state == "unborn":
        foreign = parent / "repository"
        with Repo.init(foreign):
            pass
    else:
        foreign = _repository(parent)
    payload = {**_payload(task_id), "worktree_path": str(foreign), "uncommitted": True}
    probe = _TransportProbe()
    application = _application(engine, probe)
    _, result = _send(
        transport,
        application,
        project_id,
        payload,
        monkeypatch=monkeypatch,
        capsys=capsys,
        tmp_path=tmp_path,
    )
    _assert_refusal(result, "WORKTREE_REPOSITORY_MISMATCH")
    with Session(engine) as session:
        task = session.get(Task, task_id)
        assert task is not None
        assert task.status is TaskStatus.IN_PROGRESS
        assert session.exec(select(TaskCompletionEvidence)).all() == []


@pytest.mark.parametrize("transport", ["api", "cli"])
@pytest.mark.parametrize(
    ("acceptance", "acknowledged", "accepted"),
    [
        ("fully_met", False, False),
        ("fully_met", True, True),
        ("partially_met", False, True),
    ],
)
def test_unavailable_completion_policy_crosses_both_transports(  # noqa: PLR0913
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    transport: Literal["api", "cli"],
    acceptance: str,
    *,
    acknowledged: bool,
    accepted: bool,
) -> None:
    """A lost target cannot silently count as a fully accepted clean completion."""
    project_id, _sprint_id, _story_id, task_id = seed_started_execution(engine)
    root = _repository(tmp_path)
    _bind(engine, project_id, root)
    root.rename(tmp_path / "disappeared-repository")
    payload = {
        **_payload(task_id),
        "acceptance_result": acceptance,
        "uncommitted": acknowledged,
    }
    probe = _TransportProbe()
    application = _application(engine, probe)
    _, result = _send(
        transport,
        application,
        project_id,
        payload,
        monkeypatch=monkeypatch,
        capsys=capsys,
        tmp_path=tmp_path,
    )
    assert result["ok"] is accepted
    if accepted:
        output = cast("JsonObject", result["output"])
        evidence = cast("JsonObject", output["repository_evidence"])
        assert evidence["state"] == "unavailable"
        assert evidence["uncommitted_acknowledged"] is acknowledged
        assert output["repository_warnings"] == ["REPOSITORY_UNAVAILABLE"]
    else:
        _assert_refusal(result, "REPOSITORY_UNAVAILABLE")
        rows = durable_rows(engine)
        probe.forbid = True
        _, replay = _send(
            transport,
            application,
            project_id,
            payload,
            monkeypatch=monkeypatch,
            capsys=capsys,
            tmp_path=tmp_path,
        )
        assert replay["replayed"] is True
        payload["uncommitted"] = True
        _, conflict = _send(
            transport,
            application,
            project_id,
            payload,
            monkeypatch=monkeypatch,
            capsys=capsys,
            tmp_path=tmp_path,
        )
        _assert_refusal(conflict, "different input")
        assert durable_rows(engine) == rows
        probe.forbid = False
        payload["idempotency_key"] = "unavailable-acknowledged-new-key"
        _, acknowledged_result = _send(
            transport,
            application,
            project_id,
            payload,
            monkeypatch=monkeypatch,
            capsys=capsys,
            tmp_path=tmp_path,
        )
        assert acknowledged_result["ok"] is True


@pytest.mark.parametrize("transport", ["api", "cli"])
@pytest.mark.parametrize("explicit", [False, True])
def test_exact_replay_after_target_disappearance_and_changed_semantics_refuse(  # noqa: PLR0913
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    transport: Literal["api", "cli"],
    *,
    explicit: bool,
) -> None:
    """Reverse acknowledgement/path comparisons cannot ignore omitted incoming keys."""
    project_id, _sprint_id, _story_id, task_id = seed_started_execution(engine)
    root = _repository(tmp_path)
    _bind(engine, project_id, root)
    probe = _TransportProbe()
    application = _application(engine, probe)
    payload = _payload(task_id)
    if explicit:
        payload.update({"uncommitted": True, "worktree_path": str(root)})
    _, first = _send(
        transport,
        application,
        project_id,
        payload,
        monkeypatch=monkeypatch,
        capsys=capsys,
        tmp_path=tmp_path,
    )
    assert first["ok"] is True
    rows = durable_rows(engine)
    root.rename(tmp_path / "disappeared-repository")
    probe.forbid = True

    def forbid_position(*_args: object, **_kwargs: object) -> None:
        message = "A stored receipt must replay before reading current position."
        pytest.fail(message)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(application, "position", forbid_position)
    _, replay = _send(
        transport,
        application,
        project_id,
        payload,
        monkeypatch=monkeypatch,
        capsys=capsys,
        tmp_path=tmp_path,
    )
    assert replay["ok"] is True
    assert replay["replayed"] is True
    assert replay["output"] == first["output"]
    for change in (
        {"uncommitted": not explicit},
        {"worktree_path": None if explicit else str(root)},
        {"worktree_path": str(tmp_path / "different")},
    ):
        _, conflict = _send(
            transport,
            application,
            project_id,
            {**payload, **change},
            monkeypatch=monkeypatch,
            capsys=capsys,
            tmp_path=tmp_path,
        )
        _assert_refusal(conflict, "different input")
        assert durable_rows(engine) == rows


@pytest.mark.parametrize("acknowledgement", [None, "true", 1, 0])
def test_public_api_and_application_reject_nonliteral_boolean(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    acknowledgement: object,
) -> None:
    """Public null/coercible values must not become completion acknowledgement."""
    payload = {**_payload(1), "uncommitted": acknowledgement}
    with pytest.raises(ValidationError):
        CompleteTaskRequest.model_validate({"project_id": 1, **payload})
    application = _application(engine, _TransportProbe())
    monkeypatch.setattr(api_module, "_application", lambda: application)
    response = TestClient(api_module.app).post(
        "/api/projects/1/sprint/task/complete", json=payload
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


@pytest.mark.parametrize(
    "field",
    [
        "repository_evidence",
        "head_sha",
        "branch_name",
        "dirty",
        "dirty_path_count",
        "common_git_dir",
        "status_fingerprint",
    ],
)
def test_spoofed_observation_fields_are_rejected(field: str) -> None:
    """Client observations must never enter the public semantic completion input."""
    payload = {**_payload(1), field: "spoofed"}
    with pytest.raises(ValidationError):
        CompleteTaskRequest.model_validate({"project_id": 1, **payload})
    response = TestClient(api_module.app).post(
        "/api/projects/1/sprint/task/complete", json=payload
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


@pytest.mark.parametrize("key", ["complete-task", "legacy-retry-task"])
def test_frozen_original_and_retry_receipts_keep_exact_bytes_and_replay(
    tmp_path: Path,
    key: str,
) -> None:
    """Current-generated receipt fixtures cannot prove pre-revision compatibility."""
    engine = _frozen_completed_history(tmp_path / "frozen-history.sqlite")
    try:
        ensure_business_db_ready(engine)
        with Session(engine) as session:
            receipt = session.exec(
                select(WorkflowTransitionReceipt).where(
                    col(WorkflowTransitionReceipt.idempotency_key) == key,
                    col(WorkflowTransitionReceipt.request_kind) == "complete_task",
                )
            ).one()
            raw_request = receipt.request_json
            raw_hash = receipt.request_fingerprint
            raw_result = receipt.result_json
        assert raw_result is not None
        request = CompleteTask.model_validate_json(raw_request)
        assert canonical_json(request.model_dump(mode="json")) == raw_request
        assert canonical_hash(request.model_dump(mode="json")) == raw_hash
        assert "uncommitted" not in json.loads(raw_request)
        rows = durable_rows(engine)
        probe = _TransportProbe()
        probe.forbid = True
        domain = WorkflowDomain(
            engine=engine,
            graph=execution_graph(),
            clock=SystemClock(),
            _task_repository_probe=probe,
        )
        direct = domain.transition(request)
        assert direct.ok is True
        assert direct.replayed is True
        old_output = json.loads(raw_result)["output"]
        assert direct.model_dump(mode="json")["output"] == old_output
        assert "repository_evidence" not in old_output
        operator_input = {
            field: request.model_dump(mode="json")[field]
            for field in (
                "instance_key",
                "outcome_summary",
                "artifact_refs",
                "acceptance_result",
                "checklist_result",
            )
        }
        operator_input.update({"uncommitted": False, "worktree_path": None})
        replay_service = DurableTransitionReplayService(engine=engine)
        query = TransitionReplayQuery(
            request_kind="complete_task",
            project_id=request.project_id,
            idempotency_key=key,
            actor=request.actor,
            correlation_id=request.correlation_id,
            operator_input=operator_input,
        )
        semantic = replay_service.replay(query)
        assert semantic is not None
        assert semantic.ok
        assert semantic.replayed
        assert semantic.model_dump(mode="json")["output"] == old_output
        application_replay = _application(engine, probe).complete_task(
            CompleteTaskRequest.model_validate(
                {
                    **operator_input,
                    "project_id": request.project_id,
                    "idempotency_key": key,
                    "actor": request.actor,
                    "correlation_id": request.correlation_id,
                }
            )
        )
        assert application_replay.ok
        assert application_replay.replayed
        assert application_replay.model_dump(mode="json")["output"] == old_output
        changed = replay_service.replay(
            query.model_copy(
                update={"operator_input": {**operator_input, "uncommitted": True}}
            )
        )
        assert changed is not None
        assert not changed.ok
        assert changed.error is not None
        assert "new idempotency key" in changed.error.message
        direct_changed = domain.transition(
            CompleteTask.model_validate(
                {**json.loads(raw_request), "uncommitted": True}
            )
        )
        assert not direct_changed.ok
        assert direct_changed.error is not None
        assert "new idempotency key" in direct_changed.error.message
        assert durable_rows(engine) == rows
    finally:
        engine.dispose()


@pytest.mark.parametrize("transport", ["api", "cli"])
@pytest.mark.parametrize("acknowledged", [False, True])
def test_verification_timeout_refuses_then_reuses_the_unchanged_transport_key(  # noqa: PLR0913
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    transport: Literal["api", "cli"],
    *,
    acknowledged: bool,
) -> None:
    """A rolled-back timeout must retain the public refusal envelope and retry key."""
    project_id, _sprint_id, _story_id, task_id = seed_started_execution(engine)
    root = _repository(tmp_path)
    _bind(engine, project_id, root)
    probe = _TransportProbe()
    original_revision = probe.inspect_revision

    def timeout_revision(path: Path | str) -> RepositoryRevisionProbeResult:
        raise RepositoryProbeError(RepositoryProbeErrorCode.PROBE_TIMED_OUT, str(path))

    monkeypatch.setattr(probe, "inspect_revision", timeout_revision)
    application = _application(engine, probe)
    payload = {**_payload(task_id), "uncommitted": acknowledged}
    before = durable_rows(engine)
    status, result = _send(
        transport,
        application,
        project_id,
        payload,
        monkeypatch=monkeypatch,
        capsys=capsys,
        tmp_path=tmp_path,
    )
    assert status == (HTTPStatus.CONFLICT if transport == "api" else 1)
    _assert_refusal(result, "REPOSITORY_VERIFICATION_TIMEOUT")
    assert "unchanged request with the same idempotency key" in json.dumps(result)
    assert durable_rows(engine) == before
    monkeypatch.setattr(probe, "inspect_revision", original_revision)
    status, successful = _send(
        transport,
        application,
        project_id,
        payload,
        monkeypatch=monkeypatch,
        capsys=capsys,
        tmp_path=tmp_path,
    )
    assert status == (HTTPStatus.OK if transport == "api" else 0)
    assert successful["ok"] is True
    assert successful["replayed"] is False


@pytest.mark.parametrize(
    "target_state", ["missing", "file", "non_git", "unreadable", "malformed"]
)
@pytest.mark.parametrize(
    ("acceptance", "acknowledged", "accepted"),
    [
        ("partially_met", False, True),
        ("fully_met", True, True),
        ("fully_met", False, False),
    ],
)
def test_api_explicit_unusable_path_is_generic_in_response_storage_and_replay(  # noqa: PLR0913
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    target_state: str,
    acceptance: str,
    *,
    acknowledged: bool,
    accepted: bool,
) -> None:
    """An arbitrary explicit path must not expose existence, type, or metadata state."""
    project_id, _sprint_id, _story_id, task_id = seed_started_execution(engine)
    root = _repository(tmp_path)
    _bind(engine, project_id, root)
    probe = _TransportProbe()
    target = tmp_path / "selected-target"
    if target_state == "file":
        target.write_text("ordinary file\n", encoding="utf-8")
    elif target_state == "non_git":
        target.mkdir()
    elif target_state == "unreadable":
        target = root
        probe.unavailable = True
    payload = {
        **_payload(task_id),
        "acceptance_result": acceptance,
        "uncommitted": acknowledged,
        "worktree_path": "\0" if target_state == "malformed" else str(target),
    }
    application = _application(engine, probe)
    status, result = _send(
        "api",
        application,
        project_id,
        payload,
        monkeypatch=monkeypatch,
        capsys=capsys,
        tmp_path=tmp_path,
    )
    assert status == (HTTPStatus.OK if accepted else HTTPStatus.CONFLICT)
    assert result["ok"] is accepted
    if accepted:
        output = cast("JsonObject", result["output"])
        evidence = cast("JsonObject", output["repository_evidence"])
        assert evidence["state"] == "unavailable"
        assert evidence["probe_error_code"] == "WORKTREE_PATH_UNUSABLE"
        assert (
            evidence["error_summary"] == "The selected worktree path could not be used."
        )
        assert evidence["uncommitted_acknowledged"] is acknowledged
        assert "head_sha" not in evidence
        assert output["repository_warning_messages"] == [
            "Repository evidence was unavailable at Task completion: "
            "The selected worktree path could not be used."
        ]
        with Session(engine) as session:
            stored = session.exec(select(TaskCompletionEvidence)).one()
            assert json.loads(stored.repository_evidence_json or "null") == evidence
    else:
        _assert_refusal(result, "REPOSITORY_UNAVAILABLE_ACKNOWLEDGEMENT_REQUIRED")
        with Session(engine) as session:
            assert session.exec(select(TaskCompletionEvidence)).all() == []
    serialized = json.dumps(result)
    for old_code in (
        "PATH_MISSING",
        "PATH_NOT_DIRECTORY",
        "NOT_GIT_WORKTREE",
        "GIT_METADATA_UNREADABLE",
        "MALFORMED_PATH",
    ):
        assert old_code not in serialized
    rows = durable_rows(engine)
    probe.forbid = True
    replay_status, replay = _send(
        "api",
        application,
        project_id,
        payload,
        monkeypatch=monkeypatch,
        capsys=capsys,
        tmp_path=tmp_path,
    )
    assert replay_status == status
    assert replay["replayed"] is True
    assert replay["output"] == result["output"]
    assert replay.get("error") == result.get("error")
    assert durable_rows(engine) == rows


@pytest.mark.parametrize("failure_phase", ["preparation", "verification"])
@pytest.mark.parametrize("retry", [False, True])
@pytest.mark.parametrize(
    ("acceptance", "acknowledged", "accepted"),
    [
        ("partially_met", False, True),
        ("fully_met", True, True),
        ("fully_met", False, False),
    ],
)
def test_api_filesystem_preflight_denial_preserves_completion_policy_and_replay(  # noqa: PLR0913
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure_phase: str,
    acceptance: str,
    *,
    retry: bool,
    acknowledged: bool,
    accepted: bool,
) -> None:
    """A real-adapter filesystem denial must produce safe API evidence or refusal."""
    command, root, _observed_probe = _policy_command(engine, tmp_path, retry=retry)
    probe = _TransportProbe()
    if failure_phase == "preparation":
        _deny_target_preflight(root, monkeypatch, "is_dir")
    else:

        def denied_revision(path: Path | str) -> RepositoryRevisionProbeResult:
            probe._check(path)
            _deny_target_preflight(root, monkeypatch, "is_dir")
            return probe.adapter.inspect_revision(path)

        monkeypatch.setattr(probe, "inspect_revision", denied_revision)
    payload = {
        **_payload(command.task_id),
        "acceptance_result": acceptance,
        "uncommitted": acknowledged,
        "worktree_path": str(root),
    }
    if retry:
        payload["instance_key"] = (
            f"retry:{command.retry_attempt_id}:task:{command.task_id}"
        )
    application = _application(engine, probe)
    before = durable_rows(engine)
    status, result = _send(
        "api",
        application,
        command.project_id,
        payload,
        monkeypatch=monkeypatch,
        capsys=capsys,
        tmp_path=tmp_path,
    )
    assert status == (HTTPStatus.OK if accepted else HTTPStatus.CONFLICT)
    assert result["ok"] is accepted
    if accepted:
        output = cast("JsonObject", result["output"])
        evidence = cast("JsonObject", output["repository_evidence"])
        assert evidence["state"] == "unavailable"
        assert evidence["probe_error_code"] == "WORKTREE_PATH_UNUSABLE"
        assert (
            evidence["error_summary"] == "The selected worktree path could not be used."
        )
        assert evidence["uncommitted_acknowledged"] is acknowledged
        assert "head_sha" not in evidence
        assert output["repository_warning_messages"] == [
            "Repository evidence was unavailable at Task completion: "
            "The selected worktree path could not be used."
        ]
        with Session(engine) as session:
            model = SprintRetryTaskEvidence if retry else TaskCompletionEvidence
            stored = session.exec(select(model)).one()
            assert json.loads(stored.repository_evidence_json or "null") == evidence
    else:
        _assert_refusal(result, "REPOSITORY_UNAVAILABLE_ACKNOWLEDGEMENT_REQUIRED")
        after = durable_rows(engine)
        assert (
            after.tables["task_completion_evidence"]
            == before.tables["task_completion_evidence"]
        )
        assert (
            after.tables["sprint_retry_task_evidence"]
            == before.tables["sprint_retry_task_evidence"]
        )
    assert "GIT_METADATA_UNREADABLE" not in json.dumps(result)
    rows = durable_rows(engine)
    probe.forbid = True
    replay_status, replay = _send(
        "api",
        application,
        command.project_id,
        payload,
        monkeypatch=monkeypatch,
        capsys=capsys,
        tmp_path=tmp_path,
    )
    assert replay_status == status
    assert replay["replayed"] is True
    assert replay["output"] == result["output"]
    assert replay.get("error") == result.get("error")
    assert durable_rows(engine) == rows


@pytest.mark.parametrize(
    "probe_error",
    [
        RepositoryProbeErrorCode.PATH_MISSING,
        RepositoryProbeErrorCode.PATH_NOT_DIRECTORY,
        RepositoryProbeErrorCode.NOT_GIT_WORKTREE,
        RepositoryProbeErrorCode.GIT_METADATA_UNREADABLE,
        RepositoryProbeErrorCode.MALFORMED_PATH,
    ],
)
def test_api_failure_after_explicit_capture_redacts_the_public_evidence(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    probe_error: RepositoryProbeErrorCode,
) -> None:
    """Post-capture failures must keep explicit-path privacy even at the bound path."""
    project_id, _sprint_id, _story_id, task_id = seed_started_execution(engine)
    root = _repository(tmp_path)
    _bind(engine, project_id, root)
    probe = _TransportProbe()

    def unavailable_revision(path: Path | str) -> RepositoryRevisionProbeResult:
        probe._check(path)
        raise RepositoryProbeError(probe_error, str(path))

    monkeypatch.setattr(probe, "inspect_revision", unavailable_revision)
    application = _application(engine, probe)
    payload = {**_payload(task_id), "worktree_path": str(root), "uncommitted": True}
    status, result = _send(
        "api",
        application,
        project_id,
        payload,
        monkeypatch=monkeypatch,
        capsys=capsys,
        tmp_path=tmp_path,
    )
    assert status == HTTPStatus.OK
    assert result["ok"] is True
    output = cast("JsonObject", result["output"])
    evidence = cast("JsonObject", output["repository_evidence"])
    assert evidence["probed_path_matches_binding"] is True
    assert evidence["state"] == "unavailable"
    assert evidence["probe_error_code"] == "WORKTREE_PATH_UNUSABLE"
    assert evidence["error_summary"] == "The selected worktree path could not be used."
    assert output["repository_warning_messages"] == [
        "Repository evidence was unavailable at Task completion: "
        "The selected worktree path could not be used."
    ]
    with Session(engine) as session:
        stored = session.exec(select(TaskCompletionEvidence)).one()
        assert json.loads(stored.repository_evidence_json or "null") == evidence
    rows = durable_rows(engine)
    probe.forbid = True
    _, replay = _send(
        "api",
        application,
        project_id,
        payload,
        monkeypatch=monkeypatch,
        capsys=capsys,
        tmp_path=tmp_path,
    )
    assert replay["replayed"] is True
    assert replay["output"] == result["output"]
    assert durable_rows(engine) == rows
