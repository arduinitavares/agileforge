# services/contracts/specification_authoring.py
"""Closed host-to-model contract for direct Specification authoring."""

from __future__ import annotations

import hashlib
from importlib import import_module
from typing import TYPE_CHECKING, Annotated, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from services.contracts.vision_evidence import VisionEvidenceBundle
from services.specs.candidate_contract import (
    CandidateSourceKind,
    CandidateSourceManifestEntry,
    Fingerprint,
    StableIdReplacement,
)
from workflow.fingerprints import canonical_hash

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
SPECIFICATION_AUTHOR_PROMPT_HASH: str = (
    "sha256:ab4ec877a7fa25a38100820269c5aad25a476fb55d29cd51296123bd01dfe678"
)
SPECIFICATION_VISION_SOURCE_ID: str = "SRC.vision.accepted"
SPECIFICATION_PRODUCT_GOAL_SOURCE_ID: str = "SRC.product-goal.active"
SPECIFICATION_REPOSITORY_EVIDENCE_SOURCE_ID: str = (
    "SRC.repository-evidence.accepted-vision"
)
SPECIFICATION_ACTIVE_REPOSITORY_SOURCE_ID: str = "SRC.repository-context.active"


def compute_specification_author_prompt_hash(prompt_text: str) -> str:
    """Hash normalized packaged to-spec instructions for durable provenance."""
    normalized = " ".join(prompt_text.strip().lower().split())
    return "sha256:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


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


def _validate_current_repository_context(
    contexts: tuple[SpecificationSourceContext, ...],
) -> None:
    """Bind freshly collected repository bytes to their host fingerprint."""
    for context in contexts:
        if context.source_id != SPECIFICATION_ACTIVE_REPOSITORY_SOURCE_ID:
            continue
        evidence = VisionEvidenceBundle.model_validate(context.content)
        if (
            context.kind is not CandidateSourceKind.REPOSITORY
            or context.fingerprint != evidence.evidence_fingerprint
        ):
            message = "current repository source context fingerprint changed"
            raise ValueError(message)


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
            SPECIFICATION_VISION_SOURCE_ID: (
                CandidateSourceKind.VISION,
                self.accepted_vision.fingerprint,
            ),
            SPECIFICATION_PRODUCT_GOAL_SOURCE_ID: (
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
        _validate_current_repository_context(self.source_context)
        return self


def specification_authoring_input_fingerprint(
    contract: SpecificationAuthoringInput,
) -> str:
    """Hash model-visible semantic input without host database row identities."""
    data = contract.model_dump(mode="json")
    data.pop("project_id")
    accepted_vision = data["accepted_vision"]
    accepted_goal = data["accepted_product_goal"]
    if not isinstance(accepted_vision, dict) or not isinstance(accepted_goal, dict):
        message = "Specification authoring lineage must be objects."
        raise TypeError(message)
    accepted_vision.pop("artifact_id")
    accepted_goal.pop("artifact_id")
    base = data.get("base_specification")
    if isinstance(base, dict):
        base.pop("spec_version_id")
    prior = data.get("prior_candidate")
    if isinstance(prior, dict):
        prior.pop("base_specification_id")
    return canonical_hash(data)


def specification_authoring_fact_fingerprint(
    contract: SpecificationAuthoringInput,
) -> str:
    """Hash portable accepted lineage facts that authorize one candidate."""
    base = contract.base_specification
    prior = contract.prior_candidate
    return canonical_hash(
        {
            "operation": contract.operation,
            "accepted_vision_fingerprint": contract.accepted_vision.fingerprint,
            "accepted_product_goal_fingerprint": (
                contract.accepted_product_goal.fingerprint
            ),
            "base_payload_fingerprint": (
                None if base is None else base.payload_fingerprint
            ),
            "prior_candidate_fingerprint": (
                None if prior is None else prior.candidate_fingerprint
            ),
        }
    )


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
    "SPECIFICATION_ACTIVE_REPOSITORY_SOURCE_ID",
    "SPECIFICATION_AUTHOR_PROMPT_HASH",
    "SPECIFICATION_AUTHOR_PROMPT_VERSION",
    "SPECIFICATION_AUTHOR_VERSION",
    "SPECIFICATION_PRODUCT_GOAL_SOURCE_ID",
    "SPECIFICATION_REPOSITORY_EVIDENCE_SOURCE_ID",
    "SPECIFICATION_VISION_SOURCE_ID",
    "AcceptedProductGoalContext",
    "AcceptedVisionContext",
    "BaseSpecificationContext",
    "PriorCandidateContext",
    "SpecificationAuthoringInput",
    "SpecificationAuthoringOutput",
    "SpecificationSourceContext",
    "compute_specification_author_prompt_hash",
    "specification_authoring_fact_fingerprint",
    "specification_authoring_input_fingerprint",
]
