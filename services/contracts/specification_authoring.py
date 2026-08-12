# services/contracts/specification_authoring.py
"""Closed host-to-model contract for direct Specification authoring."""

from __future__ import annotations

import hashlib
from importlib import import_module
from typing import TYPE_CHECKING, Annotated, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from services.specs.candidate_contract import (
    CandidateSourceKind,
    CandidateSourceManifestEntry,
    Fingerprint,
    StableIdReplacement,
)

if TYPE_CHECKING:
    from utils.agileforge_spec_profile_v2 import SpecificationPayload

_SPECIFICATION_PAYLOAD_MODEL = cast(
    "type[BaseModel]",
    import_module("utils.agileforge_spec_profile_v2").SpecificationPayload,
)

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]

SPECIFICATION_AUTHOR_VERSION: str = "2.0.0"
SPECIFICATION_AUTHOR_PROMPT_VERSION: str = "agileforge.to-spec.prompt.v2"
SPECIFICATION_AUTHOR_PROMPT_HASH: str = "sha256:" + hashlib.sha256(
    SPECIFICATION_AUTHOR_PROMPT_VERSION.encode("utf-8")
).hexdigest()


class _FrozenClosedModel(BaseModel):
    """Immutable DTO base that rejects undeclared model-controlled fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class AcceptedVisionContext(_FrozenClosedModel):
    """Exact accepted Vision supplied as read-only authoring context."""

    artifact_id: Annotated[int, Field(gt=0)]
    fingerprint: Fingerprint
    statement: Annotated[str, Field(min_length=1)]
    components: JsonObject


class AcceptedProductGoalContext(_FrozenClosedModel):
    """Exact active accepted Product Goal supplied to to-spec."""

    artifact_id: Annotated[int, Field(gt=0)]
    fingerprint: Fingerprint
    statement: Annotated[str, Field(min_length=1)]


class SpecificationSourceContext(_FrozenClosedModel):
    """One host-owned source manifest row plus its model-readable content."""

    source_id: Annotated[str, Field(pattern=r"^SRC\.[a-z0-9][a-z0-9.-]{1,96}$")]
    kind: CandidateSourceKind
    fingerprint: Fingerprint
    content: JsonObject


class BaseSpecificationContext(_FrozenClosedModel):
    """Pinned accepted base for a full-result amendment."""

    spec_version_id: Annotated[int, Field(gt=0)]
    payload_fingerprint: Fingerprint
    payload: SpecificationPayload


class PriorCandidateContext(_FrozenClosedModel):
    """Exact rejected/feedback candidate used only for immutable revision context."""

    candidate_fingerprint: Fingerprint
    payload: SpecificationPayload
    decision: Literal["rejected", "feedback"]
    rationale: Annotated[str, Field(min_length=1)]
    base_specification_id: Annotated[int, Field(gt=0)] | None = None
    base_payload_fingerprint: Fingerprint | None = None

    @model_validator(mode="after")
    def validate_base_pair(self) -> Self:
        """Keep prior amendment base identity paired."""
        if (self.base_specification_id is None) != (
            self.base_payload_fingerprint is None
        ):
            message = "prior candidate base identity must be paired"
            raise ValueError(message)
        return self


class SpecificationAuthoringInput(_FrozenClosedModel):
    """Complete host-built input for the sole semantic authoring call."""

    schema_version: Literal["agileforge.spec-authoring-input.v2"] = (
        "agileforge.spec-authoring-input.v2"
    )
    project_id: Annotated[int, Field(gt=0)]
    project_name: Annotated[str, Field(min_length=1)]
    operation: Literal["initial", "revision", "amendment"]
    accepted_vision: AcceptedVisionContext
    accepted_product_goal: AcceptedProductGoalContext
    source_manifest: tuple[CandidateSourceManifestEntry, ...]
    source_context: tuple[SpecificationSourceContext, ...]
    base_specification: BaseSpecificationContext | None = None
    prior_candidate: PriorCandidateContext | None = None

    @field_validator("source_manifest")
    @classmethod
    def canonicalize_manifest(
        cls,
        value: tuple[CandidateSourceManifestEntry, ...],
    ) -> tuple[CandidateSourceManifestEntry, ...]:
        """Canonicalize the set-like source manifest by stable source ID."""
        return tuple(sorted(value, key=lambda item: item.source_id))

    @field_validator("source_context")
    @classmethod
    def canonicalize_source_context(
        cls,
        value: tuple[SpecificationSourceContext, ...],
    ) -> tuple[SpecificationSourceContext, ...]:
        """Keep source content in the exact manifest order."""
        return tuple(sorted(value, key=lambda item: item.source_id))

    @model_validator(mode="after")
    def validate_composition_and_sources(self) -> Self:
        """Bind operation semantics and every source byte to one manifest row."""
        if self.operation == "initial" and (
            self.base_specification is not None or self.prior_candidate is not None
        ):
            message = "initial authoring cannot include a base or prior candidate"
            raise ValueError(message)
        if self.operation == "amendment" and (
            self.base_specification is None or self.prior_candidate is not None
        ):
            message = "amendment authoring requires only an accepted base"
            raise ValueError(message)
        if self.operation == "revision" and self.prior_candidate is None:
            message = "revision authoring requires one terminal prior candidate"
            raise ValueError(message)
        if self.prior_candidate is not None:
            prior_base = (
                self.prior_candidate.base_specification_id,
                self.prior_candidate.base_payload_fingerprint,
            )
            current_base = (
                None
                if self.base_specification is None
                else self.base_specification.spec_version_id,
                None
                if self.base_specification is None
                else self.base_specification.payload_fingerprint,
            )
            if prior_base != current_base:
                message = "revision base does not match the prior candidate"
                raise ValueError(message)
        manifest = {
            item.source_id: (item.kind, item.fingerprint)
            for item in self.source_manifest
        }
        contexts = {
            item.source_id: (item.kind, item.fingerprint)
            for item in self.source_context
        }
        if len(manifest) != len(self.source_manifest) or manifest != contexts:
            message = "source context must exactly match the unique source manifest"
            raise ValueError(message)
        expected = {
            f"SRC.vision.{self.accepted_vision.artifact_id}": (
                CandidateSourceKind.VISION,
                self.accepted_vision.fingerprint,
            ),
            f"SRC.product-goal.{self.accepted_product_goal.artifact_id}": (
                CandidateSourceKind.PRODUCT_GOAL,
                self.accepted_product_goal.fingerprint,
            ),
        }
        if any(
            manifest.get(source_id) != identity
            for source_id, identity in expected.items()
        ):
            message = (
                "source manifest must include exact accepted Vision and Product Goal"
            )
            raise ValueError(message)
        return self


class SpecificationAuthoringOutput(_FrozenClosedModel):
    """Only semantic bytes and explicit amendment declarations are model-owned."""

    payload: SpecificationPayload
    removal_justifications: dict[str, Annotated[str, Field(min_length=1)]] = Field(
        default_factory=dict
    )
    stable_id_replacements: tuple[StableIdReplacement, ...] = ()


for _contract in (
    BaseSpecificationContext,
    PriorCandidateContext,
    SpecificationAuthoringInput,
    SpecificationAuthoringOutput,
):
    _contract.model_rebuild(
        _types_namespace={"SpecificationPayload": _SPECIFICATION_PAYLOAD_MODEL}
    )


__all__ = [
    "SPECIFICATION_AUTHOR_PROMPT_HASH",
    "SPECIFICATION_AUTHOR_PROMPT_VERSION",
    "SPECIFICATION_AUTHOR_VERSION",
    "AcceptedProductGoalContext",
    "AcceptedVisionContext",
    "BaseSpecificationContext",
    "PriorCandidateContext",
    "SpecificationAuthoringInput",
    "SpecificationAuthoringOutput",
    "SpecificationSourceContext",
]
