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
    BacklogReviewRequest,
    CloseStoryRequest,
    CompleteTaskRequest,
    CreateProjectCommand,
    DeliveryActionRequest,
    ExpectedPlanningReviewBinding,
    PostSprintTriageRequest,
    ProductGoalOutcomeRequest,
    ProductGoalResponseRequest,
    ProductGoalReviewRequest,
    RepositoryAttachRequest,
    RepositoryRefreshRequest,
    RoadmapReviewRequest,
    SpecificationReviewRequest,
    SpecificationSourceRegistrationRequest,
    SpecificationStructuringRequest,
    SprintCloseRequest,
    SprintPlanningRequest,
    SprintPlanReviewRequest,
    SprintReviewRequest,
    SprintStartRequest,
    StoryDependenciesApplyRequest,
    StoryDependencyEdgeRequest,
    StoryEligibilityReconcileRequest,
    StoryReadinessRepair,
    StoryReadinessRepairRequest,
    StoryReviewRequest,
    StorySetCorrectionRequest,
    StorySprintSelectionRequest,
    VisionBootstrapRequest,
    VisionResponseRequest,
    VisionReviewRequest,
    VisionRevisionRequest,
    execution_action_decision_is_transportable,
    planning_action_decision_is_transportable,
    production_application,
)
from services.specification_source_registration import (
    SpecificationSourceRegistrationError,
)
from services.vision_evidence import VisionEvidenceCollectionError
from utils.runtime_controls import UI_LAUNCH_NONCE_ENV
from workflow.contracts import (
    JsonObject,
    NodeCategory,
    NodeDecision,
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


class SpecificationSourceApiRequest(MutationApiRequest):
    """Semantic repository paths for one exact source registration."""

    source_path: str = Field(min_length=1)
    adr_paths: tuple[str, ...] = ()
    preparation_capability: Literal["grill-with-docs"]


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
    """Semantic Story review without caller-owned machine identity."""


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


class DeliveryActionApiRequest(MutationApiRequest):
    """Transport metadata and optional semantic decision selector only."""

    instance_key: str | None = None


class StoryDeliveryActionApiRequest(DeliveryActionApiRequest):
    """Story delivery request with one exact caller-owned selector."""

    instance_key: SemanticText


class StorySetCorrectionApiRequest(MutationApiRequest):
    """Exact accepted Story-set identity selected from the current graph."""

    instance_key: str = Field(pattern=r"^backlog_item:[^\s:]+$")
    accepted_story_artifact_id: PositiveStoryId
    accepted_story_artifact_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class SprintPlanningApiRequest(MutationApiRequest):
    """Strict operator-owned Sprint planning semantics."""

    user_input: str | None = None
    selected_story_ids: list[int] = Field(default_factory=list)
    max_story_points: int | None = Field(default=None, gt=0)
    team_name: SemanticText | None = None

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
    selected_scope_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
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
        """Keep reviewed dependents inside scope while retaining prerequisites."""
        pairs = [
            (item.dependent_story_id, item.prerequisite_story_id)
            for item in self.reviewed_edges
        ]
        if len(set(pairs)) != len(pairs):
            message = "reviewed_edges must not contain duplicate Story pairs."
            raise ValueError(message)
        selected = set(self.selected_story_ids)
        if any(left not in selected for left, _right in pairs):
            message = "reviewed edge dependents must remain in selected_story_ids."
            raise ValueError(message)
        return self


class StoryEligibilityReconcileApiRequest(MutationApiRequest):
    """Transport payload to reconcile Story structural eligibility evidence."""

    story_ids: list[PositiveStoryId] | None = None

    @field_validator("story_ids")
    @classmethod
    def canonicalize_story_ids(cls, value: list[int] | None) -> list[int] | None:
        """Reject duplicate Story IDs before application construction."""
        if value is None:
            return None
        if len(set(value)) != len(value):
            message = "story_ids must not contain duplicate Story IDs."
            raise ValueError(message)
        return sorted(value)


class StorySprintSelectionApiRequest(MutationApiRequest):
    """One human Story-selection intent guarded by exact current state."""

    story_id: PositiveStoryId
    intent: Literal["select", "remove", "defer"]
    expected_state_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    rationale: SemanticText | None = None

    @field_validator("actor", "correlation_id", "rationale")
    @classmethod
    def reject_blank_audit_text(cls, value: str | None) -> str | None:
        """Reject selection audit text before constructing a domain request."""
        if value is not None and not value.strip():
            message = "Selection audit metadata must be nonblank."
            raise ValueError(message)
        return value


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
    "complete_task": "sprint/task/complete",
    "decide_backlog": "backlog/decide",
    "decide_roadmap": "roadmap/decide",
    "decide_sprint_plan": "sprint/decide",
    "decide_story": "story/decide",
    "record_post_sprint_triage": "sprint/triage",
    "decide_product_goal_review": "goals/review",
    "decide_specification": "specifications/review",
    "decide_vision_review": "vision/review",
    "fulfill_product_goal": "goals/complete",
    "generate_vision_bootstrap": "vision/bootstrap",
    "record_product_goal_interview_turn": "goals/respond",
    "register_specification_source": "specifications/source",
    "structure_specification": "specifications/structure",
    "record_sprint_plan": "sprint/generate",
    "record_vision_interview_turn": "vision/respond",
    "repair_story_readiness": "story/readiness/repair",
    "review_sprint": "sprint/review",
    "start_sprint": "sprint/start",
}

_ACTIONABLE_WAITING_REQUEST_KINDS = frozenset(
    {
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
            detail["actions"] = _workflow_actions(
                result.position,
                application=_application(),
            )
        raise HTTPException(
            status_code=status,
            detail=detail,
        )
    return {"status": "success", "data": result.model_dump(mode="json")}


def _workflow_actions(
    position: WorkflowPosition,
    *,
    application: object | None = None,
) -> list[JsonObject]:
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
        and (
            decision.recommendation_kind
            in {RecommendationKind.REQUIRED, RecommendationKind.RECOVERY}
            or (
                decision.recommendation_kind is RecommendationKind.OPTIONAL_REENTRY
                and (
                    decision.request_kind == "register_specification_source"
                    or (
                        decision.request_kind == "record_sprint_plan"
                        and decision.reason_code == "SPRINT_PLAN_CORRECTION_AVAILABLE"
                    )
                    or (
                        decision.request_kind == "record_story_draft"
                        and decision.reason_code == "STORY_CORRECTION_AVAILABLE"
                    )
                )
            )
        )
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
        if (
            request_kind == "record_story_draft"
            and decision.reason_code == "STORY_CORRECTION_AVAILABLE"
        ):
            endpoint = "story/correct"
            transport = "semantic"
        elif request_kind in SEMANTIC_API_PATHS:
            endpoint = SEMANTIC_API_PATHS[request_kind]
            transport = "semantic"
        elif request_kind in DELIVERY_API_PATHS:
            endpoint = DELIVERY_API_PATHS[request_kind]
            transport = "semantic"
        else:
            continue
        action: JsonObject = {
            "node_id": decision.node_id,
            "instance_key": decision.instance_key,
            "request_kind": request_kind,
            "endpoint": endpoint,
            "transport": transport,
        }
        actions.append(action)
        _project_action_availability(
            action=action,
            application=application,
            decision=decision,
            project_id=position.project_id,
        )
    return actions


def _project_action_availability(
    *,
    action: JsonObject,
    application: object | None,
    decision: NodeDecision,
    project_id: int,
) -> None:
    """Lock actions whose provider-free inputs cannot be opened safely."""
    if application is None:
        return
    capability_method = {
        "generate_vision_bootstrap": "vision_bootstrap_capability",
        "register_specification_source": "specification_source_capability",
    }.get(decision.request_kind)
    if capability_method is not None:
        checker = getattr(application, capability_method, None)
        if callable(checker):
            try:
                capability = checker(project_id=project_id)
            except (
                VisionEvidenceCollectionError,
                SpecificationSourceRegistrationError,
            ) as error:
                action["availability"] = "locked"
                action["reason_code"] = error.code.value
                return
            if not capability.available:
                action["availability"] = "locked"
                action["reason_code"] = (
                    capability.code or "REPOSITORY_EVIDENCE_CAPABILITY_UNAVAILABLE"
                )
        return
    if (
        decision.request_kind != "record_story_draft"
        or decision.reason_code != "STORY_CORRECTION_AVAILABLE"
    ):
        return
    checker = getattr(
        application,
        "story_set_correction_decision_is_executable",
        None,
    )
    if callable(checker) and not checker(
        project_id=project_id,
        decision=decision,
    ):
        action["availability"] = "locked"
        action["reason_code"] = "STORY_CORRECTION_INPUT_UNAVAILABLE"


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
    application = _application()
    position = application.position(project_id=project_id)
    return {
        "status": "success",
        "data": position.model_dump(mode="json"),
        "actions": _workflow_actions(position, application=application),
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


@app.post("/api/projects/{project_id}/specifications/source")
def register_specification_source(
    project_id: int,
    req: SpecificationSourceApiRequest,
    expected_decision_fingerprint: Annotated[
        str,
        Header(alias="X-AgileForge-Expected-Decision", include_in_schema=False),
    ],
) -> dict[str, object]:
    """Capture one exact external to-spec source from semantic paths."""
    return _result_payload(
        _application().register_specification_source(
            SpecificationSourceRegistrationRequest(
                project_id=project_id,
                expected_decision_fingerprint=expected_decision_fingerprint,
                source_path=req.source_path,
                adr_paths=req.adr_paths,
                preparation_capability=req.preparation_capability,
                **_metadata(req),
            )
        )
    )


@app.post("/api/projects/{project_id}/specifications/structure")
def structure_specification(
    project_id: int,
    req: MutationApiRequest,
    expected_decision_fingerprint: Annotated[
        str,
        Header(alias="X-AgileForge-Expected-Decision", include_in_schema=False),
    ],
) -> dict[str, object]:
    """Run one guarded host-prepared action over the registered source."""
    return _result_payload(
        _application().structure_specification(
            SpecificationStructuringRequest(
                project_id=project_id,
                expected_decision_fingerprint=expected_decision_fingerprint,
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


@app.get("/api/projects/{project_id}/backlog/review")
def get_backlog_review(project_id: int) -> dict[str, object]:
    """Return the exact graph-selected Backlog review and machine binding."""
    return _read_payload(_application().backlog_review(project_id))


@app.get("/api/projects/{project_id}/roadmap/review")
def get_roadmap_review(project_id: int) -> dict[str, object]:
    """Return the exact graph-selected Roadmap review and machine binding."""
    return _read_payload(_application().roadmap_review(project_id))


@app.get("/api/projects/{project_id}/story/reviews")
def get_story_reviews(project_id: int) -> dict[str, object]:
    """Return every exact pending Story review in stable display order."""
    return _read_payload(_application().story_reviews(project_id))


@app.get("/api/projects/{project_id}/sprint/plan/review")
def get_sprint_plan_review(project_id: int) -> dict[str, object]:
    """Return the exact graph-selected Sprint-plan review and machine binding."""
    return _read_payload(_application().sprint_plan_review(project_id))


@app.post("/api/projects/{project_id}/specifications/review")
def review_specification(
    project_id: int,
    req: ReviewApiRequest,
    expected_candidate_fingerprint: Annotated[
        str,
        Header(alias="X-AgileForge-Expected-Candidate", include_in_schema=False),
    ],
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


@app.get("/api/projects/{project_id}/sprint/status")
def get_current_sprint(project_id: int) -> dict[str, object]:
    """Return the same selected Sprint status projection used by the CLI."""
    return _read_payload(
        _application().reads.sprint_status(project_id=project_id, sprint_id=None)
    )


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


def _delivery_request(
    project_id: int,
    req: DeliveryActionApiRequest,
) -> DeliveryActionRequest:
    return DeliveryActionRequest(
        project_id=project_id,
        instance_key=req.instance_key,
        **_metadata(req),
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


@app.post("/api/projects/{project_id}/story/correct")
def correct_project_story_set(
    project_id: int,
    req: StorySetCorrectionApiRequest,
    expected_decision: Annotated[
        str,
        Header(
            alias="X-AgileForge-Expected-Decision",
            min_length=1,
            pattern=r"^sha256:[0-9a-f]{64}$",
        ),
    ],
) -> dict[str, object]:
    """Correct one exact accepted Story set through the current graph decision."""
    return _result_payload(
        _application().correct_story_set(
            StorySetCorrectionRequest(
                project_id=project_id,
                instance_key=req.instance_key,
                expected_decision_fingerprint=expected_decision,
                accepted_story_artifact_id=req.accepted_story_artifact_id,
                accepted_story_artifact_fingerprint=(
                    req.accepted_story_artifact_fingerprint
                ),
                **_metadata(req),
            )
        )
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
                team_name=req.team_name,
                **_metadata(req),
            )
        )
    )


@app.post("/api/projects/{project_id}/backlog/decide")
def decide_project_backlog(
    project_id: int,
    req: BacklogReviewApiRequest,
    expected_decision: Annotated[
        str,
        Header(
            alias="X-AgileForge-Expected-Decision",
            min_length=1,
            pattern=r"^\S+$",
        ),
    ],
) -> dict[str, object]:
    """Review the graph-selected Backlog from semantic operator input."""
    return _result_payload(
        _application().decide_backlog(
            BacklogReviewRequest(
                project_id=project_id,
                decision=req.decision,
                rationale=req.rationale,
                **_metadata(req),
            ),
            expected=ExpectedPlanningReviewBinding(
                decision_fingerprint=expected_decision,
                instance_key=None,
            ),
        )
    )


@app.post("/api/projects/{project_id}/roadmap/decide")
def decide_project_roadmap(
    project_id: int,
    req: RoadmapReviewApiRequest,
    expected_decision: Annotated[
        str,
        Header(
            alias="X-AgileForge-Expected-Decision",
            min_length=1,
            pattern=r"^\S+$",
        ),
    ],
) -> dict[str, object]:
    """Review the graph-selected Roadmap from semantic operator input."""
    return _result_payload(
        _application().decide_roadmap(
            RoadmapReviewRequest(
                project_id=project_id,
                decision=req.decision,
                rationale=req.rationale,
                **_metadata(req),
            ),
            expected=ExpectedPlanningReviewBinding(
                decision_fingerprint=expected_decision,
                instance_key=None,
            ),
        )
    )


@app.post("/api/projects/{project_id}/story/decide")
def decide_project_story(
    project_id: int,
    req: StoryReviewApiRequest,
    expected_decision: Annotated[
        str,
        Header(
            alias="X-AgileForge-Expected-Decision",
            min_length=1,
            pattern=r"^\S+$",
        ),
    ],
    expected_instance: Annotated[
        str,
        Header(
            alias="X-AgileForge-Expected-Instance",
            min_length=1,
            pattern=r"^\S+$",
        ),
    ],
) -> dict[str, object]:
    """Review one exact graph-selected Story artifact instance."""
    return _result_payload(
        _application().decide_story(
            StoryReviewRequest(
                project_id=project_id,
                decision=req.decision,
                rationale=req.rationale,
                **_metadata(req),
            ),
            expected=ExpectedPlanningReviewBinding(
                decision_fingerprint=expected_decision,
                instance_key=expected_instance,
            ),
        )
    )


@app.post("/api/projects/{project_id}/sprint/decide")
def decide_project_sprint_plan(
    project_id: int,
    req: SprintPlanReviewApiRequest,
    expected_decision: Annotated[
        str,
        Header(
            alias="X-AgileForge-Expected-Decision",
            min_length=1,
            pattern=r"^\S+$",
        ),
    ],
) -> dict[str, object]:
    """Review the graph-selected Sprint plan from semantic operator input."""
    return _result_payload(
        _application().decide_sprint_plan(
            SprintPlanReviewRequest(
                project_id=project_id,
                decision=req.decision,
                rationale=req.rationale,
                **_metadata(req),
            ),
            expected=ExpectedPlanningReviewBinding(
                decision_fingerprint=expected_decision,
                instance_key=None,
            ),
        )
    )


@app.post("/api/projects/{project_id}/story/structural-eligibility/reconcile")
def reconcile_project_story_structural_eligibility(
    project_id: int,
    req: StoryEligibilityReconcileApiRequest,
) -> dict[str, object]:
    """Reconcile provider-free structural eligibility for active Stories."""
    return _read_payload(
        _application().reconcile_story_eligibility(
            StoryEligibilityReconcileRequest(
                project_id=project_id,
                story_ids=tuple(req.story_ids) if req.story_ids is not None else None,
                **_metadata(req),
            )
        )
    )


@app.post("/api/projects/{project_id}/story/sprint-selection")
def apply_project_story_sprint_selection(
    project_id: int,
    req: StorySprintSelectionApiRequest,
) -> dict[str, object]:
    """Apply one explicit human Sprint-selection intent to an exact Story."""
    return _read_payload(
        _application().apply_story_sprint_selection(
            StorySprintSelectionRequest(
                project_id=project_id,
                story_id=req.story_id,
                intent=req.intent,
                expected_state_fingerprint=req.expected_state_fingerprint,
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
                selected_scope_fingerprint=req.selected_scope_fingerprint,
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
    expected_decision: Annotated[
        str,
        Header(alias="X-AgileForge-Expected-Decision", include_in_schema=False),
    ],
) -> dict[str, object]:
    """Start the exact accepted current Sprint plan."""
    return _result_payload(
        _application().start_sprint(
            SprintStartRequest(
                project_id=project_id,
                expected_decision_fingerprint=expected_decision,
                **_metadata(req),
            )
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
