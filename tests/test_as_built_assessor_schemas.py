"""Tests for As-Built Assessment agent schemas."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from orchestrator_agent.agent_tools.as_built_assessor.schemes import (
    AGENT_VERSION,
    ASSESSMENT_SCHEMA_VERSION,
    EVIDENCE_PACK_BUILDER_VERSION,
    AsBuiltAssessment,
    AsBuiltAssessmentCacheMeta,
    AsBuiltAssessorInput,
    AssessmentStatus,
    AuthorityTarget,
    CapabilityAssessment,
    EvidencePack,
    OpenSpecContext,
    OriginalSpecContext,
    RepoSnapshot,
)


def _repo_snapshot() -> RepoSnapshot:
    return RepoSnapshot(path="/repo", git_commit="abc123", dirty=False)


def _evidence_pack(authority_targets: list[AuthorityTarget]) -> EvidencePack:
    return EvidencePack(
        schema_version="agileforge.as_built_evidence_pack.v1",
        builder_version=EVIDENCE_PACK_BUILDER_VERSION,
        authority_fingerprint="sha256:authority",
        evidence_pack_fingerprint="sha256:pack",
        generated_at="2026-05-28T12:00:00Z",
        repo_snapshot=_repo_snapshot(),
        warnings=[],
        file_manifest_summary={"total_files": 3},
        authority_targets=authority_targets,
        source_snippets=[],
        test_snippets=[],
        doc_snippets=[],
        cli_observations=[],
        search_observations=[],
        limitations=[],
    )


def test_output_schema_accepts_all_statuses() -> None:
    """Accept each status while allowing low-confidence complete assessments."""
    statuses: list[AssessmentStatus] = [
        "observed",
        "observed_with_missing_evidence",
        "contradicted",
        "not_observed",
        "unclear",
    ]

    assessment = AsBuiltAssessment(
        schema_version=ASSESSMENT_SCHEMA_VERSION,
        project_id=2,
        assessment_id="as-built-2-abc",
        agent_version=AGENT_VERSION,
        evidence_pack_builder_version=EVIDENCE_PACK_BUILDER_VERSION,
        authority_fingerprint="sha256:authority",
        evidence_pack_fingerprint="sha256:pack",
        generated_at="2026-05-28T12:05:00Z",
        assessment_summary="Assessment completed.",
        repo_snapshot=_repo_snapshot(),
        capability_assessments=[
            CapabilityAssessment(
                authority_ref=f"REQ.status-{index}",
                invariant_refs=[f"INV-{index:04d}"],
                capability_title=f"Capability {index}",
                status=status,
                confidence="low",
                evidence=[],
                limitations=["Tests were not executed."],
                recommended_backlog_treatment="po_review_required",
                reasoning=(
                    "Bounded evidence supports only a conservative assessment."
                ),
            )
            for index, status in enumerate(statuses)
        ],
        cross_cutting_findings=[],
        open_questions=[],
        is_complete=True,
        clarifying_questions=[],
    )

    assert assessment.is_complete is True
    assert [item.status for item in assessment.capability_assessments] == statuses


def test_output_schema_rejects_extra_fields() -> None:
    """Reject unexpected output fields so agent drift is visible."""
    payload = {
        "schema_version": ASSESSMENT_SCHEMA_VERSION,
        "project_id": 2,
        "assessment_id": "as-built-2-abc",
        "agent_version": AGENT_VERSION,
        "evidence_pack_builder_version": EVIDENCE_PACK_BUILDER_VERSION,
        "authority_fingerprint": "sha256:authority",
        "evidence_pack_fingerprint": "sha256:pack",
        "generated_at": "2026-05-28T12:05:00Z",
        "assessment_summary": "Assessment completed.",
        "repo_snapshot": _repo_snapshot().model_dump(mode="json"),
        "capability_assessments": [],
        "cross_cutting_findings": [],
        "open_questions": [],
        "is_complete": True,
        "clarifying_questions": [],
        "unexpected": "blocked",
    }

    with pytest.raises(ValidationError):
        AsBuiltAssessment.model_validate(payload)


def test_input_schema_accepts_unknown_spec_mode_and_no_history() -> None:
    """Allow unknown spec mode while preserving authority targets."""
    target = AuthorityTarget(
        authority_ref="REQ.live-squad-recommendation",
        invariant_refs=["INV-a4b296c058e88663"],
        title="Live squad recommendation",
        invariant_type="STATE_TRANSITION",
        source_requirement_id="REQ.live-squad-recommendation",
        terms=["live squad recommendation", "market is open"],
        parameters={"state": "live recommendation run"},
    )

    parsed = AsBuiltAssessorInput(
        project_id=2,
        assessment_id="as-built-2-abc",
        compiled_authority='{"invariants":[]}',
        original_spec=OriginalSpecContext(
            spec_mode="unknown",
            json="{}",
            markdown="",
        ),
        repo_evidence_pack=_evidence_pack([target]),
        openspec_context=OpenSpecContext(
            present=False,
            spec_summaries=[],
            change_summaries=[],
        ),
        prior_as_built_assessment="NO_HISTORY",
        user_input="",
    )

    assert parsed.original_spec.spec_mode == "unknown"
    assert parsed.repo_evidence_pack.authority_targets[0].authority_ref == (
        "REQ.live-squad-recommendation"
    )


def test_input_schema_rejects_extra_fields() -> None:
    """Unexpected input fields should fail before agent invocation."""
    with pytest.raises(ValidationError):
        AsBuiltAssessorInput.model_validate(
            {
                "project_id": 2,
                "assessment_id": "as-built-2-abc",
                "compiled_authority": '{"invariants":[]}',
                "original_spec": OriginalSpecContext(
                    spec_mode="unknown",
                    json="{}",
                    markdown="",
                ).model_dump(mode="json", by_alias=True),
                "repo_evidence_pack": _evidence_pack([]).model_dump(mode="json"),
                "openspec_context": OpenSpecContext().model_dump(mode="json"),
                "unexpected": "blocked",
            }
        )


def test_empty_authority_targets_requires_explicit_limitation() -> None:
    """Make empty target packs inspectable rather than silently complete."""
    pack = _evidence_pack([])
    assert pack.has_no_targets_limitation() is False

    pack_with_limitation = pack.model_copy(
        update={"limitations": ["No authority targets were extracted."]}
    )
    assert pack_with_limitation.has_no_targets_limitation() is True


def test_cache_meta_contains_required_freshness_fields() -> None:
    """Cache metadata must include all fields used by freshness checks."""
    meta = AsBuiltAssessmentCacheMeta(
        schema_version=ASSESSMENT_SCHEMA_VERSION,
        agent_version=AGENT_VERSION,
        evidence_pack_builder_version=EVIDENCE_PACK_BUILDER_VERSION,
        authority_fingerprint="sha256:authority",
        repo_git_commit="abc123",
        repo_dirty=False,
        evidence_pack_fingerprint="sha256:pack",
        assessment_fingerprint="sha256:assessment",
        generated_at="2026-05-28T12:05:00Z",
    )

    assert meta.agent_version == AGENT_VERSION
    assert meta.evidence_pack_builder_version == EVIDENCE_PACK_BUILDER_VERSION
