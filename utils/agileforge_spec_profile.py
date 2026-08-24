"""AgileForge structured specification profile v1."""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION: str = "agileforge.spec.v1"
MARKDOWN_PROFILE: str = "agileforge.spec_markdown.v1"
JSON_SCHEMA_ID: str = "https://agileforge.local/schemas/agileforge.spec.v1.json"
_ITEM_ID_RE: re.Pattern[str] = re.compile(
    r"^(GOAL|NON_GOAL|REQ|QUALITY|CONSTRAINT|INTERFACE|DATA|DECISION|"
    r"ASSUMPTION|RISK|EXAMPLE|OPEN_QUESTION)\.[a-z0-9][a-z0-9.-]{1,96}$"
)
_MARKDOWN_LEADING_RE: re.Pattern[str] = re.compile(
    r"^(\s*)((?:[#\-*+>])|(?:\d+\.)(?=\s|$))"
)


class AgileForgeSpecType(StrEnum):
    """Supported typed item categories in AgileForge spec profile v1."""

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


class AgileForgeSpecStatus(StrEnum):
    """Lifecycle states for spec artifacts and items."""

    DRAFT = "draft"
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    CHANGED = "changed"
    DEFERRED = "deferred"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class RequirementLevel(StrEnum):
    """Controlled requirement levels."""

    MUST = "MUST"
    MUST_NOT = "MUST_NOT"
    SHOULD = "SHOULD"
    MAY = "MAY"
    INFORMATIVE = "INFORMATIVE"


class VerificationMethod(StrEnum):
    """Supported verification methods."""

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
    """Supported relation edge types."""

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


class SpecRendering(BaseModel):
    """Rendering metadata for a deterministic Markdown view."""

    model_config = ConfigDict(extra="forbid")

    markdown_profile: Literal["agileforge.spec_markdown.v1"] = MARKDOWN_PROFILE
    rendered_markdown_sha256: str | None = None


class ControlledTerm(BaseModel):
    """A project-local term definition."""

    model_config = ConfigDict(extra="forbid")

    term: Annotated[str, Field(min_length=1)]
    definition: Annotated[str, Field(min_length=1)]
    scope: Literal["artifact", "domain", "project"] = "artifact"


class ExternalReference(BaseModel):
    """External reference metadata. Linked content is not authority by itself."""

    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, Field(pattern=r"^EXT\.[a-z0-9][a-z0-9.-]{1,96}$")]
    title: Annotated[str, Field(min_length=1)]
    url: str | None = None
    summary: Annotated[str, Field(min_length=1)]


class SourceNote(BaseModel):
    """Source context captured by the spec generator."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["user_note", "interview", "import", "external_summary"]
    text: Annotated[str, Field(min_length=1)]
    external_ref_id: str | None = None


class SpecItem(BaseModel):
    """One typed AgileForge spec item."""

    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, Field(pattern=_ITEM_ID_RE.pattern)]
    type: AgileForgeSpecType
    status: AgileForgeSpecStatus
    title: Annotated[str, Field(min_length=1)]
    statement: Annotated[str, Field(min_length=1)]
    level: RequirementLevel | None = None
    rationale: str | None = None
    verification: VerificationMethod | None = None
    acceptance: list[Annotated[str, Field(min_length=1)]] = Field(default_factory=list)
    tags: list[Annotated[str, Field(min_length=1)]] = Field(default_factory=list)
    source_notes: list[SourceNote] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_item_contract(self) -> Self:
        """Enforce ID/type consistency and normative evidence."""
        prefix = self.id.split(".", maxsplit=1)[0]
        if prefix != self.type.value:
            message = "item id prefix must match item type"
            raise ValueError(message)
        normative_types: set[AgileForgeSpecType] = {
            AgileForgeSpecType.REQ,
            AgileForgeSpecType.QUALITY,
            AgileForgeSpecType.CONSTRAINT,
            AgileForgeSpecType.INTERFACE,
            AgileForgeSpecType.DATA,
        }
        if self.type in normative_types:
            missing: list[str] = []
            if self.level is None:
                missing.append("level")
            if self.verification is None:
                missing.append("verification")
            if not self.acceptance:
                missing.append("acceptance")
            if missing:
                raise ValueError(
                    f"normative item {self.id} missing required fields: "
                    + ", ".join(missing)
                )
        return self


class SpecRelation(BaseModel):
    """A first-class relation edge between spec items."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_: Annotated[str, Field(alias="from", pattern=_ITEM_ID_RE.pattern)]
    type: RelationType
    to: Annotated[str, Field(pattern=_ITEM_ID_RE.pattern)]
    rationale: str | None = None


class TechnicalSpecArtifact(BaseModel):
    """Canonical AgileForge spec profile v1 artifact."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_version: Literal["agileforge.spec.v1"] = SCHEMA_VERSION
    artifact_id: Annotated[str, Field(pattern=r"^SPEC\.[a-z0-9][a-z0-9.-]{1,96}$")]
    title: Annotated[str, Field(min_length=1)]
    status: AgileForgeSpecStatus
    version: Annotated[str, Field(min_length=1)]
    created_at: Annotated[str, Field(min_length=1)]
    updated_at: Annotated[str, Field(min_length=1)]
    summary: Annotated[str, Field(min_length=1)]
    problem_statement: Annotated[str, Field(min_length=1)]
    items: list[SpecItem]
    relations: list[SpecRelation] = Field(default_factory=list)
    controlled_terms: list[ControlledTerm] = Field(default_factory=list)
    external_references: list[ExternalReference] = Field(default_factory=list)
    rendering: SpecRendering = Field(default_factory=SpecRendering)

    @model_validator(mode="after")
    def validate_relations(self) -> Self:
        """Ensure cross-references only point at known profile objects."""
        seen_item_ids: set[str] = set()
        duplicate_ids: list[str] = []
        for item in self.items:
            if item.id in seen_item_ids:
                duplicate_ids.append(item.id)
            seen_item_ids.add(item.id)
        if duplicate_ids:
            message = f"duplicate item ids: {', '.join(sorted(duplicate_ids))}"
            raise ValueError(message)
        for relation in self.relations:
            for endpoint in (relation.from_, relation.to):
                if endpoint not in seen_item_ids:
                    message = f"unknown relation endpoint: {endpoint}"
                    raise ValueError(message)
        external_ids: set[str] = {
            reference.id for reference in self.external_references
        }
        for item in self.items:
            for note in item.source_notes:
                if note.external_ref_id and note.external_ref_id not in external_ids:
                    message = (
                        "unknown external reference endpoint: "
                        f"{note.external_ref_id}"
                    )
                    raise ValueError(message)
        return self


def canonical_spec_json(artifact: TechnicalSpecArtifact) -> str:
    """Return deterministic JSON for storage and hashing."""
    return json.dumps(
        artifact.model_dump(mode="json", by_alias=True),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def canonical_spec_hash(artifact: TechnicalSpecArtifact) -> str:
    """Return sha256 hash over canonical spec JSON bytes."""
    digest = hashlib.sha256(canonical_spec_json(artifact).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def rendered_markdown_hash(markdown: str) -> str:
    """Return sha256 hash over rendered Markdown bytes."""
    digest = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _escape_markdown_text(value: str) -> str:
    escaped: str = (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    return "\n".join(
        _MARKDOWN_LEADING_RE.sub(r"\1\\\2", line)
        for line in escaped.split("\n")
    )


def _line(label: str, value: str | None) -> str:
    return f"- {label}: {_escape_markdown_text(value) if value else '-'}"


def render_markdown(artifact: TechnicalSpecArtifact) -> str:
    """Return deterministic Markdown for the AgileForge spec artifact."""
    lines: list[str] = [
        f"# {_escape_markdown_text(artifact.title)}",
        "",
        _line("Schema", artifact.schema_version),
        _line("Artifact id", artifact.artifact_id),
        _line("Status", artifact.status.value),
        _line("Version", artifact.version),
        _line("Created", artifact.created_at),
        _line("Updated", artifact.updated_at),
        _line("Markdown profile", artifact.rendering.markdown_profile),
        "",
        "## Summary",
        "",
        _escape_markdown_text(artifact.summary),
        "",
        "## Problem Statement",
        "",
        _escape_markdown_text(artifact.problem_statement),
        "",
        "## Controlled Terms",
        "",
    ]

    controlled_terms: list[ControlledTerm] = sorted(
        artifact.controlled_terms,
        key=lambda term: term.term,
    )
    if controlled_terms:
        for term in controlled_terms:
            lines.extend(
                [
                    f"### {_escape_markdown_text(term.term)}",
                    "",
                    _line("Scope", term.scope),
                    _line("Definition", term.definition),
                    "",
                ]
            )
    else:
        lines.extend(["- None", ""])

    lines.extend(["## Items", ""])
    for item in sorted(artifact.items, key=lambda item: item.id):
        lines.extend(
            [
                f"### {_escape_markdown_text(item.id)} - "
                f"{_escape_markdown_text(item.title)}",
                "",
                _line("Type", item.type.value),
                _line("Status", item.status.value),
                _line("Level", item.level.value if item.level else None),
                _line(
                    "Verification",
                    item.verification.value if item.verification else None,
                ),
                _line(
                    "Tags",
                    ", ".join(_escape_markdown_text(tag) for tag in sorted(item.tags))
                    if item.tags
                    else None,
                ),
                "",
                "Statement:",
                "",
                _escape_markdown_text(item.statement),
                "",
            ]
        )
        if item.rationale:
            lines.extend(["Rationale:", "", _escape_markdown_text(item.rationale), ""])
        lines.extend(["Acceptance:", ""])
        if item.acceptance:
            lines.extend(
                f"- {_escape_markdown_text(acceptance)}"
                for acceptance in item.acceptance
            )
        else:
            lines.append("- None")
        lines.append("")

    lines.extend(["## Relations", ""])
    relations: list[SpecRelation] = sorted(
        artifact.relations,
        key=lambda relation: (relation.from_, relation.type.value, relation.to),
    )
    if relations:
        for relation in relations:
            lines.append(
                f"- {_escape_markdown_text(relation.from_)} "
                f"{_escape_markdown_text(relation.type.value)} "
                f"{_escape_markdown_text(relation.to)}"
            )
            if relation.rationale:
                lines.append(
                    f"  - Rationale: {_escape_markdown_text(relation.rationale)}"
                )
    else:
        lines.append("- None")

    lines.extend(
        [
            "",
            "<!-- agileforge-review-notes:start -->",
            "<!-- agileforge-review-notes:end -->",
            "",
        ]
    )
    return "\n".join(lines)


def export_agileforge_spec_schema() -> dict[str, Any]:
    """Return JSON Schema for the AgileForge spec profile."""
    schema: dict[str, Any] = TechnicalSpecArtifact.model_json_schema()
    schema["$id"] = JSON_SCHEMA_ID
    schema["additionalProperties"] = False
    return schema
