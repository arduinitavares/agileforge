# services/contracts/specification_authoring.py
"""Closed host-to-model contract for Specification structuring."""

from __future__ import annotations

import hashlib
from importlib import import_module
from typing import TYPE_CHECKING, Annotated, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from services.contracts.specification_source import (
    SPECIFICATION_SOURCE_CONTEXT_ID,
    SPECIFICATION_SOURCE_PRIMARY_ID,
    SpecificationRepositoryRevision,
    source_bundle_fingerprint,
    specification_source_adr_id,
)
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

SPECIFICATION_STRUCTURER_VERSION: str = "1.0.0"
SPECIFICATION_STRUCTURER_PROMPT_VERSION: str = (
    "agileforge.specification-structurer.prompt.v1"
)
SPECIFICATION_STRUCTURER_PROMPT_HASH: str = (
    "sha256:fec7c251132af921dd721e5e3cdea758eef95ce0437bfd85d2f24dad00c70e21"
)
SPECIFICATION_VISION_SOURCE_ID: str = "SRC.vision.accepted"
SPECIFICATION_PRODUCT_GOAL_SOURCE_ID: str = "SRC.product-goal.active"


def compute_specification_structurer_prompt_hash(prompt_text: str) -> str:
    """Hash normalized packaged structuring instructions for provenance."""
    normalized = " ".join(prompt_text.strip().lower().split())
    return "sha256:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class _FrozenClosedModel(BaseModel):
    """Immutable DTO base that rejects undeclared model-controlled fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class AcceptedVisionContext(_FrozenClosedModel):
    """Exact accepted Vision supplied as read-only structuring context."""

    artifact_id: Annotated[int, Field(gt=0)]
    fingerprint: Fingerprint
    statement: Annotated[str, Field(min_length=1)]
    components: JsonObject
    component_basis: tuple[JsonObject, ...] = ()
    assumptions: tuple[JsonObject, ...] = ()
    conflicts: tuple[JsonObject, ...] = ()


class AcceptedProductGoalContext(_FrozenClosedModel):
    """Exact active accepted Product Goal supplied to the structurer."""

    artifact_id: Annotated[int, Field(gt=0)]
    fingerprint: Fingerprint
    statement: Annotated[str, Field(min_length=1)]


class SpecificationStructuringDocument(_FrozenClosedModel):
    """One registered document projected as exact provider-readable UTF-8 prose."""

    source_id: Annotated[str, Field(pattern=r"^SRC\.[a-z0-9][a-z0-9.-]{1,126}$")]
    relative_path: Annotated[str, Field(min_length=1)]
    text: str
    byte_length: Annotated[int, Field(ge=0)]
    content_fingerprint: Fingerprint

    @model_validator(mode="after")
    def validate_exact_text(self) -> Self:
        """Bind model-visible prose to its exact registered UTF-8 bytes."""
        raw = self.text.encode("utf-8")
        expected = "sha256:" + hashlib.sha256(raw).hexdigest()
        if len(raw) != self.byte_length or expected != self.content_fingerprint:
            message = "structuring document text must match exact UTF-8 bytes"
            raise ValueError(message)
        return self


class SpecificationStructuringContextCapture(_FrozenClosedModel):
    """Expose the registered root Context state without inventing empty prose."""

    state: Literal["absent", "present"]
    document: SpecificationStructuringDocument | None = None

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        """Keep absent and present Context states explicit and exact."""
        if self.state == "absent" and self.document is not None:
            message = "absent structuring context cannot contain a document"
            raise ValueError(message)
        if self.state == "present" and self.document is None:
            message = "present structuring context requires a document"
            raise ValueError(message)
        if self.document is not None and (
            self.document.source_id != SPECIFICATION_SOURCE_CONTEXT_ID
            or self.document.relative_path != "CONTEXT.md"
        ):
            message = "structuring context must be the registered root CONTEXT.md"
            raise ValueError(message)
        return self


class RegisteredRepositoryEvidence(_FrozenClosedModel):
    """Exact durable binding evidence paired with the portable source revision."""

    repository_binding_id: Annotated[int, Field(gt=0)]
    binding_fingerprint: Fingerprint
    head_sha: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    branch_name: str | None
    detached_head: bool
    dirty: bool
    status_fingerprint: Fingerprint
    status_entries: tuple[JsonObject, ...]
    remotes: tuple[str, ...]
    warnings: tuple[JsonObject, ...]
    probe_version: Annotated[str, Field(min_length=1)]


class RegisteredSpecificationSource(_FrozenClosedModel):
    """Exact registered bundle projected for model consumption and provenance."""

    specification_source_id: Annotated[int, Field(gt=0)]
    source_fingerprint: Fingerprint
    producer_capability: Literal["to-spec"]
    preparation_capability: Literal["grill-with-docs"]
    source: SpecificationStructuringDocument
    context: SpecificationStructuringContextCapture
    adrs: tuple[SpecificationStructuringDocument, ...] = ()
    repository_revision: SpecificationRepositoryRevision
    repository_evidence: RegisteredRepositoryEvidence
    accepted_vision_fingerprint: Fingerprint
    accepted_product_goal_fingerprint: Fingerprint

    @field_validator("adrs")
    @classmethod
    def canonicalize_adrs(
        cls,
        value: tuple[SpecificationStructuringDocument, ...],
    ) -> tuple[SpecificationStructuringDocument, ...]:
        """Treat ADR selection as a canonical path-keyed set."""
        return tuple(sorted(value, key=lambda item: item.relative_path))

    @model_validator(mode="after")
    def validate_registered_bundle(self) -> Self:
        """Rebuild and fingerprint the canonical exact-byte registration."""
        from services.contracts.specification_source import (  # noqa: PLC0415
            SpecificationContextCapture,
            SpecificationSourceBundle,
            SpecificationSourceDocument,
        )

        def source_document(
            document: SpecificationStructuringDocument,
        ) -> SpecificationSourceDocument:
            import base64  # noqa: PLC0415

            return SpecificationSourceDocument(
                source_id=document.source_id,
                relative_path=document.relative_path,
                content_base64=base64.b64encode(document.text.encode("utf-8")).decode(
                    "ascii"
                ),
                byte_length=document.byte_length,
                content_fingerprint=document.content_fingerprint,
            )

        if self.source.source_id != SPECIFICATION_SOURCE_PRIMARY_ID:
            message = "registered structuring source must use the stable primary ID"
            raise ValueError(message)
        if self.context.document is None:
            context = SpecificationContextCapture(state="absent")
        else:
            context = SpecificationContextCapture(
                state="present",
                document=source_document(self.context.document),
            )
        bundle = SpecificationSourceBundle(
            producer_capability=self.producer_capability,
            preparation_capability=self.preparation_capability,
            source=source_document(self.source),
            context=context,
            adrs=tuple(source_document(item) for item in self.adrs),
            repository_revision=self.repository_revision,
            accepted_vision_fingerprint=self.accepted_vision_fingerprint,
            accepted_product_goal_fingerprint=(self.accepted_product_goal_fingerprint),
        )
        if source_bundle_fingerprint(bundle) != self.source_fingerprint:
            message = "registered structuring source fingerprint changed"
            raise ValueError(message)
        if (
            self.repository_evidence.head_sha,
            self.repository_evidence.dirty,
            self.repository_evidence.status_fingerprint,
        ) != (
            self.repository_revision.head_sha,
            self.repository_revision.dirty,
            self.repository_revision.status_fingerprint,
        ):
            message = "registered repository evidence must match source revision"
            raise ValueError(message)
        expected_adr_ids = {
            specification_source_adr_id(document.relative_path)
            for document in self.adrs
        }
        if {document.source_id for document in self.adrs} != expected_adr_ids:
            message = "registered ADR source IDs changed"
            raise ValueError(message)
        return self


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


def _expected_source_manifest(
    contract: SpecificationStructuringInput,
) -> dict[str, tuple[CandidateSourceKind, str]]:
    registered = contract.registered_source
    expected: dict[str, tuple[CandidateSourceKind, str]] = {
        SPECIFICATION_VISION_SOURCE_ID: (
            CandidateSourceKind.VISION,
            contract.accepted_vision.fingerprint,
        ),
        SPECIFICATION_PRODUCT_GOAL_SOURCE_ID: (
            CandidateSourceKind.PRODUCT_GOAL,
            contract.accepted_product_goal.fingerprint,
        ),
        registered.source.source_id: (
            CandidateSourceKind.EXTERNAL,
            registered.source.content_fingerprint,
        ),
    }
    if registered.context.document is not None:
        context = registered.context.document
        expected[context.source_id] = (
            CandidateSourceKind.REPOSITORY,
            context.content_fingerprint,
        )
    expected.update(
        {
            adr.source_id: (
                CandidateSourceKind.REPOSITORY,
                adr.content_fingerprint,
            )
            for adr in registered.adrs
        }
    )
    return expected


class SpecificationStructuringInput(_FrozenClosedModel):
    """Complete host-built input for the sole semantic structuring call."""

    schema_version: Literal["agileforge.spec-structuring-input.v1"] = (
        "agileforge.spec-structuring-input.v1"
    )
    project_id: Annotated[int, Field(gt=0)]
    project_name: Annotated[str, Field(min_length=1)]
    operation: Literal["initial", "revision", "amendment"]
    accepted_vision: AcceptedVisionContext
    accepted_product_goal: AcceptedProductGoalContext
    registered_source: RegisteredSpecificationSource
    source_manifest: tuple[CandidateSourceManifestEntry, ...]
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

    @model_validator(mode="after")
    def validate_composition_and_sources(self) -> Self:
        """Bind composition and all provider prose to one registered source."""
        if self.operation == "initial" and (
            self.base_specification is not None or self.prior_candidate is not None
        ):
            message = "initial structuring cannot include a base or prior candidate"
            raise ValueError(message)
        if self.operation == "amendment" and (
            self.base_specification is None or self.prior_candidate is not None
        ):
            message = "amendment structuring requires only an accepted base"
            raise ValueError(message)
        if self.operation == "revision" and self.prior_candidate is None:
            message = "revision structuring requires one terminal prior candidate"
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
        if (
            self.registered_source.accepted_vision_fingerprint
            != self.accepted_vision.fingerprint
            or self.registered_source.accepted_product_goal_fingerprint
            != self.accepted_product_goal.fingerprint
        ):
            message = "registered source does not match accepted lineage"
            raise ValueError(message)
        manifest = {
            item.source_id: (item.kind, item.fingerprint)
            for item in self.source_manifest
        }
        if len(manifest) != len(self.source_manifest) or manifest != (
            _expected_source_manifest(self)
        ):
            message = (
                "source manifest must exactly match the registered source manifest"
            )
            raise ValueError(message)
        return self


def specification_structuring_input_fingerprint(
    contract: SpecificationStructuringInput,
) -> str:
    """Hash model-visible semantic input without host database row identities."""
    data = contract.model_dump(mode="json")
    data.pop("project_id")
    accepted_vision = data["accepted_vision"]
    accepted_goal = data["accepted_product_goal"]
    registered = data["registered_source"]
    if not all(
        isinstance(item, dict) for item in (accepted_vision, accepted_goal, registered)
    ):
        message = "Specification structuring lineage must be objects."
        raise TypeError(message)
    accepted_vision.pop("artifact_id")
    accepted_goal.pop("artifact_id")
    registered.pop("specification_source_id")
    repository_evidence = registered["repository_evidence"]
    if not isinstance(repository_evidence, dict):
        message = "Specification structuring repository evidence must be an object."
        raise TypeError(message)
    repository_evidence.pop("repository_binding_id")
    repository_evidence.pop("binding_fingerprint")
    base = data.get("base_specification")
    if isinstance(base, dict):
        base.pop("spec_version_id")
    prior = data.get("prior_candidate")
    if isinstance(prior, dict):
        prior.pop("base_specification_id")
    return canonical_hash(data)


def specification_structuring_fact_fingerprint(
    contract: SpecificationStructuringInput,
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
            "registered_source_fingerprint": (
                contract.registered_source.source_fingerprint
            ),
            "base_payload_fingerprint": (
                None if base is None else base.payload_fingerprint
            ),
            "prior_candidate_fingerprint": (
                None if prior is None else prior.candidate_fingerprint
            ),
        }
    )


class SpecificationStructuringOutput(_FrozenClosedModel):
    """Only semantic bytes and explicit amendment declarations are model-owned."""

    payload: SpecificationPayload
    removal_justifications: dict[str, Annotated[str, Field(min_length=1)]] = Field(
        default_factory=dict
    )
    stable_id_replacements: tuple[StableIdReplacement, ...] = ()


for _contract in (
    BaseSpecificationContext,
    PriorCandidateContext,
    SpecificationStructuringInput,
    SpecificationStructuringOutput,
):
    _contract.model_rebuild(
        _types_namespace={"SpecificationPayload": _SPECIFICATION_PAYLOAD_MODEL}
    )


__all__ = [
    "SPECIFICATION_PRODUCT_GOAL_SOURCE_ID",
    "SPECIFICATION_STRUCTURER_PROMPT_HASH",
    "SPECIFICATION_STRUCTURER_PROMPT_VERSION",
    "SPECIFICATION_STRUCTURER_VERSION",
    "SPECIFICATION_VISION_SOURCE_ID",
    "AcceptedProductGoalContext",
    "AcceptedVisionContext",
    "BaseSpecificationContext",
    "PriorCandidateContext",
    "RegisteredRepositoryEvidence",
    "RegisteredSpecificationSource",
    "SpecificationStructuringContextCapture",
    "SpecificationStructuringDocument",
    "SpecificationStructuringInput",
    "SpecificationStructuringOutput",
    "compute_specification_structurer_prompt_hash",
    "specification_structuring_fact_fingerprint",
    "specification_structuring_input_fingerprint",
]
