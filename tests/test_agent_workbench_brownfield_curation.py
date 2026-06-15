"""Tests for brownfield source import and repository scan commands."""

# ruff: noqa: D103, PLR0913

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from sqlmodel import Session, select

from cli.main import main
from models.brownfield import BrownfieldScanAttempt, BrownfieldSourceArtifact
from models.core import Product
from models.specs import SpecRegistry
from services.agent_workbench.application import AgentWorkbenchApplication
from services.agent_workbench.brownfield_curation import BrownfieldCurationRunner
from services.agent_workbench.error_codes import ErrorCode

if TYPE_CHECKING:
    from pathlib import Path

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
