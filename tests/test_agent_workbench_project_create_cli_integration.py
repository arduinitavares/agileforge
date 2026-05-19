"""End-to-end CLI integration tests for project setup mutations."""

# ruff: noqa: ANN401, D102, D103, D107, PLC0415, PLR0913, TC002

from __future__ import annotations

import json
import os
import subprocess  # nosec B404
import sys
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, create_engine, select

from cli.main import INVALID_COMMAND_EXIT_CODE, main
from models.agent_workbench import CliMutationLedger
from models.core import Product
from models.specs import CompiledSpecAuthority, SpecAuthorityAcceptance
from services.agent_workbench.application import AgentWorkbenchApplication
from services.agent_workbench.mutation_ledger import MutationStatus
from services.agent_workbench.project_setup import (
    ProjectSetupMutationRunner,
    _retry_context_fingerprint,
)


class FakeWorkflowPort:
    """In-memory workflow port for CLI integration tests."""

    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, Any]] = {}
        self.created_sessions: list[str] = []

    def initialize_session(self, session_id: str | None = None) -> str:
        if session_id is None:
            session_id = f"session-{len(self.created_sessions) + 1}"
        self.created_sessions.append(session_id)
        self.sessions.setdefault(session_id, {"fsm_state": "SETUP_REQUIRED"})
        return session_id

    def update_session_status(
        self,
        session_id: str,
        partial_update: dict[str, Any],
    ) -> None:
        current = self.sessions.setdefault(session_id, {"fsm_state": "SETUP_REQUIRED"})
        current.update(partial_update)

    def get_session_status(self, session_id: str) -> dict[str, Any]:
        return dict(self.sessions.get(session_id, {}))

    def ensure_setup_state(
        self,
        *,
        project_id: int,
        resolved_spec_path: Path,
        lease_guard: Any,
        record_progress: Any,
    ) -> dict[str, Any]:
        session_id = str(project_id)
        current = self.get_session_status(session_id)
        if current == {}:
            if not lease_guard("workflow_session_created"):
                return {"ok": False, "error_code": "MUTATION_IN_PROGRESS"}
            self.initialize_session(session_id=session_id)
            current = self.get_session_status(session_id)
        if not record_progress("workflow_session_created"):
            return {"ok": False, "error_code": "MUTATION_RECOVERY_REQUIRED"}

        required_state = {
            "fsm_state": "SETUP_REQUIRED",
            "setup_status": "authority_pending_review",
            "setup_error": None,
            "setup_spec_file_path": str(resolved_spec_path),
        }
        merged = {**current, **required_state}
        if current != merged:
            if not lease_guard("workflow_session_status_written"):
                return {"ok": False, "error_code": "MUTATION_IN_PROGRESS"}
            self.update_session_status(session_id, required_state)
        if not record_progress("workflow_session_status_written"):
            return {"ok": False, "error_code": "MUTATION_RECOVERY_REQUIRED"}
        return {
            "ok": True,
            "session_id": session_id,
            "state": self.get_session_status(session_id),
        }


def _business_engine(path: Path) -> Engine:
    """Create an engine for a file-backed SQLite DB."""
    return create_engine(
        f"sqlite:///{path.as_posix()}",
        connect_args={"check_same_thread": False},
    )


def _write_spec(caller_dir: Path) -> Path:
    """Write a small project spec in the simulated caller repository."""
    spec_file = caller_dir / "specs" / "app.md"
    spec_file.parent.mkdir(parents=True, exist_ok=True)
    spec_file.write_text(
        "# Outside Repo Project\n\n"
        "The project must include name. The total must be <= 10.\n",
        encoding="utf-8",
    )
    return spec_file


def _write_invalid_structured_spec(caller_dir: Path) -> Path:
    """Write an invalid structured spec profile in the simulated caller repo."""
    spec_file = caller_dir / "specs" / "invalid-spec.json"
    spec_file.parent.mkdir(parents=True, exist_ok=True)
    spec_file.write_text(
        json.dumps(
            {
                "schema_version": "agileforge.spec.v1",
                "artifact_id": "SPEC.invalid",
            }
        ),
        encoding="utf-8",
    )
    return spec_file


def _write_sitecustomize_compiler_patch(caller_dir: Path) -> None:
    """Install a deterministic compiler patch visible only to the subprocess."""
    (caller_dir / "sitecustomize.py").write_text(
        """
from sqlmodel import Session

from models.specs import CompiledSpecAuthority
from services.agent_workbench import project_setup


def compile_for_test(
    *,
    engine,
    spec_version_id,
    force_recompile=None,
    tool_context=None,
    lease_guard=None,
    record_progress=None,
):
    del force_recompile, tool_context
    if lease_guard is not None and not lease_guard("compiled_authority_persisted"):
        return {"success": False, "error_code": "MUTATION_IN_PROGRESS"}
    with Session(engine) as session:
        authority = CompiledSpecAuthority(
            spec_version_id=spec_version_id,
            compiler_version="test-compiler",
            prompt_hash="sha256:test",
            compiled_artifact_json='{"ok":true}',
            scope_themes="[]",
            invariants="[]",
            eligible_feature_ids="[]",
            rejected_features="[]",
            spec_gaps="[]",
        )
        session.add(authority)
        session.commit()
        session.refresh(authority)
        authority_id = authority.authority_id
    if record_progress is not None:
        assert record_progress("compiled_authority_persisted")
        assert record_progress("product_authority_cache_persisted")
    return {
        "success": True,
        "authority_id": authority_id,
        "spec_version_id": spec_version_id,
        "compiler_version": "test-compiler",
        "prompt_hash": "sha256:test",
    }


project_setup.compile_spec_authority_for_version_with_engine = compile_for_test
""",
        encoding="utf-8",
    )


def _payload_from_completed_process(
    result: subprocess.CompletedProcess[str],
) -> dict[str, Any]:
    """Parse a subprocess stdout envelope."""
    return json.loads(result.stdout)


def _captured_payload(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    """Parse the latest in-process CLI stdout envelope."""
    captured = capsys.readouterr()
    assert captured.err == ""
    return json.loads(captured.out)


def _install_compiler(
    monkeypatch: pytest.MonkeyPatch,
    *,
    success: bool,
    error_code: str = "SPEC_COMPILE_FAILED",
) -> None:
    """Install a deterministic compiler seam for in-process retry tests."""
    from services.agent_workbench import project_setup

    def compile_for_test(
        *,
        engine: Engine,
        spec_version_id: int,
        force_recompile: bool | None = None,
        tool_context: object | None = None,
        lease_guard: Any | None = None,
        record_progress: Any | None = None,
    ) -> dict[str, Any]:
        del force_recompile, tool_context
        if not success:
            return {
                "success": False,
                "error_code": error_code,
                "error": "Injected compile failure.",
            }
        if lease_guard is not None and not lease_guard("compiled_authority_persisted"):
            return {"success": False, "error_code": "MUTATION_IN_PROGRESS"}
        with Session(engine) as session:
            authority = CompiledSpecAuthority(
                spec_version_id=spec_version_id,
                compiler_version="test-compiler",
                prompt_hash="sha256:test",
                compiled_artifact_json='{"ok":true}',
                scope_themes="[]",
                invariants="[]",
                eligible_feature_ids="[]",
                rejected_features="[]",
                spec_gaps="[]",
            )
            session.add(authority)
            session.commit()
            session.refresh(authority)
            authority_id = authority.authority_id
        if record_progress is not None:
            assert record_progress("compiled_authority_persisted")
            assert record_progress("product_authority_cache_persisted")
        return {
            "success": True,
            "authority_id": authority_id,
            "spec_version_id": spec_version_id,
            "compiler_version": "test-compiler",
            "prompt_hash": "sha256:test",
        }

    monkeypatch.setattr(
        project_setup,
        "compile_spec_authority_for_version_with_engine",
        compile_for_test,
    )


def test_project_create_cli_from_non_repo_cwd_uses_caller_relative_spec(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    caller_dir = tmp_path / "caller"
    caller_dir.mkdir()
    spec_file = _write_spec(caller_dir)
    _write_sitecustomize_compiler_patch(caller_dir)
    business_db_path = tmp_path / "business.sqlite3"
    session_db_path = tmp_path / "sessions.sqlite3"

    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(caller_dir), str(repo_root)])
    env["AGILEFORGE_DB_URL"] = f"sqlite:///{business_db_path.as_posix()}"
    env["AGILEFORGE_SESSION_DB_URL"] = f"sqlite:///{session_db_path.as_posix()}"
    env["ALLOW_PROD_DB_IN_TEST"] = "1"
    env["RELAX_ZDR_FOR_TESTS"] = "true"
    result = subprocess.run(  # nosec B603
        [
            sys.executable,
            "-m",
            "cli.main",
            "project",
            "create",
            "--name",
            "Outside Repo Project",
            "--spec-file",
            "specs/app.md",
            "--idempotency-key",
            "outside-repo-project-001",
        ],
        cwd=caller_dir,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    payload = _payload_from_completed_process(result)
    assert result.returncode == 0, payload
    assert payload["ok"] is True
    data = payload["data"]
    project_id = data["project_id"]
    assert project_id
    assert Path(data["resolved_spec_path"]) == spec_file.resolve()
    assert spec_file.resolve().is_relative_to(caller_dir.resolve())

    with Session(_business_engine(business_db_path)) as session:
        project = session.get(Product, project_id)
        assert project is not None
        assert project.name == "Outside Repo Project"
        assert session.exec(select(SpecAuthorityAcceptance)).all() == []


def test_project_create_cli_returns_error_envelope_for_invalid_structured_spec(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    spec_file = _write_invalid_structured_spec(tmp_path)
    workflow = FakeWorkflowPort()
    runner = ProjectSetupMutationRunner(engine=engine, workflow=workflow)
    app = AgentWorkbenchApplication(project_setup_runner=runner)
    _install_compiler(monkeypatch, success=True)

    create_rc = main(
        [
            "project",
            "create",
            "--name",
            "Invalid Spec Project",
            "--spec-file",
            str(spec_file),
            "--idempotency-key",
            "invalid-structured-spec-project-001",
        ],
        application=app,
    )
    payload = _captured_payload(capsys)

    assert create_rc == INVALID_COMMAND_EXIT_CODE
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "SPEC_FILE_INVALID"
    first_error = payload["data"]["setup_failure_first_error"]
    assert first_error
    assert "Invalid agileforge.spec.v1 content" in first_error


def test_project_create_rejects_markdown_spec_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Project create refuses Markdown authority input before setup mutation."""
    business_db = tmp_path / "business.db"
    engine = _business_engine(business_db)
    workflow = FakeWorkflowPort()
    runner = ProjectSetupMutationRunner(engine=engine, workflow=workflow)
    app = AgentWorkbenchApplication(project_setup_runner=runner)
    caller_dir = tmp_path / "caller"
    caller_dir.mkdir()
    spec_file = _write_spec(caller_dir)
    monkeypatch.chdir(caller_dir)

    exit_code = main(
        [
            "project",
            "create",
            "--name",
            "Markdown Project",
            "--spec-file",
            str(spec_file),
            "--idempotency-key",
            "markdown-project-create-001",
        ],
        application=app,
    )

    payload = _captured_payload(capsys)
    assert exit_code == 2
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "SPEC_SOURCE_FORMAT_UNSUPPORTED"
    assert payload["errors"][0]["remediation"] == [
        "Generate specs/spec.json as agileforge.spec.v1 JSON.",
        "Retry project create with --spec-file specs/spec.json.",
    ]
    with Session(engine) as session:
        assert session.exec(select(Product)).all() == []


def test_project_setup_retry_cli_supersedes_original_create_recovery(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    spec_file = _write_spec(tmp_path)
    workflow = FakeWorkflowPort()
    runner = ProjectSetupMutationRunner(engine=engine, workflow=workflow)
    app = AgentWorkbenchApplication(project_setup_runner=runner)
    _install_compiler(
        monkeypatch,
        success=False,
        error_code="MUTATION_RECOVERY_REQUIRED",
    )

    create_rc = main(
        [
            "project",
            "create",
            "--name",
            "Retry Project",
            "--spec-file",
            str(spec_file),
            "--idempotency-key",
            "retry-create-project-001",
        ],
        application=app,
    )
    create_payload = _captured_payload(capsys)
    assert create_rc == 1
    assert create_payload["ok"] is False
    assert create_payload["errors"][0]["code"] == "MUTATION_RECOVERY_REQUIRED"
    project_id = create_payload["data"]["project_id"]
    original_event_id = create_payload["data"]["mutation_event_id"]

    _install_compiler(monkeypatch, success=True)
    expected_fingerprint = _retry_context_fingerprint(
        project_id=project_id,
        resolved_spec_path=spec_file.resolve(),
        workflow_state=workflow.get_session_status(str(project_id)),
    )

    retry_rc = main(
        [
            "project",
            "setup",
            "retry",
            "--project-id",
            str(project_id),
            "--spec-file",
            str(spec_file),
            "--expected-state",
            "SETUP_REQUIRED",
            "--expected-context-fingerprint",
            expected_fingerprint,
            "--recovery-mutation-event-id",
            str(original_event_id),
            "--idempotency-key",
            "retry-project-setup-001",
        ],
        application=app,
    )
    retry_payload = _captured_payload(capsys)
    assert retry_rc == 0
    assert retry_payload["ok"] is True
    retry_event_id = retry_payload["data"]["mutation_event_id"]

    with Session(engine) as session:
        projects = session.exec(select(Product)).all()
        original = session.get(CliMutationLedger, original_event_id)
        retry = session.get(CliMutationLedger, retry_event_id)
        assert len(projects) == 1
        assert original is not None
        assert retry is not None
        assert original.status == MutationStatus.SUPERSEDED.value
        assert original.superseded_by_mutation_event_id == retry_event_id
        assert retry.status == MutationStatus.SUCCEEDED.value
        assert retry.recovers_mutation_event_id == original_event_id

    replay_rc = main(
        [
            "project",
            "create",
            "--name",
            "Retry Project",
            "--spec-file",
            str(spec_file),
            "--idempotency-key",
            "retry-create-project-001",
        ],
        application=app,
    )
    replay_payload = _captured_payload(capsys)
    assert replay_rc == 0
    assert replay_payload["ok"] is True
    assert replay_payload["data"]["mutation_event_id"] == retry_event_id
