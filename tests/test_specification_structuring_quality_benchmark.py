"""Provider-free integrity checks for Specification structuring quality cases."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from utils.agileforge_spec_profile_v2 import SpecificationItem, SpecificationPayload

REPO_ROOT: Path = Path(__file__).resolve().parents[1]
FIXTURE_ROOT: Path = (
    REPO_ROOT
    / "benchmarks"
    / "authority-quality"
    / "string-calculator-negative-diagnostic"
)
REGISTERED_SOURCE_FIXTURE: Path = (
    REPO_ROOT / "tests" / "fixtures" / "issue_200" / "to-spec-source.md"
)
EXACT_SOURCE_CONTRACT: bytes = (
    b"- Reject the entire Number List when any parsed value is below zero. The public\n"
    b"  Python operation raises `ValueError` rather than returning a partial sum.\n"
    b"- Format rejection text as `negative numbers not allowed: ` followed by every\n"
    b"  canonical negative value in encounter order, separated by comma and space.\n"
    b"  Preserve duplicate occurrences.\n"
    b"- Install the `string-calculator` command with one positional Number List for\n"
    b"  supported invocations.\n"
    b"- On success, write only the decimal sum and one trailing newline to standard\n"
    b"  output, write nothing to standard error, and exit zero.\n"
    b"- On negative-number rejection, write the Python error text and one trailing\n"
    b"  newline to standard error, write no sum to standard output, and exit nonzero.\n"
)


def _load_payload(relative_path: str) -> SpecificationPayload:
    return SpecificationPayload.model_validate_json(
        (FIXTURE_ROOT / relative_path).read_text(encoding="utf-8")
    )


def _item(payload: SpecificationPayload, item_id: str) -> SpecificationItem:
    return next(item for item in payload.items if item.id == item_id)


def test_quality_fixture_preserves_the_exact_registered_source_contract() -> None:
    """Pin the quality case to the source bytes that exposed issue 202."""
    source_bytes = (FIXTURE_ROOT / "source/source.md").read_bytes()
    expected_sha256 = (FIXTURE_ROOT / "source/source.sha256").read_text(
        encoding="utf-8"
    )

    assert source_bytes == EXACT_SOURCE_CONTRACT
    assert source_bytes in REGISTERED_SOURCE_FIXTURE.read_bytes()
    assert expected_sha256 == (
        "sha256:" + hashlib.sha256(source_bytes).hexdigest() + "\n"
    )


def test_quality_fixture_records_the_known_semantic_weakening() -> None:
    """Execute the human-authored oracle without creating a runtime semantic gate."""
    evaluation: dict[str, Any] = json.loads(
        (FIXTURE_ROOT / "oracle/evaluation.json").read_text(encoding="utf-8")
    )
    review = (FIXTURE_ROOT / evaluation["review_path"]).read_text(encoding="utf-8")
    candidates = {
        case["candidate_id"]: _load_payload(case["path"])
        for case in evaluation["candidates"]
    }
    judgments = {
        case["candidate_id"]: case["expected_judgment"]
        for case in evaluation["candidates"]
    }

    assert evaluation["evaluation_mode"] == "human-reviewed-fixture-oracle"
    assert evaluation["runtime_effect"] == "none"
    assert evaluation["human_review_remains_final"] is True
    assert "Verdict: gold_corrected" in review
    assert judgments == {
        "acceptable-exact": "semantically_acceptable",
        "weakened-begins-with": "semantically_unacceptable",
    }

    exact_error = _item(
        candidates["acceptable-exact"], "INTERFACE.python-negative-error"
    )
    weakened_error = _item(
        candidates["weakened-begins-with"], "INTERFACE.python-negative-error"
    )
    assert exact_error.statement == (
        "The public add operation MUST raise ValueError whose complete message "
        "equals `negative numbers not allowed: ` followed by every canonical "
        "negative value in encounter order, separated by comma and space, with "
        "duplicate occurrences preserved."
    )
    assert weakened_error.statement == (
        "The public add operation MUST raise ValueError with text beginning "
        "negative numbers not allowed: followed by canonical negative occurrences "
        "separated by comma and space."
    )
