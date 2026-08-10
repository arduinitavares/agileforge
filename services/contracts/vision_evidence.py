"""Strict, bounded repository evidence contracts for Vision bootstrap."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

from workflow.contracts import JsonObject
from workflow.fingerprints import canonical_hash, canonical_json

MAX_EVIDENCE_ITEMS: int = 8
MAX_EVIDENCE_ITEM_BYTES: int = 32 * 1024
MAX_EVIDENCE_TOTAL_BYTES: int = 96 * 1024

type VisionEvidenceKind = Literal[
    "project_metadata",
    "repository_provenance",
    "readme",
    "context",
    "package_metadata",
    "technical_specification",
]
type VisionEvidenceTrust = Literal[
    "operator_provided",
    "observed_provenance",
    "unreviewed_repository_evidence",
]

_APPROVED_EVIDENCE_PATH_KINDS: dict[str, VisionEvidenceKind] = {
    "README.md": "readme",
    "CONTEXT.md": "context",
    "pyproject.toml": "package_metadata",
    "specs/spec.json": "technical_specification",
    "specs/spec.md": "technical_specification",
    "docs/spec/spec.json": "technical_specification",
    "docs/spec/spec.md": "technical_specification",
}

_JSON_OBJECT_ADAPTER: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)


def _required_text(value: str, label: str) -> str:
    normalized = value.strip()
    if not normalized:
        msg = f"{label} must not be blank."
        raise ValueError(msg)
    return normalized


def _content_byte_length(content: str | JsonObject) -> int:
    serialized = content if isinstance(content, str) else canonical_json(content)
    return len(serialized.encode("utf-8"))


class VisionEvidenceItem(BaseModel):
    """One bounded, host-collected evidence item."""

    model_config = ConfigDict(extra="forbid")

    evidence_id: str
    kind: VisionEvidenceKind
    relative_path: str | None
    content_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    trust: VisionEvidenceTrust
    content: str | JsonObject
    truncated: bool

    @field_validator("evidence_id")
    @classmethod
    def validate_evidence_id(cls, value: str) -> str:
        """Require a stable evidence identity."""
        return _required_text(value, "evidence_id")

    @field_validator("relative_path")
    @classmethod
    def validate_relative_path(cls, value: str | None) -> str | None:
        """Accept only explicit POSIX-relative repository paths."""
        if value is None:
            return None
        normalized = _required_text(value, "relative_path")
        candidate = PurePosixPath(normalized)
        if "\\" in normalized or candidate.is_absolute() or ".." in candidate.parts:
            msg = "relative_path must be a non-absolute POSIX path without '..'."
            raise ValueError(msg)
        return normalized

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str | JsonObject) -> str | JsonObject:
        """Normalize text evidence and limit each item's encoded payload."""
        normalized = (
            _required_text(value, "content")
            if isinstance(value, str)
            else _JSON_OBJECT_ADAPTER.validate_python(value)
        )
        if _content_byte_length(normalized) > MAX_EVIDENCE_ITEM_BYTES:
            msg = (
                f"content exceeds MAX_EVIDENCE_ITEM_BYTES ({MAX_EVIDENCE_ITEM_BYTES})."
            )
            raise ValueError(msg)
        return normalized

    @model_validator(mode="after")
    def validate_semantics(self) -> Self:
        """Bind path eligibility and declared fingerprint to content."""
        if self.relative_path is None and self.kind not in {
            "project_metadata",
            "repository_provenance",
        }:
            msg = "relative_path may be None only for metadata or provenance evidence."
            raise ValueError(msg)
        if self.relative_path is not None:
            expected_kind = _APPROVED_EVIDENCE_PATH_KINDS.get(self.relative_path)
            if expected_kind is None:
                msg = "relative_path must be one of the approved model-facing paths."
                raise ValueError(msg)
            if self.kind != expected_kind:
                msg = "kind must match the approved relative_path."
                raise ValueError(msg)
        expected_evidence_id = (
            f"file:{self.relative_path}"
            if self.relative_path is not None
            else {
                "project_metadata": "project:metadata",
                "repository_provenance": "repository:provenance",
            }[self.kind]
        )
        if self.evidence_id != expected_evidence_id:
            msg = "evidence_id must match the exact approved source identity."
            raise ValueError(msg)
        if self.content_fingerprint != canonical_hash(self.content):
            msg = "content_fingerprint must equal canonical_hash(content)."
            raise ValueError(msg)
        return self


class VisionEvidenceWarning(BaseModel):
    """One deterministic evidence-collection warning."""

    model_config = ConfigDict(extra="forbid")

    code: str
    source: str
    message: str

    @field_validator("code", "source", "message")
    @classmethod
    def validate_text(cls, value: str, info: object) -> str:
        """Reject ambiguous warning fields."""
        return _required_text(value, str(getattr(info, "field_name", "warning")))


class VisionEvidenceBundle(BaseModel):
    """One deterministic, fingerprinted evidence bundle."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["agileforge.vision-evidence.v1"]
    items: tuple[VisionEvidenceItem, ...] = Field(max_length=MAX_EVIDENCE_ITEMS)
    warnings: tuple[VisionEvidenceWarning, ...]
    evidence_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_bundle(self) -> Self:
        """Require the declared bundle fingerprint and total byte budget."""
        total_bytes = sum(_content_byte_length(item.content) for item in self.items)
        if total_bytes > MAX_EVIDENCE_TOTAL_BYTES:
            msg = (
                "evidence exceeds MAX_EVIDENCE_TOTAL_BYTES "
                f"({MAX_EVIDENCE_TOTAL_BYTES})."
            )
            raise ValueError(msg)
        payload = {
            "schema_version": self.schema_version,
            "items": [item.model_dump(mode="json") for item in self.items],
            "warnings": [warning.model_dump(mode="json") for warning in self.warnings],
        }
        if self.evidence_fingerprint != canonical_hash(payload):
            msg = "evidence_fingerprint must equal the canonical evidence bundle hash."
            raise ValueError(msg)
        return self


__all__ = [
    "MAX_EVIDENCE_ITEMS",
    "MAX_EVIDENCE_ITEM_BYTES",
    "MAX_EVIDENCE_TOTAL_BYTES",
    "VisionEvidenceBundle",
    "VisionEvidenceItem",
    "VisionEvidenceKind",
    "VisionEvidenceTrust",
    "VisionEvidenceWarning",
]
