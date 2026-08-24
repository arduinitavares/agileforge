"""Tests for closed provider and host Story contracts."""
# ruff: noqa: D103

import json
from itertools import permutations

import pytest
from pydantic import ValidationError

from services.contracts.specification_references import AcceptedSpecificationReference
from services.contracts.story import (
    CanonicalStoryItem,
    CanonicalStoryOutput,
    StoryItemEnvelope,
    UserStoryAgentItem,
    UserStoryWriterInput,
    canonicalize_story_items,
    parse_story_persona,
)
from services.story_schema_repair import with_story_schema_repair_feedback
from utils.agileforge_spec_profile_v2 import (
    RequirementLevel,
    SpecificationItem,
    SpecificationPayload,
    SpecItemType,
    VerificationMethod,
    canonical_spec_hash,
    canonical_spec_json,
)


def _reference() -> AcceptedSpecificationReference:
    payload = SpecificationPayload(
        artifact_id="SPEC.story-contract",
        title="Story contract",
        summary="Host controls item identity",
        problem_statement="Story evidence is stable.",
        items=(
            SpecificationItem(
                id="REQ.alpha",
                type=SpecItemType.REQ,
                title="Alpha",
                statement="The system must support alpha.",
                level=RequirementLevel.MUST,
                verification=VerificationMethod.UNIT_TEST,
                acceptance=("Alpha passes.",),
            ),
            SpecificationItem(
                id="DATA.beta",
                type=SpecItemType.DATA,
                title="Beta",
                statement="The system must retain beta.",
                level=RequirementLevel.SHOULD,
                verification=VerificationMethod.UNIT_TEST,
                acceptance=("Beta persists.",),
            ),
        ),
    )
    return AcceptedSpecificationReference(
        spec_version_id=5,
        spec_hash=canonical_spec_hash(payload),
        canonical_specification_json=canonical_spec_json(payload),
        payload=payload,
    )


def _story(*, criterion: str = "Verify the result.") -> UserStoryAgentItem:
    return UserStoryAgentItem(
        story_title="Calculate values",
        statement=" **As an Étudiant**, I want to calculate values, so that I learn.",
        acceptance_criteria=(criterion, "- Preserve\nUnicode ✓"),
        spec_item_ids=("REQ.alpha", "DATA.beta"),
        invest_score="High",
        estimated_effort="S",
        produced_artifacts=("calculator",),
        research_caveats=(),
        decomposition_warning=None,
        dependency_candidates=(),
    )


def test_story_canonicalization_preserves_provider_order_and_criteria_bytes() -> None:
    items = canonicalize_story_items(
        _reference(),
        parent_backlog_spec_item_ids=("DATA.beta", "REQ.alpha"),
        agent_items=(_story(), _story(criterion="Verify the second result.")),
    )

    assert [envelope.item.story_item_id for envelope in items] == ["US-0001", "US-0002"]
    assert items[0].item.persona == "Étudiant"
    assert items[0].item.acceptance_criteria == (
        "Verify the result.",
        "- Preserve\nUnicode ✓",
    )


def test_story_evidence_set_is_canonicalized_before_fingerprinting() -> None:
    fingerprints: set[str] = set()
    for spec_item_ids in permutations(("REQ.alpha", "DATA.beta")):
        item = _story().model_copy(update={"spec_item_ids": spec_item_ids})
        canonical = canonicalize_story_items(
            _reference(),
            parent_backlog_spec_item_ids=("DATA.beta", "REQ.alpha"),
            agent_items=(item,),
        )[0]
        fingerprints.add(canonical.item_fingerprint)
        assert canonical.item.spec_item_ids == ("DATA.beta", "REQ.alpha")

    assert len(fingerprints) == 1


def test_story_persona_parser_and_acceptance_criteria_fail_closed() -> None:
    assert (
        parse_story_persona(" **As the product owner**, I want a roadmap")
        == "product owner"
    )

    with pytest.raises(ValidationError, match="acceptance criterion"):
        _story(criterion=" \t\u2003")

    invalid_statement = _story().model_dump()
    invalid_statement["statement"] = "I want a roadmap without a persona."
    with pytest.raises(ValidationError, match="statement must start"):
        UserStoryAgentItem.model_validate(invalid_statement)

    with pytest.raises(ValueError, match="one through eight"):
        canonicalize_story_items(
            _reference(),
            parent_backlog_spec_item_ids=("DATA.beta", "REQ.alpha"),
            agent_items=(),
        )

    with pytest.raises(ValueError, match="one through eight"):
        canonicalize_story_items(
            _reference(),
            parent_backlog_spec_item_ids=("DATA.beta", "REQ.alpha"),
            agent_items=tuple(_story() for _ in range(9)),
        )


def test_story_fingerprint_changes_when_immutable_content_changes() -> None:
    first = canonicalize_story_items(
        _reference(),
        parent_backlog_spec_item_ids=("DATA.beta", "REQ.alpha"),
        agent_items=(_story(),),
    )[0]
    second = canonicalize_story_items(
        _reference(),
        parent_backlog_spec_item_ids=("DATA.beta", "REQ.alpha"),
        agent_items=(_story(criterion="Verify a changed result."),),
    )[0]

    assert first.item_fingerprint != second.item_fingerprint


def test_host_story_item_rejects_invalid_ids_content_and_persona() -> None:
    envelope = canonicalize_story_items(
        _reference(),
        parent_backlog_spec_item_ids=("DATA.beta", "REQ.alpha"),
        agent_items=(_story(),),
    )[0]
    payload = envelope.item.model_dump(mode="json")

    assert CanonicalStoryItem.model_validate({**payload, "story_item_id": "US-0008"})

    with pytest.raises(ValidationError, match="Story item ID"):
        CanonicalStoryItem.model_validate({**payload, "story_item_id": "US-0000"})
    with pytest.raises(ValidationError, match="Story item ID"):
        CanonicalStoryItem.model_validate({**payload, "story_item_id": "US-0009"})
    with pytest.raises(ValidationError, match="must not be blank"):
        CanonicalStoryItem.model_validate({**payload, "story_title": " \u2003"})
    with pytest.raises(ValidationError, match="persona"):
        CanonicalStoryItem.model_validate({**payload, "persona": "Different"})
    with pytest.raises(ValidationError, match="Specification item IDs"):
        CanonicalStoryItem.model_validate(
            {**payload, "spec_item_ids": ("REQ.alpha", "DATA.beta")}
        )

    with pytest.raises(ValidationError, match="fingerprint"):
        StoryItemEnvelope.model_validate(
            {
                "item": {**payload, "invest_score": "Medium"},
                "item_fingerprint": envelope.item_fingerprint,
            }
        )


def test_canonical_story_output_accepts_only_the_host_persisted_envelope() -> None:
    """Keep persisted Story content distinct from ID-free provider output."""
    item = canonicalize_story_items(
        _reference(),
        parent_backlog_spec_item_ids=("DATA.beta", "REQ.alpha"),
        agent_items=(_story(),),
    )[0]

    persisted = CanonicalStoryOutput.model_validate(
        {
            "story_items": [item.model_dump(mode="json")],
            "is_complete": True,
            "clarifying_questions": [],
        }
    )

    assert persisted.story_items == (item,)
    with pytest.raises(ValidationError, match="at least 1"):
        CanonicalStoryOutput.model_validate(
            {
                "story_items": [],
                "is_complete": True,
            }
        )
    with pytest.raises(ValidationError, match="at most 8"):
        CanonicalStoryOutput.model_validate(
            {
                "story_items": [item.model_dump(mode="json")] * 9,
                "is_complete": True,
            }
        )
    with pytest.raises(ValidationError, match="story_items"):
        CanonicalStoryOutput.model_validate(
            {
                "user_stories": [_story().model_dump(mode="json")],
                "is_complete": True,
            }
        )


def test_with_story_schema_repair_feedback_preserves_large_parent_boundary_and_rules(
) -> None:
    """Preserve the complete allow-list and rules even under large parent boundaries."""
    expected_item_count = 60
    items = tuple(
        SpecificationItem(
            id=f"REQ.{i:04d}",
            type=SpecItemType.REQ,
            title=f"Req {i}",
            statement=f"The system must support requirement {i}.",
            level=RequirementLevel.MUST,
            verification=VerificationMethod.UNIT_TEST,
            acceptance=(f"Requirement {i} passes.",),
        )
        for i in range(1, expected_item_count + 1)
    )
    payload_model = SpecificationPayload(
        artifact_id="SPEC.large-boundary",
        title="Large Boundary Specification",
        summary="60 requirement items",
        problem_statement="Testing large parent boundary repair feedback.",
        items=items,
    )
    spec_json = canonical_spec_json(payload_model)
    spec_hash = canonical_spec_hash(payload_model)
    parent_ids = tuple(f"REQ.{i:04d}" for i in range(1, expected_item_count + 1))

    payload = UserStoryWriterInput(
        accepted_specification_version_id=1,
        accepted_specification_hash=spec_hash,
        accepted_specification_json=spec_json,
        parent_backlog_item_id="PBI-000001",
        parent_backlog_spec_item_ids=parent_ids,
    )

    val_errors = [
        f"Specification item ID outside the parent boundary: REQ.UNKNOWN_{i}"
        for i in range(30)
    ]
    repaired = with_story_schema_repair_feedback(
        payload,
        error="; ".join(val_errors),
        validation_errors=val_errors,
        targeted=False,
    )

    assert repaired.user_input is not None
    assert repaired.user_input.endswith("Do not add wrapper fields.")
    assert (
        "Every user story spec_item_ids list must contain non-empty IDs selected "
        "strictly from ALLOWED_PARENT_SPEC_ITEM_IDS."
        in repaired.user_input
    )
    assert (
        "Return JSON only. Match UserStoryWriterOutput exactly. Required fields "
        "are user_stories, is_complete, and clarifying_questions. "
        "Do not add wrapper fields."
        in repaired.user_input
    )
    prefix = "ALLOWED_PARENT_SPEC_ITEM_IDS: "
    start_idx = repaired.user_input.index(prefix) + len(prefix)
    end_idx = repaired.user_input.index("\n", start_idx)
    parsed_ids = json.loads(repaired.user_input[start_idx:end_idx])
    assert parsed_ids == list(parent_ids)
    assert len(parsed_ids) == expected_item_count
