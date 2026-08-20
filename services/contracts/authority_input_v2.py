# services/contracts/authority_input_v2.py
"""Deterministic Authority input derived from a v2 Specification payload."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from utils.agileforge_spec_profile_v2 import (
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
    """One eligible item containing only invariant-source semantics."""

    id: Annotated[
        str,
        Field(min_length=1, description="Exact eligible source item identity."),
    ]
    type: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Source item category; it does not itself authorize an invariant "
                "type. Use only a faithfully matching supported invariant shape."
            ),
        ),
    ]
    statement: Annotated[
        str,
        Field(
            min_length=1,
            description="Exact normative statement available for copied parameters.",
        ),
    ]
    level: Annotated[
        str | None,
        Field(description="Exact normative level that invariants must preserve."),
    ] = None
    acceptance: Annotated[
        tuple[Annotated[str, Field(min_length=1)], ...],
        Field(
            description=(
                "Exact acceptance criteria available for copied parameters and "
                "source-map excerpts."
            )
        ),
    ] = ()


class AuthorityRelationV2(_FrozenClosedModel):
    """One typed relation whose endpoints are eligible invariant sources."""

    from_: Annotated[str, Field(alias="from", min_length=1)]
    type: Annotated[str, Field(min_length=1)]
    to: Annotated[str, Field(min_length=1)]


class AuthorityInputV2(_FrozenClosedModel):
    """Closed deterministic input contract for Authority compilation."""

    schema_version: Literal["agileforge.authority_input.v2"] = (
        "agileforge.authority_input.v2"
    )
    artifact_id: Annotated[str, Field(min_length=1)]
    normative_items: tuple[AuthorityItemV2, ...]
    normative_relations: tuple[AuthorityRelationV2, ...]
    eligible_item_ids: tuple[Annotated[str, Field(min_length=1)], ...]
    authority_input_fingerprint: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]


def _enum_value(value: object) -> str | None:
    """Return a stable string for a profile enum or nullable scalar."""
    if value is None:
        return None
    candidate = getattr(value, "value", value)
    return str(candidate)


def _authority_item(item: SpecificationItem) -> AuthorityItemV2:
    """Project only fields allowed to authorize compiler invariants."""
    return AuthorityItemV2(
        id=item.id,
        type=_enum_value(item.type) or "",
        statement=item.statement,
        level=_enum_value(item.level),
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
    fingerprint_data: dict[str, object] = {
        "schema_version": "agileforge.authority_input.v2",
        "artifact_id": payload.artifact_id,
        "normative_items": [
            item.model_dump(mode="json", by_alias=True) for item in normative_items
        ],
        "normative_relations": [
            relation.model_dump(mode="json", by_alias=True)
            for relation in normative_relations
        ],
        "eligible_item_ids": list(eligible_item_ids),
    }
    return AuthorityInputV2(
        artifact_id=payload.artifact_id,
        normative_items=normative_items,
        normative_relations=normative_relations,
        eligible_item_ids=eligible_item_ids,
        authority_input_fingerprint=_fingerprint_payload(fingerprint_data),
    )


__all__ = [
    "AuthorityInputV2",
    "AuthorityItemV2",
    "AuthorityRelationV2",
    "build_authority_input_v2",
]
