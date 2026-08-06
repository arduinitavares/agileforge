"""Host preparation of authority compiler input from durable spec facts."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlmodel import Session

from models.core import Project
from models.product_definition import (
    DiscoveryArtifact,
    ProductGoalArtifact,
    ProductGoalInterviewTurn,
    SpecificationCandidate,
    VisionArtifact,
    VisionInterviewTurn,
)
from models.specs import SpecRegistry
from models.workflow import WorkflowNodeAttempt
from services.authority_compilation_input import (
    AuthorityCompilationInputError,
    AuthorityCompilationInputService,
)
from services.specs.profile_content import normalize_spec_content_for_registry
from utils.spec_schemas import SpecAuthorityCompilerInput
from workflow.contracts import (
    FactReference,
    NodeCategory,
    NodeDecision,
    RecommendationKind,
)
from workflow.fingerprints import canonical_hash, canonical_json

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

PROJECT_ID = 41
COMPILER_MODEL = "offline/authority-compiler"


def _canonical_spec() -> tuple[str, str]:
    raw = json.dumps(
        {
            "schema_version": "agileforge.spec.v1",
            "artifact_id": "SPEC.authority-input",
            "title": "Authority input",
            "status": "draft",
            "version": "0.1",
            "created_at": "2026-08-04",
            "updated_at": "2026-08-04",
            "summary": "Compile one registered specification.",
            "problem_statement": "Compiler input must come from durable facts.",
            "items": [
                {
                    "id": "REQ.authority.input",
                    "type": "REQ",
                    "status": "accepted",
                    "level": "MUST",
                    "title": "Prepare compiler input",
                    "statement": "AgileForge MUST prepare compiler input internally.",
                    "verification": "system-test",
                    "acceptance": ["The operator supplies no specification payload."],
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
    )
    normalized = normalize_spec_content_for_registry(raw)
    return normalized.content, normalized.spec_hash


def _seed_candidate(
    session: Session,
    *,
    content: str,
    content_fingerprint: str,
) -> SpecificationCandidate:
    """Create the complete durable parent chain required by SpecRegistry."""
    started_at = datetime.now(UTC)
    attempt = WorkflowNodeAttempt(
        project_id=PROJECT_ID,
        node_id="goal.interview",
        graph_version="test",
        fact_fingerprint="facts",
        business_fact_fingerprint="business-facts",
        decision_fingerprint="decision",
        normalized_input_json="{}",
        input_fingerprint="input",
        model_id="offline",
        execution_settings_json="{}",
        idempotency_key="attempt",
        actor="test",
        started_at=started_at,
        lease_expires_at=started_at + timedelta(minutes=1),
        attempt_fingerprint="attempt-fingerprint",
    )
    session.add(attempt)
    session.flush()
    assert attempt.workflow_node_attempt_id is not None
    vision_turn = VisionInterviewTurn(
        project_id=PROJECT_ID,
        mode="initial",
        turn_number=1,
        user_text="vision",
        components_json="{}",
        vision_statement="Vision",
        is_complete=True,
        clarifying_questions_json="[]",
        output_fingerprint="vision-output",
        workflow_node_attempt_id=attempt.workflow_node_attempt_id,
        attempt_fingerprint=attempt.attempt_fingerprint,
    )
    session.add(vision_turn)
    session.flush()
    assert vision_turn.vision_interview_turn_id is not None
    vision = VisionArtifact(
        project_id=PROJECT_ID,
        version_number=1,
        components_json="{}",
        statement="Vision",
        content_fingerprint="vision-fingerprint",
        source_interview_turn_id=vision_turn.vision_interview_turn_id,
        created_by="test",
    )
    session.add(vision)
    session.flush()
    assert vision.vision_artifact_id is not None
    goal_turn = ProductGoalInterviewTurn(
        project_id=PROJECT_ID,
        vision_artifact_id=vision.vision_artifact_id,
        vision_fingerprint=vision.content_fingerprint,
        goal_number=1,
        revision_number=1,
        user_text="goal",
        components_json="{}",
        goal_statement="Goal",
        is_complete=True,
        clarifying_questions_json="[]",
        output_fingerprint="goal-output",
        workflow_node_attempt_id=attempt.workflow_node_attempt_id,
        attempt_fingerprint=attempt.attempt_fingerprint,
    )
    session.add(goal_turn)
    session.flush()
    assert goal_turn.product_goal_interview_turn_id is not None
    goal = ProductGoalArtifact(
        project_id=PROJECT_ID,
        vision_artifact_id=vision.vision_artifact_id,
        vision_fingerprint=vision.content_fingerprint,
        goal_number=1,
        revision_number=1,
        statement="Goal",
        content_fingerprint="goal-fingerprint",
        source_interview_turn_id=goal_turn.product_goal_interview_turn_id,
        created_by="test",
    )
    session.add(goal)
    session.flush()
    assert goal.product_goal_artifact_id is not None
    discovery = DiscoveryArtifact(
        project_id=PROJECT_ID,
        vision_artifact_id=vision.vision_artifact_id,
        vision_fingerprint=vision.content_fingerprint,
        product_goal_artifact_id=goal.product_goal_artifact_id,
        product_goal_fingerprint=goal.content_fingerprint,
        canonical_content_json="{}",
        content_fingerprint="discovery-fingerprint",
        producer="grill-me-with-docs",
        recorded_by="test",
    )
    session.add(discovery)
    session.flush()
    assert discovery.discovery_artifact_id is not None
    candidate = SpecificationCandidate(
        project_id=PROJECT_ID,
        vision_artifact_id=vision.vision_artifact_id,
        vision_fingerprint=vision.content_fingerprint,
        product_goal_artifact_id=goal.product_goal_artifact_id,
        product_goal_fingerprint=goal.content_fingerprint,
        discovery_artifact_id=discovery.discovery_artifact_id,
        discovery_fingerprint=discovery.content_fingerprint,
        base_spec_version_id=None,
        base_spec_hash=None,
        canonical_content_json=content,
        content_fingerprint=content_fingerprint,
        content_ref=None,
        supersedes_specification_candidate_id=None,
        recorded_by="test",
    )
    session.add(candidate)
    session.flush()
    assert candidate.specification_candidate_id is not None
    return candidate


def _seed_spec(engine: Engine) -> SpecRegistry:
    content, spec_hash = _canonical_spec()
    with Session(engine) as session:
        session.add(
            Project(
                project_id=PROJECT_ID,
                name="Authority compiler input",
                origin="brownfield",
            )
        )
        candidate = _seed_candidate(
            session,
            content=content,
            content_fingerprint=spec_hash,
        )
        spec = SpecRegistry(
            project_id=PROJECT_ID,
            spec_hash=spec_hash,
            content=content,
            status="approved",
            source_specification_candidate_id=(candidate.specification_candidate_id),
            source_vision_artifact_id=candidate.vision_artifact_id,
            source_vision_fingerprint=candidate.vision_fingerprint,
            source_product_goal_artifact_id=candidate.product_goal_artifact_id,
            source_product_goal_fingerprint=candidate.product_goal_fingerprint,
            source_discovery_artifact_id=candidate.discovery_artifact_id,
            source_discovery_fingerprint=candidate.discovery_fingerprint,
        )
        session.add(spec)
        session.commit()
        session.refresh(spec)
        assert spec.spec_version_id is not None
        return spec


def _compile_decision(spec: SpecRegistry) -> NodeDecision:
    assert spec.spec_version_id is not None
    return NodeDecision(
        node_id="authority.compile",
        instance_key=f"spec:{spec.spec_version_id}:{spec.spec_hash}",
        child_graph_id="authority",
        request_kind="compile_authority",
        category=NodeCategory.AVAILABLE,
        recommendation_kind=RecommendationKind.REQUIRED,
        reason_code="AUTHORITY_COMPILE_REQUIRED",
        fact_references=(
            FactReference(
                fact_type="spec_version",
                fact_id=str(spec.spec_version_id),
                fingerprint=spec.spec_hash,
            ),
        ),
        decision_fingerprint="decision-authority-compile",
    )


def test_builds_compiler_input_from_exact_registered_spec(engine: Engine) -> None:
    """Keep spec identity, canonical content, and model selection host-owned."""
    spec = _seed_spec(engine)

    payload = AuthorityCompilationInputService(engine=engine).build(
        project_id=PROJECT_ID,
        decision=_compile_decision(spec),
        compiler_model=COMPILER_MODEL,
    )

    assert payload["spec_version_id"] == spec.spec_version_id
    assert payload["expected_spec_hash"] == spec.spec_hash
    assert payload["compiler_model"] == COMPILER_MODEL
    compiler_input = SpecAuthorityCompilerInput.model_validate(
        payload["compiler_input"]
    )
    assert compiler_input.spec_source == spec.content
    assert compiler_input.spec_content_ref is None
    assert compiler_input.domain_hint is None
    assert compiler_input.project_id == PROJECT_ID
    assert compiler_input.spec_version_id == spec.spec_version_id
    assert compiler_input.spec_source_format == "agileforge.spec.v1"


def test_rejects_registered_content_that_no_longer_matches_its_hash(
    engine: Engine,
) -> None:
    """Never send registry content whose durable fingerprint is stale."""
    spec = _seed_spec(engine)
    changed_content, _changed_hash = _canonical_spec()
    changed = json.loads(changed_content)
    changed["summary"] = "Content changed after registration."
    with Session(engine) as session:
        stored = session.get(SpecRegistry, spec.spec_version_id)
        assert stored is not None
        stored.content = json.dumps(changed)
        session.add(stored)
        session.commit()

    with pytest.raises(
        AuthorityCompilationInputError,
        match="content does not match its stored hash",
    ):
        AuthorityCompilationInputService(engine=engine).build(
            project_id=PROJECT_ID,
            decision=_compile_decision(spec),
            compiler_model=COMPILER_MODEL,
        )


def test_rejects_a_graph_decision_for_a_superseded_registry_row(
    engine: Engine,
) -> None:
    """Authority input reads only the graph-selected approved registry version."""
    spec = _seed_spec(engine)
    with Session(engine) as session:
        stored = session.get(SpecRegistry, spec.spec_version_id)
        assert stored is not None
        stored.status = "superseded"
        session.add(stored)
        session.commit()

    with pytest.raises(
        AuthorityCompilationInputError,
        match="does not match an approved spec",
    ):
        AuthorityCompilationInputService(engine=engine).build(
            project_id=PROJECT_ID,
            decision=_compile_decision(spec),
            compiler_model=COMPILER_MODEL,
        )


def test_builds_from_graph_registered_spec_with_escaped_unicode(
    engine: Engine,
) -> None:
    """Keep the graph registry hash while normalizing model-facing content."""
    normalized_content, _normalized_hash = _canonical_spec()
    parsed = json.loads(normalized_content)
    parsed["summary"] = (
        "Compilar especifica\u00e7\u00e3o registrada: Jo\u00e3o e caf\u00e9."
    )
    parsed["problem_statement"] = (
        "A compila\u00e7\u00e3o deve preservar o hash registrado."
    )
    stored_content = canonical_json(parsed)
    stored_hash = canonical_hash(parsed)
    compiler_normalized = normalize_spec_content_for_registry(stored_content)
    assert compiler_normalized.spec_hash != stored_hash

    with Session(engine) as session:
        session.add(
            Project(
                project_id=PROJECT_ID,
                name="Authority compiler input",
                origin="brownfield",
            )
        )
        candidate = _seed_candidate(
            session,
            content=stored_content,
            content_fingerprint=stored_hash,
        )
        spec = SpecRegistry(
            project_id=PROJECT_ID,
            spec_hash=stored_hash,
            content=stored_content,
            status="approved",
            source_specification_candidate_id=(candidate.specification_candidate_id),
            source_vision_artifact_id=candidate.vision_artifact_id,
            source_vision_fingerprint=candidate.vision_fingerprint,
            source_product_goal_artifact_id=candidate.product_goal_artifact_id,
            source_product_goal_fingerprint=candidate.product_goal_fingerprint,
            source_discovery_artifact_id=candidate.discovery_artifact_id,
            source_discovery_fingerprint=candidate.discovery_fingerprint,
        )
        session.add(spec)
        session.commit()
        session.refresh(spec)

    payload = AuthorityCompilationInputService(engine=engine).build(
        project_id=PROJECT_ID,
        decision=_compile_decision(spec),
        compiler_model=COMPILER_MODEL,
    )

    assert payload["expected_spec_hash"] == stored_hash
    compiler_input = SpecAuthorityCompilerInput.model_validate(
        payload["compiler_input"]
    )
    assert compiler_input.spec_source == compiler_normalized.content
