"""Typed assumption contracts and grounding for compiled authority."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
)
from pydantic_core import PydanticCustomError

from utils.agileforge_spec_profile import (
    AgileForgeSpecStatus,
    AgileForgeSpecType,
    TechnicalSpecArtifact,
)

STRUCTURED_ITEM_ID_PATTERN: str = (
    r"^(GOAL|NON_GOAL|REQ|QUALITY|CONSTRAINT|INTERFACE|DATA|DECISION|"
    r"ASSUMPTION|RISK|EXAMPLE|OPEN_QUESTION)\.[a-z0-9][a-z0-9.-]{1,96}$"
)
NORMATIVE_ITEM_ID_PATTERN: str = (
    r"^(REQ|QUALITY|CONSTRAINT|INTERFACE|DATA)\.[a-z0-9][a-z0-9.-]{1,96}$"
)
NORMATIVE_SPEC_TYPES: frozenset[AgileForgeSpecType] = frozenset(
    {
        AgileForgeSpecType.REQ,
        AgileForgeSpecType.QUALITY,
        AgileForgeSpecType.CONSTRAINT,
        AgileForgeSpecType.INTERFACE,
        AgileForgeSpecType.DATA,
    }
)

_STRUCTURED_ITEM_ID_CUE_RE: re.Pattern[str] = re.compile(
    r"(?<!\w)(?:goal|non_goal|req|quality|constraint|interface|data|decision|"
    r"assumption|risk|example|open_question)\.[a-z0-9][a-z0-9.-]{1,96}"
    r"(?![a-z0-9.-])"
)
_STATUS_CUE_RE: re.Pattern[str] = re.compile(
    r"\b(?:draft|proposed|accepted|changed|deferred|rejected|superseded)\b"
)
_ACCEPTED_CUE_RE: re.Pattern[str] = re.compile(r"\baccepted\b")
_ITEM_CUE_RE: re.Pattern[str] = re.compile(r"\bitems?\b")
_DUPLICATE_SOURCE_ITEM_IDS_MESSAGE: str = "source_item_ids must be unique"
_EMPTY_TEXT_MESSAGE: str = "text must not be empty"
_CLAIM_LIKE_TEXT_ERROR_TYPE: str = "assumption_claim_requires_typed_form"
_CLAIM_LIKE_TEXT_MESSAGE: str = "claim-like assumption must use a typed claim variant"
_DUPLICATE_ITEM_IDS_MESSAGE: str = "item_ids must be unique"


class _StrictAssumptionModel(BaseModel):
    """Base contract for strict, closed assumption variants."""

    model_config = ConfigDict(extra="forbid")


StructuredSpecItemId = Annotated[
    str,
    Field(pattern=STRUCTURED_ITEM_ID_PATTERN),
]
NormativeSpecItemId = Annotated[
    str,
    Field(pattern=NORMATIVE_ITEM_ID_PATTERN),
]


class StructuredSpecClaimProvenance(_StrictAssumptionModel):
    """Canonical source evidence for one structured claim."""

    source: Literal["structured_spec"]
    artifact_id: Annotated[
        str,
        Field(pattern=r"^SPEC\.[a-z0-9][a-z0-9.-]{1,96}$"),
    ]
    source_item_ids: list[StructuredSpecItemId]

    @field_validator("source_item_ids")
    @classmethod
    def validate_source_item_ids(cls, value: list[str]) -> list[str]:
        """Reject duplicate evidence before producing its canonical order."""
        if len(value) != len(set(value)):
            raise ValueError(_DUPLICATE_SOURCE_ITEM_IDS_MESSAGE)
        return sorted(value)


def normalize_free_text_identity(text: str) -> str:
    """Return the stable Unicode- and case-normalized text identity."""
    return unicodedata.normalize("NFKC", text).casefold().strip()


def free_text_requires_typed_claim(text: str) -> bool:
    """Return whether finite documented claim cues require a typed variant."""
    normalized = normalize_free_text_identity(text)
    has_structured_item_id = _STRUCTURED_ITEM_ID_CUE_RE.search(normalized)
    has_status_value = _STATUS_CUE_RE.search(normalized)
    has_accepted_word = _ACCEPTED_CUE_RE.search(normalized)
    has_item_word = _ITEM_CUE_RE.search(normalized)
    return bool(
        (has_structured_item_id and has_status_value)
        or (has_accepted_word and has_item_word)
    )


class FreeTextAssumption(_StrictAssumptionModel):
    """An ordinary, reviewer-visible assumption without a structured claim."""

    kind: Literal["free_text"]
    text: Annotated[str, Field(min_length=1)]

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        """Require meaningful text outside the finite structured-claim cues."""
        text = value.strip()
        if not text:
            raise ValueError(_EMPTY_TEXT_MESSAGE)
        if free_text_requires_typed_claim(text):
            raise PydanticCustomError(
                _CLAIM_LIKE_TEXT_ERROR_TYPE,
                _CLAIM_LIKE_TEXT_MESSAGE,
            )
        return text


class ItemStatusAssumptionClaim(_StrictAssumptionModel):
    """A claim that one structured item's lifecycle status has a given value."""

    kind: Literal["item_status"]
    item_id: StructuredSpecItemId
    status: AgileForgeSpecStatus
    provenance: StructuredSpecClaimProvenance


class AcceptedNormativeCountAssumptionClaim(_StrictAssumptionModel):
    """A claim for the complete number of accepted normative items."""

    kind: Literal["accepted_normative_count"]
    count: Annotated[int, Field(strict=True, ge=0)]
    provenance: StructuredSpecClaimProvenance


class AcceptedNormativeSetAssumptionClaim(_StrictAssumptionModel):
    """A claim for the complete accepted normative item set."""

    kind: Literal["accepted_normative_set"]
    item_ids: list[NormativeSpecItemId]
    provenance: StructuredSpecClaimProvenance

    @field_validator("item_ids")
    @classmethod
    def validate_item_ids(cls, value: list[str]) -> list[str]:
        """Reject duplicate claimed items before producing canonical order."""
        if len(value) != len(set(value)):
            raise ValueError(_DUPLICATE_ITEM_IDS_MESSAGE)
        return sorted(value)


AuthorityAssumption = Annotated[
    FreeTextAssumption
    | ItemStatusAssumptionClaim
    | AcceptedNormativeCountAssumptionClaim
    | AcceptedNormativeSetAssumptionClaim,
    Field(discriminator="kind"),
]
AUTHORITY_ASSUMPTION_ADAPTER: TypeAdapter[AuthorityAssumption] = TypeAdapter(
    AuthorityAssumption
)

type GroundingFailureReason = Literal[
    "ASSUMPTION_CLAIM_SOURCE_MISMATCH",
    "ASSUMPTION_CLAIM_MISMATCH",
]


@dataclass(frozen=True)
class GroundingFailure:
    """Details for a structured claim that cannot be grounded to its source."""

    reason: GroundingFailureReason
    claim_kind: str
    claimed_value: object
    actual_value: object
    artifact_id: str
    claimed_source_item_ids: tuple[str, ...]
    actual_source_item_ids: tuple[str, ...]


def is_structured_assumption(assumption: AuthorityAssumption) -> bool:
    """Return whether an assumption requires structured-spec grounding."""
    return not isinstance(assumption, FreeTextAssumption)


def canonical_assumption_key(assumption: AuthorityAssumption) -> str:
    """Return the one stable identity key for a canonical assumption."""
    payload = assumption.model_dump(mode="json")
    if isinstance(assumption, FreeTextAssumption):
        payload["text"] = normalize_free_text_identity(assumption.text)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def render_assumption_text(assumption: AuthorityAssumption) -> str:
    """Render one assumption in compact human-readable review text."""
    if isinstance(assumption, FreeTextAssumption):
        return assumption.text
    if isinstance(assumption, ItemStatusAssumptionClaim):
        return f"{assumption.item_id} status is {assumption.status.value}"
    if isinstance(assumption, AcceptedNormativeCountAssumptionClaim):
        return f"{assumption.count} accepted normative items"
    return "accepted normative items: " + ", ".join(assumption.item_ids)


def ground_assumption(
    assumption: AuthorityAssumption,
    artifact: TechnicalSpecArtifact,
) -> AuthorityAssumption | GroundingFailure:
    """Ground one structured claim against its parsed canonical spec artifact."""
    if isinstance(assumption, FreeTextAssumption):
        return assumption

    items_by_id = {item.id: item for item in artifact.items}
    accepted_ids = sorted(
        item.id
        for item in artifact.items
        if item.type in NORMATIVE_SPEC_TYPES
        and item.status == AgileForgeSpecStatus.ACCEPTED
    )
    provenance = assumption.provenance
    claimed_sources = tuple(provenance.source_item_ids)

    if provenance.artifact_id != artifact.artifact_id:
        return GroundingFailure(
            reason="ASSUMPTION_CLAIM_SOURCE_MISMATCH",
            claim_kind=assumption.kind,
            claimed_value=assumption.model_dump(mode="json"),
            actual_value={"artifact_id": artifact.artifact_id},
            artifact_id=provenance.artifact_id,
            claimed_source_item_ids=claimed_sources,
            actual_source_item_ids=(),
        )

    if isinstance(assumption, ItemStatusAssumptionClaim):
        item = items_by_id.get(assumption.item_id)
        actual_sources = (assumption.item_id,) if item is not None else ()
        if (
            item is None
            or item.status != assumption.status
            or claimed_sources != actual_sources
        ):
            return GroundingFailure(
                reason=(
                    "ASSUMPTION_CLAIM_MISMATCH"
                    if item is not None and item.status != assumption.status
                    else "ASSUMPTION_CLAIM_SOURCE_MISMATCH"
                ),
                claim_kind=assumption.kind,
                claimed_value=assumption.status.value,
                actual_value=item.status.value if item is not None else None,
                artifact_id=artifact.artifact_id,
                claimed_source_item_ids=claimed_sources,
                actual_source_item_ids=actual_sources,
            )
        return assumption

    actual_sources = tuple(accepted_ids)
    claimed_value: object
    actual_value: object
    if isinstance(assumption, AcceptedNormativeCountAssumptionClaim):
        claimed_value = assumption.count
        actual_value = len(accepted_ids)
    else:
        claimed_value = assumption.item_ids
        actual_value = accepted_ids

    if claimed_sources != actual_sources:
        reason: GroundingFailureReason = "ASSUMPTION_CLAIM_SOURCE_MISMATCH"
    elif claimed_value != actual_value:
        reason = "ASSUMPTION_CLAIM_MISMATCH"
    else:
        return assumption
    return GroundingFailure(
        reason=reason,
        claim_kind=assumption.kind,
        claimed_value=claimed_value,
        actual_value=actual_value,
        artifact_id=artifact.artifact_id,
        claimed_source_item_ids=claimed_sources,
        actual_source_item_ids=actual_sources,
    )
