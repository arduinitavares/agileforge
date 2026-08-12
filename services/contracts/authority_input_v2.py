# services/contracts/authority_input_v2.py
"""Deterministic Authority input derived from a v2 Specification payload."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from utils.agileforge_spec_profile_v2 import (
        ControlledTerm,
        SpecificationItem,
        SpecificationPayload,
        SpecificationRelation,
    )

_ELIGIBLE_NORMATIVE_TYPES: frozenset[str] = frozenset(
    {"REQ", "QUALITY", "CONSTRAINT", "INTERFACE", "DATA"}
)


class _FrozenClosedModel(BaseModel):
    """Base contract for immutable compiler-bound DTOs."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class AuthorityItemV2(_FrozenClosedModel):
    """One semantic item with all provenance prose removed."""

    id: Annotated[str, Field(min_length=1)]
    type: Annotated[str, Field(min_length=1)]
    title: Annotated[str, Field(min_length=1)]
    statement: Annotated[str, Field(min_length=1)]
    level: str | None = None
    rationale: str | None = None
    verification: str | None = None
    acceptance: tuple[Annotated[str, Field(min_length=1)], ...] = ()


class AuthorityRelationV2(_FrozenClosedModel):
    """One relation whose endpoints are both eligible invariant sources."""

    from_: Annotated[str, Field(alias="from", min_length=1)]
    type: Annotated[str, Field(min_length=1)]
    to: Annotated[str, Field(min_length=1)]
    rationale: str | None = None


class AuthorityControlledTermV2(_FrozenClosedModel):
    """Explicit interpretation context that cannot source an invariant."""

    term: Annotated[str, Field(min_length=1)]
    definition: Annotated[str, Field(min_length=1)]
    scope: Annotated[str, Field(min_length=1)]


class AuthorityInputV2(_FrozenClosedModel):
    """Closed deterministic input contract for Authority compilation."""

    schema_version: Literal["agileforge.authority_input.v2"] = (
        "agileforge.authority_input.v2"
    )
    artifact_id: Annotated[str, Field(min_length=1)]
    normative_items: tuple[AuthorityItemV2, ...]
    review_context: tuple[AuthorityItemV2, ...]
    normative_relations: tuple[AuthorityRelationV2, ...]
    controlled_terms: tuple[AuthorityControlledTermV2, ...]
    eligible_item_ids: tuple[Annotated[str, Field(min_length=1)], ...]
    authority_input_fingerprint: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]


def _enum_value(value: object) -> str | None:
    """Return a stable string for a profile enum or nullable scalar."""
    if value is None:
        return None
    candidate = getattr(value, "value", value)
    return str(candidate)


def _authority_item(item: SpecificationItem) -> AuthorityItemV2:
    """Project one Specification item without provenance-bearing fields."""
    return AuthorityItemV2(
        id=item.id,
        type=_enum_value(item.type) or "",
        title=item.title,
        statement=item.statement,
        level=_enum_value(item.level),
        rationale=item.rationale,
        verification=_enum_value(item.verification),
        acceptance=item.acceptance,
    )


def _is_eligible(item: SpecificationItem) -> bool:
    """Return whether an item may directly source an Authority invariant."""
    item_type = _enum_value(item.type)
    level = _enum_value(item.level)
    return item_type in _ELIGIBLE_NORMATIVE_TYPES and level != "INFORMATIVE"


def _authority_relation(relation: SpecificationRelation) -> AuthorityRelationV2:
    """Project a profile relation into the compiler-bound DTO."""
    return AuthorityRelationV2(
        from_=relation.from_,
        type=_enum_value(relation.type) or "",
        to=relation.to,
        rationale=relation.rationale,
    )


def _controlled_term(term: ControlledTerm) -> AuthorityControlledTermV2:
    """Project an explicit controlled term as non-invariant context."""
    return AuthorityControlledTermV2(
        term=term.term,
        definition=term.definition,
        scope=_enum_value(term.scope) or "",
    )


def _fingerprint_payload(data: dict[str, object]) -> str:
    """Hash canonical JSON for the Authority input excluding its own digest."""
    canonical = json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def build_authority_input_v2(payload: SpecificationPayload) -> AuthorityInputV2:
    """Build canonical compiler input from one validated Specification payload."""
    normative_items = tuple(
        sorted(
            (_authority_item(item) for item in payload.items if _is_eligible(item)),
            key=lambda item: item.id,
        )
    )
    review_context = tuple(
        sorted(
            (_authority_item(item) for item in payload.items if not _is_eligible(item)),
            key=lambda item: item.id,
        )
    )
    eligible_item_ids = tuple(item.id for item in normative_items)
    eligible_item_id_set = frozenset(eligible_item_ids)
    normative_relations = tuple(
        sorted(
            (
                _authority_relation(relation)
                for relation in payload.relations
                if relation.from_ in eligible_item_id_set
                and relation.to in eligible_item_id_set
            ),
            key=lambda relation: (relation.from_, relation.type, relation.to),
        )
    )
    controlled_terms = tuple(
        sorted(
            (_controlled_term(term) for term in payload.controlled_terms),
            key=lambda term: (term.term, term.scope, term.definition),
        )
    )
    fingerprint_data: dict[str, object] = {
        "schema_version": "agileforge.authority_input.v2",
        "artifact_id": payload.artifact_id,
        "normative_items": [
            item.model_dump(mode="json", by_alias=True) for item in normative_items
        ],
        "review_context": [
            item.model_dump(mode="json", by_alias=True) for item in review_context
        ],
        "normative_relations": [
            relation.model_dump(mode="json", by_alias=True)
            for relation in normative_relations
        ],
        "controlled_terms": [
            term.model_dump(mode="json", by_alias=True) for term in controlled_terms
        ],
        "eligible_item_ids": list(eligible_item_ids),
    }
    return AuthorityInputV2(
        artifact_id=payload.artifact_id,
        normative_items=normative_items,
        review_context=review_context,
        normative_relations=normative_relations,
        controlled_terms=controlled_terms,
        eligible_item_ids=eligible_item_ids,
        authority_input_fingerprint=_fingerprint_payload(fingerprint_data),
    )


__all__ = [
    "AuthorityControlledTermV2",
    "AuthorityInputV2",
    "AuthorityItemV2",
    "AuthorityRelationV2",
    "build_authority_input_v2",
]
