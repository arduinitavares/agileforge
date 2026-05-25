"""API tests for sprint setup, candidates, and generation flow."""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any, Never, cast

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

import api as api_module
from agile_sqlmodel import (
    CompiledSpecAuthority,
    Product,
    SpecRegistry,
    Sprint,
    SprintStatus,
    SprintStory,
    Task,
    TaskStatus,
    UserStory,
    WorkflowEvent,
    WorkflowEventType,
)
from models.core import Team
from tools.spec_tools import _compute_story_input_hash
from utils.spec_schemas import (
    AlignmentFinding,
    Invariant,
    InvariantType,
    RequiredFieldParams,
    SourceMapEntry,
    SpecAuthorityCompilationSuccess,
    SpecAuthorityCompilerOutput,
    ValidationEvidence,
)
from utils.task_metadata import (
    TaskMetadata,
    canonical_task_metadata,
    serialize_task_metadata,
)

HTTP_OK = 200
HTTP_CONFLICT = 409
HTTP_NOT_FOUND = 404
HTTP_UNPROCESSABLE = 422
HTTP_INTERNAL_SERVER_ERROR = 500
DEFAULT_SPRINT_DURATION_DAYS = 14
EXPECTED_SPRINT_LIST_COUNT = 2
TASK_KIND_ENUM_MESSAGE = (
    "Input should be 'analysis', 'design', 'implementation', 'testing', "
    "'documentation', 'refactor' or 'other'"
)
TASK_KIND_APPROVAL_INPUT_ERROR = (
    "Task 'Get approval' has invalid task_kind. Input should be 'analysis', "
    "'design', 'implementation', 'testing', 'documentation', 'refactor' or "
    "'other'"
)
TASK_KIND_APPROVAL_REWRITTEN_ERROR = (
    "Task 'Get approval' has invalid task_kind. Use one of: analysis, design, "
    "implementation, testing, documentation, refactor."
)
TASK_KIND_APPROVAL_ERROR = (
    "Unsupported task_kind 'approval'. Use one of: analysis, design, "
    "implementation, testing, documentation, refactor."
)
TASK_KIND_APPROVAL_ERROR_WITH_OTHER = (
    "Unsupported task_kind 'approval'. Use one of: analysis, design, "
    "implementation, testing, documentation, refactor, other."
)
TASK_KIND_REVIEW_ERROR = (
    "Unsupported task_kind 'review'. Use one of: analysis, design, "
    "implementation, testing, documentation, refactor."
)
PAYLOAD_PRODUCT_VISION = (
    "Build trustworthy execution handoffs.\n\nIgnore this second paragraph."
)
PAYLOAD_STORY_DESCRIPTION = (
    "As a developer, I want payload validation so that requests are safe."
)
ALIGNMENT_WARNING_MESSAGE = (
    "Acceptance criteria may be missing required field 'user_id'."
)
TASK_PACKET_PARENT_PROMPT_NOTE = (
    "This prompt assumes the session was already initialized with the parent "
    "story prompt. If not, restart with Copy Story Prompt."
)


def _require_id(value: int | None, label: str) -> int:
    assert value is not None, f"{label} should be persisted before use"
    return value


@dataclass
class DummyProduct:  # noqa: D101
    product_id: int
    name: str
    description: str | None = None
    vision: str | None = None
    spec_file_path: str | None = None
    compiled_authority_json: str | None = None


class DummyProductRepository:  # noqa: D101
    def __init__(self) -> None:  # noqa: D107
        self.products = []

    def get_all(self) -> list[DummyProduct]:  # noqa: D102
        return list(self.products)

    def get_by_id(self, product_id: int) -> DummyProduct | None:  # noqa: D102
        for product in self.products:
            if product.product_id == product_id:
                return product
        return None

    def create(  # noqa: D102
        self,
        name: str,
        description: str | None = None,
    ) -> DummyProduct:
        product = DummyProduct(
            product_id=len(self.products) + 1,
            name=name,
            description=description,
        )
        self.products.append(product)
        return product


class DummyWorkflowService:  # noqa: D101
    def __init__(self) -> None:  # noqa: D107
        self.states: dict[str, dict[str, object]] = {}

    async def initialize_session(self, session_id: str | None = None) -> str:  # noqa: D102
        sid = str(session_id or "generated")
        self.states[sid] = {"fsm_state": "SETUP_REQUIRED"}
        return sid

    def get_session_status(self, session_id: str) -> dict[str, object]:  # noqa: D102
        return dict(self.states.get(str(session_id), {}))

    def update_session_status(  # noqa: D102
        self,
        session_id: str,
        partial_update: dict[str, object],
    ) -> None:
        sid = str(session_id)
        current = dict(self.states.get(sid, {}))
        current.update(partial_update)
        self.states[sid] = current

    def migrate_legacy_setup_state(self) -> int:  # noqa: D102
        return 0


def _build_client(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TestClient, DummyProductRepository, DummyWorkflowService]:
    repo = DummyProductRepository()
    workflow = DummyWorkflowService()

    monkeypatch.setattr(api_module, "product_repo", repo)
    monkeypatch.setattr(api_module, "workflow_service", workflow)

    return TestClient(api_module.app), repo, workflow


def _seed_story_phase_project(
    repo: DummyProductRepository, workflow: DummyWorkflowService
) -> int:
    product = repo.create("Sprint Project")
    product.spec_file_path = __file__
    product.compiled_authority_json = '{"ok": true}'
    workflow.states[str(product.product_id)] = {
        "fsm_state": "STORY_PERSISTENCE",
        "roadmap_releases": [
            {
                "release_name": "Release 1",
                "items": ["Enable login"],
            }
        ],
        "story_saved": {"Enable login": True},
    }
    return product.product_id


def _seed_sprint_setup_project(
    repo: DummyProductRepository, workflow: DummyWorkflowService
) -> int:
    product = repo.create("Sprint Project")
    product.spec_file_path = __file__
    product.compiled_authority_json = '{"ok": true}'
    workflow.states[str(product.product_id)] = {
        "fsm_state": "SPRINT_SETUP",
    }
    return product.product_id


def _build_sprint_assessment(
    *,
    is_complete: bool = True,
    attempt_id: str = "sprint-attempt-1",
    artifact_fingerprint: str = "sha256:reviewed",
    source_fingerprint: str | None = None,
) -> dict[str, Any]:
    assessment = {
        "sprint_goal": "Persist event deltas safely",
        "sprint_number": 1,
        "duration_days": 14,
        "selected_stories": [],
        "deselected_stories": [],
        "capacity_analysis": {
            "velocity_assumption": "Medium",
            "capacity_band": "4-5 stories",
            "selected_count": 0,
            "story_points_used": 0,
            "max_story_points": 13,
            "commitment_note": "Does this scope feel achievable in 2 weeks?",
            "reasoning": "Fits the chosen capacity.",
        },
        "is_complete": is_complete,
    }
    if is_complete:
        assessment["attempt_id"] = attempt_id
        assessment["artifact_fingerprint"] = artifact_fingerprint
        if source_fingerprint is not None:
            assessment["source_fingerprint"] = source_fingerprint
    return assessment


def _seed_sprint_draft_project(
    repo: DummyProductRepository,
    workflow: DummyWorkflowService,
    *,
    is_complete: bool = True,
) -> int:
    product = repo.create("Sprint Draft Project")
    product.spec_file_path = __file__
    product.compiled_authority_json = '{"ok": true}'
    workflow.states[str(product.product_id)] = {
        "fsm_state": "SPRINT_DRAFT",
        "sprint_attempts": [
            {
                "attempt_id": "sprint-attempt-1",
                "artifact_fingerprint": "sha256:reviewed",
                "is_complete": is_complete,
            }
        ],
        "sprint_plan_assessment": _build_sprint_assessment(is_complete=is_complete),
    }
    return product.product_id


def _seed_saved_sprint(
    session: Session,
    repo: DummyProductRepository,
    *,
    started: bool,
    created_title: str,
) -> tuple[int, int]:
    product = repo.create(created_title)
    session.add(Product(product_id=product.product_id, name=product.name))

    team = Team(name=f"Team {product.product_id}")
    session.add(team)
    session.flush()

    story = UserStory(
        product_id=product.product_id,
        title=f"{created_title} Story",
        story_description="As a user, I want saved sprint coverage",
        acceptance_criteria="- AC",
    )
    session.add(story)
    session.flush()

    sprint = Sprint(
        goal=f"{created_title} Goal",
        start_date=date(2026, 3, 1),
        end_date=date(2026, 3, 15),
        status=SprintStatus.ACTIVE if started else SprintStatus.PLANNED,
        started_at=(datetime(2026, 3, 2, 9, 0, tzinfo=UTC) if started else None),
        product_id=product.product_id,
        team_id=_require_id(team.team_id, "team_id"),
    )
    session.add(sprint)
    session.flush()

    session.add(
        SprintStory(
            sprint_id=_require_id(sprint.sprint_id, "sprint_id"),
            story_id=_require_id(story.story_id, "story_id"),
        )
    )
    session.commit()

    return product.product_id, _require_id(sprint.sprint_id, "sprint_id")


def _seed_completed_sprint(
    session: Session,
    repo: DummyProductRepository,
    *,
    created_title: str,
) -> tuple[int, int]:
    product_id, sprint_id = _seed_saved_sprint(
        session,
        repo,
        started=True,
        created_title=created_title,
    )

    sprint = session.get(Sprint, sprint_id)
    assert sprint is not None
    sprint.status = SprintStatus.COMPLETED
    sprint.completed_at = datetime(2026, 3, 15, 18, 0, tzinfo=UTC)
    session.add(sprint)
    session.commit()

    return product_id, sprint_id


def _seed_task_packet_context(
    session: Session,
    repo: DummyProductRepository,
    *,
    pinned: bool,
    task_metadata: TaskMetadata | None = None,
) -> tuple[int, int, int, int]:
    product = repo.create("Task Packet Project")
    session.add(
        Product(
            product_id=product.product_id,
            name=product.name,
            vision=PAYLOAD_PRODUCT_VISION,
        )
    )

    team = Team(name=f"Packet Team {product.product_id}")
    session.add(team)
    session.flush()

    story = UserStory(
        product_id=product.product_id,
        title="Payload Validation Story",
        story_description=PAYLOAD_STORY_DESCRIPTION,
        acceptance_criteria="- include user_id\n- reject invalid payloads",
        persona="Developer",
        story_points=3,
        rank="1",
        source_requirement="api_payload_validation",
    )
    session.add(story)
    session.flush()

    task = Task(
        description="Implement payload validation for incoming requests",
        story_id=_require_id(story.story_id, "story_id"),
        metadata_json=serialize_task_metadata(
            task_metadata or canonical_task_metadata()
        ),
    )
    session.add(task)
    session.flush()

    sprint = Sprint(
        goal="Ship a trustworthy task packet API",
        start_date=date(2026, 4, 1),
        end_date=date(2026, 4, 14),
        status=SprintStatus.PLANNED,
        product_id=product.product_id,
        team_id=_require_id(team.team_id, "team_id"),
    )
    session.add(sprint)
    session.flush()

    link = SprintStory(
        sprint_id=_require_id(sprint.sprint_id, "sprint_id"),
        story_id=_require_id(story.story_id, "story_id"),
    )
    session.add(link)

    if pinned:
        spec_version = SpecRegistry(
            product_id=product.product_id,
            spec_hash="a" * 64,
            content="# Spec\n\n## Invariants\n- Requests must include user_id.",
            status="approved",
            approved_at=datetime.now(UTC),
            approved_by="tester",
        )
        session.add(spec_version)
        session.flush()

        invariant = Invariant(
            id="INV-0123456789abcdef",
            type=InvariantType.REQUIRED_FIELD,
            parameters=RequiredFieldParams(field_name="user_id"),
        )
        artifact = SpecAuthorityCompilationSuccess(
            scope_themes=["API"],
            domain="api",
            invariants=[invariant],
            eligible_feature_rules=[],
            gaps=[],
            assumptions=[],
            source_map=[
                SourceMapEntry(
                    invariant_id=invariant.id,
                    excerpt="Requests must include user_id.",
                    location="Spec §1",
                )
            ],
            compiler_version="1.0.0",
            prompt_hash="0" * 64,
        )
        authority = CompiledSpecAuthority(
            spec_version_id=_require_id(
                spec_version.spec_version_id,
                "spec_version_id",
            ),
            compiler_version="1.0.0",
            prompt_hash="0" * 64,
            scope_themes='["API"]',
            invariants='["REQUIRED_FIELD:user_id"]',
            eligible_feature_ids="[]",
            rejected_features="[]",
            spec_gaps="[]",
            compiled_artifact_json=SpecAuthorityCompilerOutput(
                root=artifact
            ).model_dump_json(),
        )
        session.add(authority)
        session.flush()

        story.accepted_spec_version_id = _require_id(
            spec_version.spec_version_id,
            "spec_version_id",
        )
        story.validation_evidence = ValidationEvidence(
            spec_version_id=_require_id(
                spec_version.spec_version_id,
                "spec_version_id",
            ),
            validated_at=datetime.now(UTC),
            passed=True,
            rules_checked=["SPEC_VERSION_EXISTS", "SPEC_PRODUCT_MATCH"],
            invariants_checked=["REQUIRED_FIELD:user_id"],
            evaluated_invariant_ids=[invariant.id],
            finding_invariant_ids=[invariant.id],
            failures=[],
            warnings=["Double-check payload casing."],
            alignment_warnings=[
                AlignmentFinding(
                    code="REQUIRED_FIELD_MISSING",
                    invariant=invariant.id,
                    capability=None,
                    message=ALIGNMENT_WARNING_MESSAGE,
                    severity="warning",
                    created_at=datetime.now(UTC),
                )
            ],
            alignment_failures=[],
            validator_version="1.0.0",
            input_hash=_compute_story_input_hash(story),
        ).model_dump_json()

    session.add(story)
    session.commit()

    return (
        product.product_id,
        _require_id(sprint.sprint_id, "sprint_id"),
        _require_id(story.story_id, "story_id"),
        _require_id(task.task_id, "task_id"),
    )


def test_complete_story_phase_moves_to_sprint_setup(monkeypatch):  # noqa: ANN001, ANN201, D103
    client, repo, workflow = _build_client(monkeypatch)
    project_id = _seed_story_phase_project(repo, workflow)

    response = client.post(
        f"/api/projects/{project_id}/story/complete_phase",
        json={
            "expected_state": "STORY_PERSISTENCE",
            "idempotency_key": "complete-story-phase-sprint-flow",
        },
    )

    assert response.status_code == 200  # noqa: PLR2004
    payload = response.json()
    assert payload["data"]["fsm_state"] == "SPRINT_SETUP"
    assert workflow.states[str(project_id)]["fsm_state"] == "SPRINT_SETUP"


def test_sprint_candidates_endpoint_returns_normalized_items(monkeypatch):  # noqa: ANN001, ANN201, D103
    client, repo, workflow = _build_client(monkeypatch)
    project_id = _seed_sprint_setup_project(repo, workflow)

    monkeypatch.setattr(
        api_module,
        "load_sprint_candidates",
        lambda project_id: {  # noqa: ARG005
            "success": True,
            "count": 1,
            "stories": [
                {
                    "story_id": 12,
                    "story_title": "Event Delta Persistence",
                    "priority": 2,
                    "story_points": 3,
                    "persona": "Reviewer",
                    "story_origin": "refined",
                }
            ],
            "excluded_counts": {"non_refined": 1, "superseded": 0},
            "message": "Found 1 sprint candidate.",
        },
    )

    response = client.get(f"/api/projects/{project_id}/sprint/candidates")

    assert response.status_code == 200  # noqa: PLR2004
    payload = response.json()
    assert payload["data"]["count"] == 1
    assert payload["data"]["items"][0]["story_title"] == "Event Delta Persistence"
    assert payload["data"]["excluded_counts"]["non_refined"] == 1


def test_sprint_generate_rejects_numeric_velocity_request(monkeypatch):  # noqa: ANN001, ANN201, D103
    client, repo, workflow = _build_client(monkeypatch)
    project_id = _seed_sprint_setup_project(repo, workflow)

    response = client.post(
        f"/api/projects/{project_id}/sprint/generate",
        json={
            "team_velocity_assumption": 40,
            "sprint_duration_days": 14,
            "include_task_decomposition": True,
        },
    )

    assert response.status_code == 422  # noqa: PLR2004


def test_sprint_generate_failure_stays_in_setup_and_records_attempt(monkeypatch):  # noqa: ANN001, ANN201, D103
    client, repo, workflow = _build_client(monkeypatch)
    project_id = _seed_sprint_setup_project(repo, workflow)

    async def fake_run_sprint_agent_from_state(  # noqa: ANN202, PLR0913
        state,  # noqa: ANN001, ARG001
        *,
        project_id,  # noqa: ANN001, ARG001
        team_velocity_assumption,  # noqa: ANN001
        sprint_duration_days,  # noqa: ANN001, ARG001
        max_story_points,  # noqa: ANN001, ARG001
        include_task_decomposition,  # noqa: ANN001, ARG001
        selected_story_ids,  # noqa: ANN001, ARG001
        user_input,  # noqa: ANN001, ARG001
    ):
        return {
            "success": False,
            "input_context": {
                "available_stories": [],
                "team_velocity_assumption": team_velocity_assumption,
            },
            "output_artifact": {
                "error": "SPRINT_GENERATION_FAILED",
                "message": "No eligible stories.",
                "is_complete": False,
            },
            "is_complete": None,
            "error": "No eligible stories.",
        }

    monkeypatch.setattr(
        api_module, "run_sprint_agent_from_state", fake_run_sprint_agent_from_state
    )

    response = client.post(
        f"/api/projects/{project_id}/sprint/generate",
        json={
            "team_velocity_assumption": "Medium",
            "sprint_duration_days": 14,
            "include_task_decomposition": True,
        },
    )

    assert response.status_code == 200  # noqa: PLR2004
    payload = response.json()
    assert payload["data"]["is_complete"] is False
    assert payload["data"]["fsm_state"] == "SPRINT_SETUP"
    assert workflow.states[str(project_id)]["fsm_state"] == "SPRINT_SETUP"
    attempts = workflow.states[str(project_id)]["sprint_attempts"]
    assert isinstance(attempts, list)
    assert len(attempts) == 1
    first_attempt = attempts[0]
    assert isinstance(first_attempt, dict)
    first_attempt = cast("dict[str, object]", first_attempt)
    output_artifact = first_attempt["output_artifact"]
    assert isinstance(output_artifact, dict)
    output_artifact = cast("dict[str, object]", output_artifact)
    assert output_artifact["error"] == "SPRINT_GENERATION_FAILED"


def test_sprint_generate_blocks_unready_candidates_before_runtime(  # noqa: ANN201, D103
    session,  # noqa: ANN001
    monkeypatch,  # noqa: ANN001
):
    client, repo, workflow = _build_client(monkeypatch)
    project_id = _seed_sprint_setup_project(repo, workflow)

    session.add(Product(product_id=project_id, name="Sprint Project"))
    session.add(
        UserStory(
            product_id=project_id,
            title="Unsized story",
            story_description="As a planner, I want sizing before sprint planning.",
            acceptance_criteria="- Story has a deterministic estimate.",
            story_origin="refined",
            is_refined=True,
            is_superseded=False,
            story_points=None,
            rank=None,
        )
    )
    session.commit()

    async def fake_run_sprint_agent_from_state(
        *_args: object, **_kwargs: object
    ) -> Never:
        msg = "runtime should not run for unready candidates"
        raise AssertionError(msg)

    monkeypatch.setattr(
        api_module, "run_sprint_agent_from_state", fake_run_sprint_agent_from_state
    )

    response = client.post(
        f"/api/projects/{project_id}/sprint/generate",
        json={
            "team_velocity_assumption": "Medium",
            "sprint_duration_days": 14,
            "include_task_decomposition": True,
        },
    )

    assert response.status_code == HTTP_CONFLICT
    assert "SPRINT_CANDIDATES_UNSIZED" in response.json()["detail"]
    assert "SPRINT_CANDIDATES_DEFAULT_PRIORITY" in response.json()["detail"]
    assert workflow.states[str(project_id)] == {"fsm_state": "SPRINT_SETUP"}


def test_sprint_failure_validation_errors_are_public_strings_in_history(monkeypatch):  # noqa: ANN001, ANN201, D103
    client, repo, workflow = _build_client(monkeypatch)
    project_id = _seed_sprint_setup_project(repo, workflow)

    async def fake_run_sprint_agent_from_state(  # noqa: ANN202, PLR0913
        state,  # noqa: ANN001
        *,
        project_id,  # noqa: ANN001
        team_velocity_assumption,  # noqa: ANN001
        sprint_duration_days,  # noqa: ANN001
        max_story_points,  # noqa: ANN001
        include_task_decomposition,  # noqa: ANN001
        selected_story_ids,  # noqa: ANN001
        user_input,  # noqa: ANN001
    ):
        _ = (
            state,
            project_id,
            team_velocity_assumption,
            sprint_duration_days,
            max_story_points,
            include_task_decomposition,
            selected_story_ids,
            user_input,
        )
        return {
            "success": False,
            "input_context": {
                "available_stories": [],
                "team_velocity_assumption": "Medium",
            },
            "output_artifact": {
                "error": "SPRINT_GENERATION_FAILED",
                "message": "Sprint output validation failed.",
                "is_complete": False,
                "validation_errors": [TASK_KIND_APPROVAL_ERROR],
            },
            "is_complete": None,
            "error": "Sprint output validation failed.",
            "failure_stage": "output_validation",
            "failure_summary": "Sprint output validation failed.",
            "failure_artifact_id": "artifact-123",
            "raw_output_preview": None,
            "has_full_artifact": True,
        }

    monkeypatch.setattr(
        api_module, "run_sprint_agent_from_state", fake_run_sprint_agent_from_state
    )

    generate_response = client.post(
        f"/api/projects/{project_id}/sprint/generate",
        json={
            "team_velocity_assumption": "Medium",
            "sprint_duration_days": 14,
            "include_task_decomposition": True,
        },
    )

    assert generate_response.status_code == 200  # noqa: PLR2004
    generate_payload = generate_response.json()
    assert generate_payload["data"]["output_artifact"]["validation_errors"] == [
        TASK_KIND_APPROVAL_ERROR
    ]

    history_response = client.get(f"/api/projects/{project_id}/sprint/history")

    assert history_response.status_code == 200  # noqa: PLR2004
    history_payload = history_response.json()
    assert history_payload["data"]["items"][0]["output_artifact"][
        "validation_errors"
    ] == [TASK_KIND_APPROVAL_ERROR]
    assert all(
        isinstance(item, str)
        for item in history_payload["data"]["items"][0]["output_artifact"][
            "validation_errors"
        ]
    )


def test_sprint_history_normalizes_legacy_structured_validation_errors(monkeypatch):  # noqa: ANN001, ANN201, D103
    client, repo, workflow = _build_client(monkeypatch)
    project_id = _seed_sprint_setup_project(repo, workflow)
    workflow.states[str(project_id)]["sprint_attempts"] = [
        {
            "created_at": "2026-03-29T12:00:00Z",
            "trigger": "manual_refine",
            "input_context": {"available_stories": []},
            "output_artifact": {
                "error": "SPRINT_GENERATION_FAILED",
                "message": "Sprint output validation failed.",
                "is_complete": False,
                "validation_errors": [
                    {
                        "loc": ["selected_stories", 0, "tasks", 0, "task_kind"],
                        "msg": TASK_KIND_ENUM_MESSAGE,
                        "input": "review",
                    },
                    {
                        "loc": [
                            "selected_stories",
                            0,
                            "tasks",
                            0,
                            "artifact_targets",
                            0,
                        ],
                        "msg": "Artifact target looks like a file path.",
                        "input": "api.py",
                    },
                ],
            },
            "is_complete": False,
        }
    ]

    history_response = client.get(f"/api/projects/{project_id}/sprint/history")

    assert history_response.status_code == 200  # noqa: PLR2004
    history_payload = history_response.json()
    assert history_payload["data"]["items"][0]["output_artifact"][
        "validation_errors"
    ] == [
        TASK_KIND_REVIEW_ERROR,
        "Artifact target looks like a file path.",
    ]
    assert all(
        isinstance(item, str)
        for item in history_payload["data"]["items"][0]["output_artifact"][
            "validation_errors"
        ]
    )


def test_sprint_history_rewrites_legacy_task_kind_string_hints(monkeypatch):  # noqa: ANN001, ANN201, D103
    client, repo, workflow = _build_client(monkeypatch)
    project_id = _seed_sprint_setup_project(repo, workflow)
    workflow.states[str(project_id)]["sprint_attempts"] = [
        {
            "created_at": "2026-03-29T12:05:00Z",
            "trigger": "manual_refine",
            "input_context": {"available_stories": []},
            "output_artifact": {
                "error": "SPRINT_GENERATION_FAILED",
                "message": "Sprint output validation failed.",
                "is_complete": False,
                "validation_errors": [
                    TASK_KIND_APPROVAL_INPUT_ERROR,
                    TASK_KIND_APPROVAL_ERROR_WITH_OTHER,
                ],
            },
            "is_complete": False,
        }
    ]

    history_response = client.get(f"/api/projects/{project_id}/sprint/history")

    assert history_response.status_code == 200  # noqa: PLR2004
    history_payload = history_response.json()
    assert history_payload["data"]["items"][0]["output_artifact"][
        "validation_errors"
    ] == [
        TASK_KIND_APPROVAL_REWRITTEN_ERROR,
        TASK_KIND_APPROVAL_ERROR,
    ]
    assert all(
        "other" not in item
        for item in history_payload["data"]["items"][0]["output_artifact"][
            "validation_errors"
        ]
    )


def test_sprint_generate_success_moves_to_draft_and_marks_assessment_complete(  # noqa: ANN201, D103
    monkeypatch,  # noqa: ANN001
):
    client, repo, workflow = _build_client(monkeypatch)
    project_id = _seed_sprint_setup_project(repo, workflow)
    captured: dict[str, Any] = {}

    async def fake_run_sprint_agent_from_state(  # noqa: ANN202, PLR0913
        state,  # noqa: ANN001, ARG001
        *,
        project_id,  # noqa: ANN001, ARG001
        team_velocity_assumption,  # noqa: ANN001
        sprint_duration_days,  # noqa: ANN001
        max_story_points,  # noqa: ANN001
        include_task_decomposition,  # noqa: ANN001, ARG001
        selected_story_ids,  # noqa: ANN001
        user_input,  # noqa: ANN001, ARG001
    ):
        captured["selected_story_ids"] = selected_story_ids
        captured["team_velocity_assumption"] = team_velocity_assumption
        return {
            "success": True,
            "input_context": {
                "available_stories": [
                    {
                        "story_id": 12,
                        "story_title": "Event Delta Persistence",
                        "priority": 2,
                        "story_points": 3,
                    }
                ],
                "team_velocity_assumption": team_velocity_assumption,
                "sprint_duration_days": sprint_duration_days,
            },
            "output_artifact": {
                "sprint_goal": "Persist event deltas safely",
                "sprint_number": 1,
                "duration_days": 14,
                "selected_stories": [],
                "deselected_stories": [],
                "capacity_analysis": {
                    "velocity_assumption": team_velocity_assumption,
                    "capacity_band": "4-5 stories",
                    "selected_count": 0,
                    "story_points_used": 0,
                    "max_story_points": max_story_points,
                    "commitment_note": "Does this scope feel achievable in 2 weeks?",
                    "reasoning": "Fits the chosen capacity.",
                },
                "is_complete": True,
            },
            "is_complete": True,
            "error": None,
        }

    monkeypatch.setattr(
        api_module, "run_sprint_agent_from_state", fake_run_sprint_agent_from_state
    )

    response = client.post(
        f"/api/projects/{project_id}/sprint/generate",
        json={
            "team_velocity_assumption": "High",
            "sprint_duration_days": 14,
            "max_story_points": 13,
            "include_task_decomposition": False,
            "selected_story_ids": [12],
            "user_input": "Focus on persistence",
        },
    )

    assert response.status_code == 200  # noqa: PLR2004
    payload = response.json()
    assert payload["data"]["is_complete"] is True
    assert payload["data"]["fsm_state"] == "SPRINT_DRAFT"
    assert workflow.states[str(project_id)]["fsm_state"] == "SPRINT_DRAFT"
    sprint_plan_assessment = workflow.states[str(project_id)]["sprint_plan_assessment"]
    assert isinstance(sprint_plan_assessment, dict)
    sprint_plan_assessment = cast("dict[str, object]", sprint_plan_assessment)
    assert sprint_plan_assessment["is_complete"] is True
    assert captured["selected_story_ids"] == [12]
    assert captured["team_velocity_assumption"] == "High"


def test_sprint_history_resets_stale_saved_working_set_after_completed_sprint(  # noqa: ANN201, D103
    session,  # noqa: ANN001
    monkeypatch,  # noqa: ANN001
):
    client, repo, workflow = _build_client(monkeypatch)
    project_id, completed_sprint_id = _seed_completed_sprint(
        session,
        repo,
        created_title="Completed Sprint",
    )
    workflow.states[str(project_id)] = {
        "fsm_state": "SPRINT_PERSISTENCE",
        "sprint_planner_owner_sprint_id": completed_sprint_id,
        "sprint_attempts": [
            {
                "created_at": "2026-03-20T09:00:00Z",
                "trigger": "auto_transition",
                "input_context": {"available_stories": []},
                "output_artifact": _build_sprint_assessment(is_complete=True),
                "is_complete": True,
            }
        ],
        "sprint_last_input_context": {"available_stories": []},
        "sprint_plan_assessment": _build_sprint_assessment(is_complete=True),
    }

    response = client.get(f"/api/projects/{project_id}/sprint/history")

    assert response.status_code == 200  # noqa: PLR2004
    payload = response.json()
    assert payload["data"]["count"] == 0
    assert payload["data"]["items"] == []
    state = workflow.states[str(project_id)]
    assert state["sprint_attempts"] == []
    assert state["sprint_last_input_context"] is None
    assert state["sprint_plan_assessment"] is None
    assert state["sprint_planner_owner_sprint_id"] is None


def test_sprint_generate_resets_stale_saved_working_set_before_next_cycle(  # noqa: ANN201, D103
    session,  # noqa: ANN001
    monkeypatch,  # noqa: ANN001
):
    client, repo, workflow = _build_client(monkeypatch)
    project_id, completed_sprint_id = _seed_completed_sprint(
        session,
        repo,
        created_title="Completed Sprint",
    )
    workflow.states[str(project_id)] = {
        "fsm_state": "SPRINT_PERSISTENCE",
        "sprint_planner_owner_sprint_id": completed_sprint_id,
        "sprint_attempts": [
            {
                "created_at": "2026-03-20T09:00:00Z",
                "trigger": "auto_transition",
                "input_context": {"available_stories": []},
                "output_artifact": _build_sprint_assessment(is_complete=True),
                "is_complete": True,
            }
        ],
        "sprint_plan_assessment": _build_sprint_assessment(is_complete=True),
    }

    async def fake_run_sprint_agent_from_state(  # noqa: ANN202, PLR0913
        state,  # noqa: ANN001, ARG001
        *,
        project_id,  # noqa: ANN001, ARG001
        team_velocity_assumption,  # noqa: ANN001
        sprint_duration_days,  # noqa: ANN001, ARG001
        max_story_points,  # noqa: ANN001, ARG001
        include_task_decomposition,  # noqa: ANN001, ARG001
        selected_story_ids,  # noqa: ANN001
        user_input,  # noqa: ANN001, ARG001
    ):
        return {
            "success": True,
            "input_context": {
                "available_stories": [],
                "team_velocity_assumption": team_velocity_assumption,
                "selected_story_ids": selected_story_ids,
            },
            "output_artifact": _build_sprint_assessment(is_complete=True),
            "is_complete": True,
            "error": None,
        }

    monkeypatch.setattr(
        api_module, "run_sprint_agent_from_state", fake_run_sprint_agent_from_state
    )

    response = client.post(
        f"/api/projects/{project_id}/sprint/generate",
        json={
            "team_velocity_assumption": "Medium",
            "sprint_duration_days": 14,
            "include_task_decomposition": True,
        },
    )

    assert response.status_code == 200  # noqa: PLR2004
    payload = response.json()
    assert payload["data"]["trigger"] == "auto_transition"
    attempts = workflow.states[str(project_id)]["sprint_attempts"]
    assert isinstance(attempts, list)
    assert len(attempts) == 1
    reset_attempt = attempts[0]
    assert isinstance(reset_attempt, dict)
    reset_attempt = cast("dict[str, object]", reset_attempt)
    assert reset_attempt["trigger"] == "auto_transition"
    assert workflow.states[str(project_id)]["sprint_planner_owner_sprint_id"] is None


def test_sprint_history_preserves_matching_planned_sprint_working_set(  # noqa: ANN201, D103
    session,  # noqa: ANN001
    monkeypatch,  # noqa: ANN001
):
    client, repo, workflow = _build_client(monkeypatch)
    project_id, planned_sprint_id = _seed_saved_sprint(
        session,
        repo,
        started=False,
        created_title="Planned Sprint",
    )
    attempt = {
        "created_at": "2026-03-20T09:00:00Z",
        "trigger": "manual_refine",
        "input_context": {"available_stories": []},
        "output_artifact": _build_sprint_assessment(is_complete=True),
        "is_complete": True,
    }
    workflow.states[str(project_id)] = {
        "fsm_state": "SPRINT_PERSISTENCE",
        "sprint_planner_owner_sprint_id": planned_sprint_id,
        "sprint_attempts": [attempt],
        "sprint_plan_assessment": _build_sprint_assessment(is_complete=True),
    }

    response = client.get(f"/api/projects/{project_id}/sprint/history")

    assert response.status_code == 200  # noqa: PLR2004
    payload = response.json()
    assert payload["data"]["count"] == 1
    assert payload["data"]["items"][0]["trigger"] == "manual_refine"
    state = workflow.states[str(project_id)]
    assert state["sprint_attempts"] == [attempt]
    assert state["sprint_planner_owner_sprint_id"] == planned_sprint_id


def test_create_next_sprint_reset_clears_legacy_ownerless_working_set(  # noqa: ANN201, D103
    session,  # noqa: ANN001
    monkeypatch,  # noqa: ANN001
):
    client, repo, workflow = _build_client(monkeypatch)
    project_id, _completed_sprint_id = _seed_completed_sprint(
        session,
        repo,
        created_title="Completed Sprint",
    )
    workflow.states[str(project_id)] = {
        "fsm_state": "SPRINT_PERSISTENCE",
        "sprint_attempts": [
            {
                "created_at": "2026-03-20T09:00:00Z",
                "trigger": "auto_transition",
                "input_context": {"available_stories": []},
                "output_artifact": _build_sprint_assessment(is_complete=True),
                "is_complete": True,
            }
        ],
        "sprint_last_input_context": {"available_stories": []},
        "sprint_plan_assessment": _build_sprint_assessment(is_complete=True),
    }

    response = client.post(f"/api/projects/{project_id}/sprint/planner/reset")

    assert response.status_code == 200  # noqa: PLR2004
    payload = response.json()
    assert payload["data"]["count"] == 0
    assert payload["data"]["items"] == []
    state = workflow.states[str(project_id)]
    assert state["sprint_attempts"] == []
    assert state["sprint_last_input_context"] is None
    assert state["sprint_plan_assessment"] is None
    assert state["sprint_planner_owner_sprint_id"] is None


def test_sprint_save_sanitizes_assessment_and_uses_tool_contract(monkeypatch):  # noqa: ANN001, ANN201, D103
    client, repo, workflow = _build_client(monkeypatch)
    project_id = _seed_sprint_draft_project(repo, workflow)
    hydrated_context = SimpleNamespace(
        state={"preserved": True}, session_id=str(project_id)
    )
    captured: dict[str, Any] = {}

    async def fake_hydrate_context(session_id: str, project_id: int):  # noqa: ANN202
        assert session_id == str(project_id)
        return hydrated_context

    def fake_save_sprint_plan_tool(input_data, tool_context):  # noqa: ANN001, ANN202
        captured["input_data"] = input_data
        captured["tool_context"] = tool_context
        return {"success": True, "sprint_id": 9}

    monkeypatch.setattr(api_module, "_hydrate_context", fake_hydrate_context)
    monkeypatch.setattr(api_module, "save_sprint_plan_tool", fake_save_sprint_plan_tool)

    response = client.post(
        f"/api/projects/{project_id}/sprint/save",
        json={
            "team_name": "Team Alpha",
            "sprint_start_date": "2026-03-15",
        },
    )

    assert response.status_code == 200  # noqa: PLR2004
    payload = response.json()
    assert payload["data"]["fsm_state"] == "SPRINT_PERSISTENCE"
    assert workflow.states[str(project_id)]["fsm_state"] == "SPRINT_PERSISTENCE"
    assert captured["input_data"].team_id is None
    assert captured["input_data"].team_name == "Team Alpha"
    assert captured["input_data"].sprint_start_date == "2026-03-15"
    assert captured["input_data"].sprint_duration_days == 14  # noqa: PLR2004
    assert captured["tool_context"].state["preserved"] is True
    assert captured["tool_context"].state["sprint_plan"]["duration_days"] == 14  # noqa: PLR2004
    assert "is_complete" not in captured["tool_context"].state["sprint_plan"]


def test_sprint_save_api_blocks_older_complete_after_latest_failed_attempt(monkeypatch):  # noqa: ANN001, ANN201, D103
    client, repo, workflow = _build_client(monkeypatch)
    project_id = _seed_sprint_draft_project(repo, workflow)
    workflow.states[str(project_id)]["sprint_attempts"].append(
        {
            "attempt_id": "sprint-attempt-2",
            "artifact_fingerprint": "sha256:failed",
            "source_fingerprint": "sha256:source",
            "is_complete": False,
            "failure_stage": "invocation_exception",
        }
    )

    def fail_if_called(_input_data: object, _tool_context: object) -> Never:
        msg = "save tool should not be called for stale API sprint save"
        raise AssertionError(msg)

    monkeypatch.setattr(api_module, "save_sprint_plan_tool", fail_if_called)

    response = client.post(
        f"/api/projects/{project_id}/sprint/save",
        json={
            "team_name": "Team Alpha",
            "sprint_start_date": "2026-03-15",
        },
    )

    assert response.status_code == 409  # noqa: PLR2004
    assert "latest complete Sprint attempt" in response.json()["detail"]


def test_sprint_save_requires_team_name(monkeypatch):  # noqa: ANN001, ANN201, D103
    client, repo, workflow = _build_client(monkeypatch)
    project_id = _seed_sprint_draft_project(repo, workflow)

    response = client.post(
        f"/api/projects/{project_id}/sprint/save",
        json={"sprint_start_date": "2026-03-15"},
    )

    assert response.status_code == 422  # noqa: PLR2004


def test_sprint_save_requires_start_date(monkeypatch):  # noqa: ANN001, ANN201, D103
    client, repo, workflow = _build_client(monkeypatch)
    project_id = _seed_sprint_draft_project(repo, workflow)

    response = client.post(
        f"/api/projects/{project_id}/sprint/save",
        json={"team_name": "Team Alpha"},
    )

    assert response.status_code == 422  # noqa: PLR2004


def test_sprint_save_rejects_incomplete_assessment(monkeypatch):  # noqa: ANN001, ANN201, D103
    client, repo, workflow = _build_client(monkeypatch)
    project_id = _seed_sprint_draft_project(repo, workflow, is_complete=False)

    response = client.post(
        f"/api/projects/{project_id}/sprint/save",
        json={
            "team_name": "Team Alpha",
            "sprint_start_date": "2026-03-15",
        },
    )

    assert response.status_code == 409  # noqa: PLR2004
    assert (
        response.json()["detail"] == "Sprint cannot be saved until is_complete is true"
    )


def test_sprint_save_maps_open_sprint_conflict_to_http_409(monkeypatch):  # noqa: ANN001, ANN201, D103
    client, repo, workflow = _build_client(monkeypatch)
    project_id = _seed_sprint_draft_project(repo, workflow)

    async def fake_hydrate_context(session_id: str, project_id: int):  # noqa: ANN202, ARG001
        return SimpleNamespace(state={}, session_id=session_id)

    def fake_save_sprint_plan_tool(input_data, tool_context):  # noqa: ANN001, ANN202, ARG001
        return {
            "success": False,
            "error_code": "STORY_ALREADY_IN_OPEN_SPRINT",
            "error": "Stories already assigned to active or planned sprints: [12]",
        }

    monkeypatch.setattr(api_module, "_hydrate_context", fake_hydrate_context)
    monkeypatch.setattr(api_module, "save_sprint_plan_tool", fake_save_sprint_plan_tool)

    response = client.post(
        f"/api/projects/{project_id}/sprint/save",
        json={
            "team_name": "Team Alpha",
            "sprint_start_date": "2026-03-15",
        },
    )

    assert response.status_code == 409  # noqa: PLR2004
    assert (
        response.json()["detail"]
        == "Stories already assigned to active or planned sprints: [12]"
    )


def test_sprint_save_surfaces_unexpected_persistence_tool_error(monkeypatch):  # noqa: ANN001, ANN201, D103
    client, repo, workflow = _build_client(monkeypatch)
    project_id = _seed_sprint_draft_project(repo, workflow)

    async def fake_hydrate_context(session_id: str, project_id: int):  # noqa: ANN202, ARG001
        return SimpleNamespace(state={}, session_id=session_id)

    def fake_save_sprint_plan_tool(input_data, tool_context):  # noqa: ANN001, ANN202, ARG001
        return {"success": False, "error": "database unavailable"}

    monkeypatch.setattr(api_module, "_hydrate_context", fake_hydrate_context)
    monkeypatch.setattr(api_module, "save_sprint_plan_tool", fake_save_sprint_plan_tool)

    response = client.post(
        f"/api/projects/{project_id}/sprint/save",
        json={
            "team_name": "Team Alpha",
            "sprint_start_date": "2026-03-15",
        },
    )

    assert response.status_code == 500  # noqa: PLR2004
    assert response.json()["detail"] == "database unavailable"


def test_project_state_normalizes_legacy_sprint_complete(monkeypatch):  # noqa: ANN001, ANN201, D103
    client, repo, workflow = _build_client(monkeypatch)
    project_id = _seed_sprint_setup_project(repo, workflow)
    workflow.states[str(project_id)] = {
        "fsm_state": "SPRINT_COMPLETE",
        "setup_status": "passed",
    }

    response = client.get(f"/api/projects/{project_id}/state")

    assert response.status_code == 200  # noqa: PLR2004
    assert response.json()["data"]["fsm_state"] == "SPRINT_PERSISTENCE"


def test_list_sprints_returns_saved_sprints_newest_first(session, monkeypatch):  # noqa: ANN001, ANN201, D103
    client, repo, _workflow = _build_client(monkeypatch)
    project_id, older_sprint_id = _seed_saved_sprint(
        session,
        repo,
        started=True,
        created_title="Older Sprint",
    )

    team = session.exec(select(Team).where(Team.name == f"Team {project_id}")).first()
    assert team is not None

    second_story = UserStory(
        product_id=project_id,
        title="Newest Sprint Story",
        story_description="As a user, I want another sprint",
        acceptance_criteria="- AC",
    )
    session.add(second_story)
    session.flush()

    newer_sprint = Sprint(
        goal="Newest Sprint Goal",
        start_date=date(2026, 4, 1),
        end_date=date(2026, 4, 15),
        status=SprintStatus.PLANNED,
        product_id=project_id,
        team_id=team.team_id,
    )
    session.add(newer_sprint)
    session.flush()
    session.add(
        SprintStory(
            sprint_id=_require_id(newer_sprint.sprint_id, "sprint_id"),
            story_id=_require_id(second_story.story_id, "story_id"),
        )
    )
    session.commit()

    response = client.get(f"/api/projects/{project_id}/sprints")

    assert response.status_code == 200  # noqa: PLR2004
    payload = response.json()
    assert payload["data"]["count"] == 2  # noqa: PLR2004
    assert payload["data"]["runtime_summary"] == {
        "active_sprint_id": older_sprint_id,
        "planned_sprint_id": newer_sprint.sprint_id,
        "latest_completed_sprint_id": None,
        "can_create_next_sprint": False,
        "create_next_sprint_disabled_reason": (
            "A planned sprint already exists. Modify it instead of creating another."
        ),
    }
    assert payload["data"]["items"][0]["id"] == newer_sprint.sprint_id
    assert payload["data"]["items"][0]["started_at"] is None
    assert payload["data"]["items"][1]["started_at"] is not None
    assert payload["data"]["items"][0]["story_count"] == 1
    assert payload["data"]["items"][0]["history_fidelity"] == "derived"
    assert payload["data"]["items"][0]["allowed_actions"] == {
        "can_start": False,
        "start_disabled_reason": (
            "Only planned sprints without another active sprint can be started."
        ),
        "can_close": False,
        "close_disabled_reason": "Only active sprints can be closed.",
        "can_modify_planned": True,
        "modify_disabled_reason": None,
    }
    assert "selected_stories" not in payload["data"]["items"][0]


def test_start_sprint_sets_started_at_once_and_logs_event(session, monkeypatch):  # noqa: ANN001, ANN201, D103
    client, repo, _workflow = _build_client(monkeypatch)
    project_id, sprint_id = _seed_saved_sprint(
        session,
        repo,
        started=False,
        created_title="Planned Sprint",
    )

    story = session.exec(
        select(UserStory).where(UserStory.product_id == project_id)
    ).first()
    assert story is not None

    task = Task(
        description="Prepare the sprint kickoff checklist",
        story_id=story.story_id,
        metadata_json=serialize_task_metadata(
            TaskMetadata(
                task_kind="documentation",
                artifact_targets=["kickoff checklist"],
                workstream_tags=["ops"],
                relevant_invariant_ids=[],
                checklist_items=["Confirm planning notes", "Share execution links"],
            )
        ),
    )
    session.add(task)
    session.commit()

    first_response = client.patch(
        f"/api/projects/{project_id}/sprints/{sprint_id}/start"
    )
    assert first_response.status_code == 200  # noqa: PLR2004
    first_payload = first_response.json()
    sprint = first_payload["data"]["sprint"]
    started_at = first_payload["data"]["sprint"]["started_at"]
    assert started_at is not None
    assert sprint["status"] == SprintStatus.ACTIVE.value
    assert sprint["history_fidelity"] == "derived"
    assert sprint["selected_stories"][0]["tasks"][0]["checklist_items"] == [
        "Confirm planning notes",
        "Share execution links",
    ]
    assert sprint["selected_stories"][0]["tasks"][0]["is_executable"] is True

    second_response = client.patch(
        f"/api/projects/{project_id}/sprints/{sprint_id}/start"
    )
    assert second_response.status_code == 200  # noqa: PLR2004
    second_payload = second_response.json()
    assert second_payload["data"]["sprint"]["started_at"] == started_at

    events = session.exec(
        select(WorkflowEvent).where(
            WorkflowEvent.event_type == WorkflowEventType.SPRINT_STARTED
        )
    ).all()
    assert len(events) == 1


def test_start_sprint_rejects_when_another_sprint_is_active(session, monkeypatch):  # noqa: ANN001, ANN201, D103
    client, repo, _workflow = _build_client(monkeypatch)
    project_id, sprint_id = _seed_saved_sprint(
        session,
        repo,
        started=False,
        created_title="Planned Sprint",
    )

    team = session.exec(select(Team).where(Team.name == f"Team {project_id}")).first()
    assert team is not None

    active_story = UserStory(
        product_id=project_id,
        title="Active Sprint Story",
        story_description="As a user, I want active sprint coverage",
        acceptance_criteria="- AC",
    )
    session.add(active_story)
    session.flush()

    active_sprint = Sprint(
        goal="Active Sprint Goal",
        start_date=date(2026, 4, 1),
        end_date=date(2026, 4, 15),
        status=SprintStatus.ACTIVE,
        started_at=datetime(2026, 4, 1, 9, 0, tzinfo=UTC),
        product_id=project_id,
        team_id=team.team_id,
    )
    session.add(active_sprint)
    session.flush()
    session.add(
        SprintStory(
            sprint_id=_require_id(active_sprint.sprint_id, "sprint_id"),
            story_id=_require_id(active_story.story_id, "story_id"),
        )
    )
    session.commit()

    response = client.patch(f"/api/projects/{project_id}/sprints/{sprint_id}/start")

    assert response.status_code == 409  # noqa: PLR2004
    assert (
        response.json()["detail"]
        == "Another sprint is already active for this project."
    )


def test_start_sprint_rejects_completed_sprint(session, monkeypatch):  # noqa: ANN001, ANN201, D103
    client, repo, _workflow = _build_client(monkeypatch)
    project_id, sprint_id = _seed_saved_sprint(
        session,
        repo,
        started=True,
        created_title="Completed Sprint",
    )

    sprint = session.get(Sprint, sprint_id)
    assert sprint is not None
    sprint.status = SprintStatus.COMPLETED
    sprint.completed_at = datetime(2026, 3, 15, 18, 0, tzinfo=UTC)
    session.add(sprint)
    session.commit()

    response = client.patch(f"/api/projects/{project_id}/sprints/{sprint_id}/start")

    assert response.status_code == 409  # noqa: PLR2004
    assert response.json()["detail"] == "Completed sprints cannot be restarted."


def test_get_story_packet_returns_bootstrap_context_for_pinned_story(  # noqa: ANN201, D103
    session, monkeypatch  # noqa: ANN001
):
    client, repo, _workflow = _build_client(monkeypatch)
    project_id, sprint_id, story_id, _task_id = _seed_task_packet_context(
        session,
        repo,
        pinned=True,
        task_metadata=TaskMetadata(
            task_kind="implementation",
            artifact_targets=["payload validator", "request contract tests"],
            workstream_tags=["backend", "api"],
            relevant_invariant_ids=["INV-0123456789abcdef"],
        ),
    )

    response = client.get(
        f"/api/projects/{project_id}/sprints/{sprint_id}/stories/{story_id}/packet"
    )

    assert response.status_code == 200  # noqa: PLR2004
    payload = response.json()["data"]

    assert payload["schema_version"] == "story_packet.v1"
    assert payload["metadata"]["packet_id"].startswith("sp_")
    assert payload["metadata"]["generator_version"] == "v1"

    assert payload["story"]["story_id"] == story_id
    assert payload["story"]["title"] == "Payload Validation Story"
    assert payload["story"]["persona"] == "Developer"
    assert payload["task_plan"]["tasks"] == [
        {
            "id": _task_id,
            "description": "Implement payload validation for incoming requests",
            "status": "To Do",
            "task_kind": "implementation",
            "artifact_targets": [
                "payload validator",
                "request contract tests",
            ],
            "workstream_tags": ["backend", "api"],
            "checklist_items": [],
            "is_executable": False,
        }
    ]
    assert payload["source_snapshot"]["story_id"] == story_id
    assert payload["source_snapshot"]["accepted_spec_version_id"] is not None

    assert payload["context"]["sprint"]["goal"] == "Ship a trustworthy task packet API"
    assert (
        payload["context"]["product"]["vision_excerpt"]
        == "Build trustworthy execution handoffs."
    )

    constraints = payload["constraints"]
    assert constraints["story_acceptance_criteria_text"] == (
        "- include user_id\n- reject invalid payloads"
    )
    assert constraints["story_acceptance_criteria_items"] == [
        "include user_id",
        "reject invalid payloads",
    ]
    assert constraints["spec_binding"]["binding_status"] == "pinned"
    assert constraints["spec_binding"]["authority_artifact_status"] == "available"
    assert constraints["validation"]["present"] is True
    assert constraints["validation"]["freshness_status"] == "current"
    assert constraints["validation"]["input_hash_matches"] is True
    assert "task_hard_constraints" not in constraints
    assert constraints["story_compliance_boundaries"] == [
        {
            "invariant_id": "INV-0123456789abcdef",
            "type": "REQUIRED_FIELD",
            "parameters": {"field_name": "user_id"},
            "source_excerpt": "Requests must include user_id.",
            "source_location": "Spec §1",
        }
    ]
    assert any(
        finding["source"] == "validation_warning" for finding in constraints["findings"]
    )
    assert any(
        finding["source"] == "alignment_warning" for finding in constraints["findings"]
    )


def test_get_task_packet_returns_task_local_execution_context(session, monkeypatch):  # noqa: ANN001, ANN201, D103
    client, repo, _workflow = _build_client(monkeypatch)
    project_id, sprint_id, story_id, task_id = _seed_task_packet_context(
        session,
        repo,
        pinned=True,
        task_metadata=TaskMetadata(
            task_kind="implementation",
            artifact_targets=["payload validator", "request contract tests"],
            workstream_tags=["backend", "api"],
            relevant_invariant_ids=["INV-0123456789abcdef"],
            checklist_items=["Validate user_id inputs", "Cover invalid payload cases"],
        ),
    )

    response = client.get(
        f"/api/projects/{project_id}/sprints/{sprint_id}/tasks/{task_id}/packet"
    )

    assert response.status_code == 200  # noqa: PLR2004
    payload = response.json()["data"]

    assert payload["schema_version"] == "task_packet.v2"
    assert payload["metadata"]["packet_id"].startswith("tp_")
    assert payload["metadata"]["generator_version"] == "v2"

    assert payload["task"]["task_id"] == task_id
    assert payload["task"]["status"] == "To Do"
    assert (
        payload["task"]["label"] == "Implement payload validation for incoming requests"
    )
    assert payload["task"]["task_kind"] == "implementation"
    assert payload["task"]["artifact_targets"] == [
        "payload validator",
        "request contract tests",
    ]
    assert payload["task"]["workstream_tags"] == ["backend", "api"]
    assert payload["task"]["checklist_items"] == [
        "Validate user_id inputs",
        "Cover invalid payload cases",
    ]
    assert payload["task"]["is_executable"] is True
    assert payload["source_snapshot"]["task_metadata_hash"]

    assert payload["context"]["story"]["story_id"] == story_id
    assert payload["context"]["story"]["title"] == "Payload Validation Story"
    assert payload["context"]["sprint"]["goal"] == "Ship a trustworthy task packet API"
    assert (
        payload["context"]["product"]["vision_excerpt"]
        == "Build trustworthy execution handoffs."
    )

    constraints = payload["constraints"]
    assert "acceptance_criteria_text" not in constraints
    assert "acceptance_criteria_items" not in constraints
    assert constraints["spec_binding"]["binding_status"] == "pinned"
    assert constraints["spec_binding"]["authority_artifact_status"] == "available"
    assert constraints["validation"]["present"] is True
    assert constraints["validation"]["freshness_status"] == "current"
    assert constraints["validation"]["input_hash_matches"] is True
    assert constraints["task_hard_constraints"] == [
        {
            "invariant_id": "INV-0123456789abcdef",
            "type": "REQUIRED_FIELD",
            "parameters": {"field_name": "user_id"},
            "source_excerpt": "Requests must include user_id.",
            "source_location": "Spec §1",
        }
    ]
    assert constraints["story_compliance_boundaries"] == [
        {
            "invariant_id": "INV-0123456789abcdef",
            "type": "REQUIRED_FIELD",
            "parameters": {"field_name": "user_id"},
            "source_excerpt": "Requests must include user_id.",
            "source_location": "Spec §1",
        }
    ]
    assert any(
        finding["source"] == "validation_warning" for finding in constraints["findings"]
    )
    assert any(
        finding["source"] == "alignment_warning" for finding in constraints["findings"]
    )


def test_get_task_packet_returns_cancelled_status(session, monkeypatch):  # noqa: ANN001, ANN201, D103
    client, repo, _workflow = _build_client(monkeypatch)
    project_id, sprint_id, _story_id, task_id = _seed_task_packet_context(
        session,
        repo,
        pinned=True,
        task_metadata=TaskMetadata(
            task_kind="testing",
            artifact_targets=["request contract tests"],
            workstream_tags=["backend", "qa"],
            relevant_invariant_ids=[],
            checklist_items=["Cover invalid payload cases"],
        ),
    )

    task = session.get(Task, task_id)
    assert task is not None
    task.status = TaskStatus.CANCELLED
    session.add(task)
    session.commit()

    response = client.get(
        f"/api/projects/{project_id}/sprints/{sprint_id}/tasks/{task_id}/packet"
    )

    assert response.status_code == 200  # noqa: PLR2004
    payload = response.json()["data"]
    assert payload["task"]["status"] == "Cancelled"


def test_task_packet_metadata_hash_changes_when_task_metadata_changes(  # noqa: ANN201, D103
    session, monkeypatch  # noqa: ANN001
):
    client, repo, _workflow = _build_client(monkeypatch)
    project_id, sprint_id, _story_id, task_id = _seed_task_packet_context(
        session,
        repo,
        pinned=True,
    )

    first_payload = client.get(
        f"/api/projects/{project_id}/sprints/{sprint_id}/tasks/{task_id}/packet"
    ).json()["data"]

    task = session.get(Task, task_id)
    assert task is not None
    task.metadata_json = serialize_task_metadata(
        TaskMetadata(
            task_kind="design",
            artifact_targets=["sequence diagram"],
            workstream_tags=["architecture"],
            relevant_invariant_ids=[],
        )
    )
    session.add(task)
    session.commit()

    second_payload = client.get(
        f"/api/projects/{project_id}/sprints/{sprint_id}/tasks/{task_id}/packet"
    ).json()["data"]

    assert (
        first_payload["source_snapshot"]["task_metadata_hash"]
        != second_payload["source_snapshot"]["task_metadata_hash"]
    )
    assert (
        first_payload["metadata"]["source_fingerprint"]
        != second_payload["metadata"]["source_fingerprint"]
    )


def test_build_story_task_plan_orders_identical_descriptions_by_task_id(  # noqa: ANN201, D103
    session, monkeypatch  # noqa: ANN001
):
    client, repo, _workflow = _build_client(monkeypatch)
    project_id, sprint_id, story_id, first_task_id = _seed_task_packet_context(
        session,
        repo,
        pinned=True,
    )

    second_task = Task(
        description="Implement payload validation for incoming requests",
        story_id=story_id,
        metadata_json=serialize_task_metadata(
            TaskMetadata(
                task_kind="testing",
                artifact_targets=["request contract tests"],
                workstream_tags=["backend", "qa"],
                relevant_invariant_ids=[],
                checklist_items=["Cover invalid payload cases"],
            )
        ),
    )
    session.add(second_task)
    session.commit()

    first_task = session.get(Task, first_task_id)
    assert first_task is not None
    assert second_task.task_id is not None

    reversed_story = cast(
        "UserStory",
        SimpleNamespace(tasks=[second_task, first_task]),
    )
    forward_story = cast(
        "UserStory",
        SimpleNamespace(tasks=[first_task, second_task]),
    )

    reversed_plan = api_module._build_story_task_plan(reversed_story)
    forward_plan = api_module._build_story_task_plan(forward_story)
    payload = client.get(
        f"/api/projects/{project_id}/sprints/{sprint_id}/stories/{story_id}/packet"
    ).json()["data"]

    assert [item["id"] for item in reversed_plan] == [
        first_task_id,
        second_task.task_id,
    ]
    assert reversed_plan == forward_plan
    assert api_module._hash_payload(reversed_plan) == api_module._hash_payload(
        forward_plan
    )
    assert [item["id"] for item in payload["task_plan"]["tasks"]] == [
        first_task_id,
        second_task.task_id,
    ]


def test_story_packet_fingerprint_changes_when_task_plan_changes(session, monkeypatch):  # noqa: ANN001, ANN201, D103
    client, repo, _workflow = _build_client(monkeypatch)
    project_id, sprint_id, story_id, task_id = _seed_task_packet_context(
        session,
        repo,
        pinned=True,
        task_metadata=TaskMetadata(
            task_kind="implementation",
            artifact_targets=["payload validator"],
            workstream_tags=["backend"],
            relevant_invariant_ids=[],
            checklist_items=["Validate user_id inputs"],
        ),
    )

    first_payload = client.get(
        f"/api/projects/{project_id}/sprints/{sprint_id}/stories/{story_id}/packet"
    ).json()["data"]

    task = session.get(Task, task_id)
    assert task is not None
    task.metadata_json = serialize_task_metadata(
        TaskMetadata(
            task_kind="testing",
            artifact_targets=["request contract tests"],
            workstream_tags=["backend", "qa"],
            relevant_invariant_ids=[],
            checklist_items=["Cover invalid payload cases"],
        )
    )
    session.add(task)
    session.commit()

    second_payload = client.get(
        f"/api/projects/{project_id}/sprints/{sprint_id}/stories/{story_id}/packet"
    ).json()["data"]

    assert (
        first_payload["metadata"]["source_fingerprint"]
        != second_payload["metadata"]["source_fingerprint"]
    )


def test_get_task_packet_rejects_unlinked_task_sprint_pair(session, monkeypatch):  # noqa: ANN001, ANN201, D103
    client, repo, _workflow = _build_client(monkeypatch)
    project_id, _sprint_id, story_id, task_id = _seed_task_packet_context(
        session,
        repo,
        pinned=False,
    )

    team = session.exec(
        select(Team).where(Team.name == f"Packet Team {project_id}")
    ).first()
    assert team is not None

    other_sprint = Sprint(
        goal="Unlinked sprint",
        start_date=date(2026, 5, 1),
        end_date=date(2026, 5, 14),
        status=SprintStatus.PLANNED,
        product_id=project_id,
        team_id=team.team_id,
    )
    session.add(other_sprint)
    session.commit()

    response = client.get(
        f"/api/projects/{project_id}/sprints/{other_sprint.sprint_id}/tasks/{task_id}/packet"
    )

    assert response.status_code == 404  # noqa: PLR2004
    assert response.json()["detail"] == "Task packet context not found"

    linked_story = session.get(UserStory, story_id)
    assert linked_story is not None


def test_same_task_gets_different_packet_identity_across_sprints(session, monkeypatch):  # noqa: ANN001, ANN201, D103
    client, repo, _workflow = _build_client(monkeypatch)
    project_id, sprint_id, story_id, task_id = _seed_task_packet_context(
        session,
        repo,
        pinned=False,
    )

    team = session.exec(
        select(Team).where(Team.name == f"Packet Team {project_id}")
    ).first()
    assert team is not None

    second_sprint = Sprint(
        goal="Carry the task into another sprint",
        start_date=date(2026, 5, 1),
        end_date=date(2026, 5, 14),
        status=SprintStatus.PLANNED,
        product_id=project_id,
        team_id=team.team_id,
    )
    session.add(second_sprint)
    session.flush()
    session.add(
        SprintStory(
            sprint_id=_require_id(second_sprint.sprint_id, "sprint_id"),
            story_id=story_id,
        )
    )
    session.commit()

    first_payload = client.get(
        f"/api/projects/{project_id}/sprints/{sprint_id}/tasks/{task_id}/packet"
    ).json()["data"]
    second_payload = client.get(
        f"/api/projects/{project_id}/sprints/{second_sprint.sprint_id}/tasks/{task_id}/packet"
    ).json()["data"]

    assert (
        first_payload["metadata"]["packet_id"]
        != second_payload["metadata"]["packet_id"]
    )
    assert (
        first_payload["metadata"]["source_fingerprint"]
        != second_payload["metadata"]["source_fingerprint"]
    )
    assert (
        first_payload["context"]["sprint"]["goal"]
        != second_payload["context"]["sprint"]["goal"]
    )


def test_unpinned_story_packet_has_no_authority_fallback(session, monkeypatch):  # noqa: ANN001, ANN201, D103
    client, repo, _workflow = _build_client(monkeypatch)
    project_id, sprint_id, _story_id, task_id = _seed_task_packet_context(
        session,
        repo,
        pinned=False,
    )

    response = client.get(
        f"/api/projects/{project_id}/sprints/{sprint_id}/tasks/{task_id}/packet"
    )

    assert response.status_code == 200  # noqa: PLR2004
    constraints = response.json()["data"]["constraints"]
    assert constraints["spec_binding"]["binding_status"] == "unpinned"
    assert constraints["spec_binding"]["spec_version_id"] is None
    assert constraints["spec_binding"]["authority_artifact_status"] == "missing"
    assert constraints["task_hard_constraints"] == []
    assert constraints["story_compliance_boundaries"] == []
    assert constraints["validation"]["freshness_status"] == "missing"


def test_task_packet_marks_validation_as_stale_when_story_content_changes(  # noqa: ANN201, D103
    session, monkeypatch  # noqa: ANN001
):
    client, repo, _workflow = _build_client(monkeypatch)
    project_id, sprint_id, story_id, task_id = _seed_task_packet_context(
        session,
        repo,
        pinned=True,
    )

    story = session.get(UserStory, story_id)
    assert story is not None
    story.acceptance_criteria = (
        "- include user_id\n- reject invalid payloads\n- log failures"
    )
    story.ac_updated_at = datetime.now(UTC)
    session.add(story)
    session.commit()

    response = client.get(
        f"/api/projects/{project_id}/sprints/{sprint_id}/tasks/{task_id}/packet"
    )

    assert response.status_code == 200  # noqa: PLR2004
    payload = response.json()["data"]
    assert payload["constraints"]["validation"]["freshness_status"] == "stale"
    assert payload["source_snapshot"]["story_ac_updated_at"] is not None


def test_packet_renderer_escapes_html_and_xml_safely(session, monkeypatch):  # noqa: ANN001, ANN201, D103
    client, repo, _workflow = _build_client(monkeypatch)
    project_id, sprint_id, story_id, task_id = _seed_task_packet_context(  # noqa: RUF059
        session,
        repo,
        pinned=True,
        task_metadata=TaskMetadata(
            task_kind="implementation",
            artifact_targets=["payload validator"],
            workstream_tags=["backend"],
            relevant_invariant_ids=[],
            checklist_items=["Confirm request shape", "Cover invalid payload cases"],
        ),
    )

    task = session.get(Task, task_id)
    assert task is not None
    task.description = '<script>alert("XSS")</script> and </task> breaking out **bold**'
    session.add(task)
    session.commit()

    # Test human flavor preventing unescaped HTML injection and Markdown restructuring
    res_human = client.get(
        f"/api/projects/{project_id}/sprints/{sprint_id}/tasks/{task_id}/packet?flavor=human"
    )
    assert res_human.status_code == 200  # noqa: PLR2004
    human_text = res_human.json()["data"]["render"]
    assert '&lt;script&gt;alert("XSS")&lt;/script&gt;' in human_text
    assert "<script>" not in human_text
    assert "&#42;&#42;bold&#42;&#42;" in human_text
    assert "**Task Kind**: implementation" in human_text

    # Test agent flavor preventing unescaped XML closure injection
    res_agent = client.get(
        f"/api/projects/{project_id}/sprints/{sprint_id}/tasks/{task_id}/packet?flavor=cursor"
    )
    assert res_agent.status_code == 200  # noqa: PLR2004
    agent_text = res_agent.json()["data"]["render"]
    assert "&lt;/task&gt;" in agent_text
    assert "</task> breaking out" not in agent_text
    assert "<task_kind>implementation</task_kind>" in agent_text

    # Execution contract blocks are present in the agent prompt
    assert "<execution_protocol>" in agent_text
    assert "</execution_protocol>" in agent_text
    assert "<completion_report>" in agent_text
    assert "</completion_report>" in agent_text

    # Task checklist items should drive the task prompt, not story acceptance criteria.
    assert "Task Checklist" in agent_text
    assert "Verify every task checklist item before claiming completion." in agent_text
    assert TASK_PACKET_PARENT_PROMPT_NOTE in agent_text
    assert "- [ ] Confirm request shape" in agent_text
    assert "- [ ] Cover invalid payload cases" in agent_text
    assert "Acceptance Criteria Checklist" not in agent_text
    assert "Story Acceptance Criteria" not in agent_text


def test_story_packet_flavor_render_includes_story_acceptance_criteria(  # noqa: ANN201, D103
    session, monkeypatch  # noqa: ANN001
):
    client, repo, _workflow = _build_client(monkeypatch)
    project_id, sprint_id, story_id, _task_id = _seed_task_packet_context(
        session,
        repo,
        pinned=True,
        task_metadata=TaskMetadata(
            task_kind="implementation",
            artifact_targets=["payload validator"],
            workstream_tags=["backend"],
            relevant_invariant_ids=[],
            checklist_items=["Confirm request shape"],
        ),
    )

    response = client.get(
        f"/api/projects/{project_id}/sprints/{sprint_id}/stories/{story_id}/packet?flavor=cursor"
    )

    assert response.status_code == 200  # noqa: PLR2004
    payload = response.json()["data"]
    assert payload["schema_version"] == "story_packet.v1"
    assert "render" in payload
    assert "Story Acceptance Criteria" in payload["render"]
    assert "- [ ] include user_id" in payload["render"]
    assert "- [ ] reject invalid payloads" in payload["render"]
    assert "Task Checklist" not in payload["render"]


def test_story_packet_human_flavor_renders_top_level_story_fields(session, monkeypatch):  # noqa: ANN001, ANN201, D103
    client, repo, _workflow = _build_client(monkeypatch)
    project_id, sprint_id, story_id, _task_id = _seed_task_packet_context(
        session,
        repo,
        pinned=True,
        task_metadata=TaskMetadata(
            task_kind="implementation",
            artifact_targets=["payload validator"],
            workstream_tags=["backend"],
            relevant_invariant_ids=[],
            checklist_items=["Confirm request shape"],
        ),
    )

    response = client.get(
        f"/api/projects/{project_id}/sprints/{sprint_id}/stories/{story_id}/packet?flavor=human"
    )

    assert response.status_code == 200  # noqa: PLR2004
    payload = response.json()["data"]
    assert payload["schema_version"] == "story_packet.v1"
    assert "render" in payload
    assert "# Story: Payload Validation Story" in payload["render"]
    assert (
        "As a developer, I want payload validation so that requests are safe."
        in payload["render"]
    )
    assert "## Story Acceptance Criteria" in payload["render"]
    assert "## Task Plan Reference" in payload["render"]
    assert "## Task Checklist" not in payload["render"]


def test_task_packet_ignores_unknown_task_invariant_ids(session, monkeypatch):  # noqa: ANN001, ANN201, D103
    client, repo, _workflow = _build_client(monkeypatch)
    project_id, sprint_id, _story_id, task_id = _seed_task_packet_context(
        session,
        repo,
        pinned=True,
        task_metadata=TaskMetadata(
            task_kind="implementation",
            artifact_targets=["payload validator"],
            workstream_tags=["backend"],
            relevant_invariant_ids=["INV-UNKNOWN", "INV-0123456789abcdef"],
        ),
    )

    response = client.get(
        f"/api/projects/{project_id}/sprints/{sprint_id}/tasks/{task_id}/packet"
    )

    assert response.status_code == 200  # noqa: PLR2004
    assert response.json()["data"]["constraints"]["task_hard_constraints"] == [
        {
            "invariant_id": "INV-0123456789abcdef",
            "type": "REQUIRED_FIELD",
            "parameters": {"field_name": "user_id"},
            "source_excerpt": "Requests must include user_id.",
            "source_location": "Spec §1",
        }
    ]


def test_get_sprint_detail_returns_task_objects(session, monkeypatch):  # noqa: ANN001, ANN201, D103
    client, repo, _workflow = _build_client(monkeypatch)
    project_id, sprint_id, story_id, task_id = _seed_task_packet_context(
        session,
        repo,
        pinned=True,
        task_metadata=TaskMetadata(
            task_kind="design",
            artifact_targets=["mock component"],
            workstream_tags=["frontend", "ui"],
            relevant_invariant_ids=[],
            checklist_items=[
                "Sketch the component states",
                "Review the accessibility copy",
            ],
        ),
    )

    second_task = Task(
        description="Document payload validation contract",
        story_id=story_id,
        metadata_json=serialize_task_metadata(canonical_task_metadata()),
    )
    session.add(second_task)
    session.commit()

    response = client.get(f"/api/projects/{project_id}/sprints/{sprint_id}")
    assert response.status_code == 200  # noqa: PLR2004

    data = response.json()["data"]
    sprint = data["sprint"]

    assert sprint["id"] == sprint_id
    assert sprint["history_fidelity"] == "derived"
    assert data["runtime_summary"]["planned_sprint_id"] == sprint_id
    assert len(sprint["selected_stories"]) > 0
    story = sprint["selected_stories"][0]

    assert len(story["tasks"]) > 0
    tasks_by_description = {task["description"]: task for task in story["tasks"]}

    executable_task = tasks_by_description[
        "Implement payload validation for incoming requests"
    ]
    non_executable_task = tasks_by_description["Document payload validation contract"]

    assert isinstance(executable_task, dict)
    assert executable_task["id"] == task_id
    assert executable_task["task_kind"] == "design"
    assert executable_task["artifact_targets"] == ["mock component"]
    assert executable_task["workstream_tags"] == ["frontend", "ui"]
    assert executable_task["checklist_items"] == [
        "Sketch the component states",
        "Review the accessibility copy",
    ]
    assert executable_task["is_executable"] is True

    assert isinstance(non_executable_task, dict)
    assert non_executable_task["checklist_items"] == []
    assert non_executable_task["is_executable"] is False
