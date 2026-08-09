"""Version-2 replacements for the retired scope-extension graph matrix."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlmodel import Session, col, select

from models.core import Project
from models.specs import CompiledSpecAuthority, SpecAuthorityAcceptance
from models.workflow import BacklogArtifact, BacklogArtifactDecision
from services.specs.authority_selection import pending_authority_fingerprint
from tests.workflow.lifecycle_fixtures import (
    PersistedSpecificationLineage,
    seed_accepted_specification,
)
from utils.spec_schemas import SpecAuthorityCompilationSuccess
from workflow.clock import FixedClock
from workflow.contracts import GRAPH_VERSION, NodeCategory, NodeDecision
from workflow.definitions.root import ROOT_GRAPH
from workflow.domain import WorkflowDomain
from workflow.fingerprints import canonical_hash, canonical_json

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

EVALUATED_AT = datetime(2026, 8, 9, 12, tzinfo=UTC)


@dataclass(frozen=True)
class _AcceptedAuthority:
    authority_id: int
    fingerprint: str


@dataclass(frozen=True)
class _AcceptedBacklog:
    backlog_artifact_id: int
    fingerprint: str


@dataclass(frozen=True)
class _BacklogWrite:
    ordinal: int
    supersedes: int | None
    created_at: datetime


def _required(value: int | None, label: str) -> int:
    assert value is not None, f"{label} has no durable identity"
    return value


def _project(session: Session) -> int:
    project = Project(name="Automatic delivery lineage")
    session.add(project)
    session.commit()
    return _required(project.project_id, "Project")


def _domain(engine: Engine) -> WorkflowDomain:
    return WorkflowDomain(
        engine=engine,
        graph=ROOT_GRAPH,
        clock=FixedClock(now_value=EVALUATED_AT),
    )


def _decision(
    domain: WorkflowDomain,
    project_id: int,
    node_id: str,
) -> NodeDecision:
    return next(
        item
        for item in domain.position(project_id).decisions
        if item.node_id == node_id
    )


def _reference(decision: NodeDecision, fact_type: str) -> tuple[int, str]:
    reference = next(
        item for item in decision.fact_references if item.fact_type == fact_type
    )
    return int(reference.fact_id), reference.fingerprint


def _accept_authority(
    session: Session,
    *,
    project_id: int,
    lineage: PersistedSpecificationLineage,
    ordinal: int,
    decided_at: datetime,
) -> _AcceptedAuthority:
    spec_version_id = _required(lineage.spec.spec_version_id, "Specification")
    artifact = SpecAuthorityCompilationSuccess(
        scope_themes=[f"Increment {ordinal}"],
        invariants=[],
        eligible_feature_rules=[],
        gaps=[],
        assumptions=[],
        source_map=[],
        compiler_version="3.0.0",
        prompt_hash=f"{ordinal}" * 64,
    )
    authority = CompiledSpecAuthority(
        spec_version_id=spec_version_id,
        compiler_version=artifact.compiler_version,
        prompt_hash=artifact.prompt_hash,
        compiled_at=decided_at,
        compiled_artifact_json=artifact.model_dump_json(),
        scope_themes="[]",
        invariants="[]",
        eligible_feature_ids="[]",
        rejected_features="[]",
        spec_gaps="[]",
    )
    session.add(authority)
    session.flush()
    authority_id = _required(authority.authority_id, "Authority")
    fingerprint = pending_authority_fingerprint(authority)
    assert fingerprint is not None
    session.add(
        SpecAuthorityAcceptance(
            project_id=project_id,
            spec_version_id=spec_version_id,
            status="accepted",
            policy="manual",
            decided_by="operator@example.com",
            decided_at=decided_at + timedelta(seconds=1),
            rationale="Accepted for the current specification.",
            compiler_version=authority.compiler_version,
            prompt_hash=authority.prompt_hash,
            spec_hash=lineage.spec.spec_hash,
            pending_authority_id=authority_id,
            authority_fingerprint=fingerprint,
            review_fingerprint=f"sha256:review-{ordinal}",
            terminal_decision_key=f"authority-{ordinal}",
        )
    )
    session.commit()
    return _AcceptedAuthority(authority_id=authority_id, fingerprint=fingerprint)


def _accept_backlog(
    session: Session,
    *,
    project_id: int,
    lineage: PersistedSpecificationLineage,
    authority: _AcceptedAuthority,
    write: _BacklogWrite,
) -> _AcceptedBacklog:
    content = {
        "backlog_items": [
            {
                "priority": 1,
                "requirement": f"Deliver increment {write.ordinal}",
                "authority_ref": f"REQ.increment-{write.ordinal}",
                "capability_hint": None,
                "value_driver": "Strategic",
                "justification": "Advance the active Product Goal.",
                "estimated_effort": "M",
                "technical_note": None,
            }
        ],
        "is_complete": True,
        "clarifying_questions": [],
    }
    fingerprint = canonical_hash(content)
    backlog = BacklogArtifact(
        project_id=project_id,
        authority_id=authority.authority_id,
        authority_fingerprint=authority.fingerprint,
        product_goal_artifact_id=lineage.product_goal_artifact_id,
        product_goal_fingerprint=lineage.product_goal_fingerprint,
        version_number=write.ordinal,
        canonical_content_json=canonical_json(content),
        content_fingerprint=fingerprint,
        supersedes_backlog_artifact_id=write.supersedes,
        created_by="operator@example.com",
        created_at=write.created_at,
    )
    session.add(backlog)
    session.flush()
    backlog_id = _required(backlog.backlog_artifact_id, "Backlog")
    session.add(
        BacklogArtifactDecision(
            project_id=project_id,
            backlog_artifact_id=backlog_id,
            artifact_fingerprint=fingerprint,
            decision="accepted",
            rationale="Accepted for delivery.",
            reviewer="operator@example.com",
            idempotency_key=f"backlog-{write.ordinal}",
            decided_at=write.created_at + timedelta(seconds=1),
        )
    )
    session.commit()
    return _AcceptedBacklog(
        backlog_artifact_id=backlog_id,
        fingerprint=fingerprint,
    )


def _seed_first_delivery(
    engine: Engine,
) -> tuple[int, PersistedSpecificationLineage, _AcceptedAuthority, _AcceptedBacklog]:
    with Session(engine) as session:
        project_id = _project(session)
        lineage = seed_accepted_specification(
            session,
            project_id=project_id,
            content='{"increment":1}',
            recorded_at=EVALUATED_AT - timedelta(minutes=20),
        )
        authority = _accept_authority(
            session,
            project_id=project_id,
            lineage=lineage,
            ordinal=1,
            decided_at=EVALUATED_AT - timedelta(minutes=15),
        )
        backlog = _accept_backlog(
            session,
            project_id=project_id,
            lineage=lineage,
            authority=authority,
            write=_BacklogWrite(
                ordinal=1,
                supersedes=None,
                created_at=EVALUATED_AT - timedelta(minutes=14),
            ),
        )
    return project_id, lineage, authority, backlog


def test_v2_root_has_no_scope_extension_or_reconciliation_nodes(engine: Engine) -> None:
    """The current specification starts Authority without a scope wrapper."""
    with Session(engine) as session:
        project_id = _project(session)
        lineage = seed_accepted_specification(
            session,
            project_id=project_id,
            content='{"increment":1}',
            recorded_at=EVALUATED_AT - timedelta(minutes=20),
        )

    position = _domain(engine).position(project_id)
    compile_decision = _decision(_domain(engine), project_id, "authority.compile")

    assert position.graph_version == GRAPH_VERSION
    assert compile_decision.category is NodeCategory.AVAILABLE
    assert _reference(compile_decision, "spec_version") == (
        _required(lineage.spec.spec_version_id, "Specification"),
        lineage.specification_fingerprint,
    )
    assert all("scope_extension" not in item.node_id for item in position.decisions)
    assert all("reconcile" not in item.node_id for item in position.decisions)


def test_later_specification_makes_old_delivery_lineage_non_current(
    engine: Engine,
) -> None:
    """A later accepted spec selects new Authority work without mutating history."""
    project_id, first, first_authority, first_backlog = _seed_first_delivery(engine)
    with Session(engine) as session:
        replacement = seed_accepted_specification(
            session,
            project_id=project_id,
            content='{"increment":2}',
            recorded_at=EVALUATED_AT - timedelta(minutes=10),
        )

    position = _domain(engine).position(project_id)
    compile_decision = _decision(_domain(engine), project_id, "authority.compile")

    assert compile_decision.category is NodeCategory.AVAILABLE
    assert _reference(compile_decision, "spec_version") == (
        _required(replacement.spec.spec_version_id, "Replacement specification"),
        replacement.specification_fingerprint,
    )
    assert "backlog.generate" not in position.available_nodes
    assert "planning.roadmap.generate" not in position.available_nodes
    with Session(engine) as session:
        old_backlog = session.get(BacklogArtifact, first_backlog.backlog_artifact_id)
        assert old_backlog is not None
        assert old_backlog.authority_id == first_authority.authority_id
        assert old_backlog.product_goal_artifact_id == first.product_goal_artifact_id
        assert old_backlog.supersedes_backlog_artifact_id is None


def test_replacement_authority_requires_goal_bound_backlog_before_planning(
    engine: Engine,
) -> None:
    """Automatic current-lineage selection replaces explicit reconciliation."""
    project_id, _first, _first_authority, first_backlog = _seed_first_delivery(engine)
    with Session(engine) as session:
        replacement = seed_accepted_specification(
            session,
            project_id=project_id,
            content='{"increment":2}',
            recorded_at=EVALUATED_AT - timedelta(minutes=10),
        )
        replacement_authority = _accept_authority(
            session,
            project_id=project_id,
            lineage=replacement,
            ordinal=2,
            decided_at=EVALUATED_AT - timedelta(minutes=5),
        )

    domain = _domain(engine)
    before = domain.position(project_id)
    generate = _decision(domain, project_id, "backlog.generate")
    assert generate.category is NodeCategory.AVAILABLE
    assert _reference(generate, "product_goal") == (
        replacement.product_goal_artifact_id,
        replacement.product_goal_fingerprint,
    )
    assert _reference(generate, "authority") == (
        replacement_authority.authority_id,
        replacement_authority.fingerprint,
    )
    assert _reference(generate, "backlog") == (
        first_backlog.backlog_artifact_id,
        first_backlog.fingerprint,
    )
    assert "planning.roadmap.generate" not in before.available_nodes

    with Session(engine) as session:
        replacement_backlog = _accept_backlog(
            session,
            project_id=project_id,
            lineage=replacement,
            authority=replacement_authority,
            write=_BacklogWrite(
                ordinal=2,
                supersedes=first_backlog.backlog_artifact_id,
                created_at=EVALUATED_AT - timedelta(minutes=4),
            ),
        )

    after = domain.position(project_id)
    roadmap = _decision(domain, project_id, "planning.roadmap.generate")
    assert roadmap.category is NodeCategory.AVAILABLE
    assert _reference(roadmap, "backlog") == (
        replacement_backlog.backlog_artifact_id,
        replacement_backlog.fingerprint,
    )
    assert all("reconcile" not in item.node_id for item in after.decisions)
    with Session(engine) as session:
        rows = session.exec(
            select(BacklogArtifact).order_by(col(BacklogArtifact.version_number))
        ).all()
        assert [row.backlog_artifact_id for row in rows] == [
            first_backlog.backlog_artifact_id,
            replacement_backlog.backlog_artifact_id,
        ]
