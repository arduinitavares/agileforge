"""Host-prepared input tests for the isolated Vision interview."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import event
from sqlmodel import Session

from models.core import Project
from services.vision_interview_input import VisionInterviewInputService
from workflow.contracts import NodeCategory, NodeDecision, RecommendationKind

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


def _decision() -> NodeDecision:
    return NodeDecision(
        node_id="vision.interview",
        child_graph_id="vision",
        request_kind="record_vision_interview_turn",
        category=NodeCategory.AVAILABLE,
        recommendation_kind=RecommendationKind.REQUIRED,
        reason_code="VISION_INTERVIEW_REQUIRED",
        decision_fingerprint="sha256:decision",
    )


def test_builds_initial_input_from_project_and_human_text_only(engine: Engine) -> None:
    """The first Vision turn does not read authority, specs, or repository state."""
    with Session(engine) as session:
        project = Project(
            name="Vision input",
            description="Human intent only.",
            origin="greenfield",
        )
        session.add(project)
        session.commit()
        assert project.project_id is not None
        project_id = project.project_id

    payload = VisionInterviewInputService(engine=engine).build(
        project_id=project_id,
        decision=_decision(),
        user_text="We need durable workflow decisions.",
    )

    assert payload == {
        "project_name": "Vision input",
        "project_description": "Human intent only.",
        "mode": "initial",
        "user_response": "We need durable workflow decisions.",
        "prior_components": None,
        "accepted_vision_statement": None,
    }


def test_build_does_not_query_unrelated_workflow_fact_tables(engine: Engine) -> None:
    """Vision host input never expands into the global workflow snapshot."""
    with Session(engine) as session:
        project = Project(
            name="Vision input isolation",
            description="Only Vision facts may be read.",
            origin="greenfield",
        )
        session.add(project)
        session.commit()
        assert project.project_id is not None
        project_id = project.project_id

    statements: list[str] = []

    def record_query(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: object,
    ) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement.lower())

    event.listen(engine, "before_cursor_execute", record_query)
    try:
        VisionInterviewInputService(engine=engine).build(
            project_id=project_id,
            decision=_decision(),
            user_text="Keep provider prompts focused on human intent.",
        )
    finally:
        event.remove(engine, "before_cursor_execute", record_query)

    observed = "\n".join(statements)
    assert 'from projects' in observed
    for table in (
        "spec_registry",
        "compiled_spec_authority",
        "repository_baselines",
        "repository_inventories",
        "workflow_node_attempts",
    ):
        assert table not in observed
