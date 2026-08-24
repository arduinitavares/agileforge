"""Closed canonical AgileForge specification profile v2."""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION: Literal["agileforge.spec.v2"] = "agileforge.spec.v2"
RENDERER_VERSION: str = "agileforge.spec_review.v2"
_ITEM_ID_RE: re.Pattern[str] = re.compile(
    r"^(GOAL|NON_GOAL|REQ|QUALITY|CONSTRAINT|INTERFACE|DATA|DECISION|"
    r"ASSUMPTION|RISK|EXAMPLE|OPEN_QUESTION)\.[a-z0-9][a-z0-9.-]{1,96}$"
)
_EXTERNAL_REFERENCE_ID_RE: re.Pattern[str] = re.compile(
    r"^EXT\.[a-z0-9][a-z0-9.-]{1,96}$"
)
_SOURCE_ID_RE: re.Pattern[str] = re.compile(r"^SRC\.[a-z0-9][a-z0-9.-]{1,96}$")
_MARKDOWN_LEADING_RE: re.Pattern[str] = re.compile(
    r"^(\s*)((?:[#\-*+>])|(?:\d+\.)(?=\s|$))"
)


class SpecItemType(StrEnum):
    """Closed item vocabulary retained from the v1 semantic profile."""

    GOAL = "GOAL"
    NON_GOAL = "NON_GOAL"
    REQ = "REQ"
    QUALITY = "QUALITY"
    CONSTRAINT = "CONSTRAINT"
    INTERFACE = "INTERFACE"
    DATA = "DATA"
    DECISION = "DECISION"
    ASSUMPTION = "ASSUMPTION"
    RISK = "RISK"
    EXAMPLE = "EXAMPLE"
    OPEN_QUESTION = "OPEN_QUESTION"


class RequirementLevel(StrEnum):
    """Controlled requirement levels retained from the v1 profile."""

    MUST = "MUST"
    MUST_NOT = "MUST_NOT"
    SHOULD = "SHOULD"
    MAY = "MAY"
    INFORMATIVE = "INFORMATIVE"


class VerificationMethod(StrEnum):
    """Closed verification methods retained from the v1 profile."""

    INSPECTION = "inspection"
    ANALYSIS = "analysis"
    UNIT_TEST = "unit-test"
    INTEGRATION_TEST = "integration-test"
    SYSTEM_TEST = "system-test"
    ACCEPTANCE_TEST = "acceptance-test"
    MANUAL_REVIEW = "manual-review"
    MONITORING = "monitoring"
    NOT_YET_DEFINED = "not-yet-defined"


class RelationType(StrEnum):
    """Closed relation vocabulary retained from the v1 profile."""

    SATISFIES = "satisfies"
    DECOMPOSES = "decomposes"
    CONSTRAINS = "constrains"
    DEPENDS_ON = "depends_on"
    IMPLEMENTS = "implements"
    VERIFIES = "verifies"
    TRACKS = "tracks"
    SUPERSEDES = "supersedes"
    CONFLICTS_WITH = "conflicts_with"
    CLARIFIES = "clarifies"


class SourceNoteKind(StrEnum):
    """Bounded source-note categories for human review context."""

    USER_NOTE = "user_note"
    INTERVIEW = "interview"
    IMPORT = "import"
    EXTERNAL_SUMMARY = "external_summary"


class ControlledTermScope(StrEnum):
    """Scope of one controlled semantic term."""

    ARTIFACT = "artifact"
    DOMAIN = "domain"
    PROJECT = "project"


class _FrozenModel(BaseModel):
    """Forbid hidden fields and mutation in canonical v2 records."""

    model_config = ConfigDict(extra="forbid", frozen=True)


def _normalized_key(value: str) -> str:
    return " ".join(value.strip().casefold().split())


def _nonempty(value: str) -> str:
    if not value.strip():
        message = "value must not be blank"
        raise ValueError(message)
    return value


class SourceNote(_FrozenModel):
    """One intentionally ordered provenance note attached to an item."""

    source_id: Annotated[str, Field(pattern=_SOURCE_ID_RE.pattern)]
    kind: SourceNoteKind
    text: Annotated[str, Field(min_length=1)]
    external_ref_id: (
        Annotated[str, Field(pattern=_EXTERNAL_REFERENCE_ID_RE.pattern)] | None
    ) = None

    _strip_text = field_validator("source_id", "text", mode="before")(_nonempty)


class ControlledTerm(_FrozenModel):
    """A project-local term with a deterministic normalized identity."""

    term: Annotated[str, Field(min_length=1)]
    definition: Annotated[str, Field(min_length=1)]
    scope: ControlledTermScope = ControlledTermScope.ARTIFACT

    _strip_text = field_validator("term", "definition", mode="before")(_nonempty)


class ExternalReference(_FrozenModel):
    """External provenance shown to reviewers but not itself a trusted source."""

    id: Annotated[str, Field(pattern=_EXTERNAL_REFERENCE_ID_RE.pattern)]
    title: Annotated[str, Field(min_length=1)]
    url: str | None = None
    summary: Annotated[str, Field(min_length=1)]

    _strip_text = field_validator("id", "title", "summary", mode="before")(_nonempty)


class SpecificationItem(_FrozenModel):
    """One lifecycle-free typed specification item."""

    id: Annotated[str, Field(pattern=_ITEM_ID_RE.pattern)]
    type: SpecItemType
    title: Annotated[str, Field(min_length=1)]
    statement: Annotated[str, Field(min_length=1)]
    level: RequirementLevel | None = None
    rationale: str | None = None
    verification: VerificationMethod | None = None
    acceptance: tuple[Annotated[str, Field(min_length=1)], ...] = ()
    tags: tuple[Annotated[str, Field(min_length=1)], ...] = ()
    source_notes: tuple[SourceNote, ...] = ()

    _strip_text = field_validator("id", "title", "statement", mode="before")(_nonempty)

    @field_validator("rationale", mode="before")
    @classmethod
    def validate_optional_rationale(cls, value: object) -> object:
        """Reject blank optional rationale without altering supplied bytes."""
        return _nonempty(value) if isinstance(value, str) else value

    @field_validator("acceptance", "tags", mode="before")
    @classmethod
    def validate_ordered_text(cls, value: object) -> object:
        """Reject blank text while preserving declared input ordering and bytes."""
        if not isinstance(value, list | tuple):
            return value
        return tuple(
            _nonempty(item) if isinstance(item, str) else item for item in value
        )

    @model_validator(mode="after")
    def validate_item_contract(self) -> Self:
        """Validate normative evidence and duplicate normalized tag identities."""
        normalized_tags = [_normalized_key(tag) for tag in self.tags]
        if len(normalized_tags) != len(set(normalized_tags)):
            message = "duplicate normalized tags"
            raise ValueError(message)
        normative_types = {
            SpecItemType.REQ,
            SpecItemType.QUALITY,
            SpecItemType.CONSTRAINT,
            SpecItemType.INTERFACE,
            SpecItemType.DATA,
        }
        if self.type in normative_types:
            missing = [
                name
                for name, present in (
                    ("level", self.level is not None),
                    ("verification", self.verification is not None),
                    ("acceptance", bool(self.acceptance)),
                )
                if not present
            ]
            if missing:
                message = f"normative item {self.id} missing: {', '.join(missing)}"
                raise ValueError(message)
        if self.id.split(".", maxsplit=1)[0] != self.type.value:
            message = "item id prefix must match item type"
            raise ValueError(message)
        return self


class SpecificationRelation(_FrozenModel):
    """Typed edge between stable specification item IDs."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    from_: Annotated[str, Field(alias="from", pattern=_ITEM_ID_RE.pattern)]
    type: RelationType
    to: Annotated[str, Field(pattern=_ITEM_ID_RE.pattern)]
    rationale: str | None = None

    _strip_text = field_validator("from_", "to", mode="before")(_nonempty)

    @field_validator("rationale", mode="before")
    @classmethod
    def validate_optional_rationale(cls, value: object) -> object:
        """Reject blank optional relation rationale without changing bytes."""
        return _nonempty(value) if isinstance(value, str) else value


class SpecificationPayload(_FrozenModel):
    """Canonical semantic Specification bytes without lifecycle metadata."""

    schema_version: Literal["agileforge.spec.v2"] = SCHEMA_VERSION
    artifact_id: Annotated[str, Field(pattern=r"^SPEC\.[a-z0-9][a-z0-9.-]{1,96}$")]
    title: Annotated[str, Field(min_length=1)]
    summary: Annotated[str, Field(min_length=1)]
    problem_statement: Annotated[str, Field(min_length=1)]
    items: tuple[SpecificationItem, ...]
    relations: tuple[SpecificationRelation, ...] = ()
    controlled_terms: tuple[ControlledTerm, ...] = ()
    external_references: tuple[ExternalReference, ...] = ()

    _strip_text = field_validator(
        "artifact_id", "title", "summary", "problem_statement", mode="before"
    )(_nonempty)

    @model_validator(mode="after")
    def validate_cross_references(self) -> Self:
        """Validate stable IDs and all payload-internal references."""
        _validate_items_and_relations(self.items, self.relations)
        _validate_terms(self.controlled_terms)
        _validate_external_references(self.items, self.external_references)
        return self


def _validate_items_and_relations(
    items: tuple[SpecificationItem, ...],
    relations: tuple[SpecificationRelation, ...],
) -> None:
    item_ids = [item.id for item in items]
    if len(item_ids) != len(set(item_ids)):
        message = "duplicate item ids"
        raise ValueError(message)
    item_id_set = set(item_ids)
    for relation in relations:
        for endpoint in (relation.from_, relation.to):
            if endpoint not in item_id_set:
                message = f"unknown relation endpoint: {endpoint}"
                raise ValueError(message)
    relation_keys = [
        (relation.from_, relation.type.value, relation.to) for relation in relations
    ]
    if len(relation_keys) != len(set(relation_keys)):
        message = "duplicate relation edge"
        raise ValueError(message)


def _validate_terms(terms: tuple[ControlledTerm, ...]) -> None:
    normalized_terms = [
        (_normalized_key(term.term), term.scope.value) for term in terms
    ]
    if len(normalized_terms) != len(set(normalized_terms)):
        message = "duplicate normalized controlled terms"
        raise ValueError(message)


def _validate_external_references(
    items: tuple[SpecificationItem, ...],
    external_references: tuple[ExternalReference, ...],
) -> None:
    external_ids = [reference.id for reference in external_references]
    if len(external_ids) != len(set(external_ids)):
        message = "duplicate external reference ids"
        raise ValueError(message)
    external_id_set = set(external_ids)
    for item in items:
        for note in item.source_notes:
            if (
                note.external_ref_id is not None
                and note.external_ref_id not in external_id_set
            ):
                message = f"unknown external reference endpoint: {note.external_ref_id}"
                raise ValueError(message)


def _canonical_item(item: SpecificationItem) -> dict[str, Any]:
    """Return one item with only its declared unordered tags normalized."""
    data = item.model_dump(mode="json")
    data["tags"] = sorted(item.tags, key=_normalized_key)
    return data


def _canonical_payload_data(payload: SpecificationPayload) -> dict[str, Any]:
    """Return the canonical payload tree before JSON encoding."""
    data = payload.model_dump(mode="json", by_alias=True)
    data["items"] = [
        _canonical_item(item)
        for item in sorted(payload.items, key=lambda item: item.id)
    ]
    data["relations"] = [
        relation.model_dump(mode="json", by_alias=True)
        for relation in sorted(
            payload.relations,
            key=lambda relation: (relation.from_, relation.type.value, relation.to),
        )
    ]
    data["controlled_terms"] = [
        term.model_dump(mode="json")
        for term in sorted(
            payload.controlled_terms,
            key=lambda term: (_normalized_key(term.term), term.scope.value),
        )
    ]
    data["external_references"] = [
        reference.model_dump(mode="json")
        for reference in sorted(
            payload.external_references,
            key=lambda reference: reference.id,
        )
    ]
    return data


def canonical_spec_json(payload: SpecificationPayload) -> str:
    """Return canonical UTF-8 JSON for the lifecycle-free semantic payload."""
    return json.dumps(
        _canonical_payload_data(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def canonical_spec_hash(payload: SpecificationPayload) -> str:
    """Return the SHA-256 fingerprint of exact canonical semantic bytes."""
    digest = hashlib.sha256(canonical_spec_json(payload).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def rendered_markdown_hash(markdown: str) -> str:
    """Return the SHA-256 fingerprint of an exact review projection."""
    digest = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _escape_markdown_text(value: str) -> str:
    escaped = value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return "\n".join(
        _MARKDOWN_LEADING_RE.sub(r"\1\\\2", line) for line in escaped.split("\n")
    )


def _line(label: str, value: str | None) -> str:
    return f"- {label}: {_escape_markdown_text(value) if value else '-'}"


def _render_terms(payload: SpecificationPayload) -> list[str]:
    lines = ["## Controlled Terms", ""]
    if not payload.controlled_terms:
        return [*lines, "- None", ""]
    for term in sorted(
        payload.controlled_terms,
        key=lambda term: (_normalized_key(term.term), term.scope.value),
    ):
        lines.extend(
            [
                f"### {_escape_markdown_text(term.term)}",
                "",
                _line("Scope", term.scope.value),
                _line("Definition", term.definition),
                "",
            ]
        )
    return lines


def _render_item(item: SpecificationItem) -> list[str]:
    verification = item.verification.value if item.verification else None
    lines = [
        f"### {_escape_markdown_text(item.id)} - {_escape_markdown_text(item.title)}",
        "",
        _line("Type", item.type.value),
        _line("Level", item.level.value if item.level else None),
        _line("Verification", verification),
        _line(
            "Tags",
            ", ".join(
                _escape_markdown_text(tag)
                for tag in sorted(item.tags, key=_normalized_key)
            )
            if item.tags
            else None,
        ),
        "",
        "Statement:",
        "",
        _escape_markdown_text(item.statement),
        "",
    ]
    if item.rationale:
        lines.extend(["Rationale:", "", _escape_markdown_text(item.rationale), ""])
    lines.extend(["Acceptance:", ""])
    lines.extend(
        [f"- {_escape_markdown_text(value)}" for value in item.acceptance]
        if item.acceptance
        else ["- None"]
    )
    lines.extend(["", "Source Notes:", ""])
    if not item.source_notes:
        return [*lines, "- None", ""]
    for note in item.source_notes:
        lines.extend(
            [
                f"- {_escape_markdown_text(note.source_id)} "
                f"({_escape_markdown_text(note.kind.value)})",
                f"  - Text: {_escape_markdown_text(note.text)}",
                _line("  - External reference", note.external_ref_id),
            ]
        )
    lines.append("")
    return lines


def _render_relations(payload: SpecificationPayload) -> list[str]:
    lines = ["## Relations", ""]
    if not payload.relations:
        return [*lines, "- None"]
    for relation in sorted(
        payload.relations,
        key=lambda relation: (relation.from_, relation.type.value, relation.to),
    ):
        lines.append(
            f"- {_escape_markdown_text(relation.from_)} "
            f"{_escape_markdown_text(relation.type.value)} "
            f"{_escape_markdown_text(relation.to)}"
        )
        if relation.rationale:
            lines.append(f"  - Rationale: {_escape_markdown_text(relation.rationale)}")
    return lines


def _render_external_references(payload: SpecificationPayload) -> list[str]:
    lines = ["## External References", ""]
    if not payload.external_references:
        return [*lines, "- None", ""]
    for reference in sorted(
        payload.external_references,
        key=lambda reference: reference.id,
    ):
        lines.extend(
            [
                f"### {_escape_markdown_text(reference.id)} - "
                f"{_escape_markdown_text(reference.title)}",
                "",
                _line("URL", reference.url),
                _line("Summary", reference.summary),
                "",
            ]
        )
    return lines


def render_markdown(payload: SpecificationPayload) -> str:
    """Render every semantic and provenance field for deterministic review."""
    lines = [
        f"# {_escape_markdown_text(payload.title)}",
        "",
        _line("Schema", payload.schema_version),
        _line("Artifact id", payload.artifact_id),
        _line("Renderer", RENDERER_VERSION),
        "",
        "## Summary",
        "",
        _escape_markdown_text(payload.summary),
        "",
        "## Problem Statement",
        "",
        _escape_markdown_text(payload.problem_statement),
        "",
    ]
    lines.extend(_render_terms(payload))
    lines.extend(["## Items", ""])
    for item in sorted(payload.items, key=lambda item: item.id):
        lines.extend(_render_item(item))
    lines.extend([*_render_relations(payload), ""])
    lines.extend(_render_external_references(payload))
    return "\n".join(lines)


__all__ = [
    "RENDERER_VERSION",
    "SCHEMA_VERSION",
    "ControlledTerm",
    "ControlledTermScope",
    "ExternalReference",
    "RelationType",
    "RequirementLevel",
    "SourceNote",
    "SourceNoteKind",
    "SpecItemType",
    "SpecificationItem",
    "SpecificationPayload",
    "SpecificationRelation",
    "VerificationMethod",
    "canonical_spec_hash",
    "canonical_spec_json",
    "render_markdown",
    "rendered_markdown_hash",
]
