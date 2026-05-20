"""Tests for authority quality benchmark helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

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

REPO_ROOT = Path(__file__).resolve().parents[1]


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
            "guard_tokens": {"review_token": "secret-token"},
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
    assert "secret-token" not in serialized
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
    assert (
        fixture_dir / "source/raw/source.raw.md"
    ).read_text(encoding="utf-8", newline="") == "# Title\r\n"
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
                    "guard_tokens": {"review_token": "secret"},
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
    assert "secret" not in json.dumps(summary)


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
