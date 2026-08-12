"""Strict Authority compiler input from one approved v2 Specification."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

import pytest
from sqlmodel import Session

from models.product_definition import SpecificationCandidate
from models.specs import SpecRegistry
from services.authority_compilation_input import (
    AuthorityCompilationInputError,
    AuthorityCompilationInputService,
)
from services.specs.candidate_contract import (
    CandidateBuildInput,
    CandidateKind,
    CandidateSourceKind,
    CandidateSourceManifestEntry,
    build_candidate_envelope,
    canonical_candidate_json,
)
from utils.agileforge_spec_profile_v2 import SpecificationPayload
from utils.spec_schemas import SpecAuthorityCompilerInput
from workflow.contracts import (
    FactReference,
    NodeCategory,
    NodeDecision,
    RecommendationKind,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

PROJECT_ID = 41
CANDIDATE_ID = 71
SPEC_VERSION_ID = 91
VISION_ID = 17
GOAL_ID = 23
COMPILER_MODEL = "offline/authority-compiler"


def _fingerprint(label: str) -> str:
    return f"sha256:{hashlib.sha256(label.encode()).hexdigest()}"


def _payload() -> SpecificationPayload:
    return SpecificationPayload.model_validate(
        {
            "schema_version": "agileforge.spec.v2",
            "artifact_id": "SPEC.authority-input",
            "title": "Authority input",
            "summary": "Compile exact accepted semantics.",
            "problem_statement": "Provenance prose cannot become an invariant.",
            "items": [
                {
                    "id": "REQ.authority.exact-input",
                    "type": "REQ",
                    "title": "Exact compiler input",
                    "statement": "Authority MUST compile the accepted payload.",
                    "level": "MUST",
                    "verification": "system-test",
                    "acceptance": ["The exact accepted payload is compiled."],
                    "source_notes": [
                        {
                            "source_id": "SRC.operator",
                            "kind": "external_summary",
                            "text": "SECRET PROVENANCE PROSE",
                            "external_ref_id": "EXT.operator-notes",
                        }
                    ],
                },
                {
                    "id": "REQ.authority.background",
                    "type": "REQ",
                    "title": "Background only",
                    "statement": "This item is context, not an invariant source.",
                    "level": "INFORMATIVE",
                    "verification": "inspection",
                    "acceptance": ["Reviewers can read the context."],
                },
            ],
            "relations": [],
            "controlled_terms": [],
            "external_references": [
                {
                    "id": "EXT.operator-notes",
                    "title": "Operator notes",
                    "url": "https://example.invalid/operator-notes",
                    "summary": "SECRET EXTERNAL REFERENCE PROSE",
                }
            ],
        }
    )


def _seed_approved_spec(engine: Engine) -> SpecRegistry:
    payload = _payload()
    vision_fingerprint = _fingerprint("vision")
    goal_fingerprint = _fingerprint("goal")
    envelope = build_candidate_envelope(
        payload=payload,
        metadata=CandidateBuildInput(
            candidate_kind=CandidateKind.INITIAL,
            accepted_vision_id=VISION_ID,
            accepted_vision_fingerprint=vision_fingerprint,
            accepted_product_goal_id=GOAL_ID,
            accepted_product_goal_fingerprint=goal_fingerprint,
            source_manifest=(
                CandidateSourceManifestEntry(
                    source_id="SRC.operator",
                    kind=CandidateSourceKind.PRODUCT_GOAL,
                    fingerprint=goal_fingerprint,
                ),
            ),
            accepted_fact_fingerprint=_fingerprint("facts"),
            producer_input_fingerprint=_fingerprint("producer-input"),
            producer_capability="to-spec",
            producer_version="2.0.0",
            model_id="offline/to-spec",
            model_configuration_fingerprint=_fingerprint("model-config"),
            prompt_fingerprint=_fingerprint("prompt"),
            workflow_node_attempt_id=61,
            attempt_fingerprint=_fingerprint("attempt"),
            correlation_id="correlation-41",
            produced_at=datetime(2026, 8, 11, 12, tzinfo=UTC),
        ),
    )
    candidate = SpecificationCandidate(
        specification_candidate_id=CANDIDATE_ID,
        project_id=PROJECT_ID,
        candidate_kind="initial",
        vision_artifact_id=VISION_ID,
        vision_fingerprint=vision_fingerprint,
        product_goal_artifact_id=GOAL_ID,
        product_goal_fingerprint=goal_fingerprint,
        canonical_envelope_json=canonical_candidate_json(payload, envelope),
        payload_fingerprint=envelope.payload_fingerprint,
        source_manifest_fingerprint=envelope.source_manifest_fingerprint,
        producer_input_fingerprint=envelope.producer_input_fingerprint,
        rendered_view_fingerprint=envelope.review_view_fingerprint,
        candidate_fingerprint=envelope.candidate_fingerprint,
        workflow_node_attempt_id=envelope.workflow_node_attempt_id,
        attempt_fingerprint=envelope.attempt_fingerprint,
        recorded_by="operator",
    )
    spec = SpecRegistry(
        spec_version_id=SPEC_VERSION_ID,
        project_id=PROJECT_ID,
        spec_hash=envelope.payload_fingerprint,
        status="approved",
        approved_by="operator",
        source_specification_candidate_id=CANDIDATE_ID,
        source_specification_candidate_fingerprint=envelope.candidate_fingerprint,
        source_vision_artifact_id=VISION_ID,
        source_vision_fingerprint=vision_fingerprint,
        source_product_goal_artifact_id=GOAL_ID,
        source_product_goal_fingerprint=goal_fingerprint,
    )
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.commit()
    with Session(engine) as session:
        session.add(candidate)
        session.add(spec)
        session.commit()
        session.refresh(spec)
    return spec


def _decision(spec: SpecRegistry, *, fingerprint: str | None = None) -> NodeDecision:
    assert spec.spec_version_id is not None
    selected_fingerprint = fingerprint or spec.spec_hash
    return NodeDecision(
        node_id="authority.compile",
        instance_key=f"spec:{spec.spec_version_id}:{selected_fingerprint}",
        child_graph_id="authority",
        request_kind="compile_authority",
        category=NodeCategory.AVAILABLE,
        recommendation_kind=RecommendationKind.REQUIRED,
        reason_code="AUTHORITY_COMPILE_REQUIRED",
        fact_references=(
            FactReference(
                fact_type="spec_version",
                fact_id=str(spec.spec_version_id),
                fingerprint=selected_fingerprint,
            ),
        ),
        decision_fingerprint=_fingerprint("compile-decision"),
    )


def test_builds_exact_typed_v2_input_without_provenance(engine: Engine) -> None:
    """Only exact eligible semantics cross the compiler boundary."""
    spec = _seed_approved_spec(engine)

    result = AuthorityCompilationInputService(engine=engine).build(
        project_id=PROJECT_ID,
        decision=_decision(spec),
        compiler_model=COMPILER_MODEL,
    )

    assert result["project_id"] == PROJECT_ID
    assert result["spec_version_id"] == SPEC_VERSION_ID
    assert result["expected_spec_hash"] == spec.spec_hash
    assert result["compiler_model"] == COMPILER_MODEL
    assert "content" not in result
    assert "content_ref" not in result
    compiler_input = SpecAuthorityCompilerInput.model_validate(result["compiler_input"])
    assert compiler_input.project_id == PROJECT_ID
    assert compiler_input.spec_version_id == SPEC_VERSION_ID
    assert compiler_input.specification_fingerprint == spec.spec_hash
    assert compiler_input.authority_input.eligible_item_ids == (
        "REQ.authority.exact-input",
    )
    serialized = json.dumps(result["compiler_input"])
    assert "SECRET PROVENANCE PROSE" not in serialized
    assert "SECRET EXTERNAL REFERENCE PROSE" not in serialized
    assert "source_notes" not in serialized
    assert "external_references" not in serialized
    assert "This item is context, not an invariant source." not in serialized
    assert "REQ.authority.background" not in serialized


@pytest.mark.parametrize("failure", ["superseded", "decision-mismatch"])
def test_rejects_superseded_or_mismatched_graph_selection(
    engine: Engine,
    failure: Literal["superseded", "decision-mismatch"],
) -> None:
    """Graph selection must identify the current approved payload exactly."""
    spec = _seed_approved_spec(engine)
    decision = _decision(spec)
    if failure == "superseded":
        with Session(engine) as session:
            stored = session.get(SpecRegistry, SPEC_VERSION_ID)
            assert stored is not None
            stored.status = "superseded"
            session.add(stored)
            session.commit()
    else:
        decision = _decision(spec, fingerprint=_fingerprint("wrong-payload"))

    with pytest.raises(
        AuthorityCompilationInputError,
        match="approved spec",
    ):
        AuthorityCompilationInputService(engine=engine).build(
            project_id=PROJECT_ID,
            decision=decision,
            compiler_model=COMPILER_MODEL,
        )


@pytest.mark.parametrize("corruption", ["tampered", "noncanonical"])
def test_rejects_tampered_or_noncanonical_candidate_envelope(
    engine: Engine,
    corruption: Literal["tampered", "noncanonical"],
) -> None:
    """Persisted candidate bytes must reload as the exact canonical contract."""
    spec = _seed_approved_spec(engine)
    with Session(engine) as session:
        candidate = session.get(SpecificationCandidate, CANDIDATE_ID)
        assert candidate is not None
        if corruption == "noncanonical":
            candidate.canonical_envelope_json += "\n"
        else:
            raw = json.loads(candidate.canonical_envelope_json)
            raw["payload"]["items"][0]["statement"] = "Tampered statement."
            candidate.canonical_envelope_json = json.dumps(
                raw,
                sort_keys=True,
                separators=(",", ":"),
            )
        session.add(candidate)
        session.commit()

    with pytest.raises(
        AuthorityCompilationInputError,
        match="candidate envelope",
    ):
        AuthorityCompilationInputService(engine=engine).build(
            project_id=PROJECT_ID,
            decision=_decision(spec),
            compiler_model=COMPILER_MODEL,
        )


@pytest.mark.parametrize("source", ["vision", "goal", "specification"])
def test_rejects_direct_source_fingerprint_mismatch(
    engine: Engine,
    source: Literal["vision", "goal", "specification"],
) -> None:
    """Registry, candidate, and envelope must agree on every direct source hash."""
    spec = _seed_approved_spec(engine)
    with Session(engine) as session:
        candidate = session.get(SpecificationCandidate, CANDIDATE_ID)
        registry = session.get(SpecRegistry, SPEC_VERSION_ID)
        assert candidate is not None
        assert registry is not None
        if source == "vision":
            candidate.vision_fingerprint = _fingerprint("wrong-vision")
            session.add(candidate)
        elif source == "goal":
            registry.source_product_goal_fingerprint = _fingerprint("wrong-goal")
            session.add(registry)
        else:
            candidate.payload_fingerprint = _fingerprint("wrong-specification")
            session.add(candidate)
        session.commit()

    with pytest.raises(AuthorityCompilationInputError, match="source candidate"):
        AuthorityCompilationInputService(engine=engine).build(
            project_id=PROJECT_ID,
            decision=_decision(spec),
            compiler_model=COMPILER_MODEL,
        )
