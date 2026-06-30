"""Tests for downstream execution gates after Scope Discovery."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import pytest

from db.migrations import ensure_schema_current
from models.agent_workbench import DiscoveryChallengeArtifact, DiscoveryPrd
from models.core import Product
from models.specs import (
    CompiledSpecAuthority,
    SpecAuthorityAcceptance,
    SpecRegistry,
)
from services.agent_workbench import (
    backlog_phase,
    roadmap_phase,
    sprint_phase,
    story_phase,
)
from services.agent_workbench.backlog_phase import BacklogPhaseRunner
from services.agent_workbench.roadmap_phase import RoadmapPhaseRunner
from services.agent_workbench.sprint_phase import SprintPhaseRunner
from services.agent_workbench.story_phase import StoryPhaseRunner
from services.specs.compiler_service import _compiled_authority_artifact_json
from utils.spec_schemas import SpecAuthorityCompilationSuccess

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine
    from sqlmodel import Session


class _WorkflowService:
    """Workflow service double for direct phase command tests."""

    def __init__(self, *, fsm_state: str) -> None:
        self.state: dict[str, Any] = {
            "fsm_state": fsm_state,
            "setup_status": "passed",
            "product_vision_assessment": {
                "product_vision_statement": "A clear saved vision.",
                "is_complete": True,
            },
            "pending_spec_content": "SPEC CONTENT",
            "compiled_authority_cached": "AUTHORITY JSON",
        }

    def get_session_status(self, session_id: str) -> dict[str, Any]:
        """Return current workflow state."""
        del session_id
        return dict(self.state)

    async def initialize_session(self, *, session_id: str) -> str:
        """Return the initialized session id without mutating state."""
        return session_id

    def update_session_status(
        self,
        session_id: str,
        partial_update: dict[str, Any],
    ) -> None:
        """Persist workflow state updates."""
        del session_id
        self.state.update(partial_update)


def _seed_discovery_project(
    session: Session,
    *,
    challenge_readiness: str = "ready_for_prd",
    prd_status: str | None = "accepted",
) -> int:
    """Seed a project with discovery artifacts but no accepted authority."""
    product = Product(
        name=f"Execution Guard {challenge_readiness} {prd_status}",
        vision="A clear saved vision.",
        spec_file_path="specs/spec.json",
        compiled_authority_json='{"authority": true}',
    )
    session.add(product)
    session.commit()
    session.refresh(product)
    assert product.product_id is not None
    project_id = product.product_id
    challenge = DiscoveryChallengeArtifact(
        project_id=project_id,
        producer="grill-with-docs",
        readiness=challenge_readiness,
        original_idea="Add discovered execution scope.",
        content_json=json.dumps(
            {
                "producer": "grill-with-docs",
                "readiness": challenge_readiness,
                "original_idea": "Add discovered execution scope.",
                "assumptions": ["The authority gate will enforce acceptance."],
                "non_goals": ["Do not run work from drafts."],
                "risks": ["Direct CLI calls could bypass guided routing."],
                "evidence_conflicts": [],
                "open_questions": [],
            }
        ),
        artifact_fingerprint="sha256:challenge-execution",
        request_hash="sha256:challenge-request",
        idempotency_key=f"challenge-{challenge_readiness}",
    )
    session.add(challenge)
    session.commit()
    session.refresh(challenge)
    if prd_status is not None:
        assert challenge.challenge_artifact_id is not None
        prd = DiscoveryPrd(
            project_id=project_id,
            challenge_artifact_id=challenge.challenge_artifact_id,
            producer="to-prd",
            status=prd_status,
            version="1",
            title="Discovered Execution Scope",
            content_json=json.dumps(
                {
                    "producer": "to-prd",
                    "title": "Discovered Execution Scope",
                    "user_stories": ["As a user, I get guarded execution."],
                }
            ),
            artifact_fingerprint=f"sha256:prd-{prd_status}",
            request_hash=f"sha256:prd-request-{prd_status}",
            idempotency_key=f"prd-{prd_status}",
            reviewed_by="reviewer" if prd_status == "accepted" else None,
            reviewed_at=(
                datetime(2026, 6, 1, 12, tzinfo=UTC)
                if prd_status == "accepted"
                else None
            ),
        )
        session.add(prd)
        session.commit()
    return project_id


def _seed_current_authority_project(session: Session) -> int:
    """Seed a project with accepted current authority."""
    prompt_hash = "b" * 64
    product = Product(
        name="Execution Guard Accepted Authority",
        vision="A clear saved vision.",
        compiled_authority_json='{"authority": true}',
    )
    session.add(product)
    session.commit()
    session.refresh(product)
    assert product.product_id is not None
    project_id = product.product_id
    spec = SpecRegistry(
        product_id=project_id,
        spec_hash="sha256:accepted-spec",
        content="# Accepted Spec\n",
        status="approved",
        approved_at=datetime(2026, 6, 1, 12, tzinfo=UTC),
        approved_by="test",
    )
    session.add(spec)
    session.commit()
    session.refresh(spec)
    assert spec.spec_version_id is not None
    authority = CompiledSpecAuthority(
        spec_version_id=spec.spec_version_id,
        compiler_version="test",
        prompt_hash=prompt_hash,
        compiled_artifact_json=_compiled_authority_artifact_json(
            SpecAuthorityCompilationSuccess(
                scope_themes=["Accepted execution"],
                domain="execution guard",
                invariants=[],
                eligible_feature_rules=[],
                rejected_features=[],
                gaps=[],
                assumptions=[],
                source_map=[],
                compiler_version="test",
                prompt_hash=prompt_hash,
            )
        ),
        scope_themes="[]",
        invariants="[]",
        eligible_feature_ids="[]",
        rejected_features="[]",
        spec_gaps="[]",
    )
    session.add(authority)
    session.commit()
    session.refresh(authority)
    assert authority.authority_id is not None
    acceptance = SpecAuthorityAcceptance(
        product_id=project_id,
        spec_version_id=spec.spec_version_id,
        status="accepted",
        policy="test",
        decided_by="test",
        compiler_version="test",
        prompt_hash=prompt_hash,
        spec_hash=spec.spec_hash,
        pending_authority_id=authority.authority_id,
        terminal_decision_key=(
            f"{project_id}:{spec.spec_version_id}:{authority.authority_id}"
        ),
    )
    session.add(acceptance)
    session.commit()
    return project_id


def _assert_requires_accepted_authority(result: dict[str, Any]) -> None:
    assert result["ok"] is False
    error = result["errors"][0]
    assert error["code"] == "AUTHORITY_NOT_ACCEPTED"
    assert "Accepted Authority" in error["message"]
    assert "executable work" in error["message"].casefold()
    assert "Accepted Authority" in " ".join(error["remediation"])


def _patch_phase_engines(
    monkeypatch: pytest.MonkeyPatch,
    engine: Engine,
) -> None:
    """Patch modules that import get_engine directly."""
    ensure_schema_current(engine)
    monkeypatch.setattr(backlog_phase, "get_engine", lambda: engine)
    monkeypatch.setattr(roadmap_phase, "get_engine", lambda: engine)
    monkeypatch.setattr(story_phase, "get_engine", lambda: engine)
    monkeypatch.setattr(sprint_phase, "get_engine", lambda: engine)


@pytest.mark.parametrize(
    ("phase", "fsm_state"),
    [
        ("backlog", "BACKLOG_INTERVIEW"),
        ("roadmap", "ROADMAP_INTERVIEW"),
        ("story", "STORY_INTERVIEW"),
        ("sprint", "SPRINT_SETUP"),
    ],
)
def test_direct_generation_paths_require_current_accepted_authority(
    session: Session,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    *,
    phase: str,
    fsm_state: str,
) -> None:
    """Direct lower-level generation commands cannot run from discovery drafts."""
    _patch_phase_engines(monkeypatch, engine)
    project_id = _seed_discovery_project(session, prd_status="accepted")

    async def fake_generate(*_args: object, **_kwargs: object) -> dict[str, Any]:
        return {"fsm_state": "SHOULD_NOT_RUN"}

    monkeypatch.setattr(backlog_phase, "generate_backlog_draft", fake_generate)
    monkeypatch.setattr(roadmap_phase, "generate_roadmap_draft", fake_generate)
    monkeypatch.setattr(story_phase, "generate_story_draft", fake_generate)
    monkeypatch.setattr(sprint_phase, "generate_sprint_plan", fake_generate)
    workflow = _WorkflowService(fsm_state=fsm_state)
    if phase == "backlog":
        result = BacklogPhaseRunner(workflow_service=workflow).generate(
            project_id=project_id
        )
    elif phase == "roadmap":
        result = RoadmapPhaseRunner(workflow_service=workflow).generate(
            project_id=project_id
        )
    elif phase == "story":
        result = StoryPhaseRunner(workflow_service=workflow).generate(
            project_id=project_id,
            parent_requirement="Discovered requirement",
        )
    else:
        result = SprintPhaseRunner(workflow_service=cast("Any", workflow)).generate(
            project_id=project_id,
            max_story_points=5,
        )

    _assert_requires_accepted_authority(result)


@pytest.mark.parametrize(
    ("challenge_readiness", "prd_status"),
    [
        ("needs_answers", None),
        ("ready_for_prd", "draft"),
        ("ready_for_prd", "accepted"),
    ],
)
def test_backlog_generation_rejects_non_executable_discovery_states(
    session: Session,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    *,
    challenge_readiness: str,
    prd_status: str | None,
) -> None:
    """Discovery artifacts alone never satisfy the execution authority gate."""
    _patch_phase_engines(monkeypatch, engine)
    project_id = _seed_discovery_project(
        session,
        challenge_readiness=challenge_readiness,
        prd_status=prd_status,
    )

    async def fake_generate(*_args: object, **_kwargs: object) -> dict[str, Any]:
        return {"fsm_state": "BACKLOG_REVIEW"}

    monkeypatch.setattr(backlog_phase, "generate_backlog_draft", fake_generate)

    result = BacklogPhaseRunner(
        workflow_service=_WorkflowService(fsm_state="BACKLOG_INTERVIEW")
    ).generate(project_id=project_id)

    _assert_requires_accepted_authority(result)


def test_task_update_requires_current_accepted_authority(
    session: Session,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task execution cannot run while discovery scope lacks accepted authority."""
    _patch_phase_engines(monkeypatch, engine)
    project_id = _seed_discovery_project(session, prd_status="accepted")
    runner = SprintPhaseRunner(
        workflow_service=cast("Any", _WorkflowService(fsm_state="SPRINT_VIEW"))
    )

    result = runner.task_update(
        project_id=project_id,
        task_id=404,
        status="Done",
        expected_status="To Do",
        expected_task_fingerprint="sha256:task",
        idempotency_key="task-update-without-authority",
    )

    _assert_requires_accepted_authority(result)


def test_generation_allows_current_accepted_authority(
    session: Session,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Accepted current authority keeps normal generation paths executable."""
    _patch_phase_engines(monkeypatch, engine)
    project_id = _seed_current_authority_project(session)

    async def fake_generate(*_args: object, **_kwargs: object) -> dict[str, Any]:
        return {"fsm_state": "BACKLOG_REVIEW"}

    monkeypatch.setattr(backlog_phase, "generate_backlog_draft", fake_generate)

    result = BacklogPhaseRunner(
        workflow_service=_WorkflowService(fsm_state="BACKLOG_INTERVIEW")
    ).generate(project_id=project_id)

    assert result["ok"] is True, result
    assert result["data"]["fsm_state"] == "BACKLOG_REVIEW"
