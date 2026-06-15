"""Tests for brownfield source import and repository scan commands."""

# ruff: noqa: D103, PLR0913

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlmodel import Session, select

from cli.main import main
from models.brownfield import (
    BrownfieldScanAttempt,
    BrownfieldSourceArtifact,
    BrownfieldSpecApproval,
    BrownfieldSpecDraftAttempt,
)
from models.core import Product
from models.specs import CompiledSpecAuthority, SpecRegistry
from services.agent_workbench.application import AgentWorkbenchApplication
from services.agent_workbench.brownfield_curation import BrownfieldCurationRunner
from services.agent_workbench.error_codes import ErrorCode

if TYPE_CHECKING:
    import pytest
    from sqlalchemy.engine import Engine


def _project(engine: Engine, name: str = "Brownfield App") -> int:
    with Session(engine) as session:
        product = Product(name=name)
        session.add(product)
        session.commit()
        session.refresh(product)
        assert product.product_id is not None
        return product.product_id


def _stdout_payload(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert isinstance(payload, dict)
    return payload


def _structured_spec_payload(*, title: str = "Curated Spec") -> dict[str, object]:
    return {
        "schema_version": "agileforge.spec.v1",
        "artifact_id": "SPEC.curated",
        "title": title,
        "status": "draft",
        "version": "0.1",
        "created_at": "2026-06-15",
        "updated_at": "2026-06-15",
        "summary": "Curated brownfield product specification.",
        "problem_statement": (
            "Operators need a reviewed product spec before authority compilation."
        ),
        "items": [
            {
                "id": "REQ.curated.001",
                "type": "REQ",
                "status": "proposed",
                "level": "MUST",
                "title": "Reviewed curated spec",
                "statement": (
                    "The system MUST compile authority only from reviewed "
                    "curated specs."
                ),
                "verification": "system-test",
                "acceptance": [
                    "Authority compile uses the managed approved spec path."
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


class _FakeBrownfieldRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def source_import(
        self,
        *,
        project_id: int,
        source_file: str,
        source_kind: str = "source_file",
        idempotency_key: str,
        correlation_id: str | None = None,
        changed_by: str = "cli-agent",
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "source_import",
                {
                    "project_id": project_id,
                    "source_file": source_file,
                    "source_kind": source_kind,
                    "idempotency_key": idempotency_key,
                    "correlation_id": correlation_id,
                    "changed_by": changed_by,
                },
            )
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "source_file": source_file},
            "warnings": [],
            "errors": [],
        }

    def scan(
        self,
        *,
        project_id: int,
        repo_path: str,
        source_attempt_id: str | None = None,
        idempotency_key: str,
        correlation_id: str | None = None,
        changed_by: str = "cli-agent",
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "scan",
                {
                    "project_id": project_id,
                    "repo_path": repo_path,
                    "source_attempt_id": source_attempt_id,
                    "idempotency_key": idempotency_key,
                    "correlation_id": correlation_id,
                    "changed_by": changed_by,
                },
            )
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "repo_path": repo_path},
            "warnings": [],
            "errors": [],
        }


class FakeBrownfieldWorkflow:
    """In-memory workflow port for brownfield approval tests."""

    def __init__(self) -> None:
        """Initialize empty fake workflow state."""
        self.sessions: dict[str, dict[str, object]] = {}

    def get_session_status(self, session_id: str) -> dict[str, object]:
        """Return fake workflow state for a session."""
        return dict(self.sessions.get(session_id, {}))

    def update_session_status(
        self,
        session_id: str,
        partial_update: dict[str, object],
    ) -> None:
        """Patch fake workflow state for a session."""
        current = self.sessions.setdefault(session_id, {})
        current.update(partial_update)


def test_source_import_records_non_authoritative_artifact(
    engine: Engine,
    tmp_path: Path,
) -> None:
    project_id = _project(engine)
    source = tmp_path / "notes.md"
    source.write_text("REQ: The system MUST reconcile invoices.\n", encoding="utf-8")
    runner = BrownfieldCurationRunner(engine=engine)

    result = runner.source_import(
        project_id=project_id,
        source_file=str(source),
        source_kind="notes",
        idempotency_key="source-import-001",
        changed_by="agent",
    )

    assert result["ok"] is True
    data = result["data"]
    assert data["attempt_id"].startswith("source-")
    assert data["artifact_fingerprint"].startswith("sha256:")
    assert data["source_sha256"].startswith("sha256:")
    with Session(engine) as session:
        source_rows = session.exec(select(BrownfieldSourceArtifact)).all()
        assert len(source_rows) == 1
        assert source_rows[0].project_id == project_id
        assert source_rows[0].source_file_path == str(source.resolve())
        assert source_rows[0].source_sha256 == data["source_sha256"]
        assert session.exec(select(SpecRegistry)).all() == []


def test_source_import_replays_same_idempotency_key(
    engine: Engine,
    tmp_path: Path,
) -> None:
    project_id = _project(engine)
    source = tmp_path / "notes.md"
    source.write_text("REQ: The system MUST reconcile invoices.\n", encoding="utf-8")
    runner = BrownfieldCurationRunner(engine=engine)

    first = runner.source_import(
        project_id=project_id,
        source_file=str(source),
        source_kind="notes",
        idempotency_key="source-replay-001",
        changed_by="agent",
    )
    replay = runner.source_import(
        project_id=project_id,
        source_file=str(source),
        source_kind="notes",
        idempotency_key="source-replay-001",
        changed_by="agent",
    )

    assert replay == first
    with Session(engine) as session:
        assert len(session.exec(select(BrownfieldSourceArtifact)).all()) == 1


def test_source_import_rejects_reused_idempotency_key_with_changed_file(
    engine: Engine,
    tmp_path: Path,
) -> None:
    project_id = _project(engine)
    first_source = tmp_path / "notes.md"
    first_source.write_text("REQ: First source.\n", encoding="utf-8")
    second_source = tmp_path / "changed.md"
    second_source.write_text("REQ: Changed source.\n", encoding="utf-8")
    runner = BrownfieldCurationRunner(engine=engine)

    runner.source_import(
        project_id=project_id,
        source_file=str(first_source),
        source_kind="notes",
        idempotency_key="source-reused-001",
        changed_by="agent",
    )
    result = runner.source_import(
        project_id=project_id,
        source_file=str(second_source),
        source_kind="notes",
        idempotency_key="source-reused-001",
        changed_by="agent",
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == ErrorCode.IDEMPOTENCY_KEY_REUSED.value
    with Session(engine) as session:
        assert len(session.exec(select(BrownfieldSourceArtifact)).all()) == 1


def test_source_import_rejects_missing_source_file(
    engine: Engine,
    tmp_path: Path,
) -> None:
    project_id = _project(engine)
    runner = BrownfieldCurationRunner(engine=engine)

    result = runner.source_import(
        project_id=project_id,
        source_file=str(tmp_path / "missing.md"),
        source_kind="notes",
        idempotency_key="source-missing-file-001",
        changed_by="agent",
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "BROWNFIELD_SOURCE_FILE_NOT_FOUND"


def test_scan_records_repo_snapshot_with_source_chain(
    engine: Engine,
    tmp_path: Path,
) -> None:
    project_id = _project(engine)
    source = tmp_path / "notes.md"
    source.write_text("REQ: The system MUST reconcile invoices.\n", encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text(
        "def reconcile():\n    return True\n",
        encoding="utf-8",
    )
    runner = BrownfieldCurationRunner(engine=engine)
    imported = runner.source_import(
        project_id=project_id,
        source_file=str(source),
        source_kind="notes",
        idempotency_key="source-import-002",
        changed_by="agent",
    )

    result = runner.scan(
        project_id=project_id,
        repo_path=str(repo),
        source_attempt_id=imported["data"]["attempt_id"],
        idempotency_key="scan-001",
        changed_by="agent",
    )

    assert result["ok"] is True
    data = result["data"]
    assert data["attempt_id"].startswith("scan-")
    assert data["source_attempt_id"] == imported["data"]["attempt_id"]
    assert data["source_fingerprint"] == imported["data"]["artifact_fingerprint"]
    with Session(engine) as session:
        scan_rows = session.exec(select(BrownfieldScanAttempt)).all()
        assert len(scan_rows) == 1
        assert scan_rows[0].repo_path == str(repo.resolve())
        assert scan_rows[0].source_fingerprint == imported["data"][
            "artifact_fingerprint"
        ]
        assert session.exec(select(SpecRegistry)).all() == []


def test_scan_without_source_records_no_source_fingerprint(
    engine: Engine,
    tmp_path: Path,
) -> None:
    project_id = _project(engine)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("print('hello')\n", encoding="utf-8")
    runner = BrownfieldCurationRunner(engine=engine)

    result = runner.scan(
        project_id=project_id,
        repo_path=str(repo),
        source_attempt_id=None,
        idempotency_key="scan-no-source-001",
        changed_by="agent",
    )

    assert result["ok"] is True
    assert result["data"]["source_fingerprint"] == "sha256:no-source"


def test_scan_rejects_missing_source_attempt(
    engine: Engine,
    tmp_path: Path,
) -> None:
    project_id = _project(engine)
    repo = tmp_path / "repo"
    repo.mkdir()
    runner = BrownfieldCurationRunner(engine=engine)

    result = runner.scan(
        project_id=project_id,
        repo_path=str(repo),
        source_attempt_id="source-missing",
        idempotency_key="scan-missing-source-001",
        changed_by="agent",
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "BROWNFIELD_SOURCE_NOT_FOUND"
    with Session(engine) as session:
        assert session.exec(select(BrownfieldScanAttempt)).all() == []


def test_scan_rejects_missing_repo_path(
    engine: Engine,
    tmp_path: Path,
) -> None:
    project_id = _project(engine)
    runner = BrownfieldCurationRunner(engine=engine)

    result = runner.scan(
        project_id=project_id,
        repo_path=str(tmp_path / "missing-repo"),
        idempotency_key="scan-missing-repo-001",
        changed_by="agent",
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "BROWNFIELD_REPO_PATH_NOT_FOUND"


def test_scan_manifest_skips_git_env_secret_and_large_files(
    engine: Engine,
    tmp_path: Path,
) -> None:
    project_id = _project(engine)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / ".git" / "HEAD").write_text("ref: main\n", encoding="utf-8")
    (repo / ".env").write_text("SECRET_KEY=value\n", encoding="utf-8")
    (repo / "service.secret").write_text("token=value\n", encoding="utf-8")
    (repo / "large.txt").write_bytes(b"x" * 250_000)
    (repo / "app.py").write_text("print('safe')\n", encoding="utf-8")
    runner = BrownfieldCurationRunner(engine=engine)

    result = runner.scan(
        project_id=project_id,
        repo_path=str(repo),
        idempotency_key="scan-bounded-001",
        changed_by="agent",
    )

    assert result["ok"] is True
    with Session(engine) as session:
        row = session.exec(select(BrownfieldScanAttempt)).one()
    manifest = json.loads(row.file_manifest_json)
    assert manifest == [
        {
            "path": "app.py",
            "sha256": result["data"]["manifest"][0]["sha256"],
            "size_bytes": len("print('safe')\n"),
        }
    ]


def test_spec_draft_from_typed_source_creates_reusable_candidate(
    engine: Engine,
    tmp_path: Path,
) -> None:
    project_id = _project(engine)
    source = tmp_path / "notes.md"
    source.write_text(
        "REQ: The system MUST reconcile invoices.\n"
        "DECISION: Operators approve imported invoices before posting.\n",
        encoding="utf-8",
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "routes.py").write_text("POST /invoices/import\n", encoding="utf-8")
    runner = BrownfieldCurationRunner(engine=engine)
    imported = runner.source_import(
        project_id=project_id,
        source_file=str(source),
        source_kind="notes",
        idempotency_key="draft-source-001",
        changed_by="agent",
    )
    scan = runner.scan(
        project_id=project_id,
        repo_path=str(repo),
        source_attempt_id=imported["data"]["attempt_id"],
        idempotency_key="draft-scan-001",
        changed_by="agent",
    )

    result = runner.spec_draft(
        project_id=project_id,
        scan_attempt_id=scan["data"]["attempt_id"],
        user_input="prioritize operator-visible behavior",
        idempotency_key="draft-001",
        changed_by="agent",
    )

    assert result["ok"] is True
    data = result["data"]
    assert data["attempt_id"].startswith("draft-")
    assert data["status"] == "complete"
    assert data["origin"] == "generated"
    assert data["spec_hash"].startswith("sha256:")
    assert data["artifact_fingerprint"].startswith("sha256:")
    assert data["scan_fingerprint"] == scan["data"]["artifact_fingerprint"]
    assert data["source_fingerprint"] == imported["data"]["artifact_fingerprint"]
    with Session(engine) as session:
        drafts = session.exec(select(BrownfieldSpecDraftAttempt)).all()
        assert len(drafts) == 1
        assert drafts[0].origin == "generated"
        assert drafts[0].curated_spec_json is not None
        assert json.loads(drafts[0].curated_spec_json)["schema_version"] == (
            "agileforge.spec.v1"
        )
        assert session.exec(select(SpecRegistry)).all() == []
        assert session.exec(select(CompiledSpecAuthority)).all() == []


def test_spec_draft_replays_same_idempotency_key(
    engine: Engine,
    tmp_path: Path,
) -> None:
    project_id = _project(engine)
    source = tmp_path / "notes.md"
    source.write_text("REQ: The system MUST reconcile invoices.\n", encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("print('hello')\n", encoding="utf-8")
    runner = BrownfieldCurationRunner(engine=engine)
    imported = runner.source_import(
        project_id=project_id,
        source_file=str(source),
        source_kind="notes",
        idempotency_key="draft-replay-source-001",
        changed_by="agent",
    )
    scan = runner.scan(
        project_id=project_id,
        repo_path=str(repo),
        source_attempt_id=imported["data"]["attempt_id"],
        idempotency_key="draft-replay-scan-001",
        changed_by="agent",
    )

    first = runner.spec_draft(
        project_id=project_id,
        scan_attempt_id=scan["data"]["attempt_id"],
        idempotency_key="draft-replay-001",
        changed_by="agent",
    )
    replay = runner.spec_draft(
        project_id=project_id,
        scan_attempt_id=scan["data"]["attempt_id"],
        idempotency_key="draft-replay-001",
        changed_by="agent",
    )

    assert replay == first
    with Session(engine) as session:
        assert len(session.exec(select(BrownfieldSpecDraftAttempt)).all()) == 1


def test_spec_draft_rejects_missing_scan(
    engine: Engine,
) -> None:
    project_id = _project(engine)
    runner = BrownfieldCurationRunner(engine=engine)

    result = runner.spec_draft(
        project_id=project_id,
        scan_attempt_id="scan-missing",
        idempotency_key="draft-missing-scan-001",
        changed_by="agent",
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "BROWNFIELD_SCAN_NOT_FOUND"


def test_spec_import_records_human_imported_candidate(
    engine: Engine,
    tmp_path: Path,
) -> None:
    project_id = _project(engine, name="Human Import Product")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("print('hello')\n", encoding="utf-8")
    runner = BrownfieldCurationRunner(engine=engine)
    scan = runner.scan(
        project_id=project_id,
        repo_path=str(repo),
        source_attempt_id=None,
        idempotency_key="import-scan-001",
        changed_by="agent",
    )
    curated = tmp_path / "curated.json"
    curated.write_text(
        json.dumps(_structured_spec_payload(title="Human Curated Spec")),
        encoding="utf-8",
    )

    result = runner.spec_import(
        project_id=project_id,
        curated_spec_file=str(curated),
        expected_scan_fingerprint=scan["data"]["artifact_fingerprint"],
        parent_draft_attempt_id=None,
        idempotency_key="import-001",
        changed_by="agent",
    )

    assert result["ok"] is True
    data = result["data"]
    assert data["origin"] == "human_import"
    assert data["status"] == "complete"
    assert data["spec_hash"].startswith("sha256:")
    with Session(engine) as session:
        drafts = session.exec(select(BrownfieldSpecDraftAttempt)).all()
        assert len(drafts) == 1
        assert drafts[0].origin == "human_import"
        assert drafts[0].imported_file_path == str(curated.resolve())
        assert session.exec(select(SpecRegistry)).all() == []
        assert session.exec(select(CompiledSpecAuthority)).all() == []


def test_spec_import_rejects_missing_parent_draft(
    engine: Engine,
    tmp_path: Path,
) -> None:
    project_id = _project(engine)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("print('hello')\n", encoding="utf-8")
    curated = tmp_path / "curated.json"
    curated.write_text(json.dumps(_structured_spec_payload()), encoding="utf-8")
    runner = BrownfieldCurationRunner(engine=engine)
    scan = runner.scan(
        project_id=project_id,
        repo_path=str(repo),
        idempotency_key="import-parent-scan-001",
        changed_by="agent",
    )

    result = runner.spec_import(
        project_id=project_id,
        curated_spec_file=str(curated),
        expected_scan_fingerprint=scan["data"]["artifact_fingerprint"],
        parent_draft_attempt_id="draft-missing",
        idempotency_key="import-missing-parent-001",
        changed_by="agent",
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "BROWNFIELD_DRAFT_NOT_FOUND"


def test_spec_import_rejects_scan_fingerprint_mismatch(
    engine: Engine,
    tmp_path: Path,
) -> None:
    project_id = _project(engine)
    curated = tmp_path / "curated.json"
    curated.write_text(json.dumps(_structured_spec_payload()), encoding="utf-8")
    runner = BrownfieldCurationRunner(engine=engine)

    result = runner.spec_import(
        project_id=project_id,
        curated_spec_file=str(curated),
        expected_scan_fingerprint="sha256:wrong",
        idempotency_key="import-chain-mismatch-001",
        changed_by="agent",
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "BROWNFIELD_APPROVAL_CHAIN_MISMATCH"


def test_spec_approve_registers_managed_spec_and_workflow_state(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_root = tmp_path / "config-root"
    config_root.mkdir()
    monkeypatch.setenv("AGILEFORGE_CONFIG_ROOT", str(config_root))
    project_id = _project(engine, name="Approval Product")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("print('hello')\n", encoding="utf-8")
    curated = tmp_path / "curated.json"
    curated.write_text(json.dumps(_structured_spec_payload()), encoding="utf-8")
    workflow = FakeBrownfieldWorkflow()
    workflow.sessions[str(project_id)] = {
        "fsm_state": "SETUP_REQUIRED",
        "setup_mode": "brownfield",
        "setup_status": "brownfield_curation_required",
    }
    runner = BrownfieldCurationRunner(engine=engine, workflow=workflow)
    scan = runner.scan(
        project_id=project_id,
        repo_path=str(repo),
        source_attempt_id=None,
        idempotency_key="approve-scan-001",
        changed_by="agent",
    )
    imported = runner.spec_import(
        project_id=project_id,
        curated_spec_file=str(curated),
        expected_scan_fingerprint=scan["data"]["artifact_fingerprint"],
        parent_draft_attempt_id=None,
        idempotency_key="approve-import-001",
        changed_by="agent",
    )

    result = runner.spec_approve(
        project_id=project_id,
        attempt_id=imported["data"]["attempt_id"],
        expected_artifact_fingerprint=imported["data"]["artifact_fingerprint"],
        expected_state="SETUP_REQUIRED",
        expected_setup_status="brownfield_curation_required",
        idempotency_key="approve-001",
        changed_by="agent",
    )

    assert result["ok"] is True
    data = result["data"]
    assert data["setup_status"] == "authority_compile_required"
    assert data["spec_version_id"] is not None
    managed_path = Path(data["setup_spec_file_path"])
    assert managed_path.exists()
    assert managed_path.is_relative_to(config_root / "artifacts" / "brownfield")
    assert workflow.sessions[str(project_id)]["setup_spec_file_path"] == str(
        managed_path
    )
    with Session(engine) as session:
        spec = session.get(SpecRegistry, data["spec_version_id"])
        assert spec is not None
        assert spec.content_ref == str(managed_path)
        approvals = session.exec(select(BrownfieldSpecApproval)).all()
        assert len(approvals) == 1
        assert approvals[0].spec_version_id == data["spec_version_id"]
        assert approvals[0].status == "complete"


def test_spec_approve_replay_does_not_duplicate_spec_registry(
    engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_root = tmp_path / "config-root"
    config_root.mkdir()
    monkeypatch.setenv("AGILEFORGE_CONFIG_ROOT", str(config_root))
    project_id = _project(engine, name="Replay Approval Product")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("print('hello')\n", encoding="utf-8")
    curated = tmp_path / "curated.json"
    curated.write_text(json.dumps(_structured_spec_payload()), encoding="utf-8")
    workflow = FakeBrownfieldWorkflow()
    workflow.sessions[str(project_id)] = {
        "fsm_state": "SETUP_REQUIRED",
        "setup_mode": "brownfield",
        "setup_status": "brownfield_curation_required",
    }
    runner = BrownfieldCurationRunner(engine=engine, workflow=workflow)
    scan = runner.scan(
        project_id=project_id,
        repo_path=str(repo),
        source_attempt_id=None,
        idempotency_key="replay-scan-001",
        changed_by="agent",
    )
    imported = runner.spec_import(
        project_id=project_id,
        curated_spec_file=str(curated),
        expected_scan_fingerprint=scan["data"]["artifact_fingerprint"],
        parent_draft_attempt_id=None,
        idempotency_key="replay-import-001",
        changed_by="agent",
    )
    args = {
        "project_id": project_id,
        "attempt_id": imported["data"]["attempt_id"],
        "expected_artifact_fingerprint": imported["data"]["artifact_fingerprint"],
        "expected_state": "SETUP_REQUIRED",
        "expected_setup_status": "brownfield_curation_required",
        "idempotency_key": "replay-approve-001",
        "changed_by": "agent",
    }

    first = runner.spec_approve(**args)
    replay = runner.spec_approve(**args)

    assert replay["ok"] is True
    assert replay["data"]["spec_version_id"] == first["data"]["spec_version_id"]
    with Session(engine) as session:
        assert len(session.exec(select(SpecRegistry)).all()) == 1
        assert len(session.exec(select(BrownfieldSpecApproval)).all()) == 1


def test_application_delegates_brownfield_source_and_scan() -> None:
    runner = _FakeBrownfieldRunner()
    app = AgentWorkbenchApplication(brownfield_runner=runner)

    source = app.brownfield_source_import(
        project_id=7,
        source_file="notes.md",
        source_kind="notes",
        idempotency_key="source-app-001",
        correlation_id="corr-source",
        changed_by="agent",
    )
    scan = app.brownfield_scan(
        project_id=7,
        repo_path=".",
        source_attempt_id="source-1",
        idempotency_key="scan-app-001",
        correlation_id="corr-scan",
        changed_by="agent",
    )

    assert source["ok"] is True
    assert scan["ok"] is True
    assert runner.calls == [
        (
            "source_import",
            {
                "project_id": 7,
                "source_file": "notes.md",
                "source_kind": "notes",
                "idempotency_key": "source-app-001",
                "correlation_id": "corr-source",
                "changed_by": "agent",
            },
        ),
        (
            "scan",
            {
                "project_id": 7,
                "repo_path": ".",
                "source_attempt_id": "source-1",
                "idempotency_key": "scan-app-001",
                "correlation_id": "corr-scan",
                "changed_by": "agent",
            },
        ),
    ]


def test_cli_routes_brownfield_source_import(
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = _FakeBrownfieldRunner()
    app = AgentWorkbenchApplication(brownfield_runner=runner)

    rc = main(
        [
            "brownfield",
            "source",
            "import",
            "--project-id",
            "7",
            "--source-file",
            "notes.md",
            "--source-kind",
            "notes",
            "--idempotency-key",
            "source-cli-001",
            "--correlation-id",
            "corr-source",
            "--changed-by",
            "agent",
        ],
        application=app,
    )

    payload = _stdout_payload(capsys)
    assert rc == 0
    assert payload["meta"]["command"] == "agileforge brownfield source import"
    assert runner.calls == [
        (
            "source_import",
            {
                "project_id": 7,
                "source_file": "notes.md",
                "source_kind": "notes",
                "idempotency_key": "source-cli-001",
                "correlation_id": "corr-source",
                "changed_by": "agent",
            },
        )
    ]


def test_cli_routes_brownfield_scan(
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = _FakeBrownfieldRunner()
    app = AgentWorkbenchApplication(brownfield_runner=runner)

    rc = main(
        [
            "brownfield",
            "scan",
            "--project-id",
            "7",
            "--repo-path",
            ".",
            "--source-attempt-id",
            "source-1",
            "--idempotency-key",
            "scan-cli-001",
            "--correlation-id",
            "corr-scan",
            "--changed-by",
            "agent",
        ],
        application=app,
    )

    payload = _stdout_payload(capsys)
    assert rc == 0
    assert payload["meta"]["command"] == "agileforge brownfield scan"
    assert runner.calls == [
        (
            "scan",
            {
                "project_id": 7,
                "repo_path": ".",
                "source_attempt_id": "source-1",
                "idempotency_key": "scan-cli-001",
                "correlation_id": "corr-scan",
                "changed_by": "agent",
            },
        )
    ]
