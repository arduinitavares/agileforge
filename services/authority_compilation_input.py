"""Prepare authority compiler input from the graph-selected durable spec."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import TypeAdapter
from sqlmodel import Session

from models.specs import SpecRegistry
from services.node_attempt_replay import (
    DurableNodeAttemptReplayService,
    NodeAttemptReplayQuery,
)
from services.specs.profile_content import (
    SpecContentNormalizationError,
    normalize_spec_content_for_registry,
)
from utils.spec_schemas import SpecAuthorityCompilerInput
from workflow.contracts import FactReference, JsonObject, NodeDecision, TransitionResult
from workflow.fingerprints import canonical_stored_json_hash

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
            try:
                normalized = normalize_spec_content_for_registry(spec.content)
            except SpecContentNormalizationError as error:
                message = "The registered spec content is not valid agileforge.spec.v1."
                raise AuthorityCompilationInputError(message) from error
            try:
                registered_content_hash = canonical_stored_json_hash(spec.content)
            except (TypeError, ValueError) as error:
                message = "The registered spec content does not match its stored hash."
                raise AuthorityCompilationInputError(message) from error
            if registered_content_hash != spec.spec_hash:
                message = "The registered spec content does not match its stored hash."
                raise AuthorityCompilationInputError(message)
            expected_spec_hash = spec.spec_hash

        compiler_input = SpecAuthorityCompilerInput(
            spec_source=normalized.content,
            spec_content_ref=None,
            domain_hint=None,
            project_id=project_id,
            spec_version_id=spec_version_id,
        )
        return _JSON_OBJECT.validate_python(
            {
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
