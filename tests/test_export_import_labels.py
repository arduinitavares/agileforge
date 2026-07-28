"""Tests for export import labels."""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from agile_sqlmodel import (
    CompiledSpecAuthority,
    Product,
    SpecAuthorityAcceptance,
    SpecRegistry,
    UserStory,
)
from models.core import Epic, Feature, Theme
from scripts import export_benchmark_for_labeling as exporter
from scripts import import_human_labels as importer
from tests.typing_helpers import require_id
from utils.spec_schemas import (
    Invariant,
    InvariantType,
    RequiredFieldParams,
    SourceMapEntry,
    SpecAuthorityCompilationSuccess,
    SpecAuthorityCompilerOutput,
)

EXPECTED_OUTCOME_FIELD = "expected_pass"
REVIEW_OUTCOME_FIELD = exporter.REVIEW_OUTCOME_FIELD

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine
    from sqlmodel import Session


def _seed_case_data(session: Session) -> tuple[int, int]:
    product = Product(name="Label Product", vision="Labeling")
    session.add(product)
    session.commit()
    session.refresh(product)
    product_id = require_id(product.product_id, "product_id")

    theme = Theme(product_id=product_id, title="Theme", description="")
    session.add(theme)
    session.commit()
    session.refresh(theme)
    theme_id = require_id(theme.theme_id, "theme_id")
    epic = Epic(theme_id=theme_id, title="Epic", summary="")
    session.add(epic)
    session.commit()
    session.refresh(epic)
    epic_id = require_id(epic.epic_id, "epic_id")
    feature = Feature(epic_id=epic_id, title="Feature", description="")
    session.add(feature)
    session.commit()
    session.refresh(feature)
    feature_id = require_id(feature.feature_id, "feature_id")

    story = UserStory(
        product_id=product_id,
        feature_id=feature_id,
        title="As a user, I want exports",
        story_description="As a user, I want exports for reporting.",
        acceptance_criteria="Given data, When export, Then CSV.",
    )
    session.add(story)
    session.commit()
    session.refresh(story)
    story_id = require_id(story.story_id, "story_id")

    spec = SpecRegistry(
        product_id=product_id,
        content="# Spec",
        content_ref=None,
        spec_hash="a" * 64,
        status="approved",
        approved_at=datetime.now(UTC),
        approved_by="tester",
        approval_notes=None,
    )
    session.add(spec)
    session.commit()
    session.refresh(spec)
    spec_version_id = require_id(spec.spec_version_id, "spec_version_id")

    session.add(
        CompiledSpecAuthority(
            spec_version_id=spec_version_id,
            compiler_version="2.0.0",
            prompt_hash="f" * 64,
            compiled_at=datetime.now(UTC),
            scope_themes="[]",
            invariants="[]",
            eligible_feature_ids="[]",
            rejected_features="[]",
            spec_gaps="[]",
            compiled_artifact_json='{"schema_version":"agileforge.compiled_authority.v2"}',
        )
    )
    session.flush()

    invariant = Invariant(
        id="INV-0000000000000001",
        type=InvariantType.REQUIRED_FIELD,
        parameters=RequiredFieldParams(field_name="user_id"),
    )
    artifact = SpecAuthorityCompilationSuccess(
        scope_themes=["core"],
        domain="test",
        invariants=[invariant],
        eligible_feature_rules=[],
        gaps=[],
        assumptions=[],
        source_map=[
            SourceMapEntry(
                invariant_id=invariant.id,
                excerpt="Must include user_id.",
                location="spec",
            )
        ],
        compiler_version="3.0.0",
        prompt_hash="0" * 64,
    )
    compiled = CompiledSpecAuthority(
        spec_version_id=spec_version_id,
        compiler_version="3.0.0",
        prompt_hash="0" * 64,
        compiled_at=datetime.now(UTC),
        scope_themes=json.dumps(["core"]),
        invariants=json.dumps(["REQUIRED_FIELD:user_id"]),
        eligible_feature_ids=json.dumps([]),
        rejected_features=json.dumps([]),
        spec_gaps=json.dumps([]),
        compiled_artifact_json=SpecAuthorityCompilerOutput(
            root=artifact
        ).model_dump_json(),
    )
    session.add(compiled)
    session.flush()
    session.add(
        SpecAuthorityAcceptance(
            product_id=product_id,
            spec_version_id=spec_version_id,
            status="accepted",
            policy="test",
            decided_by="label-test",
            compiler_version=compiled.compiler_version,
            prompt_hash=compiled.prompt_hash,
            spec_hash=spec.spec_hash,
            pending_authority_id=compiled.authority_id,
        )
    )
    session.commit()

    return story_id, spec_version_id


def test_export_labeling_rows_contains_context(
    engine: Engine, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify export labeling rows contains context."""
    story_id, spec_version_id = _seed_case_data(session)
    monkeypatch.setattr(exporter, "get_engine", lambda: engine)

    cases = [
        {
            "case_id": "c1",
            "story_id": story_id,
            "spec_version_id": spec_version_id,
            "enabled": True,
        }
    ]
    rows = exporter.build_labeling_rows(cases)
    assert len(rows) == 1
    row = rows[0]
    assert row["case_id"] == "c1"
    assert "As a user" in row["story_title"]
    assert row[REVIEW_OUTCOME_FIELD] == ""
    assert "invariants" in row["spec_authority_summary"]


def test_import_merge_human_labels_roundtrip(tmp_path: Path) -> None:
    """Verify import merge human labels roundtrip."""
    cases_path = tmp_path / "cases.jsonl"
    labels_path = tmp_path / "labels.csv"

    case = {
        "case_id": "c1",
        "story_id": 1,
        "spec_version_id": 2,
        EXPECTED_OUTCOME_FIELD: None,
        "expected_fail_reasons": [],
        "label_source": "validation_evidence",
    }
    cases_path.write_text(json.dumps(case) + "\n", encoding="utf-8")

    with labels_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "case_id",
                REVIEW_OUTCOME_FIELD,
                "rater_fail_reasons",
                "rater_confidence",
                "rater_notes",
                "rater_id",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "case_id": "c1",
                REVIEW_OUTCOME_FIELD: "false",
                "rater_fail_reasons": "RULE_ACCEPTANCE_CRITERIA_REQUIRED",
                "rater_confidence": "high",
                "rater_notes": "AC missing",
                "rater_id": "reviewer-1",
            }
        )

    cases = importer._read_cases(cases_path)  # pylint: disable=protected-access
    labels = importer._read_label_rows(labels_path)  # pylint: disable=protected-access
    merged, updated = importer.merge_human_labels(
        cases,
        labels,
        allow_unknown_reasons=False,
    )
    assert updated == 1
    assert merged[0][EXPECTED_OUTCOME_FIELD] is False
    assert merged[0]["expected_fail_reasons"] == ["RULE_ACCEPTANCE_CRITERIA_REQUIRED"]
    assert merged[0]["label_source"] == "human_review"
    assert merged[0]["rater_id"] == "reviewer-1"
    assert merged[0]["labeled_at"]


def test_import_merge_human_labels_from_jsonl(tmp_path: Path) -> None:
    """Verify import merge human labels from jsonl."""
    cases_path = tmp_path / "cases.jsonl"
    labels_path = tmp_path / "labels.jsonl"

    case = {
        "case_id": "c1",
        "story_id": 1,
        "spec_version_id": 2,
        EXPECTED_OUTCOME_FIELD: None,
        "expected_fail_reasons": [],
        "label_source": "validation_evidence",
    }
    cases_path.write_text(json.dumps(case) + "\n", encoding="utf-8")
    labels_path.write_text(
        json.dumps(
            {
                "case_id": "c1",
                REVIEW_OUTCOME_FIELD: "true",
                "rater_fail_reasons": "",
                "rater_confidence": "medium",
                "rater_notes": "Looks compliant",
                "rater_id": "reviewer-jsonl",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    labels = importer._read_label_rows(labels_path)  # pylint: disable=protected-access
    merged, updated = importer.merge_human_labels(
        importer._read_cases(cases_path),  # pylint: disable=protected-access
        labels,
        allow_unknown_reasons=False,
    )
    assert updated == 1
    assert merged[0][EXPECTED_OUTCOME_FIELD] is True
    assert merged[0]["expected_fail_reasons"] == []
    assert merged[0]["label_source"] == "human_review"
    assert merged[0]["rater_id"] == "reviewer-jsonl"


def test_import_rejects_unknown_reason_code(tmp_path: Path) -> None:
    """Verify import rejects unknown reason code."""
    del tmp_path
    case = {
        "case_id": "c1",
        "story_id": 1,
        "spec_version_id": 2,
    }
    with pytest.raises(ValueError):  # noqa: PT011
        importer.merge_human_labels(
            [case],
            {
                "c1": {
                    "case_id": "c1",
                    REVIEW_OUTCOME_FIELD: "false",
                    "rater_fail_reasons": "UNKNOWN_REASON_CODE",
                    "rater_id": "r1",
                }
            },
            allow_unknown_reasons=False,
        )
