"""Story-validation evidence schemas."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 - Pydantic resolves this at runtime.
from typing import Annotated, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from services.contracts.specification_validation import (  # noqa: TC001
    StorySpecificationFinding,
)

_STRUCTURAL_VALIDATION_CODES: Final[tuple[str, ...]] = (
    "STORY_ACCEPTANCE_INVALID",
    "STORY_ITEM_BINDING_INVALID",
    "SPECIFICATION_BINDING_INVALID",
    "SPEC_ITEM_REFERENCES_INVALID",
    "STORY_STATEMENT_INVALID",
    "ACCEPTANCE_CRITERIA_INVALID",
)


class StructuralValidationFailure(BaseModel):
    """One blocking finding from the closed structural rule matrix."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: Literal[
        "STORY_ACCEPTANCE_INVALID",
        "STORY_ITEM_BINDING_INVALID",
        "SPECIFICATION_BINDING_INVALID",
        "SPEC_ITEM_REFERENCES_INVALID",
        "STORY_STATEMENT_INVALID",
        "ACCEPTANCE_CRITERIA_INVALID",
    ]
    message: Annotated[str, Field(min_length=1, max_length=2000)]


class ValidationEvidence(BaseModel):
    """Complete canonical v3 snapshot for one explicit validation action."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["agileforge.story-validation-evidence.v3"]
    project_id: Annotated[int, Field(gt=0)]
    story_id: Annotated[int, Field(gt=0)]
    source_story_artifact_id: Annotated[int, Field(gt=0)]
    source_story_artifact_fingerprint: Annotated[
        str, Field(pattern=r"^sha256:[0-9a-f]{64}$")
    ]
    source_story_item_id: Annotated[str, Field(min_length=1)]
    source_story_item_fingerprint: Annotated[
        str, Field(pattern=r"^sha256:[0-9a-f]{64}$")
    ]
    source_backlog_artifact_id: Annotated[int, Field(gt=0)]
    source_backlog_artifact_fingerprint: Annotated[
        str, Field(pattern=r"^sha256:[0-9a-f]{64}$")
    ]
    source_backlog_item_id: Annotated[str, Field(min_length=1)]
    spec_version_id: Annotated[int, Field(gt=0)]
    spec_hash: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    validated_at: datetime
    story_validation_input_fingerprint: Annotated[
        str, Field(pattern=r"^sha256:[0-9a-f]{64}$")
    ]
    validator_version: Annotated[str, Field(min_length=1)]
    mode: Literal["structural", "hybrid"]
    structurally_eligible: bool
    structural_failures: tuple[StructuralValidationFailure, ...]
    structural_warnings: tuple[()] = ()
    semantic_review_state: Literal["not_requested", "valid", "invalid"]
    semantic_findings: tuple[StorySpecificationFinding, ...]
    referenced_spec_item_ids: tuple[str, ...]

    @model_validator(mode="after")
    def validate_closed_state(self) -> Self:
        """Require one internally consistent, deterministic readiness snapshot."""
        code_order = {
            code: index for index, code in enumerate(_STRUCTURAL_VALIDATION_CODES)
        }
        codes = tuple(item.code for item in self.structural_failures)
        if (
            len(codes) != len(set(codes))
            or tuple(sorted(codes, key=code_order.__getitem__)) != codes
        ):
            message = "structural failures must be unique and in canonical code order"
            raise ValueError(message)
        if self.structural_warnings:
            message = "v3 structural warnings must be empty"
            raise ValueError(message)
        if (
            tuple(sorted(set(self.referenced_spec_item_ids)))
            != self.referenced_spec_item_ids
            or not self.referenced_spec_item_ids
        ):
            message = (
                "referenced Specification item IDs must be nonempty, unique, sorted"
            )
            raise ValueError(message)
        finding_pairs = tuple(
            (item.spec_item_id, item.code) for item in self.semantic_findings
        )
        if tuple(sorted(finding_pairs)) != finding_pairs or len(finding_pairs) != len(
            set(finding_pairs)
        ):
            message = "semantic findings must be unique and canonically sorted"
            raise ValueError(message)
        if any(
            item.spec_item_id not in self.referenced_spec_item_ids
            for item in self.semantic_findings
        ):
            message = "semantic finding IDs must belong to derived references"
            raise ValueError(message)
        if self.mode == "structural" and (
            self.semantic_review_state != "not_requested" or self.semantic_findings
        ):
            message = "structural evidence cannot contain a semantic result"
            raise ValueError(message)
        if self.mode == "hybrid" and self.semantic_review_state == "not_requested":
            message = "hybrid evidence must contain a valid or invalid semantic state"
            raise ValueError(message)
        if self.semantic_review_state == "invalid" and self.semantic_findings:
            message = "invalid semantic output cannot persist provider findings"
            raise ValueError(message)
        if self.structurally_eligible != (not self.structural_failures):
            message = "structurally_eligible is inconsistent with structural failures"
            raise ValueError(message)
        return self
