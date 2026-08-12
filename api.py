"""FastAPI transport backed exclusively by the durable workflow graph."""

from __future__ import annotations

import os
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Literal, Self, TypedDict, cast

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from git import Git
from git.exc import GitCommandError
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from repositories.project import ProjectRepository
from services.agent_workbench.version import agileforge_version
from services.application import (
    AgileForgeApplication,
    AuthorityCompileRequest,
    AuthorityFeedbackRequest,
    AuthorityRepairRequest,
    AuthorityReviewRequest,
    BacklogReviewRequest,
    CloseStoryRequest,
    CompleteTaskRequest,
    CreateProjectCommand,
    DeliveryActionRequest,
    PostSprintTriageRequest,
    ProductGoalOutcomeRequest,
    ProductGoalResponseRequest,
    ProductGoalReviewRequest,
    RepositoryAttachRequest,
    RepositoryRefreshRequest,
    RoadmapReviewRequest,
    SpecificationAuthoringRequest,
    SpecificationReviewRequest,
    SprintCloseRequest,
    SprintPlanningRequest,
    SprintPlanReviewRequest,
    SprintReviewRequest,
    SprintStartRequest,
    StoryDependenciesApplyRequest,
    StoryDependencyEdgeRequest,
    StoryReadinessRepair,
    StoryReadinessRepairRequest,
    StoryReviewRequest,
    VisionBootstrapRequest,
    VisionResponseRequest,
    VisionReviewRequest,
    VisionRevisionRequest,
    execution_action_decision_is_transportable,
    planning_action_decision_is_transportable,
    production_application,
)
from utils.runtime_controls import UI_LAUNCH_NONCE_ENV
from workflow.contracts import (
    JsonObject,
    NodeCategory,
    RecommendationKind,
    TransitionResult,
    WorkflowErrorCode,
    WorkflowPosition,
)

_FRONTEND_ROOT = files("frontend")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Initialize the business schema without creating routing sessions."""
    from models.db import ensure_business_db_ready  # noqa: PLC0415

    ensure_business_db_ready()
    yield


app = FastAPI(title="AgileForge API", lifespan=lifespan)
app.mount(
    "/dashboard",
    StaticFiles(directory=str(_FRONTEND_ROOT), html=True),
    name="frontend",
)

type SemanticText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]
type PositiveStoryId = Annotated[int, Field(strict=True, gt=0)]


class CreateProjectRequest(BaseModel):
    """Exact semantic request body for creating a Project."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    repository_path: str | None = None
    idempotency_key: str = Field(min_length=1, max_length=200)
    actor: str = Field(min_length=1, max_length=200)


class MutationApiRequest(BaseModel):
    """Strict transport metadata shared by semantic mutations."""

    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=1, max_length=200)
    actor: str = Field(min_length=1, max_length=200)
    correlation_id: str | None = Field(default=None, min_length=1)


class MutationMetadata(TypedDict):
    """Validated transport metadata forwarded to application requests."""

    idempotency_key: str
    actor: str
    correlation_id: str | None


class TextResponseApiRequest(MutationApiRequest):
    """One semantic interview response."""

    text: SemanticText


class ReviewApiRequest(MutationApiRequest):
    """One semantic review choice."""

    decision: Literal["accepted", "rejected", "feedback"]
    rationale: SemanticText


class BacklogReviewApiRequest(ReviewApiRequest):
    """Semantic Backlog review without caller-owned artifact identity."""


class RoadmapReviewApiRequest(ReviewApiRequest):
    """Semantic Roadmap review without caller-owned artifact identity."""


class StoryReviewApiRequest(ReviewApiRequest):
    """Semantic Story review with one exact repeated instance selector."""

    instance_key: SemanticText


class SprintPlanReviewApiRequest(ReviewApiRequest):
    """Semantic Sprint-plan review without caller-owned artifact identity."""


class RevisionApiRequest(MutationApiRequest):
    """One semantic Vision revision reason."""

    reason: SemanticText


class GoalOutcomeApiRequest(MutationApiRequest):
    """One semantic Product Goal outcome rationale."""

    rationale: SemanticText


class RepositoryAttachApiRequest(MutationApiRequest):
    """Repository path plus transport metadata only."""

    path: str = Field(min_length=1)


class AuthorityDecisionApiRequest(MutationApiRequest):
    """Semantic authority review choice without derived identities."""

    decision: Literal["accepted", "rejected"]
    rationale: SemanticText


class AuthorityFeedbackApiRequest(MutationApiRequest):
    """Semantic human feedback without caller-owned authority identity."""

    feedback: SemanticText


class DeliveryActionApiRequest(MutationApiRequest):
    """Transport metadata and optional semantic decision selector only."""

    instance_key: str | None = None


class StoryDeliveryActionApiRequest(DeliveryActionApiRequest):
    """Story delivery request with one exact caller-owned selector."""

    instance_key: SemanticText


class SprintPlanningApiRequest(MutationApiRequest):
    """Strict operator-owned Sprint planning semantics."""

    user_input: str | None = None
    selected_story_ids: list[int] = Field(default_factory=list)
    max_story_points: int | None = Field(default=None, gt=0)
    include_task_decomposition: bool = True
    team_name: str = Field(min_length=1)

    @field_validator("selected_story_ids")
    @classmethod
    def validate_selected_story_ids(cls, value: list[int]) -> list[int]:
        """Reject invalid manual Story identities at the HTTP boundary."""
        if any(story_id <= 0 for story_id in value):
            message = "selected_story_ids must contain positive Story IDs."
            raise ValueError(message)
        if len(set(value)) != len(value):
            message = "selected_story_ids must not contain duplicates."
            raise ValueError(message)
        return value


class StoryDependenciesApplyApiRequest(MutationApiRequest):
    """Strict operator-reviewed Story dependency semantics."""

    selected_story_ids: list[PositiveStoryId] = Field(min_length=1)
    reviewed_edges: list[StoryDependencyEdgeRequest]

    @field_validator("selected_story_ids")
    @classmethod
    def validate_selected_story_ids(cls, value: list[int]) -> list[int]:
        """Reject duplicate Story selections at the HTTP boundary."""
        if len(set(value)) != len(value):
            message = "selected_story_ids must not contain duplicates."
            raise ValueError(message)
        return value

    @model_validator(mode="after")
    def validate_reviewed_edges(self) -> Self:
        """Reject duplicate and out-of-selection dependency edges."""
        pairs = [
            (item.dependent_story_id, item.prerequisite_story_id)
            for item in self.reviewed_edges
        ]
        if len(set(pairs)) != len(pairs):
            message = "reviewed_edges must not contain duplicate Story pairs."
            raise ValueError(message)
        selected = set(self.selected_story_ids)
        if any(left not in selected or right not in selected for left, right in pairs):
            message = "reviewed_edges must remain inside selected_story_ids."
            raise ValueError(message)
        return self


class StoryReadinessRepairApiRequest(MutationApiRequest):
    """Strict explicit Story readiness repairs without derived guards."""

    repairs: list[StoryReadinessRepair] = Field(min_length=1)

    @field_validator("repairs")
    @classmethod
    def validate_unique_repairs(
        cls,
        value: list[StoryReadinessRepair],
    ) -> list[StoryReadinessRepair]:
        """Reject multiple repairs for the same Story."""
        story_ids = [item.story_id for item in value]
        if len(set(story_ids)) != len(story_ids):
            message = "repairs must not contain duplicate Story IDs."
            raise ValueError(message)
        return value


class SprintStartApiRequest(MutationApiRequest):
    """Transport metadata only for the accepted current Sprint plan."""


class CompleteTaskApiRequest(MutationApiRequest):
    """Strict semantic completion evidence for one selected Task."""

    instance_key: SemanticText
    outcome_summary: SemanticText
    artifact_refs: list[SemanticText] = Field(min_length=1)
    acceptance_result: Literal["partially_met", "fully_met"]
    checklist_result: dict[SemanticText, SemanticText] = Field(min_length=1)


class CloseStoryApiRequest(MutationApiRequest):
    """Strict semantic closure evidence for one selected Story."""

    instance_key: SemanticText
    resolution: SemanticText
    delivered: SemanticText
    evidence: SemanticText
    known_gaps: SemanticText


class SprintReviewApiRequest(MutationApiRequest):
    """Transport metadata only for the graph-selected terminal Sprint review."""

    instance_key: SemanticText


class SprintCloseApiRequest(MutationApiRequest):
    """Transport metadata only for the graph-selected reviewed Sprint close."""

    instance_key: SemanticText


class PostSprintTriageApiRequest(MutationApiRequest):
    """Strict semantic post-Sprint impact and canonical payload."""

    instance_key: SemanticText
    impact: Literal["none", "backlog", "specification"]
    canonical_payload: JsonObject


class DashboardConfig(BaseModel):
    """Non-secret provenance returned by the local readiness endpoint."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["ready"] = "ready"
    process_id: int
    checkout_root: Path
    commit: str
    business_database: Path
    trace_database: Path
    launch_nonce: str | None = None


DELIVERY_API_PATHS: dict[str, str] = {
    "record_backlog_draft": "backlog/generate",
    "record_roadmap_draft": "roadmap/generate",
    "record_story_draft": "story/generate",
}

SEMANTIC_API_PATHS: dict[str, str] = {
    "abandon_product_goal": "goals/abandon",
    "apply_story_dependencies": "story/dependencies/apply",
    "begin_vision_revision": "vision/revision",
    "close_sprint": "sprint/close",
    "close_story": "story/close",
    "compile_authority": "authority/compile",
    "complete_task": "sprint/task/complete",
    "decide_authority": "authority/decision",
    "decide_backlog": "backlog/decide",
    "decide_roadmap": "roadmap/decide",
    "decide_sprint_plan": "sprint/decide",
    "decide_story": "story/decide",
    "record_authority_feedback": "authority/feedback",
    "record_post_sprint_triage": "sprint/triage",
    "decide_product_goal_review": "goals/review",
    "decide_specification": "specifications/review",
    "decide_vision_review": "vision/review",
    "fulfill_product_goal": "goals/complete",
    "generate_vision_bootstrap": "vision/bootstrap",
    "record_product_goal_interview_turn": "goals/respond",
    "author_specification": "specifications/author",
    "record_sprint_plan": "sprint/generate",
    "record_vision_interview_turn": "vision/respond",
    "repair_authority": "authority/repair",
    "repair_story_readiness": "story/readiness/repair",
    "review_sprint": "sprint/review",
    "start_sprint": "sprint/start",
}

_ACTIONABLE_WAITING_REQUEST_KINDS = frozenset(
    {
        "decide_authority",
        "decide_backlog",
        "decide_product_goal_review",
        "decide_roadmap",
        "decide_sprint_plan",
        "decide_specification",
        "decide_story",
        "decide_vision_review",
        "review_sprint",
    }
)
_SELECTOR_API_REQUEST_KINDS = frozenset(DELIVERY_API_PATHS) | {
    "close_sprint",
    "complete_task",
    "close_story",
    "decide_story",
    "record_post_sprint_triage",
    "review_sprint",
}


def _application() -> AgileForgeApplication:
    return production_application()


def build_create_project_command(req: CreateProjectRequest) -> CreateProjectCommand:
    """Translate exact API business input into the Project lifecycle command."""
    return CreateProjectCommand(
        name=req.name,
        description=req.description,
        repository_path=req.repository_path,
        idempotency_key=req.idempotency_key,
        actor=req.actor,
    )


def build_authority_decision_request(
    project_id: int,
    req: AuthorityDecisionApiRequest,
    expected_candidate_fingerprint: str | None = None,
) -> AuthorityReviewRequest:
    """Translate one API choice plus an optional hidden browser expectation."""
    return AuthorityReviewRequest(
        project_id=project_id,
        idempotency_key=req.idempotency_key,
        actor=req.actor,
        correlation_id=req.correlation_id,
        decision=req.decision,
        rationale=req.rationale,
        expected_candidate_fingerprint=expected_candidate_fingerprint,
    )


def build_authority_feedback_request(
    project_id: int,
    req: AuthorityFeedbackApiRequest,
) -> AuthorityFeedbackRequest:
    """Translate feedback text without accepting durable authority identity."""
    return AuthorityFeedbackRequest(
        project_id=project_id,
        feedback=req.feedback,
        idempotency_key=req.idempotency_key,
        actor=req.actor,
        correlation_id=req.correlation_id,
    )


def _result_payload(result: TransitionResult) -> dict[str, object]:
    if not isinstance(result, TransitionResult):
        raise TypeError(type(result).__name__)
    if not result.ok:
        if result.error is None:
            status = 400
        elif result.error.code is WorkflowErrorCode.PROJECT_NOT_FOUND:
            status = 404
        else:
            status = 409
        detail = result.model_dump(mode="json")
        if result.position is not None:
            detail["actions"] = _workflow_actions(result.position)
        raise HTTPException(
            status_code=status,
            detail=detail,
        )
    return {"status": "success", "data": result.model_dump(mode="json")}


def _workflow_actions(position: WorkflowPosition) -> list[JsonObject]:
    """Advertise only decisions that their fixed API route can select exactly."""
    candidates = tuple(
        decision
        for decision in position.decisions
        if (
            decision.category is NodeCategory.AVAILABLE
            or (
                decision.category is NodeCategory.WAITING
                and decision.request_kind in _ACTIONABLE_WAITING_REQUEST_KINDS
            )
        )
        and decision.recommendation_kind
        in {RecommendationKind.REQUIRED, RecommendationKind.RECOVERY}
        and decision.request_kind in SEMANTIC_API_PATHS | DELIVERY_API_PATHS
        and planning_action_decision_is_transportable(position.project_id, decision)
        and execution_action_decision_is_transportable(decision)
    )
    semantic_counts = Counter(
        decision.request_kind
        for decision in candidates
        if decision.request_kind not in _SELECTOR_API_REQUEST_KINDS
    )
    selector_counts = Counter(
        (decision.request_kind, decision.instance_key)
        for decision in candidates
        if decision.request_kind in _SELECTOR_API_REQUEST_KINDS
    )
    actions: list[JsonObject] = []
    for decision in candidates:
        request_kind = decision.request_kind
        if request_kind in _SELECTOR_API_REQUEST_KINDS:
            selectable = (
                request_kind != "decide_story" or decision.instance_key is not None
            ) and selector_counts[(request_kind, decision.instance_key)] == 1
        else:
            selectable = semantic_counts[request_kind] == 1
        if not selectable:
            continue
        if request_kind in SEMANTIC_API_PATHS:
            endpoint = SEMANTIC_API_PATHS[request_kind]
            transport = "semantic"
        elif request_kind in DELIVERY_API_PATHS:
            endpoint = DELIVERY_API_PATHS[request_kind]
            transport = "semantic"
        else:
            continue
        actions.append(
            {
                "node_id": decision.node_id,
                "instance_key": decision.instance_key,
                "request_kind": request_kind,
                "endpoint": endpoint,
                "transport": transport,
            }
        )
    return actions


def _read_payload(result: JsonObject) -> dict[str, object]:
    """Translate one typed read projection envelope to HTTP."""
    if result.get("ok") is not True:
        errors = result.get("errors")
        first = errors[0] if isinstance(errors, list) and errors else None
        code = first.get("code") if isinstance(first, dict) else None
        status = 404 if isinstance(code, str) and code.endswith("NOT_FOUND") else 409
        raise HTTPException(status_code=status, detail=result)
    return {
        "status": "success",
        "data": result.get("data", {}),
        "warnings": result.get("warnings", []),
    }


def _metadata(request: MutationApiRequest) -> MutationMetadata:
    return {
        "idempotency_key": request.idempotency_key,
        "actor": request.actor,
        "correlation_id": request.correlation_id,
    }


def _database_path(environment_name: str) -> Path:
    value = os.environ.get(environment_name, "")
    prefix = "sqlite:///"
    if not value.startswith(prefix):
        message = f"{environment_name} must contain an absolute SQLite URL"
        raise RuntimeError(message)
    path = Path(value.removeprefix(prefix))
    if not path.is_absolute():
        message = f"{environment_name} must contain an absolute SQLite URL"
        raise RuntimeError(message)
    return path


def _checkout_commit(checkout_root: Path) -> str:
    output = Git().execute(
        command=["git", "-C", str(checkout_root), "rev-parse", "HEAD"]
    )
    return cast("str", output).strip()


def _runtime_provenance(checkout_root: Path) -> str:
    try:
        top_level = Git().execute(
            command=["git", "-C", str(checkout_root), "rev-parse", "--show-toplevel"]
        )
    except GitCommandError:
        top_level = None
    if (
        top_level is not None
        and Path(cast("str", top_level)).resolve() == checkout_root
    ):
        return _checkout_commit(checkout_root)
    return f"installed:agileforge@{agileforge_version()}"


@app.get("/")
def root() -> RedirectResponse:
    """Redirect to the workflow dashboard."""
    return RedirectResponse(url="/dashboard")


@app.get("/api/dashboard/config")
def get_dashboard_config() -> DashboardConfig:
    """Return deterministic local readiness and checkout provenance."""
    checkout_root = Path(__file__).resolve().parent
    return DashboardConfig(
        process_id=os.getpid(),
        checkout_root=checkout_root,
        commit=_runtime_provenance(checkout_root),
        business_database=_database_path("AGILEFORGE_DB_URL"),
        trace_database=_database_path("AGILEFORGE_ADK_EXECUTION_TRACE_DB_URL"),
        launch_nonce=os.environ.get(UI_LAUNCH_NONCE_ENV) or None,
    )


@app.post("/api/projects")
def create_project(req: CreateProjectRequest) -> dict[str, object]:
    """Create one Project from semantic business input only."""
    return _result_payload(
        _application().create_project(build_create_project_command(req))
    )


@app.get("/api/projects")
def get_projects() -> dict[str, object]:
    """Return Project identity without a routing projection."""
    return _read_payload(_application().reads.project_list())


@app.get("/api/projects/{project_id}")
def get_project(project_id: int) -> dict[str, object]:
    """Return one non-routing Project detail projection."""
    return _read_payload(_application().reads.project_show(project_id=project_id))


@app.delete("/api/projects/{project_id}")
def delete_project(project_id: int) -> dict[str, str]:
    """Delete one Project and its current durable records transactionally."""
    if not ProjectRepository().delete_project(project_id):
        raise HTTPException(status_code=404, detail="Project not found.")
    return {"status": "success"}


@app.get("/api/projects/{project_id}/position")
def get_project_position(project_id: int) -> dict[str, object]:
    """Return the only workflow routing projection."""
    position = _application().position(project_id=project_id)
    return {
        "status": "success",
        "data": position.model_dump(mode="json"),
        "actions": _workflow_actions(position),
    }


@app.post("/api/projects/{project_id}/vision/bootstrap")
def bootstrap_project_vision(
    project_id: int,
    req: MutationApiRequest,
) -> dict[str, object]:
    """Generate one replay-safe Project Vision draft from host evidence."""
    return _result_payload(
        _application().bootstrap_vision(
            VisionBootstrapRequest(
                project_id=project_id,
                **_metadata(req),
            )
        )
    )


@app.post("/api/projects/{project_id}/vision/respond")
def respond_to_project_vision(
    project_id: int,
    req: TextResponseApiRequest,
) -> dict[str, object]:
    """Record one semantic Project Vision interview response."""
    return _result_payload(
        _application().respond_to_vision(
            VisionResponseRequest(
                project_id=project_id,
                text=req.text,
                **_metadata(req),
            )
        )
    )


@app.get("/api/projects/{project_id}/vision/status")
def get_vision_status(project_id: int) -> dict[str, object]:
    """Return the current durable Project Vision projection."""
    return _read_payload(_application().reads.vision_status(project_id=project_id))


@app.post("/api/projects/{project_id}/vision/review")
def review_project_vision(
    project_id: int,
    req: ReviewApiRequest,
    expected_candidate_fingerprint: Annotated[
        str | None,
        Header(alias="X-AgileForge-Expected-Candidate", include_in_schema=False),
    ] = None,
) -> dict[str, object]:
    """Record one semantic Project Vision review decision."""
    return _result_payload(
        _application().review_vision(
            VisionReviewRequest(
                project_id=project_id,
                decision=req.decision,
                rationale=req.rationale,
                expected_candidate_fingerprint=expected_candidate_fingerprint,
                **_metadata(req),
            )
        )
    )


@app.post("/api/projects/{project_id}/vision/revision")
def revise_project_vision(
    project_id: int,
    req: RevisionApiRequest,
) -> dict[str, object]:
    """Begin one eligible semantic Project Vision revision."""
    return _result_payload(
        _application().begin_vision_revision(
            VisionRevisionRequest(
                project_id=project_id,
                reason=req.reason,
                **_metadata(req),
            )
        )
    )


@app.post("/api/projects/{project_id}/goals/respond")
def respond_to_product_goal(
    project_id: int,
    req: TextResponseApiRequest,
) -> dict[str, object]:
    """Record one semantic Product Goal interview response."""
    return _result_payload(
        _application().respond_to_product_goal(
            ProductGoalResponseRequest(
                project_id=project_id,
                text=req.text,
                **_metadata(req),
            )
        )
    )


@app.get("/api/projects/{project_id}/goals/status")
def get_product_goal_status(project_id: int) -> dict[str, object]:
    """Return the current durable Product Goal projection."""
    return _read_payload(
        _application().reads.product_goal_status(project_id=project_id)
    )


@app.post("/api/projects/{project_id}/goals/review")
def review_product_goal(
    project_id: int,
    req: ReviewApiRequest,
    expected_candidate_fingerprint: Annotated[
        str | None,
        Header(alias="X-AgileForge-Expected-Candidate", include_in_schema=False),
    ] = None,
) -> dict[str, object]:
    """Record one semantic Product Goal review decision."""
    return _result_payload(
        _application().review_product_goal(
            ProductGoalReviewRequest(
                project_id=project_id,
                decision=req.decision,
                rationale=req.rationale,
                expected_candidate_fingerprint=expected_candidate_fingerprint,
                **_metadata(req),
            )
        )
    )


def _product_goal_outcome(
    project_id: int,
    req: GoalOutcomeApiRequest,
    outcome: Literal["fulfilled", "abandoned"],
) -> dict[str, object]:
    return _result_payload(
        _application().resolve_product_goal(
            ProductGoalOutcomeRequest(
                project_id=project_id,
                outcome=outcome,
                rationale=req.rationale,
                **_metadata(req),
            )
        )
    )


@app.post("/api/projects/{project_id}/goals/complete")
def complete_product_goal(
    project_id: int,
    req: GoalOutcomeApiRequest,
) -> dict[str, object]:
    """Record fulfillment of the current Product Goal."""
    return _product_goal_outcome(project_id, req, "fulfilled")


@app.post("/api/projects/{project_id}/goals/abandon")
def abandon_product_goal(
    project_id: int,
    req: GoalOutcomeApiRequest,
) -> dict[str, object]:
    """Record abandonment of the current Product Goal."""
    return _product_goal_outcome(project_id, req, "abandoned")


@app.post("/api/projects/{project_id}/specifications/author")
def author_specification(
    project_id: int,
    req: MutationApiRequest,
) -> dict[str, object]:
    """Run one exact Specification-authoring action from host context."""
    return _result_payload(
        _application().author_specification(
            SpecificationAuthoringRequest(
                project_id=project_id,
                **_metadata(req),
            )
        )
    )


@app.get("/api/projects/{project_id}/specifications/review")
def get_specification_review(project_id: int) -> dict[str, object]:
    """Return current durable specification review content."""
    return _read_payload(
        _application().reads.specification_review(project_id=project_id)
    )


@app.post("/api/projects/{project_id}/specifications/review")
def review_specification(
    project_id: int,
    req: ReviewApiRequest,
    expected_candidate_fingerprint: Annotated[
        str | None,
        Header(alias="X-AgileForge-Expected-Candidate", include_in_schema=False),
    ] = None,
) -> dict[str, object]:
    """Record one semantic specification review decision."""
    return _result_payload(
        _application().review_specification(
            SpecificationReviewRequest(
                project_id=project_id,
                decision=req.decision,
                rationale=req.rationale,
                expected_candidate_fingerprint=expected_candidate_fingerprint,
                **_metadata(req),
            )
        )
    )


@app.post("/api/projects/{project_id}/repository")
def attach_repository(
    project_id: int,
    req: RepositoryAttachApiRequest,
) -> dict[str, object]:
    """Attach a repository path with server-derived active binding guard."""
    return _result_payload(
        _application().attach_repository(
            RepositoryAttachRequest(
                project_id=project_id,
                path=req.path,
                **_metadata(req),
            )
        )
    )


@app.get("/api/projects/{project_id}/repository")
def get_repository(project_id: int) -> dict[str, object]:
    """Return current immutable repository provenance."""
    return _read_payload(_application().reads.repository_status(project_id=project_id))


@app.post("/api/projects/{project_id}/repository/refresh")
def refresh_repository(
    project_id: int,
    req: MutationApiRequest,
) -> dict[str, object]:
    """Refresh repository provenance with a server-derived binding guard."""
    return _result_payload(
        _application().refresh_repository(
            RepositoryRefreshRequest(project_id=project_id, **_metadata(req))
        )
    )


@app.get("/api/projects/{project_id}/authority/status")
def get_authority_status(project_id: int) -> dict[str, object]:
    """Return durable authority status without routing state."""
    return _read_payload(_application().reads.authority_status(project_id=project_id))


@app.get("/api/projects/{project_id}/authority/invariants")
def get_authority_invariants(
    project_id: int,
    spec_version_id: int | None = None,
) -> dict[str, object]:
    """Return durable compiled authority invariants."""
    return _read_payload(
        _application().reads.authority_invariants(
            project_id=project_id,
            spec_version_id=spec_version_id,
        )
    )


@app.get("/api/projects/{project_id}/authority/review")
def get_authority_review(
    project_id: int,
    include_spec: str = "auto",
) -> dict[str, object]:
    """Return the pending authority review packet."""
    return _read_payload(
        _application().reads.authority_review(
            project_id=project_id,
            include_spec=include_spec,
        )
    )


def _artifact_history(
    *,
    project_id: int,
    node_id: str,
    instance_key: str | None = None,
) -> dict[str, object]:
    return _read_payload(
        _application().reads.artifact_history(
            project_id=project_id,
            node_id=node_id,
            instance_key=instance_key,
        )
    )


@app.get("/api/projects/{project_id}/vision/history")
def get_vision_history(project_id: int) -> dict[str, object]:
    """Return durable Vision attempt history."""
    return _artifact_history(project_id=project_id, node_id="vision.interview")


@app.get("/api/projects/{project_id}/backlog/history")
def get_backlog_history(project_id: int) -> dict[str, object]:
    """Return durable Backlog attempt history."""
    return _artifact_history(project_id=project_id, node_id="backlog.generate")


@app.get("/api/projects/{project_id}/roadmap/history")
def get_roadmap_history(project_id: int) -> dict[str, object]:
    """Return durable Roadmap attempt history."""
    return _artifact_history(
        project_id=project_id,
        node_id="planning.roadmap.generate",
    )


@app.get("/api/projects/{project_id}/story/pending")
def get_story_pending(project_id: int) -> dict[str, object]:
    """Return durable pending Story coverage."""
    return _read_payload(_application().reads.story_pending(project_id=project_id))


@app.get("/api/projects/{project_id}/story/history")
def get_story_history(
    project_id: int,
    instance_key: str | None = None,
) -> dict[str, object]:
    """Return durable Story attempt history."""
    return _artifact_history(
        project_id=project_id,
        node_id="planning.story.generate",
        instance_key=instance_key,
    )


@app.get("/api/projects/{project_id}/story/dependencies")
def get_story_dependencies(project_id: int) -> dict[str, object]:
    """Return durable Story dependency inspection data."""
    return _read_payload(
        _application().reads.story_dependencies_inspect(project_id=project_id)
    )


@app.get("/api/stories/{story_id}")
def get_story(story_id: int) -> dict[str, object]:
    """Return one durable Story record."""
    return _read_payload(_application().reads.story_show(story_id=story_id))


@app.get("/api/projects/{project_id}/sprint/candidates")
def get_sprint_candidates(project_id: int) -> dict[str, object]:
    """Return durable Sprint candidate Story facts."""
    return _read_payload(_application().reads.sprint_candidates(project_id=project_id))


@app.get("/api/projects/{project_id}/sprint/history")
def get_sprint_history(project_id: int) -> dict[str, object]:
    """Return durable Sprint planning and execution history."""
    return _read_payload(_application().reads.sprint_history(project_id=project_id))


@app.get("/api/projects/{project_id}/sprint/metrics")
def get_sprint_metrics(project_id: int) -> dict[str, object]:
    """Return durable Sprint metrics."""
    return _read_payload(_application().reads.sprint_metrics(project_id=project_id))


@app.get("/api/projects/{project_id}/sprints")
def get_sprints(project_id: int) -> dict[str, object]:
    """Return retained Sprint list and history data."""
    return _read_payload(_application().reads.sprint_history(project_id=project_id))


@app.get("/api/projects/{project_id}/sprints/{sprint_id}")
def get_sprint(project_id: int, sprint_id: int) -> dict[str, object]:
    """Return one durable Sprint status projection."""
    return _read_payload(
        _application().reads.sprint_status(
            project_id=project_id,
            sprint_id=sprint_id,
        )
    )


@app.get("/api/projects/{project_id}/sprints/{sprint_id}/tasks")
def get_sprint_tasks(project_id: int, sprint_id: int) -> dict[str, object]:
    """Return durable task tickets for one Sprint."""
    return _read_payload(
        _application().reads.sprint_tasks(
            project_id=project_id,
            sprint_id=sprint_id,
        )
    )


@app.get("/api/projects/{project_id}/sprints/{sprint_id}/tasks/{task_id}")
def get_sprint_task(
    project_id: int,
    sprint_id: int,
    task_id: int,
) -> dict[str, object]:
    """Return one durable Sprint task ticket."""
    return _read_payload(
        _application().reads.sprint_task_show(
            project_id=project_id,
            sprint_id=sprint_id,
            task_id=task_id,
        )
    )


@app.get("/api/projects/{project_id}/sprints/{sprint_id}/tasks/{task_id}/execution")
def get_task_execution(
    project_id: int,
    sprint_id: int,
    task_id: int,
) -> dict[str, object]:
    """Return retained execution history for one task."""
    return _read_payload(
        _application().reads.sprint_task_history(
            project_id=project_id,
            sprint_id=sprint_id,
            task_id=task_id,
        )
    )


@app.get("/api/projects/{project_id}/sprints/{sprint_id}/tasks/{task_id}/packet")
def get_task_packet(
    project_id: int,
    sprint_id: int,
    task_id: int,
    flavor: str | None = None,
) -> dict[str, object]:
    """Return a bounded task context packet."""
    return _read_payload(
        _application().reads.task_packet(
            project_id=project_id,
            sprint_id=sprint_id,
            task_id=task_id,
            flavor=flavor,
        )
    )


@app.get("/api/projects/{project_id}/sprints/{sprint_id}/stories/{story_id}/packet")
def get_story_packet(
    project_id: int,
    sprint_id: int,
    story_id: int,
    flavor: str | None = None,
) -> dict[str, object]:
    """Return a bounded Story context packet."""
    return _read_payload(
        _application().reads.story_packet(
            project_id=project_id,
            sprint_id=sprint_id,
            story_id=story_id,
            flavor=flavor,
        )
    )


@app.post("/api/projects/{project_id}/authority/decision")
def decide_project_authority(
    project_id: int,
    req: AuthorityDecisionApiRequest,
    expected_candidate_fingerprint: Annotated[
        str | None,
        Header(alias="X-AgileForge-Expected-Candidate", include_in_schema=False),
    ] = None,
) -> dict[str, object]:
    """Record one human choice with server-derived authority identity."""
    return _result_payload(
        _application().decide_authority(
            build_authority_decision_request(
                project_id,
                req,
                expected_candidate_fingerprint,
            )
        )
    )


@app.post("/api/projects/{project_id}/authority/feedback")
def record_project_authority_feedback(
    project_id: int,
    req: AuthorityFeedbackApiRequest,
) -> dict[str, object]:
    """Record feedback for the graph-selected rejected authority."""
    return _result_payload(
        _application().record_authority_feedback(
            build_authority_feedback_request(project_id, req)
        )
    )


def _delivery_request(
    project_id: int,
    req: DeliveryActionApiRequest,
) -> DeliveryActionRequest:
    return DeliveryActionRequest(
        project_id=project_id,
        instance_key=req.instance_key,
        **_metadata(req),
    )


@app.post("/api/projects/{project_id}/authority/compile")
def compile_project_authority(
    project_id: int,
    req: MutationApiRequest,
) -> dict[str, object]:
    """Compile authority from host-prepared current specification input."""
    return _result_payload(
        _application().compile_authority(
            AuthorityCompileRequest(project_id=project_id, **_metadata(req))
        )
    )


@app.post("/api/projects/{project_id}/authority/repair")
def repair_project_authority(
    project_id: int,
    req: MutationApiRequest,
) -> dict[str, object]:
    """Repair rejected authority from host-prepared compiler input."""
    return _result_payload(
        _application().repair_authority(
            AuthorityRepairRequest(project_id=project_id, **_metadata(req))
        )
    )


@app.post("/api/projects/{project_id}/backlog/generate")
def generate_project_backlog(
    project_id: int,
    req: DeliveryActionApiRequest,
) -> dict[str, object]:
    """Generate Backlog from host-prepared durable input."""
    return _result_payload(
        _application().generate_backlog(_delivery_request(project_id, req))
    )


@app.post("/api/projects/{project_id}/roadmap/generate")
def generate_project_roadmap(
    project_id: int,
    req: DeliveryActionApiRequest,
) -> dict[str, object]:
    """Generate Roadmap from host-prepared durable input."""
    return _result_payload(
        _application().generate_roadmap(_delivery_request(project_id, req))
    )


@app.post("/api/projects/{project_id}/story/generate")
def generate_project_story(
    project_id: int,
    req: StoryDeliveryActionApiRequest,
) -> dict[str, object]:
    """Generate Story drafts from host-prepared durable input."""
    return _result_payload(
        _application().generate_story(_delivery_request(project_id, req))
    )


@app.post("/api/projects/{project_id}/sprint/generate")
def generate_project_sprint(
    project_id: int,
    req: SprintPlanningApiRequest,
) -> dict[str, object]:
    """Generate one host-prepared Sprint plan from operator semantics."""
    return _result_payload(
        _application().generate_sprint(
            SprintPlanningRequest(
                project_id=project_id,
                guidance=req.user_input,
                selected_story_ids=tuple(req.selected_story_ids),
                max_story_points=req.max_story_points,
                include_task_decomposition=req.include_task_decomposition,
                team_name=req.team_name,
                **_metadata(req),
            )
        )
    )


@app.post("/api/projects/{project_id}/backlog/decide")
def decide_project_backlog(
    project_id: int,
    req: BacklogReviewApiRequest,
) -> dict[str, object]:
    """Review the graph-selected Backlog from semantic operator input."""
    return _result_payload(
        _application().decide_backlog(
            BacklogReviewRequest(
                project_id=project_id,
                decision=req.decision,
                rationale=req.rationale,
                **_metadata(req),
            )
        )
    )


@app.post("/api/projects/{project_id}/roadmap/decide")
def decide_project_roadmap(
    project_id: int,
    req: RoadmapReviewApiRequest,
) -> dict[str, object]:
    """Review the graph-selected Roadmap from semantic operator input."""
    return _result_payload(
        _application().decide_roadmap(
            RoadmapReviewRequest(
                project_id=project_id,
                decision=req.decision,
                rationale=req.rationale,
                **_metadata(req),
            )
        )
    )


@app.post("/api/projects/{project_id}/story/decide")
def decide_project_story(
    project_id: int,
    req: StoryReviewApiRequest,
) -> dict[str, object]:
    """Review one exact graph-selected Story artifact instance."""
    return _result_payload(
        _application().decide_story(
            StoryReviewRequest(
                project_id=project_id,
                instance_key=req.instance_key,
                decision=req.decision,
                rationale=req.rationale,
                **_metadata(req),
            )
        )
    )


@app.post("/api/projects/{project_id}/sprint/decide")
def decide_project_sprint_plan(
    project_id: int,
    req: SprintPlanReviewApiRequest,
) -> dict[str, object]:
    """Review the graph-selected Sprint plan from semantic operator input."""
    return _result_payload(
        _application().decide_sprint_plan(
            SprintPlanReviewRequest(
                project_id=project_id,
                decision=req.decision,
                rationale=req.rationale,
                **_metadata(req),
            )
        )
    )


@app.post("/api/projects/{project_id}/story/dependencies/apply")
def apply_project_story_dependencies(
    project_id: int,
    req: StoryDependenciesApplyApiRequest,
) -> dict[str, object]:
    """Apply only operator-reviewed Story dependency semantics."""
    return _result_payload(
        _application().apply_story_dependencies(
            StoryDependenciesApplyRequest(
                project_id=project_id,
                selected_story_ids=tuple(req.selected_story_ids),
                reviewed_edges=tuple(req.reviewed_edges),
                **_metadata(req),
            )
        )
    )


@app.post("/api/projects/{project_id}/story/readiness/repair")
def repair_project_story_readiness(
    project_id: int,
    req: StoryReadinessRepairApiRequest,
) -> dict[str, object]:
    """Apply explicit Story points and rank repairs against current facts."""
    return _result_payload(
        _application().repair_story_readiness(
            StoryReadinessRepairRequest(
                project_id=project_id,
                repairs=tuple(req.repairs),
                **_metadata(req),
            )
        )
    )


@app.post("/api/projects/{project_id}/sprint/start")
def start_project_sprint(
    project_id: int,
    req: SprintStartApiRequest,
) -> dict[str, object]:
    """Start the exact accepted current Sprint plan."""
    return _result_payload(
        _application().start_sprint(
            SprintStartRequest(project_id=project_id, **_metadata(req))
        )
    )


@app.post("/api/projects/{project_id}/sprint/task/complete")
def complete_project_task(
    project_id: int,
    req: CompleteTaskApiRequest,
) -> dict[str, object]:
    """Complete one exact Task from semantic outcome and checklist evidence."""
    return _result_payload(
        _application().complete_task(
            CompleteTaskRequest(
                project_id=project_id,
                instance_key=req.instance_key,
                outcome_summary=req.outcome_summary,
                artifact_refs=tuple(req.artifact_refs),
                acceptance_result=req.acceptance_result,
                checklist_result=req.checklist_result,
                **_metadata(req),
            )
        )
    )


@app.post("/api/projects/{project_id}/story/close")
def close_project_story(
    project_id: int,
    req: CloseStoryApiRequest,
) -> dict[str, object]:
    """Close one exact Story from semantic delivery evidence."""
    return _result_payload(
        _application().close_story(
            CloseStoryRequest(
                project_id=project_id,
                instance_key=req.instance_key,
                resolution=req.resolution,
                delivered=req.delivered,
                evidence=req.evidence,
                known_gaps=req.known_gaps,
                **_metadata(req),
            )
        )
    )


@app.post("/api/projects/{project_id}/sprint/review")
def review_project_sprint(
    project_id: int,
    req: SprintReviewApiRequest,
) -> dict[str, object]:
    """Review the exact graph-selected terminal Sprint."""
    return _result_payload(
        _application().review_sprint(
            SprintReviewRequest(
                project_id=project_id,
                instance_key=req.instance_key,
                **_metadata(req),
            )
        )
    )


@app.post("/api/projects/{project_id}/sprint/close")
def close_project_sprint(
    project_id: int,
    req: SprintCloseApiRequest,
) -> dict[str, object]:
    """Close the exact graph-selected reviewed Sprint."""
    return _result_payload(
        _application().close_sprint(
            SprintCloseRequest(
                project_id=project_id,
                instance_key=req.instance_key,
                **_metadata(req),
            )
        )
    )


@app.post("/api/projects/{project_id}/sprint/triage")
def record_project_post_sprint_triage(
    project_id: int,
    req: PostSprintTriageApiRequest,
) -> dict[str, object]:
    """Record semantic triage for one exact completed Sprint."""
    return _result_payload(
        _application().record_post_sprint_triage(
            PostSprintTriageRequest(
                project_id=project_id,
                instance_key=req.instance_key,
                impact=req.impact,
                canonical_payload=req.canonical_payload,
                **_metadata(req),
            )
        )
    )


if __name__ == "__main__":
    import uvicorn

    from utils.runtime_config import get_api_host, get_api_port, get_api_reload

    uvicorn.run(
        "api:app",
        host=get_api_host(),
        port=get_api_port(),
        reload=get_api_reload(),
    )
