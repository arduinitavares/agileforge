"""Tests for HTML snapshot export."""

from __future__ import annotations

import html
import json
import re
from datetime import date
from typing import TYPE_CHECKING

import pytest
from sqlmodel import Session

from agile_sqlmodel import Project, SpecRegistry
from models.core import Sprint, UserStory
from models.enums import SprintStatus
from models.product_definition import SpecificationCandidate, SpecificationDecision
from scripts.export_snapshot import export_snapshot_command
from tests.typing_helpers import require_id
from tests.workflow.lifecycle_fixtures import seed_accepted_specification
from tools.export_snapshot import (
    _render_snapshot_styles,
    _render_stories_table,
    _select_refined_current_sprint_stories,
    export_project_snapshot_html,
)
from workflow.fingerprints import canonical_json

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine


def _insert_basic_project(session: Session) -> Project:
    project = Project(
        name="Test Project",
        description="Demo",
    )
    session.add(project)
    session.commit()
    session.refresh(project)
    return project


def _insert_accepted_spec(
    session: Session,
    *,
    project_id: int,
    title: str,
) -> SpecRegistry:
    """Persist one accepted current-lifecycle specification."""
    return seed_accepted_specification(
        session,
        project_id=project_id,
        content=json.dumps({"title": title}),
    ).spec


def test_export_snapshot_html_basic(engine: Engine, tmp_path: Path) -> None:
    """Verify export snapshot html basic."""
    with Session(engine) as session:
        project = _insert_basic_project(session)
        project_id = require_id(
            project.project_id, "project_id"
        )  # Capture before session closes
        _insert_accepted_spec(session, project_id=project_id, title="Snapshot")

    output_path = export_project_snapshot_html(
        project_id=project_id,
        output_dir=tmp_path,
        engine_override=engine,
    )

    html = output_path.read_text(encoding="utf-8")
    assert output_path.exists()
    assert "Test Project" in html
    assert "product vision" in html
    assert "Deliver one verified product increment." in html
    assert "Snapshot" in html
    assert "Candidate Envelope" in html
    assert "Source Manifest" in html
    assert "Candidate fingerprint" in html
    assert "fixture" in html
    assert "Accepted for fixture delivery." in html
    assert "Current Sprint Stories" in html
    assert "Project Backlog (All Stories)" in html


@pytest.mark.parametrize("mutation", ["feedback", "rejected", "mismatched"])
def test_export_snapshot_requires_exact_accepted_decision_before_writing(
    engine: Engine,
    tmp_path: Path,
    mutation: str,
) -> None:
    """A registry status alone never proves human Specification acceptance."""
    with Session(engine) as session:
        project = _insert_basic_project(session)
        project_id = require_id(project.project_id, "project_id")
        lineage = seed_accepted_specification(
            session,
            project_id=project_id,
            content=json.dumps({"title": f"decision-{mutation}"}),
        )
        decision_id = lineage.spec.source_specification_decision_id
        if mutation in {"feedback", "rejected"}:
            decision = session.get(SpecificationDecision, decision_id)
            assert decision is not None
            decision.decision = mutation
            session.add(decision)
            session.commit()
        else:
            session.connection().exec_driver_sql("PRAGMA foreign_keys = OFF")
            session.connection().exec_driver_sql(
                "UPDATE specification_decisions SET candidate_fingerprint = ? "
                "WHERE specification_decision_id = ?",
                ("sha256:" + "0" * 64, decision_id),
            )
            session.commit()

    with pytest.raises(ValueError, match="SPECIFICATION_NOT_ACCEPTED"):
        export_project_snapshot_html(
            project_id=project_id,
            output_dir=tmp_path,
            engine_override=engine,
        )

    assert list(tmp_path.iterdir()) == []


def test_export_snapshot_rejects_tampered_specification_candidate_before_writing(
    engine: Engine,
    tmp_path: Path,
) -> None:
    """The export never falls back when the registry candidate bytes are invalid."""
    with Session(engine) as session:
        project = _insert_basic_project(session)
        project_id = require_id(project.project_id, "project_id")
        lineage = seed_accepted_specification(
            session,
            project_id=project_id,
            content=json.dumps({"title": "tampered-candidate"}),
        )
        candidate = session.get(
            SpecificationCandidate,
            lineage.specification_candidate_id,
        )
        assert candidate is not None
        candidate.canonical_envelope_json = '{"payload":{},"envelope":{}}'
        session.add(candidate)
        session.commit()

    with pytest.raises(ValueError, match="SPECIFICATION_CANDIDATE_INVALID"):
        export_project_snapshot_html(
            project_id=project_id,
            output_dir=tmp_path,
            engine_override=engine,
        )

    assert list(tmp_path.iterdir()) == []


def test_export_snapshot_reports_missing_accepted_spec(
    engine: Engine, tmp_path: Path
) -> None:
    """Do not reconstruct specification content from removed Project fields."""
    with Session(engine) as session:
        project = _insert_basic_project(session)

    output_path = export_project_snapshot_html(
        project_id=require_id(project.project_id, "project_id"),
        output_dir=tmp_path,
        engine_override=engine,
    )

    html = output_path.read_text(encoding="utf-8")
    assert "No accepted specification available" in html


def test_snapshot_story_selector_uses_active_sprint_and_active_story_lineage() -> None:
    """Retain the current-Sprint selector while the Specification source changes."""
    active = Sprint(
        sprint_id=11,
        project_id=1,
        team_id=1,
        goal="Current",
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 14),
        status=SprintStatus.ACTIVE,
    )
    later_planned = Sprint(
        sprint_id=12,
        project_id=1,
        team_id=1,
        goal="Later",
        start_date=date(2026, 8, 15),
        end_date=date(2026, 8, 28),
        status=SprintStatus.PLANNED,
    )

    def story(story_id: int, *, superseded: bool = False) -> UserStory:
        return UserStory(
            story_id=story_id,
            project_id=1,
            source_story_artifact_id=21,
            source_story_artifact_fingerprint="sha256:story-artifact",
            source_story_item_id=f"US-{story_id:06d}",
            source_story_item_fingerprint=f"sha256:story-item-{story_id}",
            accepted_spec_version_id=31,
            accepted_spec_hash="sha256:spec",
            spec_item_ids_json='["REQ-001"]',
            title=f"Story {story_id}",
            story_description="As an operator, I want current work.",
            acceptance_criteria_json='["It works."]',
            persona="operator",
            rank=f"{story_id:06d}",
            is_superseded=superseded,
        )

    selected = _select_refined_current_sprint_stories(
        [story(1), story(2, superseded=True), story(3)],
        [active, later_planned],
        {11: [1, 2], 12: [3]},
    )

    assert [item.story_id for item in selected] == [1]


def _story_with_acceptance_criteria(acceptance_criteria_json: str) -> UserStory:
    """Build one Story whose persisted criteria are rendered at the HTML boundary."""
    return UserStory(
        story_id=1,
        project_id=1,
        source_story_artifact_id=21,
        source_story_artifact_fingerprint="sha256:story-artifact",
        source_story_item_id="US-000001",
        source_story_item_fingerprint="sha256:story-item-1",
        accepted_spec_version_id=31,
        accepted_spec_hash="sha256:spec",
        spec_item_ids_json='["REQ-001"]',
        title="Acceptance rendering",
        story_description="As an operator, I want readable acceptance criteria.",
        acceptance_criteria_json=acceptance_criteria_json,
        persona="operator",
        rank="000001",
    )


def test_snapshot_story_table_renders_canonical_acceptance_criteria_items() -> None:
    """Render exact criteria as escaped list items with visible line preservation."""
    criteria = [
        "First line\nsecond line",
        "- Unicode ✓",
        '<unsafe>&"',
        "Third criterion",
    ]
    story = _story_with_acceptance_criteria(canonical_json(criteria))

    rendered = _render_stories_table([story])

    assert '<ul class="acceptance-criteria">' in rendered
    assert "<li>First line\nsecond line</li>" in rendered
    assert "<li>- Unicode ✓</li>" in rendered
    assert "<li>&lt;unsafe&gt;&amp;&quot;</li>" in rendered
    assert "<li>Third criterion</li>" in rendered
    criteria_cell = rendered.rsplit("<td>", maxsplit=1)[1].split("</td>", maxsplit=1)[0]
    assert "[" not in criteria_cell
    assert html.escape(canonical_json(criteria)) not in criteria_cell
    assert "\\n" not in criteria_cell
    assert ".acceptance-criteria > li { white-space: pre-wrap; }" in (
        _render_snapshot_styles()
    )


@pytest.mark.parametrize(
    "acceptance_criteria_json",
    [
        "[]",
        '["   "]',
        '["criterion", 1]',
        '[ "criterion" ]',
        '["✓"]',
        '{"criterion":"value"}',
        '["unterminated"',
    ],
)
def test_snapshot_story_table_rejects_noncanonical_acceptance_criteria(
    acceptance_criteria_json: str,
) -> None:
    """Fail closed when persisted criteria are not the exact accepted array."""
    story = _story_with_acceptance_criteria(acceptance_criteria_json)

    with pytest.raises(
        ValueError,
        match=re.escape(
            "Story acceptance criteria must be a canonical non-empty JSON string list."
        ),
    ):
        _render_stories_table([story])


def test_export_snapshot_command_writes_file(engine: Engine, tmp_path: Path) -> None:
    """Verify export snapshot command writes file."""
    with Session(engine) as session:
        project = _insert_basic_project(session)

    output_path = export_snapshot_command(
        project_id=require_id(project.project_id, "project_id"),
        output_dir=tmp_path,
        engine_override=engine,
    )

    assert output_path.exists()
