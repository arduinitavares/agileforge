"""Tests for authority quality benchmark helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import scripts.authority_quality_benchmark as aqb
from scripts.authority_quality_benchmark import (
    build_run_manifest,
    build_source_meta,
    evaluate_todomvc_authority_guardrails,
    extract_compiled_authority,
    main,
    normalize_source_text,
    sanitize_review_packet,
    sha256_text,
    write_json,
    write_text,
)

if TYPE_CHECKING:
    import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
REVIEW_FIELD = "review_" "token"
REDACTED_VALUE = "secret-" "token"
REDACTED_SHORT_VALUE = "secret"
CLI_USAGE_ERROR = 2


def _petstore_fixture_dir() -> Path:
    return REPO_ROOT / "benchmarks/authority-quality/petstore"


def _gherkin_fixture_dir() -> Path:
    return REPO_ROOT / "benchmarks/authority-quality/gherkin"


def _petstore_gold_spec() -> dict[str, Any]:
    return json.loads(
        (_petstore_fixture_dir() / "agileforge/gold-spec/spec.json").read_text(
            encoding="utf-8"
        )
    )


def _complete_petstore_authority() -> dict[str, Any]:
    return {
        "invariants": [
            {
                "id": "INV-list-pets-route",
                "type": "ROUTE_CONTRACT",
                "parameters": {
                    "source_item_id": "INTERFACE.list-pets",
                    "source_level": "MUST",
                    "route": "GET /pets",
                    "route_name": "list pets",
                    "behavior": (
                        "GET /pets lists pets and returns a 200 response with "
                        "a Pets array; default errors return Error."
                    ),
                },
            },
            {
                "id": "INV-create-pet-route",
                "type": "ROUTE_CONTRACT",
                "parameters": {
                    "source_item_id": "INTERFACE.create-pet",
                    "source_level": "MUST",
                    "route": "POST /pets",
                    "route_name": "create pet",
                    "behavior": (
                        "POST /pets creates a pet from a Pet request body and "
                        "returns the created Pet; default errors return Error."
                    ),
                },
            },
            {
                "id": "INV-get-pet-route",
                "type": "ROUTE_CONTRACT",
                "parameters": {
                    "source_item_id": "INTERFACE.get-pet",
                    "source_level": "MUST",
                    "route": "GET /pets/{petId}",
                    "route_name": "show pet",
                    "behavior": (
                        "GET /pets/{petId} returns the matching Pet on success "
                        "and default errors return Error."
                    ),
                },
            },
            {
                "id": "INV-limit-max",
                "type": "MAX_VALUE",
                "parameters": {"field_name": "limit", "max_value": 100},
                "source_refs": ["CONSTRAINT.limit-maximum.statement"],
            },
            {
                "id": "INV-pet-id-param",
                "type": "DATA_CONTRACT",
                "parameters": {
                    "source_item_id": "INTERFACE.pet-id-path-parameter",
                    "source_level": "MUST",
                    "subject": "petId path parameter",
                    "fields": ["petId"],
                    "rule": "petId is a required path parameter for /pets/{petId}.",
                },
            },
            {
                "id": "INV-pet-schema",
                "type": "DATA_CONTRACT",
                "parameters": {
                    "source_item_id": "DATA.pet-schema",
                    "source_level": "MUST",
                    "subject": "Pet schema",
                    "fields": ["id", "name"],
                    "rule": "Pet requires id and name fields.",
                },
            },
            {
                "id": "INV-pets-array",
                "type": "DATA_CONTRACT",
                "parameters": {
                    "source_item_id": "DATA.pets-array",
                    "source_level": "MUST",
                    "subject": "Pets array",
                    "fields": ["Pet"],
                    "rule": "Pets is an array of Pet objects with maxItems 100.",
                },
            },
            {
                "id": "INV-error-schema",
                "type": "DATA_CONTRACT",
                "parameters": {
                    "source_item_id": "DATA.error-schema",
                    "source_level": "MUST",
                    "subject": "Error schema",
                    "fields": ["code", "message"],
                    "rule": "Error requires code and message fields.",
                },
            },
        ],
        "source_map": [
            {
                "invariant_id": "INV-list-pets-route",
                "location": "INTERFACE.list-pets.statement",
                "excerpt": "GET /pets lists pets and returns a Pets array.",
            },
            {
                "invariant_id": "INV-create-pet-route",
                "location": "INTERFACE.create-pet.statement",
                "excerpt": "POST /pets creates a pet from a Pet request body.",
            },
            {
                "invariant_id": "INV-get-pet-route",
                "location": "INTERFACE.get-pet.statement",
                "excerpt": "GET /pets/{petId} returns a pet.",
            },
        ],
    }


def _gherkin_gold_spec() -> dict[str, Any]:
    return json.loads(
        (_gherkin_fixture_dir() / "agileforge/gold-spec/spec.json").read_text(
            encoding="utf-8"
        )
    )


def _complete_gherkin_authority() -> dict[str, Any]:
    return {
        "invariants": [
            {
                "id": "INV-member-discount-given",
                "type": "STATE_TRANSITION",
                "parameters": {
                    "source_item_id": "REQ.member-discount-scenario",
                    "source_level": "MUST",
                    "state": "checkout cart",
                    "trigger": (
                        "Given a signed-in member has an eligible cart "
                        "totaling 100.00"
                    ),
                    "outcome": (
                        "eligibility context is established for the member "
                        "discount scenario"
                    ),
                },
            },
            {
                "id": "INV-member-discount-when-then",
                "type": "STATE_TRANSITION",
                "parameters": {
                    "source_item_id": "REQ.member-discount-scenario",
                    "source_level": "MUST",
                    "state": "checkout total",
                    "trigger": "When the checkout total is calculated",
                    "outcome": (
                        "Then a 10 percent member discount is applied and "
                        "the final total is shown as 90.00"
                    ),
                },
            },
            {
                "id": "INV-expired-coupon-flow",
                "type": "USER_INTERACTION",
                "parameters": {
                    "source_item_id": "REQ.expired-coupon-outline",
                    "source_level": "MUST",
                    "trigger": "When the shopper applies an expired coupon",
                    "target": "coupon code field",
                    "expected_response": (
                        "Then the coupon is rejected and the Coupon expired "
                        "message is shown for each scenario outline example"
                    ),
                },
            },
            {
                "id": "INV-expired-coupon-examples",
                "type": "DATA_CONTRACT",
                "parameters": {
                    "source_item_id": "REQ.expired-coupon-outline",
                    "source_level": "MUST",
                    "subject": "Scenario Outline Examples table",
                    "fields": ["code", "expired_on", "message"],
                    "rule": (
                        "Examples include SPRING10 and SAVE20 rows with "
                        "expired_on dates and Coupon expired messages."
                    ),
                },
            },
            {
                "id": "INV-address-doc-string",
                "type": "DATA_CONTRACT",
                "parameters": {
                    "source_item_id": "REQ.shipping-address-arguments",
                    "source_level": "MUST",
                    "subject": "shipping address doc string",
                    "fields": ["address"],
                    "rule": (
                        "The shipping address doc string with 100 Market Street "
                        "and Springfield, CA 90000 is retained during order "
                        "validation."
                    ),
                },
            },
            {
                "id": "INV-line-items-data-table",
                "type": "DATA_CONTRACT",
                "parameters": {
                    "source_item_id": "REQ.shipping-address-arguments",
                    "source_level": "MUST",
                    "subject": "line item data table",
                    "fields": ["sku", "quantity"],
                    "rule": (
                        "The line item data table rows for SKU-1 and SKU-2 "
                        "are retained during order validation."
                    ),
                },
            },
            {
                "id": "INV-address-validation-flow",
                "type": "STATE_TRANSITION",
                "parameters": {
                    "source_item_id": "REQ.shipping-address-arguments",
                    "source_level": "MUST",
                    "state": "order validation",
                    "trigger": "When the order is validated",
                    "outcome": (
                        "Then the shipping address doc string and line item "
                        "data table are retained"
                    ),
                },
            },
        ],
        "source_map": [
            {
                "invariant_id": "INV-member-discount-given",
                "location": "REQ.member-discount-scenario.acceptance[0]",
                "excerpt": "Given a signed-in member has an eligible cart.",
            },
            {
                "invariant_id": "INV-expired-coupon-flow",
                "location": "REQ.expired-coupon-outline.statement",
                "excerpt": "Scenario Outline: Expired coupons are rejected.",
            },
            {
                "invariant_id": "INV-address-doc-string",
                "location": "REQ.shipping-address-arguments.statement",
                "excerpt": "Given the shopper provides this shipping address.",
            },
        ],
    }


def test_normalize_source_text_converts_crlf_and_ensures_trailing_newline() -> None:
    """Source normalization converts line endings and leaves one final newline."""
    raw = "# Title\r\n\r\nLine one\rLine two\r\n"

    normalized = normalize_source_text(raw)

    assert normalized == "# Title\n\nLine one\nLine two\n"
    assert sha256_text(normalized).startswith("sha256:")
    assert len(sha256_text(normalized)) == len("sha256:") + 64


def test_normalize_source_text_preserves_trailing_spaces_before_newline() -> None:
    """Source normalization preserves spaces and tabs before the final newline."""
    raw = "Line with spaces  \t\r\n"

    normalized = normalize_source_text(raw)

    assert normalized == "Line with spaces  \t\n"


def test_build_source_meta_records_hashes_and_license_note() -> None:
    """Source metadata records artifacts, hashes, normalization, and license."""
    raw = "# Raw\n"
    normalized = "# Normalized\n"

    meta = build_source_meta(
        source_url="https://example.test/source",
        fetched_at="2026-05-20T12:00:00Z",
        raw_artifact="source/raw/source.raw.md",
        raw_text=raw,
        normalized_artifact="source/source.md",
        normalized_text=normalized,
        normalization_method="raw-markdown-copy",
        normalization_tool="manual",
        normalization_tool_version="n/a",
        normalization_notes="Line endings normalized to LF.",
        license_note="Public fixture retained for benchmark review.",
        immutable_source_url="https://example.test/source@abc123.md",
        upstream_commit="abc123",
    )

    assert meta["source_url"] == "https://example.test/source"
    assert meta["immutable_source_url"] == "https://example.test/source@abc123.md"
    assert meta["upstream_commit"] == "abc123"
    assert meta["fetched_at"] == "2026-05-20T12:00:00Z"
    assert meta["raw_artifact"] == "source/raw/source.raw.md"
    assert meta["raw_sha256"] == sha256_text(raw)
    assert meta["normalized_artifact"] == "source/source.md"
    assert meta["normalized_sha256"] == sha256_text(normalized)
    assert meta["normalization"]["method"] == "raw-markdown-copy"
    assert meta["normalization"]["tool"] == "manual"
    assert meta["normalization"]["tool_version"] == "n/a"
    assert meta["normalization"]["notes"] == "Line endings normalized to LF."
    assert meta["license_note"] == "Public fixture retained for benchmark review."


def test_write_json_sorts_keys_and_adds_newline(tmp_path: Path) -> None:
    """Stable JSON output creates parents, sorts keys, and writes a newline."""
    output = tmp_path / "nested" / "meta.json"

    write_json(output, {"b": 2, "a": 1})

    assert output.read_text(encoding="utf-8") == '{\n  "a": 1,\n  "b": 2\n}\n'
    assert json.loads(output.read_text(encoding="utf-8")) == {"a": 1, "b": 2}


def test_write_text_creates_parent_directories_and_writes_utf8(tmp_path: Path) -> None:
    """Text output creates parent directories and writes UTF-8 content."""
    output = tmp_path / "nested" / "source.md"

    write_text(output, "Café\n")

    assert output.read_text(encoding="utf-8") == "Café\n"


def test_sanitize_review_packet_removes_guard_tokens_and_project_ids() -> None:
    """Review packet sanitization removes committed-unsafe envelope data."""
    packet = {
        "ok": True,
        "data": {
            "guard_tokens": {REVIEW_FIELD: REDACTED_VALUE},
            "project": {
                "project_id": "proj_raw_123",
                "id": 123,
                "name": "Fixture",
            },
            "review_summary": {"acceptance_status": "accept_ready"},
            "review_findings": [{"id": "FIND-1", "severity": "low"}],
            "pending_authority": {
                "authority_id": 55,
                "artifact": {
                    "invariants": [{"id": "INV-1"}],
                    "assumptions": [],
                    "gaps": [],
                    "eligible_feature_rules": [{"id": "EFR-1"}],
                    "rejected_features": [],
                },
            },
        },
        "meta": {"correlation_id": "secret-correlation"},
    }

    sanitized = sanitize_review_packet(packet)

    serialized = json.dumps(sanitized)
    assert "guard_tokens" not in serialized
    assert REDACTED_VALUE not in serialized
    assert "secret-correlation" not in serialized
    assert "project_id" not in serialized
    assert "proj_raw_123" not in serialized
    assert sanitized["review_summary"] == {"acceptance_status": "accept_ready"}
    assert sanitized["review_findings"] == [{"id": "FIND-1", "severity": "low"}]
    assert sanitized["pending_authority_summary"] == {
        "assumption_count": 0,
        "eligible_feature_rule_count": 1,
        "gap_count": 0,
        "invariant_count": 1,
        "rejected_feature_count": 0,
    }


def test_sanitize_review_packet_handles_malformed_data() -> None:
    """Malformed review packet shapes produce a minimal committed-safe summary."""
    assert sanitize_review_packet({}) == {"review_summary": None, "review_findings": []}
    assert sanitize_review_packet({"data": []}) == {
        "review_summary": None,
        "review_findings": [],
    }


def test_sanitize_review_packet_handles_non_dict_packets() -> None:
    """Non-dict review packet values produce a minimal committed-safe summary."""
    expected = {"review_summary": None, "review_findings": []}

    for packet in (None, [], "x"):
        assert sanitize_review_packet(cast("Any", packet)) == expected


def test_extract_compiled_authority_reads_pending_authority_artifact() -> None:
    """Compiled authority is read from the pending authority artifact."""
    packet = {
        "data": {
            "pending_authority": {
                "artifact": {
                    "invariants": [{"id": "INV-1"}],
                    "assumptions": [],
                }
            }
        }
    }

    artifact = extract_compiled_authority(packet)

    assert artifact == {"invariants": [{"id": "INV-1"}], "assumptions": []}


def test_extract_compiled_authority_handles_malformed_data() -> None:
    """Malformed review packet shapes do not produce authority content."""
    assert extract_compiled_authority({}) == {}
    assert extract_compiled_authority({"data": {"pending_authority": []}}) == {}
    assert (
        extract_compiled_authority(
            {"data": {"pending_authority": {"artifact": "not-json"}}}
        )
        == {}
    )


def test_extract_compiled_authority_handles_non_dict_packets() -> None:
    """Non-dict review packet values do not produce authority content."""
    for packet in (None, [], "x"):
        assert extract_compiled_authority(cast("Any", packet)) == {}


def test_build_run_manifest_records_hashes_without_project_ids() -> None:
    """Run manifests record reproducibility metadata without raw project ids."""
    manifest = build_run_manifest(
        agileforge_commit="abc1234",
        agileforge_branch="dev/authority-coverage-matrix-phase-2e",
        schema_version="agileforge.spec.v1",
        compiler_version="1.0.0",
        spec_generation_model="manual",
        authority_compiler_model="openrouter/openai/gpt-5.4-mini",
        prompt_versions=["writing-technical-specs@local"],
        normalized_source_text="# Source\r\n",
        gold_spec_text='{"schema_version":"agileforge.spec.v1"}',
        compiled_authority_text='{"invariants":[]}',
        create_command="agileforge project create --project-id proj_raw_123",
        review_command="agileforge authority review --project-id=proj_raw_123",
        extraction_command="authority_quality_benchmark extract-review",
        generated_at="2026-05-20T12:00:00Z",
        acceptance_mutation_status="not_run",
    )

    serialized = json.dumps(manifest)
    assert "project_id" not in serialized
    assert "proj_raw_123" not in serialized
    assert manifest["agileforge_commit"] == "abc1234"
    assert manifest["agileforge_branch"] == "dev/authority-coverage-matrix-phase-2e"
    assert manifest["schema_version"] == "agileforge.spec.v1"
    assert manifest["compiler_version"] == "1.0.0"
    assert manifest["spec_generation_model"] == "manual"
    assert manifest["authority_compiler_model"] == "openrouter/openai/gpt-5.4-mini"
    assert manifest["prompt_versions"] == ["writing-technical-specs@local"]
    assert manifest["normalized_source_sha256"] == sha256_text("# Source\n")
    assert manifest["gold_spec_sha256"] == sha256_text(
        '{"schema_version":"agileforge.spec.v1"}'
    )
    assert manifest["compiled_authority_sha256"] == sha256_text('{"invariants":[]}')
    assert manifest["commands"] == {
        "create": "agileforge project create --project-id REDACTED",
        "extraction": "authority_quality_benchmark extract-review",
        "review": "agileforge authority review --project-id=REDACTED",
    }
    assert manifest["generated_at"] == "2026-05-20T12:00:00Z"
    assert manifest["acceptance_mutation_status"] == "not_run"


def test_build_run_manifest_redacts_local_command_artifacts() -> None:
    """Run manifests redact local-only command ids, keys, and guard tokens."""
    manifest = build_run_manifest(
        agileforge_commit="abc1234",
        agileforge_branch="dev/authority-coverage-matrix-phase-2e",
        schema_version="agileforge.spec.v1",
        compiler_version="1.0.0",
        spec_generation_model="manual",
        authority_compiler_model="openrouter/openai/gpt-5.4-mini",
        prompt_versions=["writing-technical-specs@local"],
        normalized_source_text="# Source\n",
        gold_spec_text='{"schema_version":"agileforge.spec.v1"}',
        compiled_authority_text='{"invariants":[]}',
        create_command=(
            "agileforge project create --project-id project-space-secret "
            "--project-id=project-equals-secret --idempotency-key idem-space "
            "--idempotency-key=idem-equals"
        ),
        review_command=(
            "agileforge authority review --review-token token-space "
            "--review-token=token-equals"
        ),
        extraction_command=(
            "authority_quality_benchmark extract-review "
            "--idempotency-key extract-idem --review-token=extract-token"
        ),
        generated_at="2026-05-20T12:00:00Z",
        acceptance_mutation_status="not_run",
    )

    serialized = json.dumps(manifest)
    for raw_value in (
        "project-space-secret",
        "project-equals-secret",
        "idem-space",
        "idem-equals",
        "token-space",
        "token-equals",
        "extract-idem",
        "extract-token",
    ):
        assert raw_value not in serialized

    assert manifest["commands"] == {
        "create": (
            "agileforge project create --project-id REDACTED "
            "--project-id=REDACTED --idempotency-key REDACTED "
            "--idempotency-key=REDACTED"
        ),
        "extraction": (
            "authority_quality_benchmark extract-review "
            "--idempotency-key REDACTED --review-token=REDACTED"
        ),
        "review": (
            "agileforge authority review --review-token REDACTED "
            "--review-token=REDACTED"
        ),
    }


def test_init_source_command_writes_normalized_source_and_metadata(
    tmp_path: Path,
) -> None:
    """The init-source command writes source fixtures and source metadata."""
    raw_path = tmp_path / "raw.md"
    fixture_dir = tmp_path / "fixture"
    raw_path.write_text("# Title\r\n", encoding="utf-8", newline="")

    exit_code = main(
        [
            "init-source",
            "--fixture-dir",
            str(fixture_dir),
            "--source-url",
            "https://example.test/source.md",
            "--immutable-source-url",
            "https://example.test/source/abc123/source.md",
            "--upstream-commit",
            "abc123",
            "--raw-input",
            str(raw_path),
            "--raw-artifact-name",
            "source.raw.md",
            "--fetched-at",
            "2026-05-20T12:00:00Z",
            "--normalization-method",
            "raw-markdown-copy",
            "--normalization-tool",
            "manual",
            "--normalization-tool-version",
            "n/a",
            "--normalization-notes",
            "Line endings normalized to LF.",
            "--license-note",
            "Public fixture retained for benchmark review.",
        ]
    )

    assert exit_code == 0
    with (fixture_dir / "source/raw/source.raw.md").open(
        "r",
        encoding="utf-8",
        newline="",
    ) as raw_file:
        assert raw_file.read() == "# Title\r\n"
    assert (fixture_dir / "source/source.md").read_text(encoding="utf-8") == "# Title\n"
    assert (fixture_dir / "source/source.sha256").read_text(
        encoding="utf-8"
    ).startswith("sha256:")
    meta = json.loads((fixture_dir / "source/source.meta.json").read_text())
    assert meta["source_url"] == "https://example.test/source.md"
    assert meta["immutable_source_url"] == "https://example.test/source/abc123/source.md"
    assert meta["upstream_commit"] == "abc123"


def test_extract_review_command_writes_compiled_authority_and_summary(
    tmp_path: Path,
) -> None:
    """The extract-review command writes authority and sanitized review JSON."""
    fixture_dir = tmp_path / "fixture"
    review_path = tmp_path / "review.json"
    review_path.write_text(
        json.dumps(
            {
                "data": {
                    "guard_tokens": {REVIEW_FIELD: REDACTED_SHORT_VALUE},
                    "review_summary": {"acceptance_status": "accept_ready"},
                    "review_findings": [],
                    "pending_authority": {
                        "artifact": {"invariants": [{"id": "INV-1"}]}
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "extract-review",
            "--fixture-dir",
            str(fixture_dir),
            "--review-packet",
            str(review_path),
        ]
    )

    assert exit_code == 0
    authority = json.loads(
        (fixture_dir / "agileforge/compiled-authority.json").read_text()
    )
    summary = json.loads((fixture_dir / "agileforge/review-summary.json").read_text())
    assert authority == {"invariants": [{"id": "INV-1"}]}
    assert REDACTED_SHORT_VALUE not in json.dumps(summary)


def test_evaluate_authority_command_writes_todomvc_guardrail_result(
    tmp_path: Path,
) -> None:
    """The evaluate-authority command writes semantic guardrail results."""
    fixture_dir = REPO_ROOT / "benchmarks/authority-quality/todomvc"
    output_path = tmp_path / "evaluation.json"

    exit_code = main(
        [
            "evaluate-authority",
            "--fixture-dir",
            str(fixture_dir),
            "--output",
            str(output_path),
        ]
    )

    assert exit_code == 0
    result = json.loads(output_path.read_text(encoding="utf-8"))
    assert result["fixture"] == "todomvc"
    assert result["verdict"] == "REJECT"
    finding_codes = {finding["code"] for finding in result["findings"]}
    assert "MISSING_MUST_AUTHORITY" in finding_codes


def test_petstore_fixture_has_first_class_source_and_gold_spec_artifacts() -> None:
    """Petstore fixture includes committed source and reviewed gold spec artifacts."""
    fixture_dir = _petstore_fixture_dir()

    expected_files = {
        "source/source.md",
        "source/source.meta.json",
        "source/source.sha256",
        "agileforge/gold-spec/spec.json",
        "agileforge/gold-spec/spec.md",
        "agileforge/gold-spec/change-log.md",
    }

    present_files = {
        str(path.relative_to(fixture_dir))
        for path in fixture_dir.glob("**/*")
        if path.is_file()
    }
    assert expected_files <= present_files


def test_evaluate_authority_command_dispatches_petstore_guardrails(
    tmp_path: Path,
) -> None:
    """Petstore fixture evaluation uses Petstore-specific guardrails."""
    authority_path = tmp_path / "authority.json"
    output_path = tmp_path / "evaluation.json"
    authority_path.write_text(
        json.dumps(_complete_petstore_authority()),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "evaluate-authority",
            "--fixture-dir",
            str(_petstore_fixture_dir()),
            "--authority",
            str(authority_path),
            "--output",
            str(output_path),
        ]
    )

    assert exit_code == 0
    result = json.loads(output_path.read_text(encoding="utf-8"))
    assert result["fixture"] == "petstore"
    assert result["verdict"] == "ACCEPT"
    assert result["findings"] == []
    assert result["weak_or_missing_must_items"] == []


def test_gherkin_fixture_has_first_class_source_and_gold_spec_artifacts() -> None:
    """Gherkin fixture includes committed source and reviewed gold spec artifacts."""
    fixture_dir = _gherkin_fixture_dir()

    expected_files = {
        "source/source.md",
        "source/source.meta.json",
        "source/source.sha256",
        "agileforge/gold-spec/spec.json",
        "agileforge/gold-spec/spec.md",
        "agileforge/gold-spec/change-log.md",
    }

    present_files = {
        str(path.relative_to(fixture_dir))
        for path in fixture_dir.glob("**/*")
        if path.is_file()
    }
    assert expected_files <= present_files


def test_evaluate_authority_command_dispatches_gherkin_guardrails(
    tmp_path: Path,
) -> None:
    """Gherkin fixture evaluation uses Gherkin-specific guardrails."""
    authority_path = tmp_path / "authority.json"
    output_path = tmp_path / "evaluation.json"
    authority_path.write_text(
        json.dumps(_complete_gherkin_authority()),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "evaluate-authority",
            "--fixture-dir",
            str(_gherkin_fixture_dir()),
            "--authority",
            str(authority_path),
            "--output",
            str(output_path),
        ]
    )

    assert exit_code == 0
    result = json.loads(output_path.read_text(encoding="utf-8"))
    assert result["fixture"] == "gherkin"
    assert result["verdict"] == "ACCEPT"
    assert result["findings"] == []
    assert result["weak_or_missing_must_items"] == []


def test_evaluate_authority_command_fails_without_fixture_evaluator(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Fixtures with no registered evaluator fail clearly."""
    fixture_dir = tmp_path / "unsupported-fixture"
    (fixture_dir / "agileforge/gold-spec").mkdir(parents=True)
    (fixture_dir / "agileforge").mkdir(exist_ok=True)
    (fixture_dir / "agileforge/gold-spec/spec.json").write_text(
        "{}\n",
        encoding="utf-8",
    )
    (fixture_dir / "agileforge/compiled-authority.json").write_text(
        "{}\n",
        encoding="utf-8",
    )

    exit_code = main(["evaluate-authority", "--fixture-dir", str(fixture_dir)])

    assert exit_code == CLI_USAGE_ERROR
    captured = capsys.readouterr()
    assert (
        "no authority evaluator registered for fixture 'unsupported-fixture'"
        in captured.err
    )


def test_evaluate_authority_command_fails_without_gold_spec(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Fixture evaluation reports missing gold spec artifacts clearly."""
    fixture_dir = tmp_path / "petstore"
    (fixture_dir / "agileforge").mkdir(parents=True)
    (fixture_dir / "agileforge/compiled-authority.json").write_text(
        "{}\n",
        encoding="utf-8",
    )

    exit_code = main(["evaluate-authority", "--fixture-dir", str(fixture_dir)])

    assert exit_code == CLI_USAGE_ERROR
    assert "gold spec not found" in capsys.readouterr().err


def test_petstore_guardrails_accept_complete_api_contract_authority() -> None:
    """Petstore guardrails accept complete endpoint, parameter, and schema coverage."""
    result = aqb.evaluate_petstore_authority_guardrails(
        gold_spec=_petstore_gold_spec(),
        authority=_complete_petstore_authority(),
        review_summary={},
    )

    assert result["fixture"] == "petstore"
    assert result["verdict"] == "ACCEPT"
    assert result["findings"] == []
    assert result["weak_or_missing_must_items"] == []


def test_petstore_guardrails_reject_missing_required_contracts() -> None:
    """Petstore guardrails reject missing required API contract coverage."""
    result = aqb.evaluate_petstore_authority_guardrails(
        gold_spec=_petstore_gold_spec(),
        authority={"invariants": []},
        review_summary={"review_summary": {"acceptance_status": "accept_ready"}},
    )

    assert result["verdict"] == "REJECT"
    finding_codes = {finding["code"] for finding in result["findings"]}
    assert "MISSING_MUST_AUTHORITY" in finding_codes
    assert "FALSE_POSITIVE_ACCEPT_READY" in finding_codes
    assert {
        "INTERFACE.list-pets",
        "INTERFACE.create-pet",
        "INTERFACE.get-pet",
        "CONSTRAINT.limit-maximum",
        "INTERFACE.pet-id-path-parameter",
        "DATA.pet-schema",
        "DATA.pets-array",
        "DATA.error-schema",
    } <= set(result["weak_or_missing_must_items"])


def test_petstore_guardrails_reject_context_free_required_schema_fields() -> None:
    """Petstore schema and parameter fields need subject context."""
    authority = _complete_petstore_authority()
    authority["invariants"] = [
        invariant
        for invariant in authority["invariants"]
        if invariant["id"]
        not in {"INV-pet-id-param", "INV-pet-schema", "INV-error-schema"}
    ]
    authority["invariants"].extend(
        [
            {
                "id": "INV-pet-id-field",
                "type": "REQUIRED_FIELD",
                "parameters": {"field_name": "petId"},
                "source_refs": ["INTERFACE.pet-id-path-parameter.statement"],
            },
            {
                "id": "INV-pet-id",
                "type": "REQUIRED_FIELD",
                "parameters": {"field_name": "id"},
                "source_refs": ["DATA.pet-schema.acceptance[0]"],
            },
            {
                "id": "INV-pet-name",
                "type": "REQUIRED_FIELD",
                "parameters": {"field_name": "name"},
                "source_refs": ["DATA.pet-schema.acceptance[1]"],
            },
            {
                "id": "INV-error-code",
                "type": "REQUIRED_FIELD",
                "parameters": {"field_name": "code"},
                "source_refs": ["DATA.error-schema.acceptance[0]"],
            },
            {
                "id": "INV-error-message",
                "type": "REQUIRED_FIELD",
                "parameters": {"field_name": "message"},
                "source_refs": ["DATA.error-schema.acceptance[1]"],
            },
        ]
    )

    result = aqb.evaluate_petstore_authority_guardrails(
        gold_spec=_petstore_gold_spec(),
        authority=authority,
        review_summary={},
    )

    assert result["verdict"] == "REJECT"
    assert {
        "INTERFACE.pet-id-path-parameter",
        "DATA.pet-schema",
        "DATA.error-schema",
    } <= set(result["weak_or_missing_must_items"])


def test_gherkin_guardrails_accept_complete_scenario_authority() -> None:
    """Gherkin guardrails accept complete scenario and step coverage."""
    result = aqb.evaluate_gherkin_authority_guardrails(
        gold_spec=_gherkin_gold_spec(),
        authority=_complete_gherkin_authority(),
        review_summary={},
    )

    assert result["fixture"] == "gherkin"
    assert result["verdict"] == "ACCEPT"
    assert result["findings"] == []
    assert result["weak_or_missing_must_items"] == []


def test_gherkin_guardrails_reject_missing_scenario_steps() -> None:
    """Gherkin guardrails reject missing required scenario step coverage."""
    result = aqb.evaluate_gherkin_authority_guardrails(
        gold_spec=_gherkin_gold_spec(),
        authority={"invariants": []},
        review_summary={"review_summary": {"acceptance_status": "accept_ready"}},
    )

    assert result["verdict"] == "REJECT"
    finding_codes = {finding["code"] for finding in result["findings"]}
    assert "MISSING_MUST_AUTHORITY" in finding_codes
    assert "FALSE_POSITIVE_ACCEPT_READY" in finding_codes
    assert {
        "REQ.member-discount-scenario",
        "REQ.expired-coupon-outline",
        "REQ.shipping-address-arguments",
    } <= set(result["weak_or_missing_must_items"])


def test_gherkin_guardrails_reject_required_field_scenario_compression() -> None:
    """Gherkin scenarios need behavior mappings, not field existence checks."""
    result = aqb.evaluate_gherkin_authority_guardrails(
        gold_spec=_gherkin_gold_spec(),
        authority={
            "invariants": [
                {
                    "id": "INV-scenario-name",
                    "type": "REQUIRED_FIELD",
                    "parameters": {"field_name": "Scenario"},
                    "source_refs": ["REQ.member-discount-scenario.statement"],
                }
            ]
        },
        review_summary={},
    )

    finding_codes = {finding["code"] for finding in result["findings"]}
    assert result["verdict"] == "REJECT"
    assert "UNSAFE_REQUIRED_FIELD_COMPRESSION" in finding_codes
    assert "REQ.member-discount-scenario" in result["weak_or_missing_must_items"]


def test_gherkin_guardrails_accept_step_intent_without_literal_keywords() -> None:
    """Gherkin coverage is based on step intent, not keyword extraction."""
    authority = _complete_gherkin_authority()
    authority["invariants"][0]["parameters"]["trigger"] = (
        "signed-in member has an eligible cart totaling 100.00"
    )
    authority["invariants"][1]["parameters"]["trigger"] = (
        "checkout total is calculated"
    )
    authority["invariants"][1]["parameters"]["outcome"] = (
        "10 percent member discount is applied and final total is shown as 90.00"
    )

    result = aqb.evaluate_gherkin_authority_guardrails(
        gold_spec=_gherkin_gold_spec(),
        authority=authority,
        review_summary={},
    )

    assert "REQ.member-discount-scenario" not in result[
        "weak_or_missing_must_items"
    ]


def test_benchmark_prompt_forbids_deterministic_extraction_solution() -> None:
    """Shared external review prompt forbids deterministic extraction advice."""
    prompt = (
        REPO_ROOT / "benchmarks/authority-quality/review-prompt.md"
    ).read_text(encoding="utf-8")

    assert "Do not recommend deterministic requirement extraction" in prompt
    assert "Human-reviewed gold structured spec JSON" in prompt


def test_oracle_notes_warn_against_reviewer_leakage() -> None:
    """Fixture oracle notes warn that reviewers must not receive them."""
    for fixture in ("todomvc", "petstore", "gherkin"):
        notes = (
            REPO_ROOT
            / f"benchmarks/authority-quality/{fixture}/oracle/oracle-notes.md"
        ).read_text(encoding="utf-8")
        assert "Do not provide these notes" in notes
        assert "external LLM reviewers" in notes


def test_local_benchmark_run_artifacts_are_gitignored() -> None:
    """Local benchmark run artifacts stay out of committed benchmark fixtures."""
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")

    assert ".agileforge/" in gitignore


def test_todomvc_fixture_is_rejected_by_semantic_guardrails() -> None:
    """TodoMVC guardrails reject the current structurally accepted authority."""
    fixture_dir = REPO_ROOT / "benchmarks/authority-quality/todomvc"
    gold_spec = json.loads(
        (fixture_dir / "agileforge/gold-spec/spec.json").read_text(encoding="utf-8")
    )
    authority = json.loads(
        (fixture_dir / "agileforge/compiled-authority.json").read_text(
            encoding="utf-8"
        )
    )
    review_summary = json.loads(
        (fixture_dir / "agileforge/review-summary.json").read_text(encoding="utf-8")
    )

    result = evaluate_todomvc_authority_guardrails(
        gold_spec=gold_spec,
        authority=authority,
        review_summary=review_summary,
    )

    assert result["verdict"] == "REJECT"
    finding_codes = {finding["code"] for finding in result["findings"]}
    assert {
        "MISSING_MUST_AUTHORITY",
        "UNSAFE_REQUIRED_FIELD_COMPRESSION",
        "SOURCE_REF_SEMANTIC_MISMATCH",
        "MODALITY_OVER_PROMOTION",
        "EXAMPLE_USED_AS_NORMATIVE_SOURCE",
        "FALSE_POSITIVE_ACCEPT_READY",
    } <= finding_codes


def test_todomvc_guardrails_use_top_level_source_map_for_coverage() -> None:
    """Normalized authority source_map entries count as source references."""
    fixture_dir = REPO_ROOT / "benchmarks/authority-quality/todomvc"
    gold_spec = json.loads(
        (fixture_dir / "agileforge/gold-spec/spec.json").read_text(encoding="utf-8")
    )
    authority = {
        "invariants": [
            {
                "id": "INV-new-todo",
                "type": "USER_INTERACTION",
                "parameters": {
                    "source_item_id": "REQ.new-todo",
                    "source_level": "MUST",
                    "trigger": "Enter keypress",
                    "target": "new todo input",
                    "expected_response": (
                        "create a todo from non-empty trimmed text, append it "
                        "to the list, and clear the input"
                    ),
                },
            }
        ],
        "source_map": [
            {
                "invariant_id": "INV-new-todo",
                "location": "REQ.new-todo.statement",
                "excerpt": (
                    "The top input must be focused when the page loads, "
                    "pressing Enter in that input must create a todo from "
                    "non-empty trimmed text, append it to the todo list, and "
                    "clear the input."
                ),
            }
        ],
    }

    result = evaluate_todomvc_authority_guardrails(
        gold_spec=gold_spec,
        authority=authority,
        review_summary={},
    )

    assert "REQ.new-todo" not in result["weak_or_missing_must_items"]


def test_todomvc_guardrails_use_behavioral_source_item_id_for_coverage() -> None:
    """Validated behavioral source_item_id counts even without source_map."""
    fixture_dir = REPO_ROOT / "benchmarks/authority-quality/todomvc"
    gold_spec = json.loads(
        (fixture_dir / "agileforge/gold-spec/spec.json").read_text(encoding="utf-8")
    )
    authority = {
        "invariants": [
            {
                "id": "INV-new-todo",
                "type": "USER_INTERACTION",
                "parameters": {
                    "source_item_id": "REQ.new-todo",
                    "source_level": "MUST",
                    "trigger": "Enter keypress",
                    "target": "new todo input",
                    "expected_response": (
                        "create a todo from non-empty trimmed text, append it "
                        "to the list, and clear the input"
                    ),
                },
            }
        ],
        "source_map": [],
    }

    result = evaluate_todomvc_authority_guardrails(
        gold_spec=gold_spec,
        authority=authority,
        review_summary={},
    )

    assert "REQ.new-todo" not in result["weak_or_missing_must_items"]


def test_todomvc_guardrails_accept_item_id_gap_for_deferable_must_item() -> None:
    """Explicit item-ID gaps account for non-runtime MUST coverage."""
    fixture_dir = REPO_ROOT / "benchmarks/authority-quality/todomvc"
    gold_spec = json.loads(
        (fixture_dir / "agileforge/gold-spec/spec.json").read_text(encoding="utf-8")
    )

    result = evaluate_todomvc_authority_guardrails(
        gold_spec=gold_spec,
        authority={"invariants": [], "gaps": ["REQ.readme: deferred to full spec."]},
        review_summary={},
    )

    assert "REQ.readme" not in result["weak_or_missing_must_items"]


def test_todomvc_guardrails_accept_code_style_gap_for_manual_review() -> None:
    """Manual style/tooling constraints may be deferred as item-ID gaps."""
    fixture_dir = REPO_ROOT / "benchmarks/authority-quality/todomvc"
    gold_spec = json.loads(
        (fixture_dir / "agileforge/gold-spec/spec.json").read_text(encoding="utf-8")
    )

    result = evaluate_todomvc_authority_guardrails(
        gold_spec=gold_spec,
        authority={
            "invariants": [],
            "gaps": [
                "CONSTRAINT.code-style-rules: deferred to lint/manual review."
            ],
        },
        review_summary={},
    )

    assert "CONSTRAINT.code-style-rules" not in result[
        "weak_or_missing_must_items"
    ]


def test_todomvc_guardrails_do_not_accept_gap_for_core_behavior() -> None:
    """Core interactive behavior must be represented, not only gapped."""
    fixture_dir = REPO_ROOT / "benchmarks/authority-quality/todomvc"
    gold_spec = json.loads(
        (fixture_dir / "agileforge/gold-spec/spec.json").read_text(encoding="utf-8")
    )

    result = evaluate_todomvc_authority_guardrails(
        gold_spec=gold_spec,
        authority={"invariants": [], "gaps": ["REQ.new-todo: deferred to full spec."]},
        review_summary={},
    )

    assert "REQ.new-todo" in result["weak_or_missing_must_items"]


def test_todomvc_guardrails_scope_required_field_compression_to_rich_items() -> None:
    """README existence alone is weak coverage, not behavioral compression."""
    fixture_dir = REPO_ROOT / "benchmarks/authority-quality/todomvc"
    gold_spec = json.loads(
        (fixture_dir / "agileforge/gold-spec/spec.json").read_text(encoding="utf-8")
    )
    authority = {
        "invariants": [
            {
                "id": "INV-readme",
                "type": "REQUIRED_FIELD",
                "parameters": {"field_name": "README"},
            }
        ],
        "source_map": [
            {
                "invariant_id": "INV-readme",
                "location": "REQ.readme.title",
                "excerpt": "README",
            }
        ],
    }

    result = evaluate_todomvc_authority_guardrails(
        gold_spec=gold_spec,
        authority=authority,
        review_summary={},
    )

    finding_codes = {finding["code"] for finding in result["findings"]}
    assert "UNSAFE_REQUIRED_FIELD_COMPRESSION" not in finding_codes
    assert "REQ.readme" in result["weak_or_missing_must_items"]


def test_todomvc_guardrails_allow_non_goal_forbidden_capability() -> None:
    """NON_GOAL exclusions are not weak-guidance modality promotions."""
    fixture_dir = REPO_ROOT / "benchmarks/authority-quality/todomvc"
    gold_spec = json.loads(
        (fixture_dir / "agileforge/gold-spec/spec.json").read_text(encoding="utf-8")
    )
    authority = {
        "invariants": [
            {
                "id": "INV-non-goal",
                "type": "FORBIDDEN_CAPABILITY",
                "parameters": {"capability": "custom visual design"},
            }
        ],
        "source_map": [
            {
                "invariant_id": "INV-non-goal",
                "location": "NON_GOAL.customized-visual-design.statement",
                "excerpt": (
                    "The app is not intended to introduce a distinct visual "
                    "design beyond minimal app.css changes."
                ),
            }
        ],
    }

    result = evaluate_todomvc_authority_guardrails(
        gold_spec=gold_spec,
        authority=authority,
        review_summary={},
    )

    finding_codes = {finding["code"] for finding in result["findings"]}
    assert "MODALITY_OVER_PROMOTION" not in finding_codes


def test_todomvc_guardrails_pin_core_missing_behavior_items() -> None:
    """TodoMVC guardrails identify the high-risk missing behavioral contracts."""
    fixture_dir = REPO_ROOT / "benchmarks/authority-quality/todomvc"
    result = evaluate_todomvc_authority_guardrails(
        gold_spec=json.loads(
            (fixture_dir / "agileforge/gold-spec/spec.json").read_text(
                encoding="utf-8"
            )
        ),
        authority=json.loads(
            (fixture_dir / "agileforge/compiled-authority.json").read_text(
                encoding="utf-8"
            )
        ),
        review_summary=json.loads(
            (fixture_dir / "agileforge/review-summary.json").read_text(
                encoding="utf-8"
            )
        ),
    )

    weak_or_missing = set(result["weak_or_missing_must_items"])
    assert {
        "CONSTRAINT.code-style-rules",
        "REQ.new-todo",
        "REQ.toggle-all",
        "REQ.item-interactions",
        "REQ.editing",
        "REQ.counter",
        "REQ.clear-completed",
        "REQ.persistence",
        "REQ.routing",
        "REQ.filtered-state",
    } <= weak_or_missing
