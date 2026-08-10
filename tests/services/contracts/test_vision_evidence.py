"""Contract tests for context-grounded Vision evidence."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from services.contracts.vision_evidence import (
    MAX_EVIDENCE_ITEM_BYTES,
    MAX_EVIDENCE_ITEMS,
    MAX_EVIDENCE_TOTAL_BYTES,
    VisionEvidenceBundle,
    VisionEvidenceItem,
    VisionEvidenceWarning,
)
from workflow.fingerprints import canonical_hash

EXPECTED_EVIDENCE_ITEMS: int = 8


def _item(**overrides: object) -> VisionEvidenceItem:
    content = "Repository context"
    values: dict[str, object] = {
        "evidence_id": "file:README.md",
        "kind": "readme",
        "relative_path": "README.md",
        "content_fingerprint": canonical_hash(content),
        "trust": "unreviewed_repository_evidence",
        "content": content,
        "truncated": False,
    }
    values.update(overrides)
    return VisionEvidenceItem(**values)


def test_evidence_contract_exposes_approved_bounds() -> None:
    """Evidence collection uses the approved hard bounds."""
    assert MAX_EVIDENCE_ITEMS == EXPECTED_EVIDENCE_ITEMS
    assert MAX_EVIDENCE_ITEM_BYTES == 32 * 1024
    assert MAX_EVIDENCE_TOTAL_BYTES == 96 * 1024


def test_evidence_item_rejects_absolute_path() -> None:
    """Evidence paths never expose absolute host paths."""
    with pytest.raises(ValidationError, match="relative_path"):
        VisionEvidenceItem(
            evidence_id="file:README.md",
            kind="readme",
            relative_path="/private/repository/README.md",
            content_fingerprint="sha256:" + "0" * 64,
            trust="unreviewed_repository_evidence",
            content="Example",
            truncated=False,
        )


@pytest.mark.parametrize("kind", ["source_code", "README"])
def test_evidence_item_rejects_unknown_kind(kind: str) -> None:
    """Only the approved evidence kinds are accepted."""
    with pytest.raises(ValidationError, match="kind"):
        _item(kind=kind)


def test_evidence_item_rejects_unknown_trust() -> None:
    """Only the approved evidence trust labels are accepted."""
    with pytest.raises(ValidationError, match="trust"):
        _item(trust="trusted")


def test_evidence_item_rejects_extra_fields_and_noncanonical_fingerprint() -> None:
    """Evidence items reject unmodeled fields and mismatched content hashes."""
    with pytest.raises(ValidationError, match="extra"):
        _item(extra_context="not allowed")
    with pytest.raises(ValidationError, match="content_fingerprint"):
        _item(content_fingerprint="sha256:" + "0" * 64)


@pytest.mark.parametrize("relative_path", ["../README.md", "docs\\guide.md"])
def test_evidence_item_requires_posix_relative_paths(relative_path: str) -> None:
    """Repository evidence paths must be POSIX-relative."""
    with pytest.raises(ValidationError, match="relative_path"):
        _item(relative_path=relative_path)


@pytest.mark.parametrize(
    ("relative_path", "kind"),
    [
        (".git/config", "readme"),
        (".git/HEAD", "readme"),
        ("docs/.git/config", "technical_specification"),
        ("src/agent.py", "readme"),
        ("README.md", "context"),
    ],
)
def test_evidence_item_rejects_unapproved_or_mismatched_model_paths(
    relative_path: str,
    kind: str,
) -> None:
    """Only the seven approved paths may reach the Vision model."""
    with pytest.raises(ValidationError, match=r"relative_path|kind"):
        _item(relative_path=relative_path, kind=kind)


@pytest.mark.parametrize(
    ("relative_path", "kind"),
    [
        ("README.md", "readme"),
        ("CONTEXT.md", "context"),
        ("pyproject.toml", "package_metadata"),
        ("specs/spec.json", "technical_specification"),
        ("specs/spec.md", "technical_specification"),
        ("docs/spec/spec.json", "technical_specification"),
        ("docs/spec/spec.md", "technical_specification"),
    ],
)
def test_evidence_item_accepts_each_approved_model_path(
    relative_path: str,
    kind: str,
) -> None:
    """Each explicitly allowed path retains its declared evidence kind."""
    assert (
        _item(
            evidence_id=f"file:{relative_path}",
            relative_path=relative_path,
            kind=kind,
        ).relative_path
        == relative_path
    )


def test_evidence_item_allows_pathless_metadata_only() -> None:
    """Only metadata/provenance evidence may have no source path."""
    metadata = _item(
        evidence_id="project:metadata",
        kind="project_metadata",
        relative_path=None,
        trust="operator_provided",
    )

    assert metadata.relative_path is None
    with pytest.raises(ValidationError, match="relative_path"):
        _item(relative_path=None)


@pytest.mark.parametrize(
    "overrides",
    [
        {
            "evidence_id": "project:other",
            "kind": "project_metadata",
            "relative_path": None,
            "trust": "operator_provided",
        },
        {
            "evidence_id": "repository:other",
            "kind": "repository_provenance",
            "relative_path": None,
            "trust": "observed_provenance",
        },
        {"evidence_id": "file:CONTEXT.md"},
        {"evidence_id": "file:.git/config"},
    ],
)
def test_evidence_item_rejects_noncanonical_source_identity(
    overrides: dict[str, object],
) -> None:
    """Bind every evidence ID to its exact metadata or allowlisted file source."""
    with pytest.raises(ValidationError, match="evidence_id"):
        _item(**overrides)


def test_bundle_requires_canonical_fingerprint_and_item_limit() -> None:
    """Bundles bind item/warning payloads to one bounded canonical hash."""
    item = _item()
    warning = VisionEvidenceWarning(
        code="ignored",
        source="README.md",
        message="Ignored optional section.",
    )
    values: dict[str, object] = {
        "schema_version": "agileforge.vision-evidence.v1",
        "items": (item,),
        "warnings": (warning,),
        "evidence_fingerprint": canonical_hash(
            {
                "schema_version": "agileforge.vision-evidence.v1",
                "items": [item.model_dump(mode="json")],
                "warnings": [warning.model_dump(mode="json")],
            }
        ),
    }

    assert VisionEvidenceBundle(**values).items == (item,)
    with pytest.raises(ValidationError, match="evidence_fingerprint"):
        VisionEvidenceBundle.model_validate(
            values | {"evidence_fingerprint": "sha256:" + "0" * 64}
        )
    with pytest.raises(ValidationError, match="at most 8"):
        VisionEvidenceBundle.model_validate(values | {"items": (item,) * 9})
