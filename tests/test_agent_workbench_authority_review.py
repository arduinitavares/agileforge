"""Tests for read-only authority review packets."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from models.core import Product
from models.specs import CompiledSpecAuthority, SpecRegistry
from services.agent_workbench.authority_projection import pending_authority_fingerprint
from services.agent_workbench.authority_review import (
    REVIEW_TOKEN_SCHEMA,
    AuthorityReviewService,
    build_authority_review_snapshot,
    canonical_json_hash,
    coverage_summary_fingerprint,
    sha256_prefixed,
)
from tests.typing_helpers import require_id
from utils.agileforge_spec_profile import (
    TechnicalSpecArtifact,
    canonical_spec_hash,
    canonical_spec_json,
)
from utils.spec_schemas import (
    AuthorityQualityReport,
    Invariant,
    InvariantType,
    RequiredFieldParams,
    SourceMapEntry,
    SpecAuthorityCompilationSuccess,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest
    from sqlalchemy.engine import Engine
    from sqlmodel import Session

PROMPT_HASH = "a" * 64
COMPILER_VERSION = "1.0.0"
INVARIANT_ID = "INV-0123456789abcdef"


def _engine(session: Session) -> Engine:
    """Return the test session bind as an engine for review services."""
    return cast("Engine", session.get_bind())


def _compiled_success_json(
    *,
    source_excerpt: str,
    source_location: str | None = "REQ.guard-tokens.statement",
) -> str:
    success = SpecAuthorityCompilationSuccess(
        scope_themes=["Authority review"],
        domain="agent workbench",
        invariants=[
            Invariant(
                id=INVARIANT_ID,
                type=InvariantType.REQUIRED_FIELD,
                parameters=RequiredFieldParams(field_name="guard_tokens"),
            )
        ],
        eligible_feature_rules=[],
        rejected_features=[],
        gaps=[],
        assumptions=[],
        source_map=[
            SourceMapEntry(
                invariant_id=INVARIANT_ID,
                excerpt=source_excerpt,
                location=source_location,
            )
        ],
        compiler_version=COMPILER_VERSION,
        prompt_hash=PROMPT_HASH,
        ir_schema_version=None,
        ir_provenance=None,
    )
    return _stored_compiled_success_json_from_success(success)


def _stored_compiled_success_json_from_success(
    success: SpecAuthorityCompilationSuccess,
) -> str:
    from services.specs.compiler_service import (  # noqa: PLC0415
        _compiled_authority_artifact_json,
    )

    return _compiled_authority_artifact_json(success)


def _stored_compiled_success_json(
    *,
    source_excerpt: str,
    source_location: str | None = "REQ.guard-tokens.statement",
) -> str:
    return _compiled_success_json(
        source_excerpt=source_excerpt,
        source_location=source_location,
    )


def _legacy_compiled_success_json(*, source_excerpt: str) -> str:
    payload = json.loads(_compiled_success_json(source_excerpt=source_excerpt))
    payload.pop("schema_version", None)
    return json.dumps(payload)


def _seed_pending_review_project(  # noqa: PLR0913
    session: Session,
    *,
    tmp_path: Path,
    spec_content: str,
    artifact_json: str | None = None,
    spec_bytes: bytes | None = None,
    spec_filename: str = "spec.json",
) -> tuple[int, int, int, Path]:
    spec_path = tmp_path / spec_filename
    raw_bytes = spec_bytes if spec_bytes is not None else spec_content.encode("utf-8")
    spec_path.write_bytes(raw_bytes)
    spec_hash = sha256_prefixed(raw_bytes)

    product = Product(
        name=f"Authority Review {spec_filename}",
        description="Seeded authority review project",
        spec_file_path=str(spec_path),
    )
    session.add(product)
    session.commit()
    session.refresh(product)
    project_id = require_id(product.product_id, "product_id")

    spec = SpecRegistry(
        product_id=project_id,
        spec_hash=spec_hash,
        content=spec_content,
        content_ref=str(spec_path),
        status="approved",
        approved_at=datetime(2026, 5, 17, 12, tzinfo=UTC),
        approved_by="review-test",
        approval_notes="Approved for review test.",
    )
    session.add(spec)
    session.commit()
    session.refresh(spec)
    spec_version_id = require_id(spec.spec_version_id, "spec_version_id")

    authority = CompiledSpecAuthority(
        spec_version_id=spec_version_id,
        compiler_version=COMPILER_VERSION,
        prompt_hash=PROMPT_HASH,
        compiled_at=datetime(2026, 5, 17, 13, tzinfo=UTC),
        compiled_artifact_json=artifact_json
        or _compiled_success_json(
            source_excerpt="The review output must include guard tokens."
        ),
        scope_themes=json.dumps(["Authority review"]),
        invariants=json.dumps(
            [{"id": INVARIANT_ID, "text": "guard_tokens are required"}]
        ),
        eligible_feature_ids=json.dumps([]),
        rejected_features=json.dumps([]),
        spec_gaps=json.dumps([]),
    )
    session.add(authority)
    session.commit()
    session.refresh(authority)
    authority_id = require_id(authority.authority_id, "authority_id")
    return project_id, spec_version_id, authority_id, spec_path


def _base_spec() -> str:
    return _agileforge_spec_profile_payload()


def _agileforge_spec_profile_payload(
    *,
    artifact_id: str = "SPEC.authority-review",
    title: str = "Authority Review Spec",
    summary: str = "Review packets expose deterministic authority evidence.",
    requirement_statement: str = "The review output must include guard tokens.",
    acceptance: list[str] | None = None,
) -> str:
    """Return minimal canonical AgileForge spec JSON for review tests."""
    requirement_acceptance = acceptance or [
        "The authority review packet includes guard token evidence."
    ]
    artifact = TechnicalSpecArtifact.model_validate(
        {
            "schema_version": "agileforge.spec.v1",
            "artifact_id": artifact_id,
            "title": title,
            "status": "draft",
            "version": "0.1.0",
            "created_at": "2026-05-17T12:00:00Z",
            "updated_at": "2026-05-17T12:00:00Z",
            "summary": summary,
            "problem_statement": (
                "Reviewers need structured metadata for canonical spec artifacts."
            ),
            "items": [
                {
                    "id": "GOAL.review-evidence",
                    "type": "GOAL",
                    "status": "draft",
                    "title": "Expose review evidence",
                    "statement": "Reviewers can identify the exact spec artifact.",
                },
                {
                    "id": "REQ.guard-tokens",
                    "type": "REQ",
                    "status": "draft",
                    "title": "Guard token packet evidence",
                    "statement": requirement_statement,
                    "level": "MUST",
                    "verification": "inspection",
                    "acceptance": requirement_acceptance,
                },
            ],
            "relations": [
                {
                    "from": "REQ.guard-tokens",
                    "type": "satisfies",
                    "to": "GOAL.review-evidence",
                    "rationale": "Guard tokens identify reviewed authority input.",
                }
            ],
        }
    )
    return canonical_spec_json(artifact)


def test_review_returns_pending_authority_packet_with_guard_tokens(
    session: Session,
    tmp_path: Path,
) -> None:
    """Review returns a pending packet with decision guard tokens."""
    project_id, spec_version_id, authority_id, spec_path = (
        _seed_pending_review_project(
            session,
            tmp_path=tmp_path,
            spec_content=_base_spec(),
        )
    )

    result = AuthorityReviewService(engine=_engine(session)).review(
        project_id=project_id
    )

    assert result["ok"] is True
    data = result["data"]
    guard_tokens = data["guard_tokens"]
    assert data["project"]["fsm_state"] == "SETUP_REQUIRED"
    assert data["project"]["setup_status"] == "authority_pending_review"
    assert data["spec"]["spec_version_id"] == spec_version_id
    assert data["spec"]["resolved_path"] == str(spec_path.resolve())
    assert data["pending_authority"]["authority_id"] == authority_id
    assert data["pending_authority"]["authority_fingerprint"] == (
        pending_authority_fingerprint(
            session.get(CompiledSpecAuthority, authority_id)
        )
    )
    assert guard_tokens == {
        "review_token": guard_tokens["review_token"],
        "pending_authority_id": authority_id,
        "expected_authority_fingerprint": data["pending_authority"][
            "authority_fingerprint"
        ],
        "expected_source_spec_hash": data["spec"]["spec_hash"],
        "expected_disk_spec_hash": data["spec"]["disk_sha256"],
        "expected_resolved_spec_path": str(spec_path.resolve()),
        "expected_state": "SETUP_REQUIRED",
        "expected_setup_status": "authority_pending_review",
        "expected_content_included": True,
        "expected_omission_assessment": "complete",
        "expected_coverage_summary_fingerprint": data["spec"][
            "coverage_summary_fingerprint"
        ],
    }
    assert guard_tokens["review_token"].startswith(
        "agileforge.authority_review.v1:sha256:"
    )
    assert data["next_actions"] == [
        {
            "command": f"agileforge authority accept --project-id {project_id}",
            "mode": "human",
            "installed": True,
            "requires_cli_installation": False,
            "requires": [],
            "reason": "Record the reviewed pending authority as canonical.",
        },
        {
            "command": (
                "agileforge authority reject "
                f"--project-id {project_id} "
                f"--review-token {guard_tokens['review_token']} "
                '--reason "..." --idempotency-key <idempotency_key>'
            ),
            "mode": "human",
            "installed": True,
            "requires_cli_installation": False,
            "requires": ["review_token", "reason", "idempotency_key"],
            "reason": "Record that the pending authority must not be used.",
        },
    ]
    guidance = data["review_guidance"]
    assert guidance["acceptance_statement"].startswith("Accept only if")
    assert "Reject if invariants are invented" in guidance["acceptance_statement"]
    assert "Yes, this compiled interpretation" not in guidance["acceptance_statement"]


def test_review_missing_project_returns_project_not_found(
    session: Session,
) -> None:
    """Missing projects return the stable project lookup error."""
    result = AuthorityReviewService(engine=_engine(session)).review(
        project_id=999_999
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "PROJECT_NOT_FOUND"


def test_review_project_without_pending_authority_returns_not_pending(
    session: Session,
    tmp_path: Path,
) -> None:
    """Projects with an approved spec but no compiled authority are not pending."""
    spec_content = _base_spec()
    spec_path = tmp_path / "approved-spec.json"
    spec_path.write_text(spec_content, encoding="utf-8")
    product = Product(
        name="Authority Review Without Pending Authority",
        description="Seeded project without compiled authority",
        spec_file_path=str(spec_path),
    )
    session.add(product)
    session.commit()
    session.refresh(product)
    project_id = require_id(product.product_id, "product_id")
    session.add(
        SpecRegistry(
            product_id=project_id,
            spec_hash=sha256_prefixed(spec_content.encode("utf-8")),
            content=spec_content,
            content_ref=str(spec_path),
            status="approved",
            approved_at=datetime(2026, 5, 17, 12, tzinfo=UTC),
            approved_by="review-test",
            approval_notes="Approved but not compiled.",
        )
    )
    session.commit()

    result = AuthorityReviewService(engine=_engine(session)).review(
        project_id=project_id
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "AUTHORITY_NOT_PENDING"


def test_review_text_format_returns_ok_packet_with_human_text(
    session: Session,
    tmp_path: Path,
) -> None:
    """Text format preserves JSON envelope data and adds human-readable text."""
    project_id, _spec_version_id, authority_id, spec_path = (
        _seed_pending_review_project(
            session,
            tmp_path=tmp_path,
            spec_content=_base_spec(),
        )
    )

    result = AuthorityReviewService(engine=_engine(session)).review(
        project_id=project_id,
        output_format="text",
    )

    assert result["ok"] is True
    assert result["errors"] == []
    data = result["data"]
    assert data["project"]["project_id"] == project_id
    assert data["pending_authority"]["authority_id"] == authority_id
    assert data["spec"]["resolved_path"] == str(spec_path.resolve())
    assert isinstance(data["text"], str)
    assert "Authority review" in data["text"]
    assert f"Project: {project_id}" in data["text"]
    assert f"Pending authority: {authority_id}" in data["text"]
    assert "Review token:" in data["text"]
    assert "ACCEPT:" in data["text"]
    assert "REJECT:" in data["text"]
    assert "--idempotency-key <idempotency_key>" in data["text"]


def test_review_text_format_summarizes_human_acceptance_decision(
    session: Session,
    tmp_path: Path,
) -> None:
    """Text format answers the human accept/reject review questions."""
    spec_content = _agileforge_spec_profile_payload(
        requirement_statement=(
            "The product has exactly two user-visible consent decisions."
        ),
        acceptance=[
            "The authority review preserves the two-consent model.",
        ],
    )
    authority_quality = AuthorityQualityReport.model_validate(
        {
            "schema_version": "agileforge.authority_quality.v1",
            "summary": {
                "original_invariant_count": 4,
                "final_invariant_count": 3,
                "merged_invariant_count": 1,
                "merged_assumption_count": 0,
                "review_group_count": 1,
                "near_duplicate_group_count": 0,
                "over_split_group_count": 1,
                "noisy_assumption_group_count": 0,
            },
            "merged_items": [],
            "review_groups": [
                {
                    "group_id": "AQ-GROUP-001",
                    "group_type": "over_split_invariants",
                    "severity": "warning",
                    "member_ids": [INVARIANT_ID],
                    "reason": "duplicate/over-split consent invariant noise",
                    "merge_allowed": False,
                    "truncated": False,
                }
            ],
        }
    )
    success = SpecAuthorityCompilationSuccess(
        scope_themes=["Authority review"],
        domain="agent workbench",
        invariants=[
            Invariant(
                id=INVARIANT_ID,
                type=InvariantType.REQUIRED_FIELD,
                parameters=RequiredFieldParams(field_name="consent_decisions"),
            )
        ],
        eligible_feature_rules=[],
        rejected_features=[],
        gaps=[],
        assumptions=[
            "operator confirms export boundary before review-state export",
        ],
        source_map=[
            SourceMapEntry(
                invariant_id=INVARIANT_ID,
                excerpt="The product has exactly two user-visible consent decisions.",
                location="REQ.guard-tokens.statement",
            )
        ],
        compiler_version=COMPILER_VERSION,
        prompt_hash=PROMPT_HASH,
        ir_schema_version=None,
        ir_provenance=None,
        authority_quality=authority_quality,
    )
    project_id, _spec_version_id, authority_id, _spec_path = (
        _seed_pending_review_project(
            session,
            tmp_path=tmp_path,
            spec_content=spec_content,
            artifact_json=_stored_compiled_success_json_from_success(success),
        )
    )
    authority = session.get(CompiledSpecAuthority, authority_id)
    assert authority is not None
    authority.rejected_features = json.dumps(
        [
            {
                "id": "NON_GOAL-training",
                "text": (
                    "NON_GOAL: future training automation is excluded from "
                    "current authority."
                ),
                "support": "direct",
                "source_refs": ["NON_GOAL.training"],
                "source_excerpt": "Future training automation is not current behavior.",
            }
        ]
    )
    session.add(authority)
    session.commit()

    result = AuthorityReviewService(engine=_engine(session)).review(
        project_id=project_id,
        output_format="text",
    )

    assert result["ok"] is True
    text = result["data"]["text"]
    assert "Recommendation: accept" in text
    assert "Preserved requirements:" in text
    assert "two user-visible consent decisions" in text
    assert "Gaps:" in text
    assert "No blocking gaps found." in text
    assert "Assumptions:" in text
    assert "operator confirms export boundary" in text
    assert "Excluded/non-current scope:" in text
    assert "NON_GOAL" in text
    assert "future training automation" in text
    assert "Warnings:" in text
    assert "duplicate/over-split" in text
    assert "ACCEPT:" in text
    assert "REJECT:" in text


def test_review_preserves_rejected_features_from_valid_compiled_authority(
    session: Session,
    tmp_path: Path,
) -> None:
    """Valid review packets preserve normalized rejected feature entries."""
    project_id, _spec_version_id, authority_id, _spec_path = (
        _seed_pending_review_project(
            session,
            tmp_path=tmp_path,
            spec_content=_base_spec(),
        )
    )
    authority = session.get(CompiledSpecAuthority, authority_id)
    assert authority is not None
    authority.rejected_features = json.dumps(
        [
            {
                "id": "REJ-1",
                "text": "Do not add unauthenticated dashboard publishing.",
            }
        ]
    )
    session.add(authority)
    session.commit()

    result = AuthorityReviewService(engine=_engine(session)).review(
        project_id=project_id
    )

    assert result["data"]["pending_authority"]["artifact"]["rejected_features"] == [
        {
            "id": "REJ-1",
            "text": "Do not add unauthenticated dashboard publishing.",
            "support": "inferred",
            "source_refs": [],
            "source_excerpt": None,
        }
    ]


def test_review_accepts_v2_stored_compiled_artifact(
    session: Session,
    tmp_path: Path,
) -> None:
    """Authority review should accept the compiler-service stored v2 envelope."""
    project_id, _spec_version_id, _authority_id, _spec_path = (
        _seed_pending_review_project(
            session,
            tmp_path=tmp_path,
            spec_content=_base_spec(),
            artifact_json=_stored_compiled_success_json(
                source_excerpt="The review output must include guard tokens."
            ),
        )
    )

    result = AuthorityReviewService(engine=_engine(session)).review(
        project_id=project_id
    )

    assert result["ok"] is True
    assert result["data"]["review_summary"]["acceptance_status"] == "accept_ready"
    assert "COMPILED_AUTHORITY_INVALID" not in {
        finding["code"] for finding in result["data"]["review_findings"]
    }
    assert result["data"]["pending_authority"]["artifact"]["scope_themes"] == [
        "Authority review"
    ]


def test_review_rejects_unsupported_compiled_authority_schema(
    session: Session,
    tmp_path: Path,
) -> None:
    """Unsupported stored artifacts should fail closed with regenerate guidance."""
    project_id, _spec_version_id, _authority_id, _spec_path = (
        _seed_pending_review_project(
            session,
            tmp_path=tmp_path,
            spec_content=_base_spec(),
            artifact_json=_legacy_compiled_success_json(
                source_excerpt="The review output must include guard tokens."
            ),
        )
    )

    result = AuthorityReviewService(engine=_engine(session)).review(
        project_id=project_id
    )

    assert result["ok"] is False
    error = result["errors"][0]
    assert error["code"] == "COMPILED_AUTHORITY_SCHEMA_UNSUPPORTED"
    assert "agileforge authority regenerate" in " ".join(error["remediation"])


def test_review_malformed_compiled_artifact_blocks_acceptance(
    session: Session,
    tmp_path: Path,
) -> None:
    """Malformed compiler artifacts are structural blockers."""
    malformed_artifact = json.loads(
        _compiled_success_json(
            source_excerpt="The review output must include guard tokens."
        )
    )
    malformed_artifact["assumptions"] = [
        {
            "id": "ASM-7",
            "text": "Review assumes CLI output is JSON.",
            "support": "direct",
            "source_refs": ["REQ.guard-tokens.statement"],
            "source_excerpt": "The review output must include guard tokens.",
        }
    ]
    malformed_artifact["invariants"] = "bad"
    project_id, _spec_version_id, authority_id, _spec_path = (
        _seed_pending_review_project(
            session,
            tmp_path=tmp_path,
            spec_content=_base_spec(),
            artifact_json=json.dumps(malformed_artifact),
        )
    )
    authority = session.get(CompiledSpecAuthority, authority_id)
    assert authority is not None
    authority.eligible_feature_ids = json.dumps(
        [
            {
                "id": "ELIG-2",
                "text": "Allow read-only packet rendering.",
                "support": "direct",
                "source_refs": ["REQ.guard-tokens.statement"],
                "source_excerpt": "The review output must include guard tokens.",
            }
        ]
    )
    authority.rejected_features = json.dumps(
        [
            {
                "id": "REJ-9",
                "text": "Reject automatic authority acceptance.",
                "support": "direct",
                "source_refs": ["REQ.guard-tokens.acceptance.0"],
                "source_excerpt": "Humans must approve authority before use.",
            }
        ]
    )
    authority.spec_gaps = json.dumps(
        [
            {
                "id": "GAP-3",
                "text": "Spec does not define text output formatting.",
                "support": "inferred",
                "source_refs": [],
                "source_excerpt": None,
            }
        ]
    )
    session.add(authority)
    session.commit()

    result = AuthorityReviewService(engine=_engine(session)).review(
        project_id=project_id
    )

    assert result["ok"] is True
    summary = result["data"]["review_summary"]
    assert summary["acceptance_status"] == "blocked"
    assert "COMPILED_AUTHORITY_INVALID" in summary["blocking_finding_codes"]
    findings = result["data"]["review_findings"]
    invalid = next(
        finding
        for finding in findings
        if finding["code"] == "COMPILED_AUTHORITY_INVALID"
    )
    assert invalid["severity"] == "blocking"
    assert invalid["override_allowed"] is False
    artifact = result["data"]["pending_authority"]["artifact"]
    assert artifact["eligible_feature_rules"] == [
        {
            "id": "ELIG-2",
            "text": "Allow read-only packet rendering.",
            "support": "direct",
            "source_refs": ["REQ.guard-tokens.statement"],
            "source_excerpt": "The review output must include guard tokens.",
        }
    ]
    assert artifact["rejected_features"] == [
        {
            "id": "REJ-9",
            "text": "Reject automatic authority acceptance.",
            "support": "direct",
            "source_refs": ["REQ.guard-tokens.acceptance.0"],
            "source_excerpt": "Humans must approve authority before use.",
        }
    ]
    assert artifact["gaps"][:1] == [
        {
            "id": "GAP-3",
            "text": "Spec does not define text output formatting.",
            "support": "inferred",
            "source_refs": [],
            "source_excerpt": None,
        }
    ]
    assert artifact["assumptions"] == [
        {
            "id": "ASM-7",
            "text": "Review assumes CLI output is JSON.",
            "support": "direct",
            "source_refs": ["REQ.guard-tokens.statement"],
            "source_excerpt": "The review output must include guard tokens.",
        }
    ]


def test_review_includes_full_source_under_default_limit(
    session: Session,
    tmp_path: Path,
) -> None:
    """Default review includes source content when the file is under the limit."""
    project_id, _spec_version_id, _authority_id, _spec_path = (
        _seed_pending_review_project(
            session,
            tmp_path=tmp_path,
            spec_content=_base_spec(),
        )
    )

    result = AuthorityReviewService(engine=_engine(session)).review(
        project_id=project_id
    )

    spec = result["data"]["spec"]
    assert spec["content_included"] is True
    assert spec["content_truncated"] is False
    assert spec["source_content"] == _base_spec()
    assert spec["source_content_sha256"] == sha256_prefixed(_base_spec().encode())


def test_review_uses_latest_spec_content_ref_instead_of_product_path(
    session: Session,
    tmp_path: Path,
) -> None:
    """Review reads and hashes the latest SpecRegistry content_ref path only."""
    spec_a = _base_spec()
    spec_b = _agileforge_spec_profile_payload(
        artifact_id="SPEC.product-path",
        title="Product Path Spec",
        summary="This product path must not be read.",
        requirement_statement="This product path must not be read.",
        acceptance=["The registry content_ref remains authoritative."],
    )
    project_id, spec_version_id, _authority_id, spec_path_a = (
        _seed_pending_review_project(
            session,
            tmp_path=tmp_path,
            spec_content=spec_a,
            spec_filename="spec-a.json",
        )
    )
    spec_path_b = tmp_path / "spec-b.json"
    spec_path_b.write_text(spec_b, encoding="utf-8")
    product = session.get(Product, project_id)
    assert product is not None
    product.spec_file_path = str(spec_path_b)
    session.add(product)
    session.commit()

    result = AuthorityReviewService(engine=_engine(session)).review(
        project_id=project_id
    )

    spec = result["data"]["spec"]
    assert spec["spec_version_id"] == spec_version_id
    assert spec["resolved_path"] == str(spec_path_a.resolve())
    assert spec["source_content"] == spec_a
    assert spec["disk_sha256"] == sha256_prefixed(spec_a.encode("utf-8"))
    assert spec["disk_sha256"] != sha256_prefixed(spec_b.encode("utf-8"))


def test_review_missing_latest_spec_content_ref_does_not_fallback_to_product_path(
    session: Session,
    tmp_path: Path,
) -> None:
    """Review returns a source error when latest SpecRegistry has no content_ref."""
    project_id, _spec_version_id, _authority_id, spec_path = (
        _seed_pending_review_project(
            session,
            tmp_path=tmp_path,
            spec_content=_base_spec(),
        )
    )
    spec = session.get(SpecRegistry, _spec_version_id)
    assert spec is not None
    spec.content_ref = None
    session.add(spec)
    session.commit()
    assert spec_path.is_file()

    result = AuthorityReviewService(engine=_engine(session)).review(
        project_id=project_id
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "SPEC_FILE_NOT_FOUND"
    assert result["errors"][0]["details"]["path"] is None


def test_review_blocks_stale_registry_hash_without_leaking_source(
    session: Session,
    tmp_path: Path,
) -> None:
    """Hash mismatch returns AUTHORITY_SOURCE_CHANGED without source content."""
    original = _base_spec()
    project_id, _spec_version_id, _authority_id, spec_path = (
        _seed_pending_review_project(
            session,
            tmp_path=tmp_path,
            spec_content=original,
        )
    )
    changed = _agileforge_spec_profile_payload(
        summary="This stale file must not be disclosed.",
        requirement_statement="This stale file must not be disclosed.",
        acceptance=["The stale source remains undisclosed."],
    )
    spec_path.write_text(changed, encoding="utf-8")

    result = AuthorityReviewService(engine=_engine(session)).review(
        project_id=project_id
    )

    assert result["ok"] is False
    assert result["data"] is None
    assert result["errors"][0]["code"] == "AUTHORITY_SOURCE_CHANGED"
    assert result["errors"][0]["details"]["registry_spec_hash"] == (
        sha256_prefixed(original.encode("utf-8"))
    )
    assert result["errors"][0]["details"]["disk_spec_hash"] == (
        sha256_prefixed(changed.encode("utf-8"))
    )
    assert "source_content" not in result["errors"][0]["details"]


def test_review_resolves_symlink_and_blocks_mismatched_target_hash(
    session: Session,
    tmp_path: Path,
) -> None:
    """Symlink paths report resolved targets and block mismatched content."""
    original = _base_spec()
    target_path = tmp_path / "target.json"
    target_path.write_text(
        _agileforge_spec_profile_payload(
            summary="This symlink target must not leak.",
            requirement_statement="This symlink target must not leak.",
            acceptance=["The mismatched symlink target remains undisclosed."],
        ),
        encoding="utf-8",
    )
    symlink_path = tmp_path / "linked.json"
    symlink_path.symlink_to(target_path)
    project_id, spec_version_id, authority_id, _spec_path = (
        _seed_pending_review_project(
            session,
            tmp_path=tmp_path,
            spec_content=original,
            spec_filename="registry.json",
        )
    )
    spec = session.get(SpecRegistry, spec_version_id)
    assert spec is not None
    spec.content_ref = str(symlink_path)
    session.add(spec)
    session.commit()

    result = AuthorityReviewService(engine=_engine(session)).review(
        project_id=project_id
    )

    assert authority_id
    assert result["ok"] is False
    assert result["errors"][0]["code"] == "AUTHORITY_SOURCE_CHANGED"
    assert result["errors"][0]["details"]["resolved_path"] == str(
        target_path.resolve()
    )


def test_review_omits_large_structured_source_and_marks_omission_complete(
    session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Large structured source is omitted in auto mode without coverage blockers."""
    monkeypatch.setenv("AGILEFORGE_AUTHORITY_REVIEW_SOURCE_LIMIT_BYTES", "96")
    spec_content = _base_spec()
    project_id, _spec_version_id, _authority_id, _spec_path = (
        _seed_pending_review_project(
            session,
            tmp_path=tmp_path,
            spec_content=spec_content,
        )
    )

    result = AuthorityReviewService(engine=_engine(session)).review(
        project_id=project_id
    )

    spec = result["data"]["spec"]
    assert spec["content_included"] is False
    assert spec["content_truncated"] is True
    assert spec["source_content"] is None
    assert spec["source_content_sha256"] is None
    assert spec["coverage_summary"]["omission_assessment"] == "complete"
    assert result["data"]["guard_tokens"]["expected_omission_assessment"] == (
        "complete"
    )


def test_review_invalid_structured_source_ref_adds_actionable_review_gap(
    session: Session,
    tmp_path: Path,
) -> None:
    """Invalid structured source refs create blocking findings and action guards."""
    spec_content = _base_spec()
    project_id, _spec_version_id, _authority_id, _spec_path = (
        _seed_pending_review_project(
            session,
            tmp_path=tmp_path,
            spec_content=spec_content,
            artifact_json=_compiled_success_json(
                source_excerpt="The review output must include guard tokens.",
                source_location="REQ.missing.statement",
            ),
        )
    )

    result = AuthorityReviewService(engine=_engine(session)).review(
        project_id=project_id
    )

    artifact = result["data"]["pending_authority"]["artifact"]
    gap_texts = [str(gap["text"]) for gap in artifact["gaps"]]
    findings = result["data"]["review_findings"]
    codes = {finding["code"] for finding in findings}
    assert result["data"]["spec"]["coverage_summary"]["omission_assessment"] == (
        "complete"
    )
    assert result["data"]["review_summary"]["acceptance_status"] == "blocked"
    assert result["data"]["next_actions"][0]["blocked"] is True
    assert "fatal_review_resolution" in result["data"]["next_actions"][0]["requires"]
    assert "SOURCE_REF_INVALID" in codes
    assert any("SOURCE_REF_INVALID" in text for text in gap_texts)


def test_structured_review_does_not_emit_public_candidate_blockers(
    session: Session,
    tmp_path: Path,
) -> None:
    """Structured review exposes source-ref findings, not host candidate blockers."""
    spec_content = _agileforge_spec_profile_payload(
        requirement_statement="The system must include audit evidence.",
        acceptance=["The authority packet exposes audit evidence."],
    )
    project_id, _spec_version_id, _authority_id, _spec_path = (
        _seed_pending_review_project(
            session,
            tmp_path=tmp_path,
            spec_content=spec_content,
            artifact_json=_compiled_success_json(
                source_excerpt="The system must include audit evidence.",
                source_location="REQ.guard-tokens.statement",
            ),
        )
    )

    result = AuthorityReviewService(engine=_engine(session)).review(
        project_id=project_id
    )

    assert result["ok"] is True
    data = result["data"]
    spec = data["spec"]
    pending = data["pending_authority"]
    assert spec["format"] == "agileforge.spec.v1"
    assert spec["source_outline"] == []
    assert "requirement_candidates" not in pending
    assert pending["authority_mappings"] == []
    assert pending["ir_provenance"] == "not_applicable"
    assert all(
        not str(finding["code"]).startswith("AUTHORITY_CANDIDATE_")
        for finding in pending["review_findings"]
    )
    assert data["review_findings"] == pending["review_findings"]
    assert data["review_summary"]["acceptance_status"] == "accept_ready"
    assert data["review_summary"]["compiler_gap_count"] == 0
    assert data["review_summary"]["compiler_assumption_count"] == 0
    assert data["review_summary"]["compiler_invariant_count"] == 1


def test_review_includes_structured_spec_metadata_for_agileforge_profile_json(
    session: Session,
    tmp_path: Path,
) -> None:
    """Canonical AgileForge spec JSON adds structured spec metadata."""
    spec_content = _agileforge_spec_profile_payload()
    artifact = TechnicalSpecArtifact.model_validate_json(spec_content)
    project_id, _spec_version_id, _authority_id, _spec_path = (
        _seed_pending_review_project(
            session,
            tmp_path=tmp_path,
            spec_content=spec_content,
            spec_filename="spec.json",
            artifact_json=_compiled_success_json(
                source_excerpt="The review output must include guard tokens."
            ),
        )
    )

    result = AuthorityReviewService(engine=_engine(session)).review(
        project_id=project_id
    )

    spec = result["data"]["spec"]
    assert spec["format"] == "agileforge.spec.v1"
    assert spec["artifact_id"] == "SPEC.authority-review"
    assert spec["canonical_spec_sha256"].startswith("sha256:")
    assert spec["render_profile"] == "agileforge.spec_markdown.v1"
    assert spec["rendered_markdown_sha256"].startswith("sha256:")
    assert spec["item_count"] == len(artifact.items)
    assert spec["relation_count"] == len(artifact.relations)
    assert spec["spec_hash"].startswith("sha256:")


def test_review_does_not_block_structured_spec_on_candidate_coverage(
    session: Session,
    tmp_path: Path,
) -> None:
    """Structured spec review does not expose host candidate coverage blockers."""
    spec_content = _agileforge_spec_profile_payload()
    project_id, _spec_version_id, _authority_id, _spec_path = (
        _seed_pending_review_project(
            session,
            tmp_path=tmp_path,
            spec_content=spec_content,
            spec_filename="spec.json",
            artifact_json=_compiled_success_json(
                source_excerpt="This sentence is review evidence only.",
                source_location="REQ.guard-tokens.statement",
            ),
        )
    )

    result = AuthorityReviewService(engine=_engine(session)).review(
        project_id=project_id
    )

    assert result["ok"] is True
    data = result["data"]
    pending = data["pending_authority"]
    codes = {finding["code"] for finding in data["review_findings"]}
    assert "requirement_candidates" not in pending
    assert pending["authority_mappings"] == []
    assert "AUTHORITY_CANDIDATE_UNCOVERED" not in codes
    assert "AUTHORITY_COVERAGE_INCOMPLETE" not in codes
    assert data["review_summary"]["acceptance_status"] == "accept_ready"


def test_review_accepts_structured_source_ref_with_dotted_item_id(
    session: Session,
    tmp_path: Path,
) -> None:
    """Structured source refs may point directly at item IDs containing dots."""
    payload = json.loads(_agileforge_spec_profile_payload())
    for item in payload["items"]:
        if item["id"] == "REQ.guard-tokens":
            item["id"] = "REQ.guard.tokens"
    for relation in payload["relations"]:
        if relation["from"] == "REQ.guard-tokens":
            relation["from"] = "REQ.guard.tokens"
    spec_content = canonical_spec_json(TechnicalSpecArtifact.model_validate(payload))
    project_id, _spec_version_id, _authority_id, _spec_path = (
        _seed_pending_review_project(
            session,
            tmp_path=tmp_path,
            spec_content=spec_content,
            spec_filename="spec.json",
            artifact_json=_compiled_success_json(
                source_excerpt="The review output must include guard tokens.",
                source_location="REQ.guard.tokens",
            ),
        )
    )

    result = AuthorityReviewService(engine=_engine(session)).review(
        project_id=project_id
    )

    assert result["ok"] is True
    codes = {finding["code"] for finding in result["data"]["review_findings"]}
    assert "SOURCE_REF_INVALID" not in codes
    assert result["data"]["review_summary"]["acceptance_status"] == "accept_ready"


def test_review_treats_non_structured_dotted_source_ref_as_missing(
    session: Session,
    tmp_path: Path,
) -> None:
    """Non-structured dotted source refs are missing evidence, not invalid IDs."""
    spec_content = _agileforge_spec_profile_payload()
    project_id, _spec_version_id, _authority_id, _spec_path = (
        _seed_pending_review_project(
            session,
            tmp_path=tmp_path,
            spec_content=spec_content,
            spec_filename="spec.json",
            artifact_json=_compiled_success_json(
                source_excerpt="The review output must include guard tokens.",
                source_location="spec.line.1",
            ),
        )
    )

    result = AuthorityReviewService(engine=_engine(session)).review(
        project_id=project_id
    )

    assert result["ok"] is True
    findings = result["data"]["review_findings"]
    codes = {finding["code"] for finding in findings}
    assert "SOURCE_REFS_MISSING" in codes
    assert "SOURCE_REF_INVALID" not in codes
    missing = next(f for f in findings if f["code"] == "SOURCE_REFS_MISSING")
    assert missing["override_allowed"] is True
    assert result["data"]["review_summary"]["acceptance_status"] == "accept_ready"


def test_review_warns_when_structured_source_refs_are_missing(
    session: Session,
    tmp_path: Path,
) -> None:
    """Missing source refs are visible but not acceptance blockers."""
    artifact = json.loads(_compiled_success_json(source_excerpt=""))
    artifact["source_map"] = []
    spec_content = _agileforge_spec_profile_payload()
    project_id, _spec_version_id, _authority_id, _spec_path = (
        _seed_pending_review_project(
            session,
            tmp_path=tmp_path,
            spec_content=spec_content,
            spec_filename="spec.json",
            artifact_json=json.dumps(artifact),
        )
    )

    result = AuthorityReviewService(engine=_engine(session)).review(
        project_id=project_id
    )

    findings = result["data"]["review_findings"]
    assert any(finding["code"] == "SOURCE_REFS_MISSING" for finding in findings)
    missing = next(f for f in findings if f["code"] == "SOURCE_REFS_MISSING")
    assert missing["severity"] == "warning"
    assert missing["override_allowed"] is True
    assert result["data"]["review_summary"]["acceptance_status"] == "accept_ready"


def test_review_blocks_structured_invalid_source_ref(
    session: Session,
    tmp_path: Path,
) -> None:
    """Source refs pointing at missing spec item IDs are structural blockers."""
    spec_content = _agileforge_spec_profile_payload()
    project_id, _spec_version_id, _authority_id, _spec_path = (
        _seed_pending_review_project(
            session,
            tmp_path=tmp_path,
            spec_content=spec_content,
            spec_filename="spec.json",
            artifact_json=_compiled_success_json(
                source_excerpt="The review output must include guard tokens.",
                source_location="REQ.missing.statement",
            ),
        )
    )

    result = AuthorityReviewService(engine=_engine(session)).review(
        project_id=project_id
    )

    codes = {finding["code"] for finding in result["data"]["review_findings"]}
    assert "SOURCE_REF_INVALID" in codes
    assert result["data"]["review_summary"]["acceptance_status"] == "blocked"


def test_review_accepts_pretty_structured_spec_when_registry_hash_is_canonical(
    session: Session,
    tmp_path: Path,
) -> None:
    """Review freshness canonicalizes structured JSON before comparing hashes."""
    spec_content = _agileforge_spec_profile_payload()
    artifact = TechnicalSpecArtifact.model_validate_json(spec_content)
    pretty_spec = json.dumps(json.loads(spec_content), indent=2)
    project_id, spec_version_id, _authority_id, _spec_path = (
        _seed_pending_review_project(
            session,
            tmp_path=tmp_path,
            spec_content=spec_content,
            spec_bytes=pretty_spec.encode("utf-8"),
            spec_filename="spec.json",
            artifact_json=_compiled_success_json(
                source_excerpt="The review output must include guard tokens."
            ),
        )
    )
    spec_row = session.get(SpecRegistry, spec_version_id)
    assert spec_row is not None
    spec_row.spec_hash = canonical_spec_hash(artifact)
    spec_row.content = spec_content
    session.add(spec_row)
    session.commit()

    result = AuthorityReviewService(engine=_engine(session)).review(
        project_id=project_id
    )

    assert result["ok"] is True
    spec = result["data"]["spec"]
    assert spec["format"] == "agileforge.spec.v1"
    assert spec["disk_sha256"] == canonical_spec_hash(artifact)


def test_review_summary_counts_compiler_artifact_evidence(
    session: Session,
    tmp_path: Path,
) -> None:
    """Review summary is driven by compiler artifact counts, not candidates."""
    success = SpecAuthorityCompilationSuccess(
        scope_themes=["Authority review"],
        domain="agent workbench",
        invariants=[
            Invariant(
                id=INVARIANT_ID,
                type=InvariantType.REQUIRED_FIELD,
                parameters=RequiredFieldParams(field_name="audit_evidence"),
            )
        ],
        eligible_feature_rules=[],
        rejected_features=[],
        gaps=["Clarify retention policy."],
        assumptions=["Audit evidence is stored with each decision."],
        source_map=[
            SourceMapEntry(
                invariant_id=INVARIANT_ID,
                excerpt="The system must include audit evidence.",
                location="REQ.guard-tokens.statement",
            )
        ],
        compiler_version=COMPILER_VERSION,
        prompt_hash=PROMPT_HASH,
        ir_schema_version=None,
        ir_provenance=None,
    )
    spec_content = _agileforge_spec_profile_payload(
        requirement_statement="The system must include audit evidence.",
        acceptance=["The authority packet exposes audit evidence."],
    )
    project_id, _spec_version_id, _authority_id, _spec_path = (
        _seed_pending_review_project(
            session,
            tmp_path=tmp_path,
            spec_content=spec_content,
            artifact_json=_stored_compiled_success_json_from_success(success),
        )
    )

    result = AuthorityReviewService(engine=_engine(session)).review(
        project_id=project_id
    )

    summary = result["data"]["review_summary"]
    assert summary["acceptance_status"] == "accept_ready"
    assert summary["compiler_gap_count"] == 1
    assert summary["compiler_assumption_count"] == 1
    assert summary["compiler_invariant_count"] == 1


def test_authority_review_packet_exposes_authority_quality(
    session: Session,
    tmp_path: Path,
) -> None:
    """Review packet includes persisted authority quality report."""
    project_id, _spec_version_id, authority_id, _spec_path = (
        _seed_pending_review_project(
            session,
            tmp_path=tmp_path,
            spec_content=_base_spec(),
        )
    )
    authority = session.get(CompiledSpecAuthority, authority_id)
    assert authority is not None
    artifact = json.loads(authority.compiled_artifact_json or "{}")
    quality_summary = {
        "original_invariant_count": 8,
        "final_invariant_count": 6,
        "merged_invariant_count": 2,
        "merged_assumption_count": 3,
        "review_group_count": 4,
        "near_duplicate_group_count": 5,
        "over_split_group_count": 6,
        "noisy_assumption_group_count": 7,
    }
    artifact["authority_quality"] = {
        "schema_version": "agileforge.authority_quality.v1",
        "summary": quality_summary,
        "merged_items": [
            {
                "merge_id": "AQ-MERGE-001",
                "item_kind": "invariant",
                "kept_id": "INV-0123456789abcdef",
                "removed_ids": ["INV-2222222222222222"],
                "reason": "exact_semantic_duplicate",
                "source_evidence_count": 2,
            }
        ],
        "review_groups": [
            {
                "group_id": "AQ-GROUP-001",
                "group_type": "over_split_invariants",
                "severity": "warning",
                "member_ids": ["INV-0123456789abcdef"],
                "reason": "one source item produced many invariants",
                "merge_allowed": False,
                "truncated": False,
            }
        ],
    }
    authority.compiled_artifact_json = json.dumps(artifact)
    session.add(authority)
    session.commit()

    service = AuthorityReviewService(engine=_engine(session))
    result = service.review(project_id=project_id)

    assert result["ok"] is True
    pending = result["data"]["pending_authority"]
    quality = pending["artifact"]["authority_quality"]
    assert quality["summary"] == quality_summary
    assert quality["review_groups"][0]["group_type"] == "over_split_invariants"
    assert {
        "quality_merged_invariant_count": pending["review_summary"][
            "quality_merged_invariant_count"
        ],
        "quality_merged_assumption_count": pending["review_summary"][
            "quality_merged_assumption_count"
        ],
        "quality_review_group_count": pending["review_summary"][
            "quality_review_group_count"
        ],
        "quality_near_duplicate_group_count": pending["review_summary"][
            "quality_near_duplicate_group_count"
        ],
        "quality_over_split_group_count": pending["review_summary"][
            "quality_over_split_group_count"
        ],
        "quality_noisy_assumption_group_count": pending["review_summary"][
            "quality_noisy_assumption_group_count"
        ],
    } == {
        "quality_merged_invariant_count": quality_summary["merged_invariant_count"],
        "quality_merged_assumption_count": quality_summary["merged_assumption_count"],
        "quality_review_group_count": quality_summary["review_group_count"],
        "quality_near_duplicate_group_count": quality_summary[
            "near_duplicate_group_count"
        ],
        "quality_over_split_group_count": quality_summary["over_split_group_count"],
        "quality_noisy_assumption_group_count": quality_summary[
            "noisy_assumption_group_count"
        ],
    }


def test_review_full_include_spec_includes_large_structured_source(
    session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full include mode includes structured source even when it exceeds the limit."""
    monkeypatch.setenv("AGILEFORGE_AUTHORITY_REVIEW_SOURCE_LIMIT_BYTES", "96")
    spec_content = _agileforge_spec_profile_payload(
        summary=(
            "Review packets expose deterministic authority evidence, source "
            "evidence, compiled authority evidence, and guard fields."
        ),
        requirement_statement=(
            "The review output must include guard tokens, review tokens, source "
            "evidence, compiled authority evidence, coverage summaries, and "
            "deterministic guard fields for every pending authority packet."
        ),
    )
    project_id, _spec_version_id, _authority_id, _spec_path = (
        _seed_pending_review_project(
            session,
            tmp_path=tmp_path,
            spec_content=spec_content,
            artifact_json=_compiled_success_json(
                source_excerpt=(
                    "The review output must include guard tokens, review tokens, "
                    "source evidence, compiled authority evidence, coverage "
                    "summaries, and deterministic guard fields for every pending "
                    "authority packet."
                ),
            ),
        )
    )

    result = AuthorityReviewService(engine=_engine(session)).review(
        project_id=project_id,
        include_spec="full",
    )

    spec = result["data"]["spec"]
    assert spec["content_included"] is True
    assert spec["content_truncated"] is False
    assert spec["source_content"] == spec_content
    assert spec["source_content_sha256"] == sha256_prefixed(spec_content.encode())
    assert spec["coverage_summary"]["omission_assessment"] == "complete"


def test_review_token_changes_when_disk_hash_changes(
    session: Session,
    tmp_path: Path,
) -> None:
    """Review token changes when the on-disk source hash changes."""
    project_id, _spec_version_id, _authority_id, spec_path = (
        _seed_pending_review_project(
            session,
            tmp_path=tmp_path,
            spec_content=_base_spec(),
        )
    )
    service = AuthorityReviewService(engine=_engine(session))

    first = service.review(project_id=project_id)["data"]["guard_tokens"]
    changed = _agileforge_spec_profile_payload(
        requirement_statement="The review output must include fresh guard tokens.",
        acceptance=[
            "The authority review packet includes fresh guard token evidence."
        ],
    )
    spec_path.write_text(changed, encoding="utf-8")
    spec = session.get(SpecRegistry, _spec_version_id)
    assert spec is not None
    spec.spec_hash = canonical_spec_hash(
        TechnicalSpecArtifact.model_validate_json(changed)
    )
    session.add(spec)
    session.commit()
    second = service.review(project_id=project_id)["data"]["guard_tokens"]

    assert first["review_token"] != second["review_token"]
    assert first["expected_disk_spec_hash"] != second["expected_disk_spec_hash"]
    assert first["expected_authority_fingerprint"] == second[
        "expected_authority_fingerprint"
    ]


def test_review_snapshot_recomputes_packet_review_token(
    session: Session,
    tmp_path: Path,
) -> None:
    """Snapshot payload independently recomputes the packet review token."""
    project_id, _spec_version_id, _authority_id, _spec_path = (
        _seed_pending_review_project(
            session,
            tmp_path=tmp_path,
            spec_content=_base_spec(),
        )
    )

    snapshot = build_authority_review_snapshot(
        engine=_engine(session),
        project_id=project_id,
    )
    assert not isinstance(snapshot, dict)
    result = AuthorityReviewService(engine=_engine(session)).review(
        project_id=project_id
    )

    expected_token = f"{REVIEW_TOKEN_SCHEMA}:{canonical_json_hash(snapshot.payload)}"
    assert snapshot.review_token == expected_token
    assert result["data"]["guard_tokens"]["review_token"] == expected_token
    assert result["data"]["guard_tokens"] == snapshot.guard_tokens


def test_coverage_fingerprint_sorts_nested_covered_by_and_source_refs() -> None:
    """Coverage fingerprints sort nested coverage identity arrays."""
    first = {
        "schema": "agileforge.authority_coverage_summary.v1",
        "spec_version_id": 1,
        "resolved_spec_path": "/example/spec.json",
        "source_content_sha256": "sha256:abc",
        "content_included": True,
        "content_truncated": False,
        "source_outline": [
            {
                "section_id": "S1",
                "heading": "Contract",
                "line_start": 1,
                "line_end": 3,
                "coverage_status": "covered",
                "covered_by": ["INV-b", "INV-a", "INV-a"],
                "classification_reason": None,
            }
        ],
        "authority_items": [
            {"id": "INV-a", "source_refs": ["line 2", "line 1", "line 1"]},
        ],
        "coverage_summary": {
            "covered_sections": 1,
            "partial_sections": 0,
            "intentionally_classified_sections": 0,
            "uncovered_sections": 0,
            "unclassified_content_blocks": 0,
            "omission_assessment": "complete",
        },
    }
    second = {
        **first,
        "source_outline": [
            {
                **first["source_outline"][0],
                "covered_by": ["INV-a", "INV-b"],
            }
        ],
        "authority_items": [{"id": "INV-a", "source_refs": ["line 1", "line 2"]}],
    }

    assert coverage_summary_fingerprint(first) == coverage_summary_fingerprint(second)


def test_missing_spec_file_returns_spec_file_not_found(
    session: Session,
    tmp_path: Path,
) -> None:
    """Missing source files return SPEC_FILE_NOT_FOUND."""
    project_id, _spec_version_id, _authority_id, spec_path = (
        _seed_pending_review_project(
            session,
            tmp_path=tmp_path,
            spec_content=_base_spec(),
        )
    )
    spec_path.unlink()

    result = AuthorityReviewService(engine=_engine(session)).review(
        project_id=project_id
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "SPEC_FILE_NOT_FOUND"
    assert result["errors"][0]["details"]["resolved_path"] == str(spec_path.resolve())


def test_invalid_utf8_spec_file_returns_spec_file_invalid(
    session: Session,
    tmp_path: Path,
) -> None:
    """Invalid UTF-8 source files return SPEC_FILE_INVALID."""
    project_id, _spec_version_id, _authority_id, _spec_path = (
        _seed_pending_review_project(
            session,
            tmp_path=tmp_path,
            spec_content="",
            spec_bytes=b"# Invalid\n\n\xff",
        )
    )

    result = AuthorityReviewService(engine=_engine(session)).review(
        project_id=project_id
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "SPEC_FILE_INVALID"
    assert "utf-8" in result["errors"][0]["details"]["reason"]
