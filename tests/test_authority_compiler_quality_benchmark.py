# tests/test_authority_compiler_quality_benchmark.py
"""Provider-free quality benchmark for Authority compiler classifications."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, cast

from services.contracts.authority_input_v2 import AuthorityInputV2
from services.contracts.specification_normalizer import normalize_compiler_output
from utils.spec_schemas import (
    MaxValueParams,
    SpecAuthorityCompilationFailure,
    SpecAuthorityCompilationSuccess,
)

REPO_ROOT: Path = Path(__file__).resolve().parents[1]
FIXTURE_ROOT: Path = (
    REPO_ROOT
    / "benchmarks"
    / "authority-quality"
    / "string-calculator-tooling-constraint"
)
MEASURABLE_MAX: int = 100


def _load_json(relative_path: str) -> dict[str, Any]:
    payload: object = json.loads(
        (FIXTURE_ROOT / relative_path).read_text(encoding="utf-8")
    )
    assert isinstance(payload, dict)
    return cast("dict[str, Any]", payload)


def test_tooling_constraint_fixture_preserves_exact_source_bytes() -> None:
    """Pin the provider-quality case to its exact typed compiler input."""
    source_path = FIXTURE_ROOT / "source/authority-input.json"
    source_bytes = source_path.read_bytes()
    expected_sha256 = (FIXTURE_ROOT / "source/source.sha256").read_text(
        encoding="utf-8"
    )
    authority_input = AuthorityInputV2.model_validate_json(source_bytes)

    assert expected_sha256 == (
        "sha256:" + hashlib.sha256(source_bytes).hexdigest() + "\n"
    )
    tooling_item = authority_input.normative_items[0]
    assert tooling_item.id == "CONSTRAINT.001"
    assert tooling_item.statement == (
        "The implementation MUST target Python 3.13 or newer and manage the "
        "project exclusively with uv."
    )
    assert tooling_item.acceptance == (
        "Project configuration declares Python 3.13 or newer.",
        "Project setup and dependency operations use uv.",
    )


def test_tooling_constraint_oracle_rejects_attempt_28_and_accepts_gold() -> None:
    """Execute the human oracle without adding a runtime semantic repair gate."""
    authority_input = AuthorityInputV2.model_validate(
        _load_json("source/authority-input.json")
    )
    evaluation = _load_json("oracle/evaluation.json")
    candidates = {
        candidate["candidate_id"]: _load_json(candidate["path"])
        for candidate in evaluation["candidates"]
    }
    judgments = {
        candidate["candidate_id"]: candidate["expected_judgment"]
        for candidate in evaluation["candidates"]
    }

    invalid = normalize_compiler_output(
        json.dumps(candidates["attempt-28-invalid"]),
        authority_input=authority_input,
    ).root
    gold = normalize_compiler_output(
        json.dumps(candidates["tooling-gap-gold"]),
        authority_input=authority_input,
    ).root

    assert evaluation["evaluation_mode"] == "human-reviewed-fixture-oracle"
    assert evaluation["runtime_effect"] == "none"
    assert evaluation["human_review_remains_final"] is True
    assert judgments == {
        "attempt-28-invalid": "semantically_unacceptable",
        "tooling-gap-gold": "semantically_acceptable",
    }
    assert isinstance(invalid, SpecAuthorityCompilationFailure)
    assert invalid.reason == "INELIGIBLE_INVARIANT_SOURCE"
    assert "CONSTRAINT.001 semantics" in invalid.blocking_gaps[0]
    assert isinstance(gold, SpecAuthorityCompilationSuccess)
    assert gold.gaps == [
        "CONSTRAINT.001: unsupported tooling requirement; enforce outside "
        "compiled Authority."
    ]
    assert len(gold.invariants) == 1
    measurable = gold.invariants[0]
    assert isinstance(measurable.parameters, MaxValueParams)
    assert measurable.source_item_id == "CONSTRAINT.002"
    assert measurable.parameters.field_name == "request limit"
    assert measurable.parameters.max_value == MEASURABLE_MAX
    assert "project configuration and dependency management" not in (
        measurable.model_dump_json()
    )
