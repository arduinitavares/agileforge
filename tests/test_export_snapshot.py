"""Tests for HTML snapshot export."""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlmodel import Session

from agile_sqlmodel import (
    CompiledSpecAuthority,
    Product,
    SpecAuthorityAcceptance,
    SpecRegistry,
    Sprint,
    SprintStatus,
    SprintStory,
    TimeFrame,
    UserStory,
)
from models.core import Epic, Feature, Team, Theme
from scripts.export_snapshot import export_snapshot_command
from tests.typing_helpers import require_id
from tools.export_snapshot import export_project_snapshot_html
from utils.spec_schemas import (
    Invariant,
    InvariantType,
    RequiredFieldParams,
    SpecAuthorityCompilationSuccess,
    SpecAuthorityCompilerOutput,
)

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine


def _insert_basic_project(session: Session) -> Product:
    product = Product(
        name="Test Product",
        description="Demo",
        vision="Vision **bold**",
        roadmap="Roadmap text",
        technical_spec="Fallback spec",
    )
    session.add(product)
    session.commit()
    session.refresh(product)
    return product


def _insert_story_structure(session: Session, product_id: int) -> UserStory:
    theme = Theme(
        product_id=product_id,
        title="Payments",
        description="Payment flows",
        time_frame=TimeFrame.NOW,
    )
    session.add(theme)
    session.commit()
    session.refresh(theme)

    epic = Epic(
        theme_id=require_id(theme.theme_id, "theme_id"),
        title="Checkout",
        summary="Checkout flow",
    )
    session.add(epic)
    session.commit()
    session.refresh(epic)

    feature = Feature(
        epic_id=require_id(epic.epic_id, "epic_id"),
        title="Card payments",
        description="Support card payments",
    )
    session.add(feature)
    session.commit()
    session.refresh(feature)

    story = UserStory(
        product_id=product_id,
        feature_id=feature.feature_id,
        title="Pay with card",
        story_description="As a buyer, I want to pay with card",
        acceptance_criteria="Given a valid card, when I pay, then it succeeds",
        story_points=3,
        is_refined=True,
        story_origin="refined",
    )
    session.add(story)
    session.commit()
    session.refresh(story)
    return story


def _insert_current_sprint(
    session: Session,
    *,
    product_id: int,
    story_ids: list[int],
) -> Sprint:
    team = Team(name=f"Team-{product_id}")
    session.add(team)
    session.commit()
    session.refresh(team)

    sprint = Sprint(
        product_id=product_id,
        team_id=require_id(team.team_id, "team_id"),
        goal="Current Sprint Goal",
        start_date=date.today() - timedelta(days=3),  # noqa: DTZ011
        end_date=date.today() + timedelta(days=7),  # noqa: DTZ011
        status=SprintStatus.ACTIVE,
    )
    session.add(sprint)
    session.commit()
    session.refresh(sprint)

    for story_id in story_ids:
        session.add(
            SprintStory(
                sprint_id=require_id(sprint.sprint_id, "sprint_id"), story_id=story_id
            )
        )
    session.commit()
    return sprint


def _insert_approved_spec_with_authority(
    session: Session, product_id: int
) -> SpecRegistry:
    spec = SpecRegistry(
        product_id=product_id,
        spec_hash="hash123",
        content="# Spec\n## Section",
        content_ref="specs/test.md",
        status="approved",
        approved_by="reviewer@example.com",
        approval_notes="Looks good",
    )
    session.add(spec)
    session.commit()
    session.refresh(spec)

    success = SpecAuthorityCompilationSuccess(
        scope_themes=["Payments"],
        invariants=[
            Invariant(
                id="INV-0123456789abcdef",
                type=InvariantType.REQUIRED_FIELD,
                parameters=RequiredFieldParams(field_name="email"),
            )
        ],
        eligible_feature_rules=[],
        gaps=[],
        assumptions=[],
        source_map=[],
        compiler_version="3.0.0",
        prompt_hash="a" * 64,
    )
    compiled_json = SpecAuthorityCompilerOutput(success).model_dump_json()

    authority = CompiledSpecAuthority(
        spec_version_id=require_id(spec.spec_version_id, "spec_version_id"),
        compiler_version="3.0.0",
        prompt_hash="a" * 64,
        scope_themes=json.dumps(["Payments"]),
        invariants=json.dumps(
            [
                {
                    "id": "INV-0123456789abcdef",
                    "type": "REQUIRED_FIELD",
                    "parameters": {"field_name": "email"},
                }
            ]
        ),
        eligible_feature_ids=json.dumps([]),
        compiled_artifact_json=compiled_json,
    )
    session.add(authority)
    session.commit()

    return spec


def _snapshot_authority(
    session: Session,
    *,
    spec_version_id: int,
    compiler_version: str,
    theme: str,
) -> CompiledSpecAuthority:
    prompt_hash = compiler_version[0] * 64
    artifact = SpecAuthorityCompilationSuccess(
        scope_themes=[theme],
        invariants=[],
        eligible_feature_rules=[],
        gaps=[],
        assumptions=[],
        source_map=[],
        compiler_version=compiler_version,
        prompt_hash=prompt_hash,
    )
    row = CompiledSpecAuthority(
        spec_version_id=spec_version_id,
        compiler_version=compiler_version,
        prompt_hash=prompt_hash,
        scope_themes=json.dumps([theme]),
        invariants="[]",
        eligible_feature_ids="[]",
        rejected_features="[]",
        spec_gaps="[]",
        compiled_artifact_json=SpecAuthorityCompilerOutput(
            root=artifact
        ).model_dump_json(),
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def test_snapshot_loader_uses_exact_acceptance_bound_row(
    session: Session,
) -> None:
    """Accepted exports never fall to an older or newer candidate row."""
    from tools.export_snapshot import _load_compiled_authority  # noqa: PLC0415

    product = _insert_basic_project(session)
    product_id = require_id(product.product_id, "product_id")
    spec = SpecRegistry(
        product_id=product_id,
        spec_hash="snapshot-accepted",
        content="# Spec",
        status="approved",
    )
    session.add(spec)
    session.commit()
    session.refresh(spec)
    spec_version_id = require_id(spec.spec_version_id, "spec_version_id")
    _snapshot_authority(
        session,
        spec_version_id=spec_version_id,
        compiler_version="3.0.0",
        theme="Older",
    )
    accepted = _snapshot_authority(
        session,
        spec_version_id=spec_version_id,
        compiler_version="3.0.0",
        theme="Accepted",
    )
    _snapshot_authority(
        session,
        spec_version_id=spec_version_id,
        compiler_version="3.0.0",
        theme="Pending",
    )
    session.add(
        SpecAuthorityAcceptance(
            product_id=product_id,
            spec_version_id=spec_version_id,
            status="accepted",
            policy="test",
            decided_by="test",
            compiler_version=accepted.compiler_version,
            prompt_hash=accepted.prompt_hash,
            spec_hash=spec.spec_hash,
            pending_authority_id=accepted.authority_id,
        )
    )
    session.commit()

    loaded = _load_compiled_authority(session, spec)

    assert loaded is not None
    assert loaded.scope_themes == ["Accepted"]


def test_snapshot_loader_without_acceptance_uses_newest_row(
    session: Session,
) -> None:
    """Unaccepted exports choose newest insertion deterministically."""
    from tools.export_snapshot import _load_compiled_authority  # noqa: PLC0415

    product = _insert_basic_project(session)
    product_id = require_id(product.product_id, "product_id")
    spec = SpecRegistry(
        product_id=product_id,
        spec_hash="snapshot-pending",
        content="# Spec",
        status="approved",
    )
    session.add(spec)
    session.commit()
    session.refresh(spec)
    spec_version_id = require_id(spec.spec_version_id, "spec_version_id")
    _snapshot_authority(
        session,
        spec_version_id=spec_version_id,
        compiler_version="3.0.0",
        theme="Older",
    )
    _snapshot_authority(
        session,
        spec_version_id=spec_version_id,
        compiler_version="3.0.0",
        theme="Newest",
    )

    loaded = _load_compiled_authority(session, spec)

    assert loaded is not None
    assert loaded.scope_themes == ["Newest"]


def test_snapshot_loader_missing_exact_accepted_row_fails_closed(
    session: Session,
) -> None:
    """Accepted export never substitutes an available pending candidate."""
    from tools.export_snapshot import _load_compiled_authority  # noqa: PLC0415

    product = _insert_basic_project(session)
    product_id = require_id(product.product_id, "product_id")
    spec = SpecRegistry(
        product_id=product_id,
        spec_hash="snapshot-missing-accepted",
        content="# Spec",
        status="approved",
    )
    session.add(spec)
    session.commit()
    session.refresh(spec)
    spec_version_id = require_id(spec.spec_version_id, "spec_version_id")
    pending = _snapshot_authority(
        session,
        spec_version_id=spec_version_id,
        compiler_version="3.0.0",
        theme="Pending",
    )
    session.add(
        SpecAuthorityAcceptance(
            product_id=product_id,
            spec_version_id=spec_version_id,
            status="accepted",
            policy="test",
            decided_by="test",
            compiler_version=pending.compiler_version,
            prompt_hash=pending.prompt_hash,
            spec_hash=spec.spec_hash,
            pending_authority_id=999_999,
        )
    )
    session.commit()

    assert _load_compiled_authority(session, spec) is None


def test_export_snapshot_html_basic(engine: Engine, tmp_path: Path) -> None:
    """Verify export snapshot html basic."""
    with Session(engine) as session:
        product = _insert_basic_project(session)
        product_id = require_id(
            product.product_id, "product_id"
        )  # Capture before session closes
        story = _insert_story_structure(session, product_id)
        _insert_current_sprint(
            session,
            product_id=product_id,
            story_ids=[require_id(story.story_id, "story_id")],
        )
        _insert_approved_spec_with_authority(session, product_id)

    output_path = export_project_snapshot_html(
        product_id=product_id,
        output_dir=tmp_path,
        engine_override=engine,
    )

    html = output_path.read_text(encoding="utf-8")
    assert output_path.exists()
    assert "Test Product" in html
    assert "Product Vision" in html
    assert "Vision" in html
    # Spec content renders as markdown <h1> or falls back to <pre> with raw text
    assert "<h1>Spec</h1>" in html or "# Spec" in html
    assert "toc-level-2" in html
    assert "Current Sprint Refined Stories" in html
    assert "Project Backlog (All Stories)" in html
    assert "Payments" in html
    assert "INV-0123456789abcdef" in html


def test_export_snapshot_only_refined_current_sprint_stories(
    engine: Engine, tmp_path: Path
) -> None:
    """Verify export snapshot only refined current sprint stories."""
    with Session(engine) as session:
        product = _insert_basic_project(session)
        product_id = require_id(product.product_id, "product_id")
        in_scope_story = _insert_story_structure(session, product_id)

        non_refined_in_sprint = UserStory(
            product_id=product_id,
            title="Seed backlog story",
            story_description="As a user, I want a seed story",
            acceptance_criteria="Placeholder",
            is_refined=False,
            story_origin="backlog_seed",
        )
        refined_not_in_sprint = UserStory(
            product_id=product_id,
            title="Refined outside sprint",
            story_description="As a user, I want a refined backlog story",
            acceptance_criteria="Done when approved",
            is_refined=True,
            story_origin="refined",
        )
        session.add(non_refined_in_sprint)
        session.add(refined_not_in_sprint)
        session.commit()
        session.refresh(non_refined_in_sprint)
        session.refresh(refined_not_in_sprint)

        _insert_current_sprint(
            session,
            product_id=product_id,
            story_ids=[
                require_id(in_scope_story.story_id, "story_id"),
                require_id(non_refined_in_sprint.story_id, "story_id"),
            ],
        )

    output_path = export_project_snapshot_html(
        product_id=product_id,
        output_dir=tmp_path,
        engine_override=engine,
    )
    html = output_path.read_text(encoding="utf-8")

    assert "Pay with card" in html
    assert "Seed backlog story" in html
    assert "Refined outside sprint" in html
    assert "Total 1" in html


def test_export_snapshot_falls_back_to_product_spec(
    engine: Engine, tmp_path: Path
) -> None:
    """Verify export snapshot falls back to product spec."""
    with Session(engine) as session:
        product = _insert_basic_project(session)

    output_path = export_project_snapshot_html(
        product_id=require_id(product.product_id, "product_id"),
        output_dir=tmp_path,
        engine_override=engine,
    )

    html = output_path.read_text(encoding="utf-8")
    assert "Fallback spec" in html


def test_export_snapshot_command_writes_file(engine: Engine, tmp_path: Path) -> None:
    """Verify export snapshot command writes file."""
    with Session(engine) as session:
        product = _insert_basic_project(session)

    output_path = export_snapshot_command(
        product_id=require_id(product.product_id, "product_id"),
        output_dir=tmp_path,
        engine_override=engine,
    )

    assert output_path.exists()


def test_export_snapshot_rejects_malformed_v3_before_writing(
    engine: Engine,
    tmp_path: Path,
) -> None:
    """A selected invalid authority must abort before creating any export file."""
    with Session(engine) as session:
        product = _insert_basic_project(session)
        product_id = require_id(product.product_id, "product_id")
        spec = SpecRegistry(
            product_id=product_id,
            spec_hash="snapshot-invalid",
            content="# Approved spec",
            status="approved",
        )
        session.add(spec)
        session.commit()
        session.refresh(spec)
        session.add(
            CompiledSpecAuthority(
                spec_version_id=require_id(
                    spec.spec_version_id,
                    "spec_version_id",
                ),
                compiler_version="3.0.0",
                prompt_hash="m" * 64,
                compiled_artifact_json=json.dumps(
                    {"schema_version": "agileforge.compiled_authority.v3"}
                ),
                scope_themes="[]",
                invariants="[]",
                eligible_feature_ids="[]",
                rejected_features="[]",
                spec_gaps="[]",
            )
        )
        session.commit()

    with pytest.raises(ValueError, match="COMPILED_AUTHORITY_INVALID"):
        export_project_snapshot_html(
            product_id=product_id,
            output_dir=tmp_path,
            engine_override=engine,
        )

    assert list(tmp_path.iterdir()) == []
