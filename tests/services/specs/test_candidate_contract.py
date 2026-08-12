"""Tests for immutable v2 specification candidate envelopes."""

from __future__ import annotations

import hashlib
from copy import deepcopy

import pytest
from pydantic import ValidationError

from services.specs.candidate_contract import (
    CandidateBuildInput,
    CandidateKind,
    CandidateSourceKind,
    CandidateSourceManifestEntry,
    SpecificationCandidateEnvelope,
    StableIdReplacement,
    build_candidate_envelope,
    canonical_candidate_json,
    compute_amendment_diff,
    load_candidate_contract,
    render_candidate_review_markdown,
)
from utils.agileforge_spec_profile_v2 import SpecificationPayload, canonical_spec_hash


def _payload() -> SpecificationPayload:
    return SpecificationPayload.model_validate(
        {
            "schema_version": "agileforge.spec.v2",
            "artifact_id": "SPEC.cartola",
            "title": "Cartola Champion Squad Selector",
            "summary": "Recommend a valid champion squad.",
            "problem_statement": "Operators need repeatable squad recommendations.",
            "items": [
                {
                    "id": "GOAL.cartola.weekly-decision",
                    "type": "GOAL",
                    "title": "Weekly decision support",
                    "statement": "Help the operator choose a weekly squad.",
                    "acceptance": ["A weekly decision is available."],
                },
                {
                    "id": "REQ.cartola.budget",
                    "type": "REQ",
                    "title": "Budget constraint",
                    "statement": "The selected squad MUST stay within budget.",
                    "level": "MUST",
                    "verification": "system-test",
                    "acceptance": ["A selected squad stays within budget."],
                },
            ],
            "relations": [
                {
                    "from": "REQ.cartola.budget",
                    "type": "satisfies",
                    "to": "GOAL.cartola.weekly-decision",
                }
            ],
            "controlled_terms": [],
            "external_references": [],
        }
    )


WORKFLOW_NODE_ATTEMPT_ID = 71
BASE_SPECIFICATION_ID = 91


def _fingerprint(label: str) -> str:
    return f"sha256:{hashlib.sha256(label.encode('utf-8')).hexdigest()}"


def _envelope(**overrides: object) -> SpecificationCandidateEnvelope:
    values: dict[str, object] = {
        "payload": _payload(),
        "candidate_kind": CandidateKind.INITIAL,
        "accepted_vision_id": 17,
        "accepted_vision_fingerprint": _fingerprint("vision"),
        "accepted_product_goal_id": 23,
        "accepted_product_goal_fingerprint": _fingerprint("goal"),
        "source_manifest": (
            CandidateSourceManifestEntry(
                source_id="SRC.goal",
                kind=CandidateSourceKind.PRODUCT_GOAL,
                fingerprint=_fingerprint("goal"),
            ),
        ),
        "accepted_fact_fingerprint": _fingerprint("facts"),
        "producer_input_fingerprint": _fingerprint("input"),
        "producer_capability": "to-spec",
        "producer_version": "2.0.0",
        "model_id": "model-x",
        "model_configuration_fingerprint": _fingerprint("model-config"),
        "prompt_fingerprint": _fingerprint("prompt"),
        "workflow_node_attempt_id": WORKFLOW_NODE_ATTEMPT_ID,
        "attempt_fingerprint": _fingerprint("attempt"),
        "correlation_id": "correlation-7",
        "produced_at": "2026-08-11T12:00:00Z",
    }
    values.update(overrides)
    payload = values.pop("payload")
    assert isinstance(payload, SpecificationPayload)
    return build_candidate_envelope(
        payload=payload,
        metadata=CandidateBuildInput.model_validate(values),
    )


def test_initial_envelope_binds_immutable_payload_and_complete_review_view() -> None:
    """An initial candidate hashes exact semantic bytes and rendered review."""
    envelope = _envelope()

    assert envelope.candidate_kind is CandidateKind.INITIAL
    assert envelope.envelope_version == "agileforge.spec-candidate-envelope.v1"
    assert envelope.payload_fingerprint == canonical_spec_hash(_payload())
    assert envelope.review_view_fingerprint.startswith("sha256:")
    assert envelope.candidate_fingerprint.startswith("sha256:")
    assert envelope.workflow_node_attempt_id == WORKFLOW_NODE_ATTEMPT_ID
    assert envelope.attempt_fingerprint == _fingerprint("attempt")
    assert envelope.source_manifest_fingerprint.startswith("sha256:")
    assert envelope.base_specification_id is None
    assert envelope.amendment_diff is None
    with pytest.raises(ValidationError):
        envelope.producer_capability = "other"


def test_candidate_canonical_json_round_trips_with_fingerprint_verification() -> None:
    """Persistence can reload exactly one payload-envelope transaction safely."""
    payload = _payload()
    envelope = _envelope()
    serialized = canonical_candidate_json(payload, envelope)

    reloaded_payload, reloaded_envelope = load_candidate_contract(
        serialized,
        expected_candidate_fingerprint=envelope.candidate_fingerprint,
    )

    assert reloaded_payload == payload
    assert reloaded_envelope == envelope
    with pytest.raises(ValueError, match="candidate fingerprint"):
        load_candidate_contract(
            serialized,
            expected_candidate_fingerprint=_fingerprint("other"),
        )
    with pytest.raises(ValueError, match="noncanonical"):
        load_candidate_contract(
            f"{serialized}\n",
            expected_candidate_fingerprint=envelope.candidate_fingerprint,
        )


def test_candidate_fingerprint_excludes_host_execution_metadata() -> None:
    """Equivalent semantic candidates keep one identity across host executions."""
    baseline = _envelope()
    changed_execution = _envelope(
        workflow_node_attempt_id=72,
        attempt_fingerprint=_fingerprint("attempt-8"),
        correlation_id="correlation-8",
        produced_at="2026-08-11T12:00:01Z",
    )
    changed_payload_dict = _payload().model_dump(mode="json")
    items = changed_payload_dict["items"]
    assert isinstance(items, list)
    item = items[1]
    assert isinstance(item, dict)
    item["statement"] = "The selected squad MUST remain below the budget cap."
    changed_payload = SpecificationPayload.model_validate(changed_payload_dict)
    changed_semantics = _envelope(payload=changed_payload)

    assert baseline.candidate_fingerprint == changed_execution.candidate_fingerprint
    assert baseline.review_view_fingerprint != changed_execution.review_view_fingerprint
    assert canonical_candidate_json(
        _payload(), baseline
    ) != canonical_candidate_json(_payload(), changed_execution)
    assert baseline.payload_fingerprint != changed_semantics.payload_fingerprint
    assert baseline.review_view_fingerprint != changed_semantics.review_view_fingerprint
    assert baseline.candidate_fingerprint != changed_semantics.candidate_fingerprint


def test_candidate_fingerprint_excludes_host_lineage_row_ids() -> None:
    """Equivalent content lineage has one identity across recreated databases."""
    baseline = _envelope()
    recreated = _envelope(
        accepted_vision_id=117,
        accepted_product_goal_id=123,
    )

    assert baseline.candidate_fingerprint == recreated.candidate_fingerprint
    assert baseline.review_view_fingerprint != recreated.review_view_fingerprint
    amendment = _envelope(
        candidate_kind=CandidateKind.AMENDMENT,
        base_payload=_payload(),
        base_specification_id=BASE_SPECIFICATION_ID,
        base_payload_fingerprint=canonical_spec_hash(_payload()),
    )
    recreated_amendment = _envelope(
        candidate_kind=CandidateKind.AMENDMENT,
        base_payload=_payload(),
        base_specification_id=BASE_SPECIFICATION_ID + 1,
        base_payload_fingerprint=canonical_spec_hash(_payload()),
    )
    assert amendment.candidate_fingerprint == recreated_amendment.candidate_fingerprint


def test_complete_review_view_includes_envelope_evidence_and_changes_with_it() -> None:
    """Reviewer Markdown commits to source, producer, attempt, and base evidence."""
    baseline = _envelope()
    changed_source = _envelope(
        source_manifest=(
            CandidateSourceManifestEntry(
                source_id="SRC.goal",
                kind=CandidateSourceKind.PRODUCT_GOAL,
                fingerprint=_fingerprint("changed-goal"),
                warnings=("SOURCE_TRUNCATED: Review the bounded excerpt.",),
            ),
        )
    )

    markdown = render_candidate_review_markdown(_payload(), baseline)

    for expected in (
        "Producer capability: to-spec",
        f"Workflow node attempt id: {WORKFLOW_NODE_ATTEMPT_ID}",
        f"Attempt fingerprint: {_fingerprint('attempt')}",
        "SRC.goal",
        _fingerprint("goal"),
    ):
        assert expected in markdown
    assert baseline.review_view_fingerprint != changed_source.review_view_fingerprint
    changed_markdown = render_candidate_review_markdown(_payload(), changed_source)
    assert "SOURCE_TRUNCATED: Review the bounded excerpt." in changed_markdown
    assert markdown != changed_markdown


def test_source_warning_is_nested_under_its_own_manifest_entry() -> None:
    """A warning cannot be visually attributed to the following source."""
    envelope = _envelope(
        source_manifest=(
            CandidateSourceManifestEntry(
                source_id="SRC.repository",
                kind=CandidateSourceKind.REPOSITORY,
                fingerprint=_fingerprint("repository"),
                warnings=("SOURCE_TRUNCATED: Review repository excerpt.",),
            ),
            CandidateSourceManifestEntry(
                source_id="SRC.vision",
                kind=CandidateSourceKind.VISION,
                fingerprint=_fingerprint("vision"),
            ),
        )
    )

    markdown = render_candidate_review_markdown(_payload(), envelope)

    repository_row = markdown.index("- SRC.repository")
    warning_row = markdown.index("  - Warning: SOURCE_TRUNCATED")
    vision_row = markdown.index("- SRC.vision")
    assert repository_row < warning_row < vision_row


def test_complete_review_escapes_untrusted_envelope_text() -> None:
    """Envelope metadata cannot create reviewer-visible Markdown controls."""
    envelope = _envelope(producer_capability="# forged heading")

    markdown = render_candidate_review_markdown(_payload(), envelope)

    assert "Producer capability: \\# forged heading" in markdown


def test_source_and_attempt_metadata_are_closed_and_validated() -> None:
    """Envelope evidence cannot omit paired model configuration or stable source IDs."""
    with pytest.raises(ValidationError, match="model"):
        _envelope(model_configuration_fingerprint=None)

    with pytest.raises(ValidationError):
        CandidateSourceManifestEntry(
            source_id="",
            kind=CandidateSourceKind.PRODUCT_GOAL,
            fingerprint="sha256:goal",
        )


def test_payload_source_notes_must_reference_the_host_manifest() -> None:
    """A provider cannot invent source identities outside persisted host input."""
    payload_data = _payload().model_dump(mode="json")
    items = payload_data["items"]
    assert isinstance(items, list)
    first = items[0]
    assert isinstance(first, dict)
    first["source_notes"] = [
        {
            "source_id": "SRC.untrusted",
            "kind": "external_summary",
            "text": "Provider-invented provenance.",
        }
    ]
    payload = SpecificationPayload.model_validate(payload_data)

    with pytest.raises(ValueError, match="source manifest"):
        _envelope(payload=payload)


def test_amendment_diff_is_deterministic_and_pins_the_exact_base() -> None:
    """Amendments retain a full result and explicit stable-ID change evidence."""
    base = _payload()
    candidate_data = deepcopy(base.model_dump(mode="json"))
    items = candidate_data["items"]
    assert isinstance(items, list)
    requirement = items[1]
    assert isinstance(requirement, dict)
    requirement["statement"] = "The selected squad MUST remain below the budget cap."
    items.append(
        {
            "id": "QUALITY.cartola.response-time",
            "type": "QUALITY",
            "title": "Response time",
            "statement": "Recommendations should render promptly.",
            "level": "SHOULD",
            "verification": "system-test",
            "acceptance": ["A recommendation renders within the target."],
        }
    )
    amendment = SpecificationPayload.model_validate(candidate_data)

    diff = compute_amendment_diff(base, amendment)
    envelope = _envelope(
        payload=amendment,
        candidate_kind=CandidateKind.AMENDMENT,
        base_payload=base,
        base_specification_id=BASE_SPECIFICATION_ID,
        base_payload_fingerprint=canonical_spec_hash(base),
    )

    assert diff.added_item_ids == ("QUALITY.cartola.response-time",)
    assert diff.changed_item_ids == ("REQ.cartola.budget",)
    assert diff.removed_item_ids == ()
    assert envelope.amendment_diff == diff
    assert envelope.base_specification_id == BASE_SPECIFICATION_ID


def test_amendment_diff_tracks_relations_terms_and_external_references() -> None:
    """Non-item semantic collections have deterministic amendment deltas too."""
    base = _payload()
    candidate_data = deepcopy(base.model_dump(mode="json"))
    relations = candidate_data["relations"]
    terms = candidate_data["controlled_terms"]
    references = candidate_data["external_references"]
    assert isinstance(relations, list)
    assert isinstance(terms, list)
    assert isinstance(references, list)
    relations.append(
        {
            "from": "GOAL.cartola.weekly-decision",
            "type": "clarifies",
            "to": "REQ.cartola.budget",
        }
    )
    terms.append(
        {
            "term": "Budget",
            "definition": "The squad spending cap.",
            "scope": "project",
        }
    )
    references.append(
        {
            "id": "EXT.budget-rules",
            "title": "Budget rules",
            "summary": "Published budget constraints.",
        }
    )
    amendment = SpecificationPayload.model_validate(candidate_data)

    diff = compute_amendment_diff(base, amendment)

    assert diff.relations.added == (
        "clarifies:GOAL.cartola.weekly-decision->REQ.cartola.budget",
    )
    assert diff.controlled_terms.added == ("budget:project",)
    assert diff.external_references.added == ("EXT.budget-rules",)


def test_amendment_diff_tracks_every_mutable_top_level_field() -> None:
    """Reviewers see top-level semantic changes outside stable collections."""
    base = _payload()
    candidate_data = deepcopy(base.model_dump(mode="json"))
    candidate_data.update(
        {
            "title": "Champion Squad Decision",
            "summary": "Recommend a valid squad with explicit evidence.",
            "problem_statement": "Operators need auditable squad recommendations.",
        }
    )
    amendment = SpecificationPayload.model_validate(candidate_data)

    diff = compute_amendment_diff(base, amendment)

    assert diff.changed_fields == ("title", "summary", "problem_statement")


def test_amendment_diff_uses_canonical_tag_set_order() -> None:
    """Reordering set-like tags cannot create a semantic amendment delta."""
    base_data = deepcopy(_payload().model_dump(mode="json"))
    items = base_data["items"]
    assert isinstance(items, list)
    item = items[1]
    assert isinstance(item, dict)
    item["tags"] = ["budget", "weekly"]
    base = SpecificationPayload.model_validate(base_data)
    amendment_data = deepcopy(base_data)
    amendment_items = amendment_data["items"]
    assert isinstance(amendment_items, list)
    amendment_item = amendment_items[1]
    assert isinstance(amendment_item, dict)
    amendment_item["tags"] = ["weekly", "budget"]
    amendment = SpecificationPayload.model_validate(amendment_data)

    assert canonical_spec_hash(base) == canonical_spec_hash(amendment)
    assert compute_amendment_diff(base, amendment).changed_item_ids == ()


def test_amendment_rejects_artifact_identity_change() -> None:
    """An amendment cannot silently replace the stable Specification identity."""
    base = _payload()
    candidate_data = deepcopy(base.model_dump(mode="json"))
    candidate_data["artifact_id"] = "SPEC.different"
    amendment = SpecificationPayload.model_validate(candidate_data)

    with pytest.raises(ValueError, match="artifact id"):
        compute_amendment_diff(base, amendment)


def test_amendment_rejects_stale_base_and_unexplained_removals() -> None:
    """A candidate cannot replace scope against stale or opaque base evidence."""
    base = _payload()
    candidate_data = deepcopy(base.model_dump(mode="json"))
    items = candidate_data["items"]
    assert isinstance(items, list)
    candidate_data["items"] = [items[1]]
    candidate_data["relations"] = []
    amendment = SpecificationPayload.model_validate(candidate_data)

    with pytest.raises(ValueError, match="stale base"):
        _envelope(
            payload=amendment,
            candidate_kind=CandidateKind.AMENDMENT,
            base_payload=base,
            base_specification_id=BASE_SPECIFICATION_ID,
            base_payload_fingerprint=_fingerprint("stale"),
        )


def test_amendment_requires_reasons_for_every_removed_semantic_entry() -> None:
    """Relations, terms, and references cannot disappear without explanation."""
    base_data = deepcopy(_payload().model_dump(mode="json"))
    terms = base_data["controlled_terms"]
    references = base_data["external_references"]
    assert isinstance(terms, list)
    assert isinstance(references, list)
    terms.append(
        {
            "term": "Budget",
            "definition": "The squad spending cap.",
            "scope": "project",
        }
    )
    references.append(
        {
            "id": "EXT.budget-rules",
            "title": "Budget rules",
            "summary": "Published budget constraints.",
        }
    )
    base = SpecificationPayload.model_validate(base_data)
    amendment_data = deepcopy(base_data)
    amendment_data["relations"] = []
    amendment_data["controlled_terms"] = []
    amendment_data["external_references"] = []
    amendment = SpecificationPayload.model_validate(amendment_data)
    removed = {
        "satisfies:REQ.cartola.budget->GOAL.cartola.weekly-decision": (
            "The explicit link is obsolete."
        ),
        "budget:project": "The term is no longer needed.",
        "EXT.budget-rules": "The external reference was retired.",
    }

    with pytest.raises(ValueError, match="every removed semantic entry"):
        compute_amendment_diff(base, amendment)

    diff = compute_amendment_diff(
        base,
        amendment,
        removal_justifications=removed,
    )

    assert diff.relations.removed == (
        "satisfies:REQ.cartola.budget->GOAL.cartola.weekly-decision",
    )
    assert diff.controlled_terms.removed == ("budget:project",)
    assert diff.external_references.removed == ("EXT.budget-rules",)
    assert dict(diff.removal_justifications) == removed

    with pytest.raises(ValueError, match="removal justification"):
        _envelope(
            payload=amendment,
            candidate_kind=CandidateKind.AMENDMENT,
            base_payload=base,
            base_specification_id=BASE_SPECIFICATION_ID,
            base_payload_fingerprint=canonical_spec_hash(base),
        )


def test_stable_id_replacement_must_map_removed_to_added_item() -> None:
    """Stable-ID replacements must point from one removed item to one addition."""
    base = _payload()
    candidate_data = deepcopy(base.model_dump(mode="json"))
    items = candidate_data["items"]
    assert isinstance(items, list)
    candidate_data["items"] = [
        {
            "id": "GOAL.cartola.weekly-selection",
            "type": "GOAL",
            "title": "Weekly selection",
            "statement": "Help the operator select a weekly squad.",
            "acceptance": ["A weekly selection is available."],
        },
        items[1],
    ]
    relations = candidate_data["relations"]
    assert isinstance(relations, list)
    relation = relations[0]
    assert isinstance(relation, dict)
    relation["to"] = "GOAL.cartola.weekly-selection"
    amendment = SpecificationPayload.model_validate(candidate_data)

    replacement = StableIdReplacement(
        old_item_id="GOAL.cartola.weekly-decision",
        new_item_id="GOAL.cartola.weekly-selection",
        justification="The stable goal ID was corrected before implementation.",
    )
    envelope = _envelope(
        payload=amendment,
        candidate_kind=CandidateKind.AMENDMENT,
        base_payload=base,
        base_specification_id=BASE_SPECIFICATION_ID,
        base_payload_fingerprint=canonical_spec_hash(base),
        removal_justifications={
            "GOAL.cartola.weekly-decision": "The stable goal ID was corrected.",
            "satisfies:REQ.cartola.budget->GOAL.cartola.weekly-decision": (
                "The relation now targets the replacement goal."
            ),
        },
        stable_id_replacements=(replacement,),
    )
    assert envelope.amendment_diff is not None
    assert envelope.amendment_diff.replacements == (replacement,)


def test_stable_id_replacements_are_canonical_under_permutation() -> None:
    """Set-like replacement mappings have one diff and candidate identity."""
    base = _payload()
    candidate_data = deepcopy(base.model_dump(mode="json"))
    candidate_data["items"] = [
        {
            "id": "GOAL.cartola.selection",
            "type": "GOAL",
            "title": "Selection",
            "statement": "Help the operator select a weekly squad.",
            "acceptance": ["A weekly selection is available."],
        },
        {
            "id": "REQ.cartola.cap",
            "type": "REQ",
            "title": "Cap constraint",
            "statement": "The selected squad MUST stay within budget.",
            "level": "MUST",
            "verification": "system-test",
            "acceptance": ["A selected squad stays within budget."],
        },
    ]
    candidate_data["relations"] = [
        {
            "from": "REQ.cartola.cap",
            "type": "satisfies",
            "to": "GOAL.cartola.selection",
        }
    ]
    amendment = SpecificationPayload.model_validate(candidate_data)
    replacements = (
        StableIdReplacement(
            old_item_id="REQ.cartola.budget",
            new_item_id="REQ.cartola.cap",
            justification="Use the domain term cap.",
        ),
        StableIdReplacement(
            old_item_id="GOAL.cartola.weekly-decision",
            new_item_id="GOAL.cartola.selection",
            justification="Use the stable selection name.",
        ),
    )
    reasons = {
        "REQ.cartola.budget": "Replaced by the cap requirement.",
        "GOAL.cartola.weekly-decision": "Replaced by the selection goal.",
        "satisfies:REQ.cartola.budget->GOAL.cartola.weekly-decision": (
            "Relation endpoints were replaced."
        ),
    }

    first = _envelope(
        payload=amendment,
        candidate_kind=CandidateKind.AMENDMENT,
        base_payload=base,
        base_specification_id=BASE_SPECIFICATION_ID,
        base_payload_fingerprint=canonical_spec_hash(base),
        removal_justifications=reasons,
        stable_id_replacements=replacements,
    )
    second = _envelope(
        payload=amendment,
        candidate_kind=CandidateKind.AMENDMENT,
        base_payload=base,
        base_specification_id=BASE_SPECIFICATION_ID,
        base_payload_fingerprint=canonical_spec_hash(base),
        removal_justifications=reasons,
        stable_id_replacements=tuple(reversed(replacements)),
    )

    assert first.amendment_diff == second.amendment_diff
    assert first.candidate_fingerprint == second.candidate_fingerprint
