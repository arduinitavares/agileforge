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
        spec_hash: str,
        spec_version_id: int,
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
            "setup_status": "authority_compile_required",
            "setup_error": None,
            "setup_spec_file_path": str(resolved_spec_path),
            "setup_spec_hash": str(spec_hash),
            "setup_spec_version_id": int(spec_version_id),
            "setup_next_actions": [
                {
                    "command": "agileforge authority compile",
                    "args": {
                        "project_id": project_id,
                        "spec_version_id": int(spec_version_id),
                        "expected_spec_hash": str(spec_hash),
                        "expected_state": "SETUP_REQUIRED",
                        "expected_setup_status": "authority_compile_required",
                    },
                    "reason": "Compile pending authority before authority review.",
                }
            ],
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


def _structured_spec_payload(*, title: str = "Outside Repo Project") -> dict[str, Any]:
    """Build a minimal valid agileforge.spec.v1 fixture."""
    return {
        "schema_version": "agileforge.spec.v1",
        "artifact_id": "SPEC.outside-repo",
        "title": title,
        "status": "draft",
        "version": "0.1",
        "created_at": "2026-05-18",
        "updated_at": "2026-05-18",
        "summary": "Create a project from a structured authority spec.",
        "problem_statement": (
            "Operators need project setup to persist authority evidence."
        ),
        "items": [
            {
                "id": "REQ.outside-repo.audit",
                "type": "REQ",
                "status": "proposed",
                "level": "MUST",
                "title": "Project setup evidence",
                "statement": "The system MUST persist project setup evidence.",
                "verification": "system-test",
                "acceptance": [
                    "Project setup evidence is stored for each create request."
                ],
            }
        ],
        "relations": [],
        "controlled_terms": [],
        "external_references": [],
        "rendering": {
            "markdown_profile": "agileforge.spec_markdown.v1",
            "rendered_markdown_sha256": None,
        },
    }


def _write_structured_spec(caller_dir: Path) -> Path:
    """Write a structured project spec in the simulated caller repository."""
    spec_file = caller_dir / "specs" / "spec.json"
    spec_file.parent.mkdir(parents=True, exist_ok=True)
    spec_file.write_text(
        json.dumps(_structured_spec_payload()),
        encoding="utf-8",
    )
    return spec_file


def _write_markdown_spec(caller_dir: Path) -> Path:
    """Write unsupported Markdown authority input for rejection tests."""
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
    spec_file = _write_structured_spec(caller_dir)
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
            "specs/spec.json",
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
    assert data["setup_status"] == "authority_compile_required"
    assert data["fsm_state"] == "SETUP_REQUIRED"
    assert "pending_authority_id" not in data
    assert "compiled_authority_id" not in data
    assert data["next_actions"][0]["command"] == "agileforge authority compile"
    assert data["next_actions"][0]["args"]["project_id"] == project_id
    assert data["next_actions"][0]["args"]["spec_version_id"] == data["spec_version_id"]
    assert (
        data["next_actions"][0]["args"]["expected_spec_hash"]
        == data["spec_hash"]
    )
    assert (
        data["next_actions"][0]["args"]["expected_setup_status"]
        == "authority_compile_required"
    )

    with Session(_business_engine(business_db_path)) as session:
        project = session.get(Product, project_id)
        assert project is not None
        assert project.name == "Outside Repo Project"
        assert session.exec(select(CompiledSpecAuthority)).all() == []
        assert session.exec(select(SpecAuthorityAcceptance)).all() == []


def test_project_create_brownfield_cli_creates_shell(
    engine: Engine,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workflow = FakeWorkflowPort()
    runner = ProjectSetupMutationRunner(engine=engine, workflow=workflow)
    app = AgentWorkbenchApplication(project_setup_runner=runner)

    create_rc = main(
        [
            "project",
            "create",
            "--setup-mode",
            "brownfield",
            "--name",
            "Brownfield CLI Shell",
            "--idempotency-key",
            "brownfield-cli-shell-001",
        ],
        application=app,
    )
    payload = _captured_payload(capsys)

    assert create_rc == 0
    assert payload["ok"] is True
    data = payload["data"]
    assert data["setup_mode"] == "brownfield"
    assert data["setup_status"] == "brownfield_curation_required"
    assert data["spec_hash"] is None
    assert data["next_actions"][0]["command"] == "agileforge brownfield source import"


def test_project_create_then_authority_compile_cli_flow(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI setup flow should require explicit authority compile after create."""
    spec_file = _write_structured_spec(tmp_path)
    workflow = FakeWorkflowPort()
    runner = ProjectSetupMutationRunner(engine=engine, workflow=workflow)
    app = AgentWorkbenchApplication(project_setup_runner=runner)
    _install_compiler(monkeypatch, success=True)

    create_rc = main(
        [
            "project",
            "create",
            "--name",
            "Split Setup CLI Project",
            "--spec-file",
            str(spec_file),
            "--idempotency-key",
            "split-setup-create-001",
        ],
        application=app,
    )
    create = _captured_payload(capsys)
    assert create_rc == 0
    create_data = create["data"]
    compile_action = create_data["next_actions"][0]
    compile_args = compile_action["args"]
    assert compile_action["command"] == "agileforge authority compile"

    compile_rc = main(
        [
            "authority",
            "compile",
            "--project-id",
            str(compile_args["project_id"]),
            "--spec-version-id",
            str(compile_args["spec_version_id"]),
            "--expected-spec-hash",
            compile_args["expected_spec_hash"],
            "--expected-state",
            compile_args["expected_state"],
            "--expected-setup-status",
            compile_args["expected_setup_status"],
            "--idempotency-key",
            "split-setup-compile-001",
        ],
        application=app,
    )
    compiled = _captured_payload(capsys)

    assert compile_rc == 0
    assert compiled["ok"] is True
    assert compiled["data"]["setup_status"] == "authority_pending_review"
    assert compiled["data"]["pending_authority_id"] is not None


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
    assert payload["data"] is None
    assert (
        "Invalid agileforge.spec.v1 content"
        in payload["errors"][0]["details"]["reason"]
    )
    with Session(engine) as session:
        assert session.exec(select(Product)).all() == []


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
    spec_file = _write_markdown_spec(caller_dir)
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
    assert exit_code == INVALID_COMMAND_EXIT_CODE
    assert payload["ok"] is False
    assert payload["errors"][0]["code"] == "SPEC_SOURCE_FORMAT_UNSUPPORTED"
    assert payload["errors"][0]["remediation"] == [
        "Generate specs/spec.json as agileforge.spec.v1 JSON.",
        "Retry project create with --spec-file specs/spec.json.",
    ]
    with Session(engine) as session:
        assert session.exec(select(Product)).all() == []


def test_project_create_cli_defers_compiler_failure_to_authority_compile(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    spec_file = _write_structured_spec(tmp_path)
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
    assert create_rc == 0
    assert create_payload["ok"] is True
    data = create_payload["data"]
    project_id = data["project_id"]
    mutation_event_id = data["mutation_event_id"]
    assert data["setup_status"] == "authority_compile_required"
    assert data["fsm_state"] == "SETUP_REQUIRED"
    assert "pending_authority_id" not in data
    assert "compiled_authority_id" not in data
    assert data["next_actions"][0]["command"] == "agileforge authority compile"
    assert workflow.sessions[str(project_id)]["setup_status"] == (
        "authority_compile_required"
    )
    assert workflow.sessions[str(project_id)]["setup_spec_hash"] == data["spec_hash"]
    assert (
        workflow.sessions[str(project_id)]["setup_spec_version_id"]
        == data["spec_version_id"]
    )

    with Session(engine) as session:
        projects = session.exec(select(Product)).all()
        ledger = session.get(CliMutationLedger, mutation_event_id)
        assert len(projects) == 1
        assert ledger is not None
        assert ledger.status == MutationStatus.SUCCEEDED.value
        assert session.exec(select(CompiledSpecAuthority)).all() == []

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
    assert replay_payload["data"]["mutation_event_id"] == mutation_event_id
