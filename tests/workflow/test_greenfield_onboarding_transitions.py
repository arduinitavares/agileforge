"""Transactional greenfield onboarding transition tests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from pydantic import TypeAdapter, ValidationError
from sqlmodel import Session, col, select

from models.workflow import (
    ChallengeArtifact,
    PrdDecision,
    PrdVersion,
    SpecDraft,
    SpecDraftDecision,
)
from workflow import (
    DecideInitialSpecDraft,
    DecidePrd,
    OpenProjectShell,
    RecordChallengeArtifact,
    RecordInitialSpecDraft,
    RecordPrdVersion,
    RegisterInitialScope,
    TransitionRequest,
    WorkflowDomain,
)
from workflow.clock import FixedClock
from workflow.contracts import (
    NodeCategory,
    NodeDecision,
    TransitionResult,
    WorkflowErrorCode,
    WorkflowPosition,
)
from workflow.definitions.root import ROOT_GRAPH
from workflow.fingerprints import canonical_hash, canonical_json

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

EVALUATED_AT = datetime(2026, 8, 2, 15, tzinfo=UTC)
ACTOR = "operator@example.com"


@pytest.fixture
def domain(engine: Engine) -> WorkflowDomain:
    """Build a deterministic domain using the composed root graph."""
    return WorkflowDomain(
        engine=engine,
        graph=ROOT_GRAPH,
        clock=FixedClock(now_value=EVALUATED_AT),
    )


def _required_output_id(result: TransitionResult, key: str) -> int:
    value = result.output.get(key)
    assert isinstance(value, int)
    return value


def _available_decision(position: WorkflowPosition, node_id: str) -> NodeDecision:
    decision = next(item for item in position.decisions if item.node_id == node_id)
    assert decision.category is NodeCategory.AVAILABLE
    return decision


def _guards(
    position: WorkflowPosition,
    node_id: str,
    idempotency_key: str,
) -> dict[str, object]:
    decision = _available_decision(position, node_id)
    return {
        "project_id": position.project_id,
        "graph_version": position.graph_version,
        "fact_fingerprint": position.fact_fingerprint,
        "decision_fingerprint": decision.decision_fingerprint,
        "idempotency_key": idempotency_key,
        "actor": ACTOR,
        "correlation_id": "task-7",
        "instance_key": decision.instance_key,
    }


def _open_shell(domain: WorkflowDomain, *, key: str = "open-greenfield") -> int:
    result = domain.transition(
        OpenProjectShell(
            name=f"Greenfield {key}",
            origin="greenfield",
            idempotency_key=key,
            actor=ACTOR,
        )
    )
    assert result.ok is True
    return _required_output_id(result, "project_id")


def _record_challenge(
    domain: WorkflowDomain,
    project_id: int,
    *,
    key: str = "challenge-1",
) -> int:
    position = domain.position(project_id)
    result = domain.transition(
        RecordChallengeArtifact.model_validate(
            {
                **_guards(
                    position,
                    RecordChallengeArtifact.node_id,
                    key,
                ),
                "canonical_content": {
                    "challenge": "Trace workflow facts",
                    "constraints": ["append-only", "project-owned"],
                },
                "provenance_path": "provenance/challenge.json",
            }
        )
    )
    assert result.ok is True
    return _required_output_id(result, "challenge_artifact_id")


def _record_prd(
    domain: WorkflowDomain,
    project_id: int,
    challenge_artifact_id: int,
    *,
    key: str = "prd-1",
    supersedes_prd_version_id: int | None = None,
) -> int:
    position = domain.position(project_id)
    result = domain.transition(
        RecordPrdVersion.model_validate(
            {
                **_guards(position, RecordPrdVersion.node_id, key),
                "challenge_artifact_id": challenge_artifact_id,
                "canonical_content": {
                    "product": "AgileForge",
                    "outcomes": ["deterministic routing"],
                    "version": key,
                },
                "supersedes_prd_version_id": supersedes_prd_version_id,
                "provenance_path": "provenance/prd.json",
            }
        )
    )
    assert result.ok is True
    return _required_output_id(result, "prd_version_id")


def _decide_prd(
    domain: WorkflowDomain,
    project_id: int,
    prd_version_id: int,
    artifact_fingerprint: str,
    *,
    decision: str = "accepted",
) -> TransitionResult:
    position = domain.position(project_id)
    return domain.transition(
        DecidePrd.model_validate(
            {
                **_guards(
                    position,
                    DecidePrd.node_id,
                    f"decide-prd-{prd_version_id}-{decision}",
                ),
                "prd_version_id": prd_version_id,
                "artifact_fingerprint": artifact_fingerprint,
                "decision": decision,
                "notes": f"PRD {decision}",
            }
        )
    )


def _record_initial_spec(
    domain: WorkflowDomain,
    project_id: int,
    prd_version_id: int,
    *,
    key: str = "spec-1",
    supersedes_spec_draft_id: int | None = None,
) -> tuple[int, dict[str, object]]:
    content: dict[str, object] = {
        "scope": {
            "included": ["workflow graph"],
            "excluded": ["legacy state machine"],
        },
        "prd_version_id": prd_version_id,
        "version": key,
    }
    position = domain.position(project_id)
    result = domain.transition(
        RecordInitialSpecDraft.model_validate(
            {
                **_guards(position, RecordInitialSpecDraft.node_id, key),
                "prd_version_id": prd_version_id,
                "canonical_content": content,
                "supersedes_spec_draft_id": supersedes_spec_draft_id,
                "provenance_path": "provenance/spec.json",
            }
        )
    )
    assert result.ok is True
    return _required_output_id(result, "spec_draft_id"), content


def _artifact_fingerprint(
    engine: Engine,
    model: type[PrdVersion] | type[SpecDraft],
    identity: int,
) -> str:
    with Session(engine) as session:
        row = session.get(model, identity)
        assert row is not None
        return row.content_fingerprint


def test_closed_transition_union_contains_exactly_six_greenfield_variants() -> None:
    """Add the six concrete request variants without an open action escape hatch."""
    common = {
        "project_id": 1,
        "graph_version": "graph-v1",
        "fact_fingerprint": "sha256:facts",
        "decision_fingerprint": "sha256:decision",
        "idempotency_key": "request-key",
        "actor": ACTOR,
    }
    payloads = (
        {**common, "kind": "record_challenge_artifact", "canonical_content": {}},
        {
            **common,
            "kind": "record_prd_version",
            "challenge_artifact_id": 1,
            "canonical_content": {},
        },
        {
            **common,
            "kind": "decide_prd",
            "prd_version_id": 2,
            "artifact_fingerprint": "sha256:prd",
            "decision": "accepted",
            "notes": "Accepted",
        },
        {
            **common,
            "kind": "record_initial_spec_draft",
            "prd_version_id": 2,
            "canonical_content": {},
        },
        {
            **common,
            "kind": "decide_initial_spec_draft",
            "spec_draft_id": 3,
            "artifact_fingerprint": "sha256:spec",
            "decision": "accepted",
            "notes": "Accepted",
        },
        {**common, "kind": "register_initial_scope", "spec_draft_id": 3},
    )

    parsed_types = {
        type(TypeAdapter(TransitionRequest).validate_python(payload))
        for payload in payloads
    }

    assert parsed_types == {
        RecordChallengeArtifact,
        RecordPrdVersion,
        DecidePrd,
        RecordInitialSpecDraft,
        DecideInitialSpecDraft,
        RegisterInitialScope,
    }


def test_initial_spec_request_rejects_amendment_base_fields() -> None:
    """Keep initial-draft base identity impossible at the request boundary."""
    with pytest.raises(ValidationError):
        RecordInitialSpecDraft.model_validate(
            {
                "project_id": 1,
                "graph_version": "graph-v1",
                "fact_fingerprint": "sha256:facts",
                "decision_fingerprint": "sha256:decision",
                "idempotency_key": "spec",
                "actor": ACTOR,
                "prd_version_id": 2,
                "canonical_content": {},
                "base_spec_version_id": 9,
                "base_spec_hash": "sha256:base",
            }
        )


def test_greenfield_transitions_store_canonical_immutable_versions(
    domain: WorkflowDomain,
    engine: Engine,
) -> None:
    """Persist exact canonical JSON and hashes without mutating prior artifacts."""
    project_id = _open_shell(domain, key="canonical")
    challenge_id = _record_challenge(domain, project_id, key="canonical-challenge")
    prd_id = _record_prd(domain, project_id, challenge_id, key="canonical-prd")
    prd_fingerprint = _artifact_fingerprint(engine, PrdVersion, prd_id)
    prd_result = _decide_prd(
        domain,
        project_id,
        prd_id,
        prd_fingerprint,
    )
    assert prd_result.ok is True

    spec_id, spec_content = _record_initial_spec(
        domain,
        project_id,
        prd_id,
        key="canonical-spec",
    )

    with Session(engine) as session:
        challenge = session.get(ChallengeArtifact, challenge_id)
        prd = session.get(PrdVersion, prd_id)
        spec = session.get(SpecDraft, spec_id)
        assert challenge is not None
        assert prd is not None
        assert spec is not None
        assert challenge.content_fingerprint == canonical_hash(
            {
                "challenge": "Trace workflow facts",
                "constraints": ["append-only", "project-owned"],
            }
        )
        assert prd.canonical_content_json == canonical_json(
            {
                "product": "AgileForge",
                "outcomes": ["deterministic routing"],
                "version": "canonical-prd",
            }
        )
        assert spec.canonical_content_json == canonical_json(spec_content)
        assert spec.content_fingerprint == canonical_hash(spec_content)
        assert spec.kind == "initial"
        assert spec.base_spec_version_id is None
        assert spec.base_spec_hash is None


@pytest.mark.parametrize("decision", ["rejected", "feedback"])
def test_prd_terminal_feedback_exposes_append_only_new_version(
    domain: WorkflowDomain,
    engine: Engine,
    decision: str,
) -> None:
    """Retain the reviewed PRD and append a linked replacement version."""
    project_id = _open_shell(domain, key=f"prd-{decision}")
    challenge_id = _record_challenge(domain, project_id, key=f"challenge-{decision}")
    first_id = _record_prd(
        domain,
        project_id,
        challenge_id,
        key=f"prd-first-{decision}",
    )
    first_fingerprint = _artifact_fingerprint(engine, PrdVersion, first_id)
    decision_result = _decide_prd(
        domain,
        project_id,
        first_id,
        first_fingerprint,
        decision=decision,
    )
    assert decision_result.ok is True
    assert domain.position(project_id).available_nodes == (
        "onboarding.greenfield.prd",
        "onboarding.abandon_shell",
    )

    second_id = _record_prd(
        domain,
        project_id,
        challenge_id,
        key=f"prd-second-{decision}",
        supersedes_prd_version_id=first_id,
    )

    with Session(engine) as session:
        rows = session.exec(
            select(PrdVersion)
            .where(col(PrdVersion.project_id) == project_id)
            .order_by(col(PrdVersion.version_number))
        ).all()
        decisions = session.exec(
            select(PrdDecision).where(col(PrdDecision.project_id) == project_id)
        ).all()
        assert [row.prd_version_id for row in rows] == [first_id, second_id]
        assert rows[0].content_fingerprint == first_fingerprint
        assert rows[1].supersedes_prd_version_id == first_id
        assert [(item.prd_version_id, item.decision) for item in decisions] == [
            (first_id, decision)
        ]


def test_spec_feedback_exposes_append_only_initial_spec_replacement(
    domain: WorkflowDomain,
    engine: Engine,
) -> None:
    """Keep rejected initial drafts immutable and link the replacement."""
    project_id = _open_shell(domain, key="spec-feedback")
    challenge_id = _record_challenge(domain, project_id, key="spec-feedback-c")
    prd_id = _record_prd(domain, project_id, challenge_id, key="spec-feedback-p")
    prd_fingerprint = _artifact_fingerprint(engine, PrdVersion, prd_id)
    assert _decide_prd(
        domain,
        project_id,
        prd_id,
        prd_fingerprint,
    ).ok
    first_id, _content = _record_initial_spec(
        domain,
        project_id,
        prd_id,
        key="spec-feedback-first",
    )
    first_fingerprint = _artifact_fingerprint(engine, SpecDraft, first_id)
    position = domain.position(project_id)
    decision_result = domain.transition(
        DecideInitialSpecDraft.model_validate(
            {
                **_guards(position, DecideInitialSpecDraft.node_id, "spec-feedback-d"),
                "spec_draft_id": first_id,
                "artifact_fingerprint": first_fingerprint,
                "decision": "feedback",
                "notes": "Revise the boundary",
            }
        )
    )
    assert decision_result.ok is True

    second_id, _second_content = _record_initial_spec(
        domain,
        project_id,
        prd_id,
        key="spec-feedback-second",
        supersedes_spec_draft_id=first_id,
    )

    with Session(engine) as session:
        drafts = session.exec(
            select(SpecDraft)
            .where(col(SpecDraft.project_id) == project_id)
            .order_by(col(SpecDraft.version_number))
        ).all()
        decisions = session.exec(
            select(SpecDraftDecision).where(
                col(SpecDraftDecision.project_id) == project_id
            )
        ).all()
        assert [item.spec_draft_id for item in drafts] == [first_id, second_id]
        assert drafts[1].supersedes_spec_draft_id == first_id
        assert drafts[0].kind == drafts[1].kind == "initial"
        assert [(item.spec_draft_id, item.decision) for item in decisions] == [
            (first_id, "feedback")
        ]


def test_decision_handler_rejects_nonmatching_exact_artifact_fingerprint(
    domain: WorkflowDomain,
    engine: Engine,
) -> None:
    """Reject a request payload that targets content not offered by the graph."""
    project_id = _open_shell(domain, key="wrong-fingerprint")
    challenge_id = _record_challenge(domain, project_id, key="wrong-fingerprint-c")
    prd_id = _record_prd(
        domain,
        project_id,
        challenge_id,
        key="wrong-fingerprint-p",
    )

    result = _decide_prd(
        domain,
        project_id,
        prd_id,
        "sha256:not-the-stored-prd",
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code is WorkflowErrorCode.WORKFLOW_FACT_CONFLICT
    with Session(engine) as session:
        assert session.exec(select(PrdDecision)).all() == []
