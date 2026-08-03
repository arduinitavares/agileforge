"""Regressions for failures from the removed pre-Project greenfield flow."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from sqlmodel import Session, col, select

import workflow.domain as workflow_domain_module
from models.core import Project
from models.workflow import ChallengeArtifact, DiscoveryRun, PrdVersion
from workflow import (
    OpenProjectShell,
    RecordChallengeArtifact,
    RecordPrdVersion,
    WorkflowDomain,
)
from workflow.clock import FixedClock
from workflow.definitions.root import ROOT_GRAPH

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from workflow.contracts import TransitionResult

EVALUATED_AT = datetime(2026, 8, 2, 17, tzinfo=UTC)
ACTOR = "operator@example.com"


class _DownstreamProbeError(RuntimeError):
    """Injected failure after Project-owned provenance already committed."""


@pytest.fixture
def domain(engine: Engine) -> WorkflowDomain:
    """Build a deterministic workflow domain."""
    return WorkflowDomain(
        engine=engine,
        graph=ROOT_GRAPH,
        clock=FixedClock(now_value=EVALUATED_AT),
    )


def _output_id(result: TransitionResult, key: str) -> int:
    value = result.output.get(key)
    assert isinstance(value, int)
    return value


def test_open_project_shell_replay_never_duplicates_project_or_initial_run(
    domain: WorkflowDomain,
    engine: Engine,
) -> None:
    """Make shell identity durable before any discovery artifact exists."""
    request = OpenProjectShell(
        name="Replay Project Shell",
        origin="greenfield",
        idempotency_key="probe-open-replay",
        actor=ACTOR,
    )

    first = domain.transition(request)
    replay = domain.transition(request)

    assert first.ok is True
    assert replay == first.model_copy(update={"replayed": True})
    with Session(engine) as session:
        assert len(session.exec(select(Project)).all()) == 1
        assert len(session.exec(select(DiscoveryRun)).all()) == 1


def test_failed_downstream_transition_preserves_project_owned_provenance(
    domain: WorkflowDomain,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep prior shell and artifact commits when a later transition fails."""
    open_request = OpenProjectShell(
        name="Preserved Provenance",
        origin="greenfield",
        idempotency_key="probe-open-preserved",
        actor=ACTOR,
    )
    opened = domain.transition(open_request)
    assert opened.ok is True
    project_id = _output_id(opened, "project_id")
    challenge_position = domain.position(project_id)
    challenge_decision = next(
        item
        for item in challenge_position.decisions
        if item.node_id == RecordChallengeArtifact.node_id
    )
    challenge_result = domain.transition(
        RecordChallengeArtifact(
            project_id=project_id,
            graph_version=challenge_position.graph_version,
            fact_fingerprint=challenge_position.fact_fingerprint,
            decision_fingerprint=challenge_decision.decision_fingerprint,
            idempotency_key="probe-challenge",
            actor=ACTOR,
            instance_key=challenge_decision.instance_key,
            canonical_content={"challenge": "Persist before downstream work"},
            provenance_path="provenance/project-owned-challenge.json",
        )
    )
    assert challenge_result.ok is True
    challenge_id = _output_id(challenge_result, "challenge_artifact_id")
    prd_position = domain.position(project_id)
    prd_decision = next(
        item
        for item in prd_position.decisions
        if item.node_id == RecordPrdVersion.node_id
    )
    prd_request = RecordPrdVersion(
        project_id=project_id,
        graph_version=prd_position.graph_version,
        fact_fingerprint=prd_position.fact_fingerprint,
        decision_fingerprint=prd_decision.decision_fingerprint,
        idempotency_key="probe-prd-failure",
        actor=ACTOR,
        instance_key=prd_decision.instance_key,
        challenge_artifact_id=challenge_id,
        canonical_content={"prd": "This write will fail"},
    )

    def fail_prd_handler(*_args: object, **_kwargs: object) -> object:
        raise _DownstreamProbeError

    monkeypatch.setattr(
        workflow_domain_module,
        "execute_record_prd_version",
        fail_prd_handler,
    )

    with pytest.raises(_DownstreamProbeError):
        domain.transition(prd_request)

    shell_replay = domain.transition(open_request)
    assert shell_replay.replayed is True
    assert _output_id(shell_replay, "project_id") == project_id
    with Session(engine) as session:
        project = session.get(Project, project_id)
        challenge = session.get(ChallengeArtifact, challenge_id)
        assert project is not None
        assert challenge is not None
        assert challenge.project_id == project_id
        assert challenge.provenance_path == ("provenance/project-owned-challenge.json")
        assert (
            len(
                session.exec(
                    select(DiscoveryRun).where(
                        col(DiscoveryRun.project_id) == project_id
                    )
                ).all()
            )
            == 1
        )
        assert session.exec(select(PrdVersion)).all() == []
