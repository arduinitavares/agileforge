"""Persistence regressions for durable Vision evidence snapshots."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from models.core import Project
from models.product_definition import VisionEvidenceSnapshot
from models.repository import RepositoryBinding
from models.workflow import WorkflowNodeAttempt
from repositories.workflow import WorkflowFactLoadError, WorkflowFactRepository
from services.contracts.vision_evidence import (
    VisionEvidenceBundle,
    VisionEvidenceItem,
)
from workflow.contracts import GRAPH_VERSION, JsonObject
from workflow.fingerprints import canonical_hash, canonical_json

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


_NOW = datetime(2026, 8, 10, 12, tzinfo=UTC)
_TAMPER_STATEMENTS = {
    "evidence_json": "UPDATE vision_evidence_snapshots SET evidence_json = ? "
    "WHERE vision_evidence_snapshot_id = ?",
    "warnings_json": "UPDATE vision_evidence_snapshots SET warnings_json = ? "
    "WHERE vision_evidence_snapshot_id = ?",
    "evidence_fingerprint": "UPDATE vision_evidence_snapshots "
    "SET evidence_fingerprint = ? WHERE vision_evidence_snapshot_id = ?",
}


def _id(value: int | None) -> int:
    assert value is not None
    return value


def _attempt(session: Session, project_id: int, ordinal: int) -> WorkflowNodeAttempt:
    started_at = _NOW + timedelta(minutes=ordinal)
    attempt = WorkflowNodeAttempt(
        project_id=project_id,
        node_id="vision.bootstrap",
        instance_key=None,
        graph_version=GRAPH_VERSION,
        fact_fingerprint=canonical_hash({"facts": ordinal}),
        business_fact_fingerprint=canonical_hash({"business": ordinal}),
        decision_fingerprint=canonical_hash({"decision": ordinal}),
        normalized_input_json="{}",
        input_fingerprint=canonical_hash({"input": ordinal}),
        model_id="fake/vision",
        execution_settings_json="{}",
        idempotency_key=f"vision-evidence-{project_id}-{ordinal}",
        actor="test@example.com",
        correlation_id=None,
        started_at=started_at,
        lease_expires_at=started_at + timedelta(minutes=1),
        attempt_fingerprint=canonical_hash({"attempt": ordinal}),
    )
    session.add(attempt)
    session.flush()
    _id(attempt.workflow_node_attempt_id)
    return attempt


def _binding(session: Session, project_id: int) -> RepositoryBinding:
    binding = RepositoryBinding(
        project_id=project_id,
        worktree_path="repository",
        common_git_dir="repository/.git",
        head_sha="a" * 40,
        branch_name="main",
        detached_head=False,
        dirty=False,
        status_fingerprint=canonical_hash({"status": project_id}),
        remotes_json="[]",
        warnings_json="[]",
        probe_version="agileforge.repository-probe.v1",
        inspected_at=_NOW,
        recorded_by="test@example.com",
    )
    session.add(binding)
    session.flush()
    _id(binding.repository_binding_id)
    return binding


def _evidence_bundle(project_name: str) -> VisionEvidenceBundle:
    content: JsonObject = {"project_name": project_name}
    item = VisionEvidenceItem(
        evidence_id="project:metadata",
        kind="project_metadata",
        relative_path=None,
        content_fingerprint=canonical_hash(content),
        trust="operator_provided",
        content=content,
        truncated=False,
    )
    payload: JsonObject = {
        "schema_version": "agileforge.vision-evidence.v1",
        "items": [item.model_dump(mode="json")],
        "warnings": [],
    }
    return VisionEvidenceBundle(
        schema_version="agileforge.vision-evidence.v1",
        items=(item,),
        warnings=(),
        evidence_fingerprint=canonical_hash(payload),
    )


def _snapshot(
    *,
    project_id: int,
    attempt_id: int,
    repository_binding_id: int | None = None,
) -> VisionEvidenceSnapshot:
    evidence = _evidence_bundle(f"Project {project_id}")
    return VisionEvidenceSnapshot(
        project_id=project_id,
        repository_binding_id=repository_binding_id,
        workflow_node_attempt_id=attempt_id,
        evidence_json=canonical_json(evidence.model_dump(mode="json")),
        evidence_fingerprint=evidence.evidence_fingerprint,
        warnings_json="[]",
        created_at=_NOW,
    )


def test_fresh_schema_persists_and_loads_vision_evidence_snapshot(
    engine: Engine,
) -> None:
    """Expose the canonical snapshot as a workflow fact with its exact evidence."""
    with Session(engine) as session:
        project = Project(name="Evidence project")
        session.add(project)
        session.flush()
        project_id = _id(project.project_id)
        attempt = _attempt(session, project_id, ordinal=1)
        snapshot = _snapshot(
            project_id=project_id,
            attempt_id=_id(attempt.workflow_node_attempt_id),
        )
        session.add(snapshot)
        session.commit()

        loaded = WorkflowFactRepository(session).load_vision_snapshot(project_id)
        full_snapshot = WorkflowFactRepository(session).load(project_id)

        assert len(loaded.vision_evidence_snapshots) == 1
        assert (
            full_snapshot.vision_evidence_snapshots == loaded.vision_evidence_snapshots
        )
        fact = loaded.vision_evidence_snapshots[0]
        assert fact.vision_evidence_snapshot_id == _id(
            snapshot.vision_evidence_snapshot_id
        )
        assert fact.repository_binding_id is None
        assert fact.workflow_node_attempt_id == _id(attempt.workflow_node_attempt_id)
        assert dict(fact.evidence) == _evidence_bundle(
            f"Project {project_id}"
        ).model_dump(mode="json")
        assert fact.warnings == ()


@pytest.mark.parametrize("reference", ["attempt", "binding"])
def test_vision_evidence_snapshot_rejects_cross_project_references(
    engine: Engine,
    reference: str,
) -> None:
    """Bind each snapshot identity to the same Project as its evidence record."""
    with Session(engine) as session:
        project = Project(name="Snapshot project")
        other_project = Project(name="Other snapshot project")
        session.add_all([project, other_project])
        session.flush()
        project_id = _id(project.project_id)
        other_project_id = _id(other_project.project_id)
        attempt = _attempt(session, project_id, ordinal=1)
        other_attempt = _attempt(session, other_project_id, ordinal=2)
        binding = _binding(session, project_id)
        other_binding = _binding(session, other_project_id)
        session.add(
            _snapshot(
                project_id=project_id,
                attempt_id=(
                    _id(other_attempt.workflow_node_attempt_id)
                    if reference == "attempt"
                    else _id(attempt.workflow_node_attempt_id)
                ),
                repository_binding_id=(
                    _id(other_binding.repository_binding_id)
                    if reference == "binding"
                    else _id(binding.repository_binding_id)
                ),
            )
        )

        with pytest.raises(IntegrityError):
            session.commit()


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("evidence_json", '{"schema_version":"invalid"}'),
        ("warnings_json", '{"not":"a-list"}'),
        ("evidence_fingerprint", "sha256:" + "0" * 64),
    ],
)
def test_vision_evidence_snapshot_loader_rejects_tampered_persistence(
    engine: Engine,
    column: str,
    value: str,
) -> None:
    """Reject invalid canonical evidence, warning JSON, or evidence fingerprints."""
    with Session(engine) as session:
        project = Project(name=f"Tampered snapshot {column}")
        session.add(project)
        session.flush()
        project_id = _id(project.project_id)
        attempt = _attempt(session, project_id, ordinal=1)
        snapshot = _snapshot(
            project_id=project_id,
            attempt_id=_id(attempt.workflow_node_attempt_id),
        )
        session.add(snapshot)
        session.commit()
        snapshot_id = _id(snapshot.vision_evidence_snapshot_id)
        session.connection().exec_driver_sql(
            _TAMPER_STATEMENTS[column],
            (value, snapshot_id),
        )
        session.commit()

        with pytest.raises(WorkflowFactLoadError):
            WorkflowFactRepository(session).load_vision_snapshot(project_id)
