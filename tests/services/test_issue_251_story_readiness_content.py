# tests/services/test_issue_251_story_readiness_content.py
"""Regression tests for Issue #251: Story readiness content read projection."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from sqlmodel import Session, col, select

from models.core import UserStory
from models.workflow import StoryArtifact, StoryArtifactDecision
from services.contracts.story import (
    CanonicalStoryItem,
    CanonicalStoryOutput,
    StoryItemEnvelope,
)
from services.read_projections import DurableReadProjectionService
from tests.services.test_durable_product_definition_projections import (
    _accepted_replacement_story_project,
    _data,
    _record_and_accept_story_set,
)
from tests.test_create_user_story import _invest_assessment, _story_content
from tests.workflow.test_planning_transitions import (
    _domain,
    _record_and_accept_roadmap,
    _record_and_accept_story,
    _seed_accepted_backlog,
)
from workflow.fingerprints import canonical_hash, canonical_json

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from workflow.contracts import JsonObject

EXPECTED_STORY_COUNT = 2
EXPECTED_CRITERIA_COUNT = 2


def test_story_dependencies_inspect_supplies_distinct_sibling_story_content(
    engine: Engine,
) -> None:
    """Supply distinct story content for sibling stories."""
    # Seed a project with one PBI-000001
    project_id = _seed_accepted_backlog(
        engine, requirements=("Calculate comma and delimiter operations",)
    )
    domain = _domain(engine)
    _record_and_accept_roadmap(
        domain, project_id, requirements=("Calculate comma and delimiter operations",)
    )

    # Accept a story artifact containing 2 distinct stories under PBI-000001
    content = _story_content(
        title="Custom Calculator Feature",
        item_count=2,
    )
    _artifact_id, activated_story_ids = _record_and_accept_story_set(
        domain,
        project_id,
        backlog_item_id="PBI-000001",
        content=content,
        idempotency_suffix="sibling-stories",
    )
    assert len(activated_story_ids) == EXPECTED_STORY_COUNT

    # Read projection through story_dependencies_inspect
    service = DurableReadProjectionService(engine=engine)
    result = service.story_dependencies_inspect(project_id=project_id)

    assert result["ok"] is True
    data = _data(result)
    stories = cast("list[JsonObject]", data["stories"])
    assert len(stories) == EXPECTED_STORY_COUNT

    story_1 = next(s for s in stories if s["story_id"] == activated_story_ids[0])
    story_2 = next(s for s in stories if s["story_id"] == activated_story_ids[1])

    # Both belong to the same parent PBI
    assert story_1["backlog_item_id"] == "PBI-000001"
    assert story_2["backlog_item_id"] == "PBI-000001"

    # Distinct titles, statements, and criteria
    assert story_1["title"] == "Custom Calculator Feature 1"
    assert story_2["title"] == "Custom Calculator Feature 2"
    assert story_1["title"] != story_2["title"]

    assert "survives restarts for item 1" in cast("str", story_1["statement"])
    assert "survives restarts for item 2" in cast("str", story_2["statement"])
    assert story_1["statement"] != story_2["statement"]

    assert isinstance(story_1["acceptance_criteria"], list)
    assert len(story_1["acceptance_criteria"]) == EXPECTED_CRITERIA_COUNT
    assert isinstance(story_2["acceptance_criteria"], list)
    assert len(story_2["acceptance_criteria"]) == EXPECTED_CRITERIA_COUNT

    assert story_1["content_status"] == "consistent"
    assert story_2["content_status"] == "consistent"
    assert story_1["content_error"] is None
    assert story_2["content_error"] is None

    # Structural eligibility and readiness preserved
    assert story_1["structurally_eligible"] is True
    assert story_2["structurally_eligible"] is True
    assert story_1["sprint_selection_state"] == "unselected"
    assert story_2["sprint_selection_state"] == "unselected"


def test_story_dependencies_inspect_preserves_binding_with_duplicate_local_ids(
    engine: Engine,
) -> None:
    """Preserve story binding when multiple PBIs share local story IDs."""
    requirements = ("First PBI requirement", "Second PBI requirement")
    project_id = _seed_accepted_backlog(engine, requirements=requirements)
    domain = _domain(engine)
    _record_and_accept_roadmap(domain, project_id, requirements=requirements)

    # _record_and_accept_story generates US-0001 for each PBI
    _art1, story_id_1 = _record_and_accept_story(
        engine,
        domain,
        project_id,
        requirement="First PBI requirement",
        spec_item_id="REQ.planning-1",
        backlog_item_id="PBI-000001",
        idempotency_suffix="-pbi1",
    )
    _art2, story_id_2 = _record_and_accept_story(
        engine,
        domain,
        project_id,
        requirement="Second PBI requirement",
        spec_item_id="REQ.planning-2",
        backlog_item_id="PBI-000002",
        idempotency_suffix="-pbi2",
    )

    service = DurableReadProjectionService(engine=engine)
    result = service.story_dependencies_inspect(project_id=project_id)
    assert result["ok"] is True
    data = _data(result)
    stories = cast("list[JsonObject]", data["stories"])
    assert len(stories) == EXPECTED_STORY_COUNT

    s1 = next(s for s in stories if s["story_id"] == story_id_1)
    s2 = next(s for s in stories if s["story_id"] == story_id_2)

    # Both share identical local item ID US-0001
    assert s1["source_story_item_id"] == "US-0001"
    assert s2["source_story_item_id"] == "US-0001"
    assert s1["backlog_item_id"] == "PBI-000001"
    assert s2["backlog_item_id"] == "PBI-000002"
    assert s1["title"] == "Story for First PBI requirement"
    assert s2["title"] == "Story for Second PBI requirement"


def test_story_dependencies_inspect_reports_inconsistent_content(
    engine: Engine,
) -> None:
    """Explicitly report inconsistent Story content without breaking projection."""
    project_id = _seed_accepted_backlog(
        engine, requirements=("Calculate operations",)
    )
    domain = _domain(engine)
    _record_and_accept_roadmap(
        domain, project_id, requirements=("Calculate operations",)
    )

    _art_id, activated_story_ids = _record_and_accept_story_set(
        domain,
        project_id,
        backlog_item_id="PBI-000001",
        content=_story_content(title="Test Story", item_count=2),
        idempotency_suffix="inconsistent-test",
    )
    story_id_corrupt_ac = activated_story_ids[0]
    story_id_corrupt_fingerprint = activated_story_ids[1]

    with Session(engine) as session:
        # Corrupt acceptance criteria
        row_ac = session.get(UserStory, story_id_corrupt_ac)
        assert row_ac is not None
        row_ac.acceptance_criteria_json = "invalid json criteria"
        session.add(row_ac)

        # Corrupt title to blank string
        row_fp = session.get(UserStory, story_id_corrupt_fingerprint)
        assert row_fp is not None
        row_fp.title = ""
        session.add(row_fp)
        session.commit()

    service = DurableReadProjectionService(engine=engine)
    result = service.story_dependencies_inspect(project_id=project_id)
    assert result["ok"] is True
    data = _data(result)
    stories = cast("list[JsonObject]", data["stories"])

    s_ac = next(s for s in stories if s["story_id"] == story_id_corrupt_ac)
    assert s_ac["content_status"] == "inconsistent"
    assert s_ac["acceptance_criteria"] is None
    assert s_ac["content_error"] == "Stored Story acceptance criteria are invalid."

    s_fp = next(s for s in stories if s["story_id"] == story_id_corrupt_fingerprint)
    assert s_fp["content_status"] == "inconsistent"
    assert s_fp["title"] is None
    assert s_fp["statement"] is None
    assert s_fp["acceptance_criteria"] is None
    assert s_fp["content_error"] == "Accepted Story title is blank or missing."
    assert s_fp["sprint_selection_state"] == "unselected"
    assert s_fp["structurally_eligible"] is False


def test_story_dependencies_inspect_preserves_raw_text_for_escaping(
    engine: Engine,
) -> None:
    """Preserve raw story text for downstream safe escaping."""
    special_title = '<script>alert("xss")</script> & "special"'
    special_statement = (
        "As a <tester>, I want & need 'safe' escaping, so that <b>tags</b>"
        " render as text."
    )
    special_criterion = "<img src=x onerror=alert(1)> & 'valid'"

    project_id = _seed_accepted_backlog(
        engine, requirements=("Special characters test",)
    )
    domain = _domain(engine)
    _record_and_accept_roadmap(
        domain, project_id, requirements=("Special characters test",)
    )

    item = CanonicalStoryItem(
        story_item_id="US-0001",
        story_title=special_title,
        statement=special_statement,
        persona="<tester>",
        acceptance_criteria=(special_criterion,),
        spec_item_ids=("REQ.planning-1",),
        invest_assessment=_invest_assessment(),
        estimated_effort="S",
        effort_rationale="Small escaping check.",
        order_rationale="Sequencing within parent PBI.",
        produced_artifacts=("sanitized text",),
        research_caveats=(),
        dependency_candidates=(),
    )
    envelope = StoryItemEnvelope(
        item=item,
        item_fingerprint=canonical_hash(item.model_dump(mode="json")),
    )
    special_content = CanonicalStoryOutput(
        story_items=(envelope,),
        is_complete=True,
        clarifying_questions=(),
    ).model_dump(mode="json")

    _art_id, activated_story_ids = _record_and_accept_story_set(
        domain,
        project_id,
        backlog_item_id="PBI-000001",
        content=special_content,
        idempotency_suffix="special-chars",
    )
    story_id = activated_story_ids[0]

    service = DurableReadProjectionService(engine=engine)
    result = service.story_dependencies_inspect(project_id=project_id)
    assert result["ok"] is True
    data = _data(result)
    stories = cast("list[JsonObject]", data["stories"])
    story = next(s for s in stories if s["story_id"] == story_id)

    assert story["title"] == special_title
    assert story["statement"] == special_statement
    assert story["acceptance_criteria"] == [special_criterion]


def test_story_dependencies_inspect_reports_inconsistent_on_provenance_mismatch(
    engine: Engine,
) -> None:
    """Report inconsistent content when artifact provenance does not match."""
    project_id = _seed_accepted_backlog(
        engine, requirements=("Provenance mismatch test",)
    )
    domain = _domain(engine)
    _record_and_accept_roadmap(
        domain, project_id, requirements=("Provenance mismatch test",)
    )

    art_id, activated_story_ids = _record_and_accept_story_set(
        domain,
        project_id,
        backlog_item_id="PBI-000001",
        content=_story_content(title="Provenance Story", item_count=1),
        idempotency_suffix="provenance-test",
    )
    story_id = activated_story_ids[0]

    with Session(engine) as session:
        decision = session.exec(
            select(StoryArtifactDecision).where(
                col(StoryArtifactDecision.project_id) == project_id,
                col(StoryArtifactDecision.story_artifact_id) == art_id,
            )
        ).one()
        decision.decision = "rejected"
        session.add(decision)
        session.commit()

    service = DurableReadProjectionService(engine=engine)
    result = service.story_dependencies_inspect(project_id=project_id)
    assert result["ok"] is True
    data = _data(result)
    stories = cast("list[JsonObject]", data["stories"])
    story = next(s for s in stories if s["story_id"] == story_id)

    assert story["content_status"] == "inconsistent"
    assert story["content_error"] == (
        "Accepted Story content does not match accepted artifact provenance."
    )
    assert story["title"] is None
    assert story["statement"] is None
    assert story["acceptance_criteria"] is None


def test_story_dependencies_inspect_binds_replacement_content_not_superseded(
    engine: Engine,
) -> None:
    """Bind replacement Story content and exclude superseded content."""
    project_id, source_story_ids, replacement_story_ids = (
        _accepted_replacement_story_project(engine)
    )

    service = DurableReadProjectionService(engine=engine)
    result = service.story_dependencies_inspect(project_id=project_id)
    assert result["ok"] is True
    data = _data(result)
    stories = cast("list[JsonObject]", data["stories"])

    projected_ids = {s["story_id"] for s in stories}
    # Superseded stories are excluded from active readiness
    for source_id in source_story_ids:
        assert source_id not in projected_ids

    # Replacement stories are present and have corrected content
    for index, rep_id in enumerate(replacement_story_ids, start=1):
        assert rep_id in projected_ids
        rep_story = next(s for s in stories if s["story_id"] == rep_id)
        assert rep_story["backlog_item_id"] == "PBI-000001"
        assert rep_story["source_story_item_id"] == f"US-{index:04d}"
        assert rep_story["title"] == f"Corrected calculator Story {index}"
        assert "survives restarts for item" in cast("str", rep_story["statement"])
        assert isinstance(rep_story["acceptance_criteria"], list)
        assert len(rep_story["acceptance_criteria"]) > 0
        assert rep_story["content_status"] == "consistent"
        assert rep_story["content_error"] is None


def test_story_dependencies_inspect_reports_inconsistent_when_title_altered(
    engine: Engine,
) -> None:
    """Expose inconsistency when a nonblank title changes after acceptance."""
    project_id = _seed_accepted_backlog(
        engine, requirements=("Title mismatch test",)
    )
    domain = _domain(engine)
    _record_and_accept_roadmap(
        domain, project_id, requirements=("Title mismatch test",)
    )
    _art_id, activated_story_ids = _record_and_accept_story_set(
        domain,
        project_id,
        backlog_item_id="PBI-000001",
        content=_story_content(title="Original Title", item_count=1),
        idempotency_suffix="title-mismatch",
    )
    story_id = activated_story_ids[0]

    with Session(engine) as session:
        row = session.get(UserStory, story_id)
        assert row is not None
        row.title = "Altered Nonblank Title"
        session.add(row)
        session.commit()

    service = DurableReadProjectionService(engine=engine)
    result = service.story_dependencies_inspect(project_id=project_id)
    assert result["ok"] is True
    data = _data(result)
    stories = cast("list[JsonObject]", data["stories"])
    story = next(s for s in stories if s["story_id"] == story_id)

    assert story["content_status"] == "inconsistent"
    assert story["content_error"] == (
        "Accepted Story title does not match accepted artifact item."
    )
    assert story["title"] is None
    assert story["statement"] is None
    assert story["acceptance_criteria"] is None


def test_story_dependencies_inspect_reports_inconsistent_when_statement_altered(
    engine: Engine,
) -> None:
    """Expose inconsistency when a nonblank statement changes after acceptance."""
    project_id = _seed_accepted_backlog(
        engine, requirements=("Statement mismatch test",)
    )
    domain = _domain(engine)
    _record_and_accept_roadmap(
        domain, project_id, requirements=("Statement mismatch test",)
    )
    _art_id, activated_story_ids = _record_and_accept_story_set(
        domain,
        project_id,
        backlog_item_id="PBI-000001",
        content=_story_content(title="Original Story", item_count=1),
        idempotency_suffix="statement-mismatch",
    )
    story_id = activated_story_ids[0]

    with Session(engine) as session:
        row = session.get(UserStory, story_id)
        assert row is not None
        row.story_description = (
            "As an operator, I want an altered statement so that behavior changes."
        )
        session.add(row)
        session.commit()

    service = DurableReadProjectionService(engine=engine)
    result = service.story_dependencies_inspect(project_id=project_id)
    assert result["ok"] is True
    data = _data(result)
    stories = cast("list[JsonObject]", data["stories"])
    story = next(s for s in stories if s["story_id"] == story_id)

    assert story["content_status"] == "inconsistent"
    assert story["content_error"] == (
        "Accepted Story statement does not match accepted artifact item."
    )
    assert story["title"] is None
    assert story["statement"] is None
    assert story["acceptance_criteria"] is None


def test_story_dependencies_inspect_reports_inconsistent_when_criteria_altered(
    engine: Engine,
) -> None:
    """Expose inconsistency when a valid criteria list changes after acceptance."""
    project_id = _seed_accepted_backlog(
        engine, requirements=("Criteria mismatch test",)
    )
    domain = _domain(engine)
    _record_and_accept_roadmap(
        domain, project_id, requirements=("Criteria mismatch test",)
    )
    _art_id, activated_story_ids = _record_and_accept_story_set(
        domain,
        project_id,
        backlog_item_id="PBI-000001",
        content=_story_content(title="Original Story", item_count=1),
        idempotency_suffix="criteria-mismatch",
    )
    story_id = activated_story_ids[0]

    with Session(engine) as session:
        row = session.get(UserStory, story_id)
        assert row is not None
        row.acceptance_criteria_json = canonical_json(
            ["Completely altered valid criterion."]
        )
        session.add(row)
        session.commit()

    service = DurableReadProjectionService(engine=engine)
    result = service.story_dependencies_inspect(project_id=project_id)
    assert result["ok"] is True
    data = _data(result)
    stories = cast("list[JsonObject]", data["stories"])
    story = next(s for s in stories if s["story_id"] == story_id)

    assert story["content_status"] == "inconsistent"
    assert story["content_error"] == (
        "Accepted Story acceptance criteria do not match accepted artifact item."
    )
    assert story["title"] is None
    assert story["statement"] is None
    assert story["acceptance_criteria"] is None


def test_story_dependencies_inspect_reports_inconsistent_when_title_whitespace_altered(
    engine: Engine,
) -> None:
    """Expose inconsistency when title has whitespace altered post-acceptance."""
    project_id = _seed_accepted_backlog(
        engine, requirements=("Title whitespace test",)
    )
    domain = _domain(engine)
    _record_and_accept_roadmap(
        domain, project_id, requirements=("Title whitespace test",)
    )
    _art_id, activated_story_ids = _record_and_accept_story_set(
        domain,
        project_id,
        backlog_item_id="PBI-000001",
        content=_story_content(title="Exact Title", item_count=1),
        idempotency_suffix="title-whitespace",
    )
    story_id = activated_story_ids[0]

    with Session(engine) as session:
        row = session.get(UserStory, story_id)
        assert row is not None
        row.title = f"{row.title} "
        session.add(row)
        session.commit()

    service = DurableReadProjectionService(engine=engine)
    result = service.story_dependencies_inspect(project_id=project_id)
    assert result["ok"] is True
    data = _data(result)
    stories = cast("list[JsonObject]", data["stories"])
    story = next(s for s in stories if s["story_id"] == story_id)

    assert story["content_status"] == "inconsistent"
    assert story["content_error"] == (
        "Accepted Story title does not match accepted artifact item."
    )
    assert story["title"] is None
    assert story["statement"] is None
    assert story["acceptance_criteria"] is None


def test_story_dependencies_inspect_reports_inconsistent_on_statement_whitespace(
    engine: Engine,
) -> None:
    """Expose inconsistency when statement has whitespace altered post-acceptance."""
    project_id = _seed_accepted_backlog(
        engine, requirements=("Statement whitespace test",)
    )
    domain = _domain(engine)
    _record_and_accept_roadmap(
        domain, project_id, requirements=("Statement whitespace test",)
    )
    _art_id, activated_story_ids = _record_and_accept_story_set(
        domain,
        project_id,
        backlog_item_id="PBI-000001",
        content=_story_content(title="Exact Statement Story", item_count=1),
        idempotency_suffix="statement-whitespace",
    )
    story_id = activated_story_ids[0]

    with Session(engine) as session:
        row = session.get(UserStory, story_id)
        assert row is not None
        row.story_description = f"{row.story_description}\n"
        session.add(row)
        session.commit()

    service = DurableReadProjectionService(engine=engine)
    result = service.story_dependencies_inspect(project_id=project_id)
    assert result["ok"] is True
    data = _data(result)
    stories = cast("list[JsonObject]", data["stories"])
    story = next(s for s in stories if s["story_id"] == story_id)

    assert story["content_status"] == "inconsistent"
    assert story["content_error"] == (
        "Accepted Story statement does not match accepted artifact item."
    )
    assert story["title"] is None
    assert story["statement"] is None
    assert story["acceptance_criteria"] is None


def test_story_dependencies_inspect_reports_inconsistent_on_item_ids_mismatch(
    engine: Engine,
) -> None:
    """Expose inconsistency when story_item_ids_json diverges from content."""
    project_id = _seed_accepted_backlog(
        engine, requirements=("Item IDs mismatch test",)
    )
    domain = _domain(engine)
    _record_and_accept_roadmap(
        domain, project_id, requirements=("Item IDs mismatch test",)
    )
    art_id, activated_story_ids = _record_and_accept_story_set(
        domain,
        project_id,
        backlog_item_id="PBI-000001",
        content=_story_content(title="Item IDs Story", item_count=1),
        idempotency_suffix="item-ids-mismatch",
    )
    story_id = activated_story_ids[0]

    with Session(engine) as session:
        art = session.get(StoryArtifact, art_id)
        assert art is not None
        art.story_item_ids_json = canonical_json(["US-9999"])
        session.add(art)
        session.commit()

    service = DurableReadProjectionService(engine=engine)
    result = service.story_dependencies_inspect(project_id=project_id)
    assert result["ok"] is True
    data = _data(result)
    stories = cast("list[JsonObject]", data["stories"])
    story = next(s for s in stories if s["story_id"] == story_id)

    assert story["content_status"] == "inconsistent"
    assert story["content_error"] == (
        "Accepted Story content does not match accepted artifact provenance."
    )
    assert story["title"] is None
    assert story["statement"] is None
    assert story["acceptance_criteria"] is None
