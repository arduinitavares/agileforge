"""Prepare authority compiler input from the graph-selected durable spec."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import TypeAdapter
from sqlmodel import Session, select

from models.product_definition import SpecificationCandidate
from models.specs import SpecRegistry
from services.contracts.authority_input_v2 import build_authority_input_v2
from services.node_attempt_replay import (
    DurableNodeAttemptReplayService,
    NodeAttemptReplayQuery,
)
from services.specs.candidate_contract import load_candidate_contract
from utils.spec_schemas import SpecAuthorityCompilerInput
from workflow.contracts import FactReference, JsonObject, NodeDecision, TransitionResult

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

_JSON_OBJECT = TypeAdapter(JsonObject)


class AuthorityCompilationInputError(RuntimeError):
    """Raised when durable facts cannot produce exact compiler input."""


@dataclass(frozen=True)
class AuthorityCompilationInputService:
    """Bind one compile attempt to its exact approved specification content."""

    engine: Engine

    def replay(self, query: NodeAttemptReplayQuery) -> TransitionResult | None:
        """Recover a prior command before reading the registered spec."""
        return DurableNodeAttemptReplayService(engine=self.engine).replay(query)

    def build(
        self,
        *,
        project_id: int,
        decision: NodeDecision,
        compiler_model: str,
    ) -> JsonObject:
        """Return the normalized recipe payload for one exact compile decision."""
        reference = _spec_reference(decision)
        spec_version_id = _parse_spec_version_id(reference.fact_id)
        with Session(self.engine) as session:
            spec = session.get(SpecRegistry, spec_version_id)
            if (
                spec is None
                or spec.project_id != project_id
                or spec.status != "approved"
                or spec.spec_hash != reference.fingerprint
            ):
                message = "The compile decision does not match an approved spec."
                raise AuthorityCompilationInputError(message)

            expected_instance_key = f"spec:{spec_version_id}:{spec.spec_hash}"
            if decision.instance_key != expected_instance_key:
                message = "The compile decision instance does not match its spec."
                raise AuthorityCompilationInputError(message)

            candidate = session.exec(
                select(SpecificationCandidate).where(
                    SpecificationCandidate.project_id == project_id,
                    SpecificationCandidate.specification_candidate_id
                    == spec.source_specification_candidate_id,
                    SpecificationCandidate.candidate_fingerprint
                    == spec.source_specification_candidate_fingerprint,
                    SpecificationCandidate.payload_fingerprint == spec.spec_hash,
                )
            ).one_or_none()
            if candidate is None:
                message = (
                    "The approved spec source candidate does not match exact identity."
                )
                raise AuthorityCompilationInputError(message)
            try:
                payload, envelope = load_candidate_contract(
                    candidate.canonical_envelope_json,
                    expected_candidate_fingerprint=candidate.candidate_fingerprint,
                )
            except (TypeError, ValueError) as error:
                message = "The approved spec candidate envelope is invalid."
                raise AuthorityCompilationInputError(message) from error

            if not (
                candidate.vision_artifact_id == spec.source_vision_artifact_id
                == envelope.accepted_vision_id
                and candidate.vision_fingerprint == spec.source_vision_fingerprint
                == envelope.accepted_vision_fingerprint
                and candidate.product_goal_artifact_id
                == spec.source_product_goal_artifact_id
                == envelope.accepted_product_goal_id
                and candidate.product_goal_fingerprint
                == spec.source_product_goal_fingerprint
                == envelope.accepted_product_goal_fingerprint
                and candidate.payload_fingerprint == spec.spec_hash
                == envelope.payload_fingerprint
            ):
                message = "The approved spec source candidate has mismatched lineage."
                raise AuthorityCompilationInputError(message)
            expected_spec_hash = spec.spec_hash

        compiler_input = SpecAuthorityCompilerInput(
            authority_input=build_authority_input_v2(payload),
            project_id=project_id,
            spec_version_id=spec_version_id,
            specification_fingerprint=expected_spec_hash,
        )
        return _JSON_OBJECT.validate_python(
            {
                "project_id": project_id,
                "spec_version_id": spec_version_id,
                "expected_spec_hash": expected_spec_hash,
                "compiler_model": compiler_model,
                "compiler_input": compiler_input.model_dump(mode="json"),
            }
        )


def _spec_reference(decision: NodeDecision) -> FactReference:
    references = tuple(
        reference
        for reference in decision.fact_references
        if reference.fact_type == "spec_version"
    )
    if len(references) != 1:
        message = "Authority compilation requires exactly one spec reference."
        raise AuthorityCompilationInputError(message)
    return references[0]


def _parse_spec_version_id(value: str) -> int:
    try:
        return int(value)
    except ValueError as error:
        message = "The authority spec reference is not an integer identity."
        raise AuthorityCompilationInputError(message) from error


__all__ = [
    "AuthorityCompilationInputError",
    "AuthorityCompilationInputService",
]
