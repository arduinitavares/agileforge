"""Tests for read-only authority review packets."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

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
from utils.spec_schemas import (
    Invariant,
    InvariantType,
    RequiredFieldParams,
    SourceMapEntry,
    SpecAuthorityCompilationSuccess,
    SpecAuthorityCompilerOutput,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest
    from sqlmodel import Session

PROMPT_HASH = "a" * 64
COMPILER_VERSION = "1.0.0"
INVARIANT_ID = "INV-0123456789abcdef"


def _compiled_success_json(
    *,
    source_excerpt: str,
    source_location: str | None = "Submission Contract",
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
    )
    return SpecAuthorityCompilerOutput(root=success).model_dump_json()


def _seed_pending_review_project(  # noqa: PLR0913
    session: Session,
    *,
    tmp_path: Path,
    spec_content: str,
    artifact_json: str | None = None,
    spec_bytes: bytes | None = None,
    spec_filename: str = "spec.md",
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
    return (
        "# Submission Contract\n\n"
        "The review output must include guard tokens.\n\n"
        "## Background\n\n"
        "This section is descriptive background.\n"
    )


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

    result = AuthorityReviewService(engine=session.get_bind()).review(
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
            "command": (
                "agileforge authority accept "
                f"--project-id {project_id} "
                f"--review-token {guard_tokens['review_token']} "
                "--idempotency-key <idempotency_key>"
            ),
            "mode": "human",
            "installed": True,
            "requires_cli_installation": False,
            "requires": ["review_token", "idempotency_key"],
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

    result = AuthorityReviewService(engine=session.get_bind()).review(
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

    result = AuthorityReviewService(engine=session.get_bind()).review(
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


def test_review_fallback_preserves_persisted_authority_fields(
    session: Session,
    tmp_path: Path,
) -> None:
    """Fallback review rendering preserves normalized persisted authority fields."""
    project_id, _spec_version_id, authority_id, _spec_path = (
        _seed_pending_review_project(
            session,
            tmp_path=tmp_path,
            spec_content=_base_spec(),
            artifact_json=json.dumps(
                {
                    "assumptions": [
                        {
                            "id": "ASM-7",
                            "text": "Review assumes CLI output is JSON.",
                            "support": "direct",
                            "source_refs": ["Submission Contract"],
                            "source_excerpt": (
                                "The review output must include guard tokens."
                            ),
                        }
                    ]
                }
            ),
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
                "source_refs": ["Submission Contract"],
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
                "source_refs": ["Manual Checkpoint"],
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

    result = AuthorityReviewService(engine=session.get_bind()).review(
        project_id=project_id
    )

    artifact = result["data"]["pending_authority"]["artifact"]
    assert artifact["eligible_feature_rules"] == [
        {
            "id": "ELIG-2",
            "text": "Allow read-only packet rendering.",
            "support": "direct",
            "source_refs": ["Submission Contract"],
            "source_excerpt": "The review output must include guard tokens.",
        }
    ]
    assert artifact["rejected_features"] == [
        {
            "id": "REJ-9",
            "text": "Reject automatic authority acceptance.",
            "support": "direct",
            "source_refs": ["Manual Checkpoint"],
            "source_excerpt": "Humans must approve authority before use.",
        }
    ]
    assert artifact["gaps"] == [
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
            "source_refs": ["Submission Contract"],
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

    result = AuthorityReviewService(engine=session.get_bind()).review(
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
    spec_b = "# Submission Contract\n\nThis product path must not be read.\n"
    project_id, spec_version_id, _authority_id, spec_path_a = (
        _seed_pending_review_project(
            session,
            tmp_path=tmp_path,
            spec_content=spec_a,
            spec_filename="spec-a.md",
        )
    )
    spec_path_b = tmp_path / "spec-b.md"
    spec_path_b.write_text(spec_b, encoding="utf-8")
    product = session.get(Product, project_id)
    assert product is not None
    product.spec_file_path = str(spec_path_b)
    session.add(product)
    session.commit()

    result = AuthorityReviewService(engine=session.get_bind()).review(
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

    result = AuthorityReviewService(engine=session.get_bind()).review(
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
    changed = "# Submission Contract\n\nThis stale file must not be disclosed.\n"
    spec_path.write_text(changed, encoding="utf-8")

    result = AuthorityReviewService(engine=session.get_bind()).review(
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
    target_path = tmp_path / "target.md"
    target_path.write_text(
        "# Submission Contract\n\nThis symlink target must not leak.\n",
        encoding="utf-8",
    )
    symlink_path = tmp_path / "linked.md"
    symlink_path.symlink_to(target_path)
    project_id, spec_version_id, authority_id, _spec_path = (
        _seed_pending_review_project(
            session,
            tmp_path=tmp_path,
            spec_content=original,
            spec_filename="registry.md",
        )
    )
    spec = session.get(SpecRegistry, spec_version_id)
    assert spec is not None
    spec.content_ref = str(symlink_path)
    session.add(spec)
    session.commit()

    result = AuthorityReviewService(engine=session.get_bind()).review(
        project_id=project_id
    )

    assert authority_id
    assert result["ok"] is False
    assert result["errors"][0]["code"] == "AUTHORITY_SOURCE_CHANGED"
    assert result["errors"][0]["details"]["resolved_path"] == str(
        target_path.resolve()
    )


def test_review_omits_large_source_and_marks_omission_incomplete(
    session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Large source is omitted in auto mode and keeps the review incomplete."""
    monkeypatch.setenv("AGILEFORGE_AUTHORITY_REVIEW_SOURCE_LIMIT_BYTES", "96")
    spec_content = _base_spec() + "\n".join(
        f"- Requirement {index} must be reviewed." for index in range(20)
    )
    project_id, _spec_version_id, _authority_id, _spec_path = (
        _seed_pending_review_project(
            session,
            tmp_path=tmp_path,
            spec_content=spec_content,
        )
    )

    result = AuthorityReviewService(engine=session.get_bind()).review(
        project_id=project_id
    )

    spec = result["data"]["spec"]
    assert spec["content_included"] is False
    assert spec["content_truncated"] is True
    assert spec["source_content"] is None
    assert spec["source_content_sha256"] is None
    assert spec["coverage_summary"]["omission_assessment"] == "incomplete"
    assert result["data"]["guard_tokens"]["expected_omission_assessment"] == (
        "incomplete"
    )


def test_review_omits_large_covered_source_and_marks_omission_complete(
    session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Omitted source may be complete when outline coverage proves complete."""
    monkeypatch.setenv("AGILEFORGE_AUTHORITY_REVIEW_SOURCE_LIMIT_BYTES", "96")
    covered_requirement = (
        "The review output must include guard tokens, review tokens, source "
        "evidence, compiled authority evidence, coverage summaries, and "
        "deterministic guard fields for every pending authority packet."
    )
    spec_content = "# Submission Contract\n\n" + covered_requirement
    artifact_json = _compiled_success_json(source_excerpt=covered_requirement)
    project_id, _spec_version_id, _authority_id, _spec_path = (
        _seed_pending_review_project(
            session,
            tmp_path=tmp_path,
            spec_content=spec_content,
            artifact_json=artifact_json,
        )
    )

    result = AuthorityReviewService(engine=session.get_bind()).review(
        project_id=project_id
    )

    spec = result["data"]["spec"]
    assert spec["content_included"] is False
    assert spec["content_truncated"] is True
    assert spec["coverage_summary"]["covered_sections"] == 1
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
    service = AuthorityReviewService(engine=session.get_bind())

    first = service.review(project_id=project_id)["data"]["guard_tokens"]
    changed = _base_spec().replace("guard tokens", "fresh guard tokens")
    spec_path.write_text(changed, encoding="utf-8")
    spec = session.get(SpecRegistry, _spec_version_id)
    assert spec is not None
    spec.spec_hash = sha256_prefixed(changed.encode("utf-8"))
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
        engine=session.get_bind(),
        project_id=project_id,
    )
    result = AuthorityReviewService(engine=session.get_bind()).review(
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
        "resolved_spec_path": "/example/spec.md",
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


def test_malformed_markdown_emits_diagnostic_instead_of_failing(
    session: Session,
    tmp_path: Path,
) -> None:
    """Malformed Markdown produces coverage diagnostics without failing review."""
    spec_content = (
        "# Submission Contract\n\n"
        "The review output must include guard tokens.\n\n"
        "```json\n"
        '{"field": "unterminated fenced code must be diagnosed"}\n'
    )
    project_id, _spec_version_id, _authority_id, _spec_path = (
        _seed_pending_review_project(
            session,
            tmp_path=tmp_path,
            spec_content=spec_content,
        )
    )

    result = AuthorityReviewService(engine=session.get_bind()).review(
        project_id=project_id
    )

    assert result["ok"] is True
    spec = result["data"]["spec"]
    assert spec["coverage_summary"]["omission_assessment"] == "incomplete"
    assert spec["coverage_diagnostics"] == [
        {
            "section_id": "S1",
            "code": "MARKDOWN_FENCE_UNCLOSED",
            "message": "Fenced code block was not closed before end of file.",
        }
    ]


def test_review_ignores_markdown_headings_inside_fenced_code(
    session: Session,
    tmp_path: Path,
) -> None:
    """Heading-looking fenced code lines stay inside the nearest section."""
    spec_content = (
        "# Submission Contract\n\n"
        "```markdown\n"
        "# Not A Heading\n"
        "The fenced example must stay in this section.\n"
        "```\n\n"
        "## Background\n\n"
        "Descriptive text.\n"
    )
    project_id, _spec_version_id, _authority_id, _spec_path = (
        _seed_pending_review_project(
            session,
            tmp_path=tmp_path,
            spec_content=spec_content,
            artifact_json=_compiled_success_json(source_excerpt="unrelated source"),
        )
    )

    result = AuthorityReviewService(engine=session.get_bind()).review(
        project_id=project_id
    )

    headings = [entry["heading"] for entry in result["data"]["spec"]["source_outline"]]
    assert headings == ["Submission Contract", "Background"]


def test_review_counts_fenced_code_as_one_content_block(
    session: Session,
    tmp_path: Path,
) -> None:
    """A fenced code block contributes one requirement-bearing content block."""
    spec_content = (
        "# Submission Contract\n\n"
        "```json\n"
        '{"field_a": "must exist"}\n'
        '{"field_b": "must exist"}\n'
        "```\n"
    )
    project_id, _spec_version_id, _authority_id, _spec_path = (
        _seed_pending_review_project(
            session,
            tmp_path=tmp_path,
            spec_content=spec_content,
            artifact_json=_compiled_success_json(source_excerpt="unrelated source"),
        )
    )

    result = AuthorityReviewService(engine=session.get_bind()).review(
        project_id=project_id
    )

    summary = result["data"]["spec"]["coverage_summary"]
    assert summary["uncovered_sections"] == 1
    assert summary["unclassified_content_blocks"] == 1


def test_review_tilde_fenced_code_ignores_headings_and_counts_one_block(
    session: Session,
    tmp_path: Path,
) -> None:
    """Tilde fenced code behaves like backtick fenced code."""
    spec_content = (
        "# Submission Contract\n\n"
        "~~~markdown\n"
        "# Not A Heading\n"
        "The fenced example must stay in one block.\n"
        "This line must not become a second block.\n"
        "~~~\n\n"
        "## Background\n\n"
        "Descriptive text.\n"
    )
    project_id, _spec_version_id, _authority_id, _spec_path = (
        _seed_pending_review_project(
            session,
            tmp_path=tmp_path,
            spec_content=spec_content,
            artifact_json=_compiled_success_json(source_excerpt="unrelated source"),
        )
    )

    result = AuthorityReviewService(engine=session.get_bind()).review(
        project_id=project_id
    )

    spec = result["data"]["spec"]
    headings = [entry["heading"] for entry in spec["source_outline"]]
    assert headings == ["Submission Contract", "Background"]
    assert spec["coverage_summary"]["uncovered_sections"] == 1
    assert spec["coverage_summary"]["unclassified_content_blocks"] == 1


def test_review_tilde_fence_does_not_close_on_backticks(
    session: Session,
    tmp_path: Path,
) -> None:
    """A tilde fence ignores backtick fence markers inside the block."""
    spec_content = (
        "# Submission Contract\n\n"
        "~~~markdown\n"
        "```\n"
        "# Not A Heading\n"
        "The fenced example must remain one block.\n"
        "```\n"
        "~~~\n\n"
        "## Background\n\n"
        "Descriptive text.\n"
    )
    project_id, _spec_version_id, _authority_id, _spec_path = (
        _seed_pending_review_project(
            session,
            tmp_path=tmp_path,
            spec_content=spec_content,
            artifact_json=_compiled_success_json(source_excerpt="unrelated source"),
        )
    )

    result = AuthorityReviewService(engine=session.get_bind()).review(
        project_id=project_id
    )

    spec = result["data"]["spec"]
    headings = [entry["heading"] for entry in spec["source_outline"]]
    assert headings == ["Submission Contract", "Background"]
    assert spec["coverage_summary"]["uncovered_sections"] == 1
    assert spec["coverage_summary"]["unclassified_content_blocks"] == 1


def test_review_backtick_fence_does_not_close_on_tildes(
    session: Session,
    tmp_path: Path,
) -> None:
    """A backtick fence ignores tilde fence markers inside the block."""
    spec_content = (
        "# Submission Contract\n\n"
        "```markdown\n"
        "~~~\n"
        "# Not A Heading\n"
        "The fenced example must remain one block.\n"
        "~~~\n"
        "```\n\n"
        "## Background\n\n"
        "Descriptive text.\n"
    )
    project_id, _spec_version_id, _authority_id, _spec_path = (
        _seed_pending_review_project(
            session,
            tmp_path=tmp_path,
            spec_content=spec_content,
            artifact_json=_compiled_success_json(source_excerpt="unrelated source"),
        )
    )

    result = AuthorityReviewService(engine=session.get_bind()).review(
        project_id=project_id
    )

    spec = result["data"]["spec"]
    headings = [entry["heading"] for entry in spec["source_outline"]]
    assert headings == ["Submission Contract", "Background"]
    assert spec["coverage_summary"]["uncovered_sections"] == 1
    assert spec["coverage_summary"]["unclassified_content_blocks"] == 1


def test_review_long_backtick_fence_requires_matching_close_length(
    session: Session,
    tmp_path: Path,
) -> None:
    """A longer backtick opener ignores shorter backtick fences inside it."""
    spec_content = (
        "# Submission Contract\n\n"
        "````markdown\n"
        "```\n"
        "# Not A Heading\n"
        "The fenced example must remain one block.\n"
        "```\n"
        "````\n\n"
        "## Background\n\n"
        "Descriptive text.\n"
    )
    project_id, _spec_version_id, _authority_id, _spec_path = (
        _seed_pending_review_project(
            session,
            tmp_path=tmp_path,
            spec_content=spec_content,
            artifact_json=_compiled_success_json(source_excerpt="unrelated source"),
        )
    )

    result = AuthorityReviewService(engine=session.get_bind()).review(
        project_id=project_id
    )

    spec = result["data"]["spec"]
    headings = [entry["heading"] for entry in spec["source_outline"]]
    assert headings == ["Submission Contract", "Background"]
    assert spec["coverage_summary"]["uncovered_sections"] == 1
    assert spec["coverage_summary"]["unclassified_content_blocks"] == 1


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

    result = AuthorityReviewService(engine=session.get_bind()).review(
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

    result = AuthorityReviewService(engine=session.get_bind()).review(
        project_id=project_id
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "SPEC_FILE_INVALID"
    assert "utf-8" in result["errors"][0]["details"]["reason"]
