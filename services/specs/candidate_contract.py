"""Immutable host-owned candidate envelope for specification profile v2."""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Any, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from utils.agileforge_spec_profile_v2 import (
    RENDERER_VERSION,
    SCHEMA_VERSION,
    SpecificationPayload,
    canonical_spec_hash,
    canonical_spec_json,
    render_markdown,
    rendered_markdown_hash,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

ENVELOPE_VERSION: str = "agileforge.spec-candidate-envelope.v1"
Fingerprint = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
_MARKDOWN_LEADING_RE: re.Pattern[str] = re.compile(
    r"^(\s*)((?:[#\-*+>])|(?:\d+\.)(?=\s|$))"
)


class CandidateKind(StrEnum):
    """The explicit composition mode of one immutable candidate."""

    INITIAL = "initial"
    AMENDMENT = "amendment"


class CandidateSourceKind(StrEnum):
    """Bounded source-manifest categories outside semantic payload bytes."""

    VISION = "vision"
    PRODUCT_GOAL = "product_goal"
    REPOSITORY = "repository"
    EXTERNAL = "external"
    RESEARCH = "research"
    PROTOTYPE = "prototype"
    INTERVIEW = "interview"


class _FrozenModel(BaseModel):
    """Forbid hidden fields and mutation in immutable candidate records."""

    model_config = ConfigDict(extra="forbid", frozen=True)


def _require_nonblank(value: str) -> str:
    if not value.strip():
        message = "value must not be blank"
        raise ValueError(message)
    return value


def _optional_nonblank(value: object) -> object:
    if value is None:
        return None
    return _require_nonblank(value) if isinstance(value, str) else value


def _sha256(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class CandidateSourceManifestEntry(_FrozenModel):
    """One durable source reference used by the to-spec attempt."""

    source_id: Annotated[str, Field(min_length=1)]
    kind: CandidateSourceKind
    fingerprint: Fingerprint
    warnings: tuple[Annotated[str, Field(min_length=1)], ...] = ()

    _validate_text = field_validator(
        "source_id", "fingerprint", mode="before"
    )(_require_nonblank)

    @field_validator("warnings", mode="before")
    @classmethod
    def validate_warnings(cls, value: object) -> object:
        """Reject blank warning text without changing deterministic order."""
        if not isinstance(value, list | tuple):
            return value
        return tuple(
            _require_nonblank(item) if isinstance(item, str) else item
            for item in value
        )


class StableIdReplacement(_FrozenModel):
    """Explicit mapping for a stable-ID replacement in an amendment."""

    old_item_id: Annotated[str, Field(min_length=1)]
    new_item_id: Annotated[str, Field(min_length=1)]
    justification: Annotated[str, Field(min_length=1)]

    _validate_text = field_validator(
        "old_item_id", "new_item_id", "justification", mode="before"
    )(_require_nonblank)


class CandidateBuildInput(_FrozenModel):
    """Host-prepared evidence needed to bind a payload into a candidate."""

    candidate_kind: CandidateKind
    accepted_vision_id: Annotated[int, Field(gt=0)]
    accepted_vision_fingerprint: Fingerprint
    accepted_product_goal_id: Annotated[int, Field(gt=0)]
    accepted_product_goal_fingerprint: Fingerprint
    source_manifest: tuple[CandidateSourceManifestEntry, ...]
    accepted_fact_fingerprint: Fingerprint
    producer_input_fingerprint: Fingerprint
    producer_capability: Annotated[str, Field(min_length=1)]
    producer_version: Annotated[str, Field(min_length=1)]
    model_id: str | None = None
    model_configuration_fingerprint: Fingerprint | None = None
    prompt_fingerprint: Fingerprint
    workflow_node_attempt_id: Annotated[int, Field(gt=0)]
    attempt_fingerprint: Fingerprint
    correlation_id: Annotated[str, Field(min_length=1)]
    produced_at: AwareDatetime
    base_payload: SpecificationPayload | None = None
    base_specification_id: Annotated[int, Field(gt=0)] | None = None
    base_payload_fingerprint: Fingerprint | None = None
    removal_justifications: dict[str, str] = Field(default_factory=dict)
    stable_id_replacements: tuple[StableIdReplacement, ...] = ()

    _validate_text = field_validator(
        "accepted_vision_fingerprint",
        "accepted_product_goal_fingerprint",
        "accepted_fact_fingerprint",
        "producer_input_fingerprint",
        "producer_capability",
        "producer_version",
        "prompt_fingerprint",
        "attempt_fingerprint",
        "correlation_id",
        mode="before",
    )(_require_nonblank)
    _validate_optional_text = field_validator(
        "base_payload_fingerprint",
        "model_id",
        "model_configuration_fingerprint",
        mode="before",
    )(_optional_nonblank)


class CollectionDiff(_FrozenModel):
    """Deterministic stable-key delta for one semantic collection."""

    added: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()


class AmendmentDiff(_FrozenModel):
    """Full deterministic delta against one pinned accepted specification."""

    changed_fields: tuple[
        Literal["title", "summary", "problem_statement"], ...
    ] = ()
    items: CollectionDiff = Field(default_factory=CollectionDiff)
    relations: CollectionDiff = Field(default_factory=CollectionDiff)
    controlled_terms: CollectionDiff = Field(default_factory=CollectionDiff)
    external_references: CollectionDiff = Field(default_factory=CollectionDiff)
    removal_justifications: tuple[tuple[str, str], ...] = ()
    replacements: tuple[StableIdReplacement, ...] = ()

    @property
    def added_item_ids(self) -> tuple[str, ...]:
        """Return item additions for concise callers and review projections."""
        return self.items.added

    @property
    def changed_item_ids(self) -> tuple[str, ...]:
        """Return item changes for concise callers and review projections."""
        return self.items.changed

    @property
    def removed_item_ids(self) -> tuple[str, ...]:
        """Return item removals for concise callers and review projections."""
        return self.items.removed


class SpecificationCandidateEnvelope(_FrozenModel):
    """Immutable host metadata bound to exact semantic payload bytes."""

    envelope_version: Literal["agileforge.spec-candidate-envelope.v1"] = (
        ENVELOPE_VERSION
    )
    candidate_kind: CandidateKind
    accepted_vision_id: Annotated[int, Field(gt=0)]
    accepted_vision_fingerprint: Fingerprint
    accepted_product_goal_id: Annotated[int, Field(gt=0)]
    accepted_product_goal_fingerprint: Fingerprint
    base_specification_id: Annotated[int, Field(gt=0)] | None = None
    base_payload_fingerprint: Fingerprint | None = None
    source_manifest: tuple[CandidateSourceManifestEntry, ...]
    source_manifest_fingerprint: Fingerprint
    accepted_fact_fingerprint: Fingerprint
    producer_input_fingerprint: Fingerprint
    producer_capability: Annotated[str, Field(min_length=1)]
    producer_version: Annotated[str, Field(min_length=1)]
    model_id: str | None = None
    model_configuration_fingerprint: Fingerprint | None = None
    prompt_fingerprint: Fingerprint
    workflow_node_attempt_id: Annotated[int, Field(gt=0)]
    attempt_fingerprint: Fingerprint
    correlation_id: Annotated[str, Field(min_length=1)]
    produced_at: AwareDatetime
    payload_fingerprint: Fingerprint
    profile_version: Literal["agileforge.spec.v2"] = SCHEMA_VERSION
    renderer_version: Annotated[str, Field(min_length=1)] = RENDERER_VERSION
    review_view_fingerprint: Fingerprint
    amendment_diff: AmendmentDiff | None = None
    candidate_fingerprint: Fingerprint

    _validate_text = field_validator(
        "accepted_vision_fingerprint",
        "accepted_product_goal_fingerprint",
        "source_manifest_fingerprint",
        "accepted_fact_fingerprint",
        "producer_input_fingerprint",
        "producer_capability",
        "producer_version",
        "prompt_fingerprint",
        "attempt_fingerprint",
        "correlation_id",
        "payload_fingerprint",
        "renderer_version",
        "review_view_fingerprint",
        "candidate_fingerprint",
        mode="before",
    )(_require_nonblank)
    _validate_optional_text = field_validator(
        "base_payload_fingerprint",
        "model_id",
        "model_configuration_fingerprint",
        mode="before",
    )(_optional_nonblank)

    @model_validator(mode="after")
    def validate_envelope(self) -> Self:
        """Validate source identity, model evidence, and initial/amendment mode."""
        source_ids = [entry.source_id for entry in self.source_manifest]
        if len(source_ids) != len(set(source_ids)):
            message = "duplicate source manifest ids"
            raise ValueError(message)
        if self.source_manifest_fingerprint != _source_manifest_fingerprint(
            self.source_manifest
        ):
            message = "source manifest fingerprint does not match source manifest"
            raise ValueError(message)
        has_model_id = self.model_id is not None
        has_model_config = self.model_configuration_fingerprint is not None
        if has_model_id != has_model_config:
            message = "model and model configuration fingerprint must be paired"
            raise ValueError(message)
        if self.producer_capability == "to-spec" and not has_model_id:
            message = "model producer requires model identity and configuration"
            raise ValueError(message)
        if self.candidate_kind is CandidateKind.INITIAL:
            if (
                self.base_specification_id is not None
                or self.base_payload_fingerprint is not None
                or self.amendment_diff is not None
            ):
                message = "initial candidate must not include an amendment base"
                raise ValueError(message)
        elif (
            self.base_specification_id is None
            or self.base_payload_fingerprint is None
            or self.amendment_diff is None
        ):
            message = "amendment candidate requires base identity and diff"
            raise ValueError(message)
        return self


def _model_json(model: BaseModel) -> str:
    return _canonical_json(model.model_dump(mode="json", by_alias=True))


def _item_json(item: BaseModel) -> str:
    """Serialize one item using the v2 profile's set-like tag ordering."""
    data = item.model_dump(mode="json", by_alias=True)
    tags = data.get("tags")
    if isinstance(tags, list):
        data["tags"] = sorted(
            tags,
            key=lambda value: " ".join(str(value).strip().casefold().split()),
        )
    return _canonical_json(data)


def _collection_diff(
    base: Mapping[str, str],
    current: Mapping[str, str],
) -> CollectionDiff:
    return CollectionDiff(
        added=tuple(sorted(current.keys() - base.keys())),
        changed=tuple(
            sorted(
                key
                for key in base.keys() & current.keys()
                if base[key] != current[key]
            )
        ),
        removed=tuple(sorted(base.keys() - current.keys())),
    )


def _relation_key(relation: BaseModel) -> str:
    data = relation.model_dump(mode="json", by_alias=True)
    return f"{data['type']}:{data['from']}->{data['to']}"


def _term_key(term: BaseModel) -> str:
    data = term.model_dump(mode="json")
    normalized = " ".join(str(data["term"]).strip().casefold().split())
    return f"{normalized}:{data['scope']}"


def compute_amendment_diff(
    base_payload: SpecificationPayload,
    payload: SpecificationPayload,
    *,
    removal_justifications: Mapping[str, str] | None = None,
    stable_id_replacements: Sequence[StableIdReplacement] = (),
) -> AmendmentDiff:
    """Compute all stable semantic collection deltas and validate removals."""
    if base_payload.artifact_id != payload.artifact_id:
        message = "stable Specification artifact id cannot change"
        raise ValueError(message)
    changed_fields = tuple(
        field
        for field in ("title", "summary", "problem_statement")
        if getattr(base_payload, field) != getattr(payload, field)
    )
    base_items = {item.id: _item_json(item) for item in base_payload.items}
    current_items = {item.id: _item_json(item) for item in payload.items}
    base_by_id = {item.id: item for item in base_payload.items}
    current_by_id = {item.id: item for item in payload.items}
    for item_id in base_by_id.keys() & current_by_id.keys():
        if base_by_id[item_id].type is not current_by_id[item_id].type:
            message = "stable item id cannot change type"
            raise ValueError(message)
    items = _collection_diff(base_items, current_items)
    relations = _collection_diff(
        {_relation_key(item): _model_json(item) for item in base_payload.relations},
        {_relation_key(item): _model_json(item) for item in payload.relations},
    )
    controlled_terms = _collection_diff(
        {_term_key(item): _model_json(item) for item in base_payload.controlled_terms},
        {_term_key(item): _model_json(item) for item in payload.controlled_terms},
    )
    external_references = _collection_diff(
        {item.id: _model_json(item) for item in base_payload.external_references},
        {item.id: _model_json(item) for item in payload.external_references},
    )
    removed_keys = {
        *items.removed,
        *relations.removed,
        *controlled_terms.removed,
        *external_references.removed,
    }
    justifications = dict(removal_justifications or {})
    if set(justifications) - removed_keys:
        message = (
            "removal justification references a semantic entry that was not removed"
        )
        raise ValueError(message)
    missing = [
        key
        for key in sorted(removed_keys)
        if not isinstance(justifications.get(key), str)
        or not justifications[key].strip()
    ]
    if missing:
        message = "removal justification is required for every removed semantic entry"
        raise ValueError(message)
    replacements = tuple(
        sorted(
            stable_id_replacements,
            key=lambda item: (item.old_item_id, item.new_item_id),
        )
    )
    old_ids = [replacement.old_item_id for replacement in replacements]
    new_ids = [replacement.new_item_id for replacement in replacements]
    if len(old_ids) != len(set(old_ids)) or len(new_ids) != len(set(new_ids)):
        message = "stable-ID replacements must be one-to-one"
        raise ValueError(message)
    for replacement in replacements:
        if replacement.old_item_id not in items.removed:
            message = "stable-ID replacement old item must be removed"
            raise ValueError(message)
        if replacement.new_item_id not in items.added:
            message = "stable-ID replacement new item must be added"
            raise ValueError(message)
    return AmendmentDiff(
        changed_fields=changed_fields,
        items=items,
        relations=relations,
        controlled_terms=controlled_terms,
        external_references=external_references,
        removal_justifications=tuple(
            (key, justifications[key]) for key in sorted(removed_keys)
        ),
        replacements=replacements,
    )


def _source_manifest_fingerprint(
    manifest: Sequence[CandidateSourceManifestEntry],
) -> str:
    data = [
        entry.model_dump(mode="json")
        for entry in sorted(manifest, key=lambda item: item.source_id)
    ]
    return _sha256(_canonical_json(data))


def _review_seed_data(envelope: SpecificationCandidateEnvelope) -> dict[str, Any]:
    data = envelope.model_dump(mode="json")
    data.pop("candidate_fingerprint")
    data.pop("review_view_fingerprint")
    return data


def _render_candidate_review(
    payload: SpecificationPayload,
    data: Mapping[str, Any],
) -> str:
    lines = [render_markdown(payload).rstrip(), "", "## Candidate Envelope", ""]
    labels = (
        ("Envelope version", "envelope_version"),
        ("Candidate kind", "candidate_kind"),
        ("Accepted Vision id", "accepted_vision_id"),
        ("Accepted Vision fingerprint", "accepted_vision_fingerprint"),
        ("Accepted Product Goal id", "accepted_product_goal_id"),
        ("Accepted Product Goal fingerprint", "accepted_product_goal_fingerprint"),
        ("Base specification id", "base_specification_id"),
        ("Base payload fingerprint", "base_payload_fingerprint"),
        ("Source manifest fingerprint", "source_manifest_fingerprint"),
        ("Accepted fact fingerprint", "accepted_fact_fingerprint"),
        ("Producer input fingerprint", "producer_input_fingerprint"),
        ("Producer capability", "producer_capability"),
        ("Producer version", "producer_version"),
        ("Model id", "model_id"),
        ("Model configuration fingerprint", "model_configuration_fingerprint"),
        ("Prompt fingerprint", "prompt_fingerprint"),
        ("Workflow node attempt id", "workflow_node_attempt_id"),
        ("Attempt fingerprint", "attempt_fingerprint"),
        ("Correlation id", "correlation_id"),
        ("Produced at", "produced_at"),
        ("Payload fingerprint", "payload_fingerprint"),
        ("Profile version", "profile_version"),
        ("Renderer version", "renderer_version"),
    )
    lines.extend(
        f"- {label}: {_escape_markdown(data[key])}"
        for label, key in labels
    )
    lines.extend(["", "### Source Manifest", ""])
    for entry in data["source_manifest"]:
        lines.append(
            f"- {_escape_markdown(entry['source_id'])} "
            f"({_escape_markdown(entry['kind'])}): "
            f"{_escape_markdown(entry['fingerprint'])}"
        )
        lines.extend(
            f"  - Warning: {_escape_markdown(warning)}"
            for warning in entry["warnings"]
        )
    lines.extend(["", "### Amendment Diff", ""])
    diff = data["amendment_diff"]
    if diff is None:
        lines.append("- None")
    else:
        lines.append(
            "- top-level fields: changed="
            f"{_escape_markdown(','.join(diff['changed_fields']))}"
        )
        for name in ("items", "relations", "controlled_terms", "external_references"):
            change = diff[name]
            lines.append(
                f"- {name}: added={_escape_markdown(','.join(change['added']))}; "
                f"changed={_escape_markdown(','.join(change['changed']))}; "
                f"removed={_escape_markdown(','.join(change['removed']))}"
            )
        lines.extend(
            f"  - Removal {_escape_markdown(item_id)}: {_escape_markdown(reason)}"
            for item_id, reason in diff["removal_justifications"]
        )
        lines.extend(
            f"  - Replacement {_escape_markdown(row['old_item_id'])} -> "
            f"{_escape_markdown(row['new_item_id'])}: "
            f"{_escape_markdown(row['justification'])}"
            for row in diff["replacements"]
        )
    lines.append("")
    return "\n".join(lines)


def _escape_markdown(value: object) -> str:
    text = "-" if value is None else str(value)
    escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return "\n".join(
        _MARKDOWN_LEADING_RE.sub(r"\1\\\2", line) for line in escaped.split("\n")
    )


def render_candidate_review_markdown(
    payload: SpecificationPayload,
    envelope: SpecificationCandidateEnvelope,
) -> str:
    """Render the complete deterministic payload-plus-envelope review view."""
    return _render_candidate_review(payload, _review_seed_data(envelope))


def _candidate_fingerprint(
    payload: SpecificationPayload,
    envelope_data: Mapping[str, Any],
) -> str:
    identity_envelope = {
        key: value
        for key, value in envelope_data.items()
        if key
        not in {
            "accepted_vision_id",
            "accepted_product_goal_id",
            "base_specification_id",
            "workflow_node_attempt_id",
            "attempt_fingerprint",
            "correlation_id",
            "produced_at",
            "review_view_fingerprint",
        }
    }
    return _sha256(
        _canonical_json(
            {
                "payload": json.loads(canonical_spec_json(payload)),
                "envelope": identity_envelope,
            }
        )
    )


def build_candidate_envelope(
    *,
    payload: SpecificationPayload,
    metadata: CandidateBuildInput,
) -> SpecificationCandidateEnvelope:
    """Bind canonical semantic bytes and complete immutable host evidence."""
    candidate_kind = metadata.candidate_kind
    source_manifest = metadata.source_manifest
    base_payload = metadata.base_payload
    base_specification_id = metadata.base_specification_id
    base_payload_fingerprint = metadata.base_payload_fingerprint
    removal_justifications = metadata.removal_justifications
    stable_id_replacements = metadata.stable_id_replacements
    manifest_source_ids = {item.source_id for item in source_manifest}
    unknown_source_ids = sorted(
        {
            note.source_id
            for item in payload.items
            for note in item.source_notes
            if note.source_id not in manifest_source_ids
        }
    )
    if unknown_source_ids:
        message = "payload source notes are absent from the host source manifest"
        raise ValueError(message)
    if candidate_kind is CandidateKind.INITIAL:
        if (
            base_payload is not None
            or base_specification_id is not None
            or base_payload_fingerprint is not None
            or removal_justifications
            or stable_id_replacements
        ):
            message = "initial candidate must not include amendment inputs"
            raise ValueError(message)
        amendment_diff: AmendmentDiff | None = None
    else:
        if (
            base_payload is None
            or base_specification_id is None
            or base_payload_fingerprint is None
        ):
            message = "amendment candidate requires a pinned base"
            raise ValueError(message)
        if canonical_spec_hash(base_payload) != base_payload_fingerprint:
            message = "stale base payload fingerprint"
            raise ValueError(message)
        amendment_diff = compute_amendment_diff(
            base_payload,
            payload,
            removal_justifications=removal_justifications,
            stable_id_replacements=stable_id_replacements,
        )
    manifest = tuple(sorted(source_manifest, key=lambda item: item.source_id))
    diff_payload = (
        None if amendment_diff is None else amendment_diff.model_dump(mode="json")
    )
    draft = SpecificationCandidateEnvelope.model_validate(
        {
            "candidate_kind": candidate_kind,
            "accepted_vision_id": metadata.accepted_vision_id,
            "accepted_vision_fingerprint": metadata.accepted_vision_fingerprint,
            "accepted_product_goal_id": metadata.accepted_product_goal_id,
            "accepted_product_goal_fingerprint": (
                metadata.accepted_product_goal_fingerprint
            ),
            "base_specification_id": base_specification_id,
            "base_payload_fingerprint": base_payload_fingerprint,
            "source_manifest": [item.model_dump(mode="json") for item in manifest],
            "source_manifest_fingerprint": _source_manifest_fingerprint(manifest),
            "accepted_fact_fingerprint": metadata.accepted_fact_fingerprint,
            "producer_input_fingerprint": metadata.producer_input_fingerprint,
            "producer_capability": metadata.producer_capability,
            "producer_version": metadata.producer_version,
            "model_id": metadata.model_id,
            "model_configuration_fingerprint": metadata.model_configuration_fingerprint,
            "prompt_fingerprint": metadata.prompt_fingerprint,
            "workflow_node_attempt_id": metadata.workflow_node_attempt_id,
            "attempt_fingerprint": metadata.attempt_fingerprint,
            "correlation_id": metadata.correlation_id,
            "produced_at": metadata.produced_at,
            "payload_fingerprint": canonical_spec_hash(payload),
            "review_view_fingerprint": _sha256("pending review fingerprint"),
            "amendment_diff": diff_payload,
            "candidate_fingerprint": _sha256("pending candidate fingerprint"),
        }
    )
    values = _review_seed_data(draft)
    values["review_view_fingerprint"] = rendered_markdown_hash(
        _render_candidate_review(payload, values)
    )
    values["candidate_fingerprint"] = _candidate_fingerprint(payload, values)
    return SpecificationCandidateEnvelope.model_validate(values)


def canonical_candidate_json(
    payload: SpecificationPayload,
    envelope: SpecificationCandidateEnvelope,
) -> str:
    """Return persistence bytes after verifying every derived candidate fingerprint."""
    if envelope.payload_fingerprint != canonical_spec_hash(payload):
        message = "payload fingerprint does not match canonical payload"
        raise ValueError(message)
    if envelope.review_view_fingerprint != rendered_markdown_hash(
        render_candidate_review_markdown(payload, envelope)
    ):
        message = "review view fingerprint does not match complete review"
        raise ValueError(message)
    data = envelope.model_dump(mode="json")
    expected_candidate_fingerprint = _candidate_fingerprint(
        payload,
        {key: value for key, value in data.items() if key != "candidate_fingerprint"},
    )
    if envelope.candidate_fingerprint != expected_candidate_fingerprint:
        message = "candidate fingerprint does not match canonical candidate"
        raise ValueError(message)
    return _canonical_json(
        {"payload": json.loads(canonical_spec_json(payload)), "envelope": data}
    )


def load_candidate_contract(
    serialized: str,
    *,
    expected_candidate_fingerprint: str,
) -> tuple[SpecificationPayload, SpecificationCandidateEnvelope]:
    """Load persistence bytes and fail closed on any fingerprint mismatch."""
    try:
        raw = json.loads(serialized)
    except json.JSONDecodeError as exc:
        message = "candidate contract JSON is invalid"
        raise ValueError(message) from exc
    if not isinstance(raw, dict):
        message = "candidate contract must be a JSON object"
        raise TypeError(message)
    payload = SpecificationPayload.model_validate(raw.get("payload"))
    envelope = SpecificationCandidateEnvelope.model_validate(raw.get("envelope"))
    canonical = canonical_candidate_json(payload, envelope)
    if serialized != canonical:
        message = "candidate contract bytes are noncanonical"
        raise ValueError(message)
    if envelope.candidate_fingerprint != expected_candidate_fingerprint:
        message = "candidate fingerprint does not match expected decision target"
        raise ValueError(message)
    return payload, envelope


__all__ = [
    "ENVELOPE_VERSION",
    "AmendmentDiff",
    "CandidateBuildInput",
    "CandidateKind",
    "CandidateSourceKind",
    "CandidateSourceManifestEntry",
    "CollectionDiff",
    "SpecificationCandidateEnvelope",
    "StableIdReplacement",
    "build_candidate_envelope",
    "canonical_candidate_json",
    "compute_amendment_diff",
    "load_candidate_contract",
    "render_candidate_review_markdown",
]
