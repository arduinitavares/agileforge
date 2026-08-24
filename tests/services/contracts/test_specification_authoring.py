"""Closed Specification structuring input and output contracts."""

from __future__ import annotations

import base64
import hashlib

import pytest
from pydantic import ValidationError

import services.contracts.specification_authoring as structuring_contracts
from services.contracts.specification_authoring import (
    SPECIFICATION_PRODUCT_GOAL_SOURCE_ID,
    SPECIFICATION_VISION_SOURCE_ID,
    AcceptedProductGoalContext,
    AcceptedVisionContext,
    RegisteredRepositoryEvidence,
    RegisteredSpecificationSource,
    SpecificationStructuringContextCapture,
    SpecificationStructuringDocument,
    SpecificationStructuringInput,
    SpecificationStructuringOutput,
    specification_structuring_completion_payload,
    specification_structuring_fact_fingerprint,
    specification_structuring_input_fingerprint,
)
from services.contracts.specification_source import (
    SPECIFICATION_SOURCE_CONTEXT_ID,
    SPECIFICATION_SOURCE_PRIMARY_ID,
    SpecificationContextCapture,
    SpecificationRepositoryRevision,
    SpecificationSourceBundle,
    SpecificationSourceDocument,
    source_bundle_fingerprint,
    specification_source_adr_id,
)
from services.specs.candidate_contract import (
    CandidateSourceKind,
    CandidateSourceManifestEntry,
)

FINGERPRINT = "sha256:" + ("a" * 64)
STATUS_FINGERPRINT = "sha256:" + ("b" * 64)
BINDING_FINGERPRINT = "sha256:" + ("c" * 64)
ADR_PATH = "docs/adr/0001-typed-boundary.md"


def _document(
    source_id: str,
    relative_path: str,
    text: str,
) -> SpecificationSourceDocument:
    raw = text.encode("utf-8")
    return SpecificationSourceDocument(
        source_id=source_id,
        relative_path=relative_path,
        content_base64=base64.b64encode(raw).decode("ascii"),
        byte_length=len(raw),
        content_fingerprint="sha256:" + hashlib.sha256(raw).hexdigest(),
    )


def _structuring_document(
    document: SpecificationSourceDocument,
) -> SpecificationStructuringDocument:
    return SpecificationStructuringDocument(
        source_id=document.source_id,
        relative_path=document.relative_path,
        text=base64.b64decode(document.content_base64, validate=True).decode("utf-8"),
        byte_length=document.byte_length,
        content_fingerprint=document.content_fingerprint,
    )


def _registered_source() -> RegisteredSpecificationSource:
    primary = _document(
        SPECIFICATION_SOURCE_PRIMARY_ID,
        "SPECIFICATION.md",
        "\ufeff# Exact source\r\n\r\nKeep trailing bytes.\r\n",
    )
    context = _document(
        SPECIFICATION_SOURCE_CONTEXT_ID,
        "CONTEXT.md",
        "Context prose.\n",
    )
    adr = _document(
        specification_source_adr_id(ADR_PATH),
        ADR_PATH,
        "# Typed boundary\n",
    )
    revision = SpecificationRepositoryRevision(
        head_sha="d" * 40,
        dirty=True,
        status_fingerprint=STATUS_FINGERPRINT,
    )
    bundle = SpecificationSourceBundle(
        source=primary,
        context=SpecificationContextCapture(state="present", document=context),
        adrs=(adr,),
        repository_revision=revision,
        accepted_vision_fingerprint=FINGERPRINT,
        accepted_product_goal_fingerprint=FINGERPRINT,
    )
    return RegisteredSpecificationSource(
        specification_source_id=7,
        source_fingerprint=source_bundle_fingerprint(bundle),
        producer_capability=bundle.producer_capability,
        preparation_capability=bundle.preparation_capability,
        source=_structuring_document(primary),
        context=SpecificationStructuringContextCapture(
            state="present",
            document=_structuring_document(context),
        ),
        adrs=(_structuring_document(adr),),
        repository_revision=revision,
        repository_evidence=RegisteredRepositoryEvidence(
            repository_binding_id=13,
            binding_fingerprint=BINDING_FINGERPRINT,
            head_sha=revision.head_sha,
            branch_name="main",
            detached_head=False,
            dirty=revision.dirty,
            status_fingerprint=revision.status_fingerprint,
            status_entries=(
                {
                    "area": "worktree",
                    "change": "modified",
                    "path": "SPECIFICATION.md",
                    "previous_path": None,
                },
            ),
            remotes=("https://example.invalid/repository.git",),
            warnings=(
                {
                    "code": "DIRTY_WORKTREE",
                    "message": "Repository has uncommitted changes.",
                },
            ),
            probe_version="agileforge.repository-probe.v1",
        ),
        accepted_vision_fingerprint=bundle.accepted_vision_fingerprint,
        accepted_product_goal_fingerprint=(bundle.accepted_product_goal_fingerprint),
    )


def _input() -> SpecificationStructuringInput:
    registered = _registered_source()
    context_document = registered.context.document
    assert context_document is not None
    manifest = (
        CandidateSourceManifestEntry(
            source_id=SPECIFICATION_VISION_SOURCE_ID,
            kind=CandidateSourceKind.VISION,
            fingerprint=FINGERPRINT,
        ),
        CandidateSourceManifestEntry(
            source_id=SPECIFICATION_PRODUCT_GOAL_SOURCE_ID,
            kind=CandidateSourceKind.PRODUCT_GOAL,
            fingerprint=FINGERPRINT,
        ),
        CandidateSourceManifestEntry(
            source_id=registered.source.source_id,
            kind=CandidateSourceKind.EXTERNAL,
            fingerprint=registered.source.content_fingerprint,
        ),
        CandidateSourceManifestEntry(
            source_id=context_document.source_id,
            kind=CandidateSourceKind.REPOSITORY,
            fingerprint=context_document.content_fingerprint,
        ),
        CandidateSourceManifestEntry(
            source_id=registered.adrs[0].source_id,
            kind=CandidateSourceKind.REPOSITORY,
            fingerprint=registered.adrs[0].content_fingerprint,
        ),
    )
    return SpecificationStructuringInput(
        project_id=9,
        project_name="Contract project",
        operation="initial",
        accepted_vision=AcceptedVisionContext(
            artifact_id=1,
            fingerprint=FINGERPRINT,
            statement="Operators can trust one exact lifecycle.",
            components={"target_user": "operator"},
            component_basis=(),
            assumptions=(),
            conflicts=(),
        ),
        accepted_product_goal=AcceptedProductGoalContext(
            artifact_id=2,
            fingerprint=FINGERPRINT,
            statement="Complete one accepted product increment.",
        ),
        registered_source=registered,
        source_manifest=manifest,
    )


def test_structuring_input_is_closed_and_binds_exact_registered_source() -> None:
    """Every provider source is exact registered text with one stable ID."""
    contract = _input()

    assert contract.schema_version == "agileforge.spec-structuring-input.v1"
    assert contract.operation == "initial"
    assert contract.base_specification is None
    assert contract.prior_candidate is None
    assert contract.registered_source.source.text.startswith("\ufeff# Exact source\r\n")
    assert contract.registered_source.source.text.endswith("bytes.\r\n")
    assert contract.registered_source.context.state == "present"
    assert contract.registered_source.context.document is not None
    assert contract.registered_source.context.document.text == "Context prose.\n"
    assert contract.registered_source.adrs[0].text == "# Typed boundary\n"
    assert tuple(item.source_id for item in contract.source_manifest) == tuple(
        sorted(
            {
                SPECIFICATION_PRODUCT_GOAL_SOURCE_ID,
                SPECIFICATION_SOURCE_CONTEXT_ID,
                SPECIFICATION_SOURCE_PRIMARY_ID,
                SPECIFICATION_VISION_SOURCE_ID,
                specification_source_adr_id(ADR_PATH),
            }
        )
    )

    raw = contract.model_dump(mode="json")
    raw["raw_markdown"] = "# caller supplied"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        SpecificationStructuringInput.model_validate(raw)


def test_structuring_input_rejects_text_not_bound_to_registered_bytes() -> None:
    """Decoded prose cannot drift from its raw-byte length and digest."""
    raw = _input().model_dump(mode="json")
    registered = raw["registered_source"]
    assert isinstance(registered, dict)
    source = registered["source"]
    assert isinstance(source, dict)
    source["text"] = "Changed but caller kept the registered fingerprint."

    with pytest.raises(ValidationError, match="exact UTF-8 bytes"):
        SpecificationStructuringInput.model_validate(raw)


def test_structuring_input_rejects_manifest_outside_registered_bundle() -> None:
    """No unregistered repository or historical evidence can enter the call."""
    raw = _input().model_dump(mode="json")
    manifest = raw["source_manifest"]
    assert isinstance(manifest, list)
    manifest.append(
        {
            "source_id": "SRC.repository-context.active",
            "kind": "repository",
            "fingerprint": FINGERPRINT,
            "warnings": [],
        }
    )

    with pytest.raises(ValidationError, match="exactly match the registered"):
        SpecificationStructuringInput.model_validate(raw)


def test_structuring_contract_has_no_authoring_compatibility_symbols() -> None:
    """The terminology cutover is a hard break without aliases."""
    retired = {
        "SPECIFICATION_AUTHOR_VERSION",
        "SPECIFICATION_AUTHOR_PROMPT_HASH",
        "SpecificationAuthoringInput",
        "specification_authoring_input_fingerprint",
        "specification_authoring_fact_fingerprint",
    }

    assert all(not hasattr(structuring_contracts, name) for name in retired)


def test_structuring_output_contains_only_semantics_and_amendment_declarations() -> (
    None
):
    """The model cannot choose lineage, hashes, review state, or attempt metadata."""
    output = SpecificationStructuringOutput.model_validate(
        {
            "payload": {
                "schema_version": "agileforge.spec.v2",
                "artifact_id": "SPEC.direct-boundary",
                "title": "Direct boundary",
                "summary": "One typed Specification structuring boundary.",
                "problem_statement": "Raw workflow JSON is ambiguous.",
                "items": [
                    {
                        "id": "REQ.direct-boundary",
                        "type": "REQ",
                        "title": "Typed structuring",
                        "statement": "The producer MUST return a typed payload.",
                        "level": "MUST",
                        "verification": "system-test",
                        "acceptance": ["The payload validates as v2."],
                        "source_notes": [
                            {
                                "source_id": SPECIFICATION_SOURCE_PRIMARY_ID,
                                "kind": "external_summary",
                                "text": "Exact registered to-spec source.",
                            }
                        ],
                    }
                ],
            },
            "removal_justifications": [],
            "stable_id_replacements": [],
        }
    )

    assert output.payload.artifact_id == "SPEC.direct-boundary"
    raw = output.model_dump(mode="json")
    raw["candidate_fingerprint"] = FINGERPRINT
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        SpecificationStructuringOutput.model_validate(raw)


def test_structuring_output_exposes_the_exact_nested_closed_v2_schema() -> None:
    """Give the provider one closed wrapper around the complete v2 profile."""
    schema = SpecificationStructuringOutput.model_json_schema()

    assert schema["additionalProperties"] is False
    assert schema["required"] == ["payload"]
    assert set(schema["properties"]) == {
        "payload",
        "removal_justifications",
        "stable_id_replacements",
    }
    assert schema["properties"]["payload"] == {"$ref": "#/$defs/SpecificationPayload"}
    payload_schema = schema["$defs"]["SpecificationPayload"]
    assert payload_schema["additionalProperties"] is False
    assert payload_schema["properties"]["schema_version"]["const"] == (
        "agileforge.spec.v2"
    )


def test_structuring_output_rejects_duplicate_removal_justification_ids() -> None:
    """One removed semantic entry cannot carry conflicting model declarations."""
    raw = {
        "payload": {
            "schema_version": "agileforge.spec.v2",
            "artifact_id": "SPEC.duplicate-removal",
            "title": "Duplicate removal",
            "summary": "Reject ambiguous removal declarations.",
            "problem_statement": "Repeated IDs cannot map deterministically.",
            "items": [],
        },
        "removal_justifications": [
            {"item_id": "REQ.old", "justification": "First reason."},
            {"item_id": "REQ.old", "justification": "Second reason."},
        ],
        "stable_id_replacements": [],
    }

    with pytest.raises(
        ValidationError,
        match="removal justification IDs must be unique",
    ):
        SpecificationStructuringOutput.model_validate(raw)


def test_structuring_output_projects_removal_records_to_internal_map() -> None:
    """Keep the provider-safe record shape outside the persisted host contract."""
    output = SpecificationStructuringOutput.model_validate(
        {
            "payload": {
                "schema_version": "agileforge.spec.v2",
                "artifact_id": "SPEC.removal-projection",
                "title": "Removal projection",
                "summary": "Project typed records into deterministic host metadata.",
                "problem_statement": "Provider and host shapes have different needs.",
                "items": [],
            },
            "removal_justifications": [
                {"item_id": "REQ.z", "justification": "Remove Z."},
                {"item_id": "REQ.a", "justification": "Remove A."},
            ],
            "stable_id_replacements": [],
        }
    )
    projected = specification_structuring_completion_payload(output)
    assert projected["removal_justifications"] == {
        "REQ.a": "Remove A.",
        "REQ.z": "Remove Z.",
    }


@pytest.mark.parametrize(
    "raw",
    [
        {
            "schema_version": "agileforge.spec.v2",
            "artifact_id": "SPEC.bare",
            "title": "Bare payload",
            "summary": "The provider omitted the required wrapper.",
            "problem_statement": "Bare provider output is ambiguous.",
            "items": [],
        },
        {
            "payload": {
                "schema_version": "agileforge.spec.v1",
                "artifact_id": "SPEC.legacy",
                "title": "Legacy payload",
                "summary": "The provider emitted a retired profile.",
                "problem_statement": "Only v2 is supported.",
                "items": [],
            }
        },
        {
            "payload": {
                "schema_version": "agileforge.spec.v2",
                "artifact_id": "SPEC.hidden-field",
                "title": "Hidden field",
                "summary": "The provider added undeclared semantic bytes.",
                "problem_statement": "Nested output must be closed.",
                "items": [],
                "lifecycle_state": "accepted",
            }
        },
    ],
)
def test_structuring_output_rejects_bare_legacy_or_extra_field_results(
    raw: dict[str, object],
) -> None:
    """Reject provider output outside the exact nested v2 contract."""
    with pytest.raises(ValidationError):
        SpecificationStructuringOutput.model_validate(raw)


def test_structuring_fingerprints_exclude_host_database_identity() -> None:
    """Equivalent semantic structuring input has one portable producer identity."""
    baseline = _input()
    recreated_data = baseline.model_dump(mode="json")
    recreated_data["project_id"] = 109
    accepted_vision = recreated_data["accepted_vision"]
    accepted_goal = recreated_data["accepted_product_goal"]
    registered = recreated_data["registered_source"]
    assert isinstance(accepted_vision, dict)
    assert isinstance(accepted_goal, dict)
    assert isinstance(registered, dict)
    accepted_vision["artifact_id"] = 101
    accepted_goal["artifact_id"] = 102
    registered["specification_source_id"] = 107
    evidence = registered["repository_evidence"]
    assert isinstance(evidence, dict)
    evidence["repository_binding_id"] = 113
    evidence["binding_fingerprint"] = "sha256:" + ("e" * 64)
    recreated = SpecificationStructuringInput.model_validate(recreated_data)

    assert specification_structuring_input_fingerprint(baseline) == (
        specification_structuring_input_fingerprint(recreated)
    )
    assert specification_structuring_fact_fingerprint(baseline) == (
        specification_structuring_fact_fingerprint(recreated)
    )

    changed_data = baseline.model_dump(mode="json")
    changed_registered = changed_data["registered_source"]
    assert isinstance(changed_registered, dict)
    changed_revision = changed_registered["repository_revision"]
    changed_evidence = changed_registered["repository_evidence"]
    assert isinstance(changed_revision, dict)
    assert isinstance(changed_evidence, dict)
    changed_revision["head_sha"] = "f" * 40
    changed_evidence["head_sha"] = "f" * 40
    source_fp = "sha256:" + ("1" * 64)
    changed_registered["source_fingerprint"] = source_fp
    with pytest.raises(ValidationError, match="source fingerprint"):
        SpecificationStructuringInput.model_validate(changed_data)


def test_initial_input_rejects_base_or_prior_candidate() -> None:
    """Initial composition cannot hide amendment or revision context."""
    raw = _input().model_dump(mode="json")
    raw["base_specification"] = {
        "spec_version_id": 3,
        "payload_fingerprint": FINGERPRINT,
        "payload": {
            "schema_version": "agileforge.spec.v2",
            "artifact_id": "SPEC.base",
            "title": "Base",
            "summary": "Base summary.",
            "problem_statement": "Base problem.",
            "items": [],
        },
    }

    with pytest.raises(ValidationError, match="initial structuring cannot include"):
        SpecificationStructuringInput.model_validate(raw)
