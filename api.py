"""FastAPI transport backed exclusively by the durable workflow graph."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Literal, TypedDict, cast

from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from git import Git
from git.exc import GitCommandError
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter

from adapters.adk.model_roles import AGENTIC_MODEL_ROLES
from repositories.project import ProjectRepository
from services.agent_workbench.version import agileforge_version
from services.application import (
    AgenticActionRequest,
    AgileForgeApplication,
    AuthorityCompileRequest,
    AuthorityRepairRequest,
    AuthorityReviewRequest,
    CreateProjectCommand,
    DiscoveryArtifactRequest,
    ProductGoalOutcomeRequest,
    ProductGoalResponseRequest,
    ProductGoalReviewRequest,
    RepositoryAttachRequest,
    RepositoryRefreshRequest,
    SpecificationCandidateRequest,
    SpecificationReviewRequest,
    VisionResponseRequest,
    VisionReviewRequest,
    VisionRevisionRequest,
    production_application,
)
from utils.model_config import get_model_id
from utils.runtime_controls import UI_LAUNCH_NONCE_ENV
from workflow.contracts import (
    JsonObject,
    NodeCategory,
    NodeDecision,
    TransitionResult,
    WorkflowError,
    WorkflowErrorCode,
    WorkflowPosition,
)
from workflow.requests import TransitionRequest

_TRANSITION_REQUEST = TypeAdapter(TransitionRequest)
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

type DecisionRationale = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]


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

    text: str = Field(min_length=1)


class ReviewApiRequest(MutationApiRequest):
    """One semantic review choice."""

    decision: Literal["accepted", "rejected", "feedback"]
    rationale: DecisionRationale


class RevisionApiRequest(MutationApiRequest):
    """One semantic Vision revision reason."""

    reason: str = Field(min_length=1)


class GoalOutcomeApiRequest(MutationApiRequest):
    """One semantic Product Goal outcome rationale."""

    rationale: DecisionRationale


class ArtifactRecordApiRequest(MutationApiRequest):
    """Caller-owned discovery or specification content only."""

    canonical_content: JsonObject
    content_ref: str | None = None


class RepositoryAttachApiRequest(MutationApiRequest):
    """Repository path plus transport metadata only."""

    path: str = Field(min_length=1)


class AuthorityDecisionApiRequest(MutationApiRequest):
    """Semantic authority review choice without derived identities."""

    decision: Literal["accepted", "rejected"]
    rationale: DecisionRationale


class AgenticActionApiRequest(MutationApiRequest):
    """Semantic input for one retained agentic delivery leaf."""

    instance_key: str | None = None
    input_payload: JsonObject
    model_id: str | None = Field(default=None, min_length=1)


class PositionedTransitionApiRequest(MutationApiRequest):
    """Semantic fields for one retained fixed delivery route."""

    instance_key: str | None = None
    input_payload: JsonObject


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


AGENTIC_API_PATHS: dict[str, str] = {
    "record_brownfield_spec_draft": "brownfield/curate",
    "compile_authority": "authority/compile",
    "repair_authority": "authority/repair",
    "record_vision_draft": "vision/generate",
    "record_backlog_draft": "backlog/generate",
    "record_roadmap_draft": "roadmap/generate",
    "record_story_draft": "story/generate",
    "record_sprint_plan": "sprint/generate",
}

_AGENTIC_NODE_IDS: dict[str, str] = {
    "record_brownfield_spec_draft": "onboarding.brownfield.curation",
    "compile_authority": "authority.compile",
    "repair_authority": "authority.repair",
    "record_vision_draft": "vision.generate",
    "record_backlog_draft": "backlog.generate",
    "record_roadmap_draft": "planning.roadmap.generate",
    "record_story_draft": "planning.story.generate",
    "record_sprint_plan": "planning.sprint.plan",
}

POSITIONED_API_PATHS: dict[str, str] = {
    "abandon_project_shell": "project/abandon",
    "abandon_scope_extension": "scope/extension/abandon",
    "apply_story_dependencies": "story/dependencies/apply",
    "close_sprint": "sprint/close",
    "close_story": "story/close",
    "complete_task": "sprint/task/complete",
    "decide_amendment_spec_draft": "scope/extension/spec/decide",
    "decide_backlog": "backlog/decide",
    "decide_brownfield_initial_spec": "brownfield/spec/decide",
    "decide_extension_prd": "scope/extension/prd/decide",
    "decide_initial_spec_draft": "discovery/spec/decide",
    "decide_prd": "discovery/prd/decide",
    "decide_roadmap": "roadmap/decide",
    "decide_sprint_plan": "sprint/decide",
    "decide_story": "story/decide",
    "decide_vision": "vision/decide",
    "reconcile_backlog": "backlog/reconcile",
    "reconcile_scope_extension": "scope/extension/reconcile",
    "record_amendment_spec_draft": "scope/extension/spec/record",
    "record_authority_feedback": "authority/feedback",
    "record_challenge_artifact": "discovery/challenge/record",
    "record_extension_challenge": "scope/extension/challenge/record",
    "record_extension_prd": "scope/extension/prd/record",
    "record_initial_spec_draft": "discovery/spec/record",
    "record_post_sprint_triage": "sprint/triage",
    "record_prd_version": "discovery/prd/record",
    "record_repository_baseline": "brownfield/baseline/record",
    "record_repository_inventory": "brownfield/inventory/record",
    "register_initial_scope": "scope/register",
    "register_scope_extension": "scope/extension/register",
    "repair_story_readiness": "story/readiness/repair",
    "review_sprint": "sprint/review",
    "start_scope_extension": "scope/extension/start",
    "start_sprint": "sprint/start",
}

SEMANTIC_API_PATHS: dict[str, str] = {
    "abandon_product_goal": "goals/abandon",
    "begin_vision_revision": "vision/revision",
    "compile_authority": "authority/compile",
    "decide_authority": "authority/decision",
    "decide_product_goal_review": "goals/review",
    "decide_specification": "specifications/review",
    "decide_vision_review": "vision/review",
    "fulfill_product_goal": "goals/complete",
    "record_discovery_artifact": "discovery",
    "record_product_goal_interview_turn": "goals/respond",
    "record_specification_candidate": "specifications",
    "record_vision_interview_turn": "vision/respond",
    "repair_authority": "authority/repair",
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
) -> AuthorityReviewRequest:
    """Translate one API choice without accepting derived review identity."""
    return AuthorityReviewRequest(
        project_id=project_id,
        idempotency_key=req.idempotency_key,
        actor=req.actor,
        correlation_id=req.correlation_id,
        decision=req.decision,
        rationale=req.rationale,
    )


def build_positioned_transition_request(
    project_id: int,
    request_kind: str,
    req: PositionedTransitionApiRequest,
    position: WorkflowPosition,
    decision: NodeDecision,
) -> TransitionRequest:
    """Build an exact typed request for one route-fixed request kind."""
    payload = dict(req.input_payload)
    payload.update(
        {
            "kind": request_kind,
            "project_id": project_id,
            "graph_version": position.graph_version,
            "fact_fingerprint": position.fact_fingerprint,
            "decision_fingerprint": decision.decision_fingerprint,
            "instance_key": decision.instance_key,
            "idempotency_key": req.idempotency_key,
            "actor": req.actor,
            "correlation_id": req.correlation_id,
        }
    )
    return _TRANSITION_REQUEST.validate_python(payload)


def _result_payload(result: TransitionResult) -> dict[str, object]:
    if not isinstance(result, TransitionResult):
        raise TypeError(type(result).__name__)
    if not result.ok:
        status = 409 if result.error is not None else 400
        detail = result.model_dump(mode="json")
        if result.position is not None:
            detail["actions"] = _workflow_actions(result.position)
        raise HTTPException(
            status_code=status,
            detail=detail,
        )
    return {"status": "success", "data": result.model_dump(mode="json")}


def _current_decision(
    application: AgileForgeApplication,
    *,
    project_id: int,
    request_kind: str,
    instance_key: str | None,
) -> tuple[WorkflowPosition, NodeDecision | None]:
    position = application.position(project_id=project_id)
    candidates = tuple(
        decision
        for decision in position.decisions
        if decision.request_kind == request_kind
        and decision.category in {NodeCategory.AVAILABLE, NodeCategory.WAITING}
        and (instance_key is None or decision.instance_key == instance_key)
    )
    return position, candidates[0] if len(candidates) == 1 else None


def _transition_not_available(
    position: WorkflowPosition,
    request_kind: str,
) -> TransitionResult:
    return TransitionResult(
        ok=False,
        position=position,
        error=WorkflowError(
            code=WorkflowErrorCode.TRANSITION_NOT_AVAILABLE,
            message=f"No unique {request_kind} transition is currently available.",
        ),
    )


def _workflow_actions(position: WorkflowPosition) -> list[JsonObject]:
    """Advertise one fixed API route for each exact available decision."""
    actions: list[JsonObject] = []
    for decision in position.decisions:
        if decision.category is not NodeCategory.AVAILABLE:
            continue
        request_kind = decision.request_kind
        if request_kind in SEMANTIC_API_PATHS:
            endpoint = SEMANTIC_API_PATHS[request_kind]
            transport = "semantic"
        elif request_kind in AGENTIC_API_PATHS:
            endpoint = AGENTIC_API_PATHS[request_kind]
            transport = "agentic"
        elif request_kind in POSITIONED_API_PATHS:
            endpoint = POSITIONED_API_PATHS[request_kind]
            transport = "positioned"
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
) -> dict[str, object]:
    """Record one semantic Project Vision review decision."""
    return _result_payload(
        _application().review_vision(
            VisionReviewRequest(
                project_id=project_id,
                decision=req.decision,
                rationale=req.rationale,
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
) -> dict[str, object]:
    """Record one semantic Product Goal review decision."""
    return _result_payload(
        _application().review_product_goal(
            ProductGoalReviewRequest(
                project_id=project_id,
                decision=req.decision,
                rationale=req.rationale,
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


@app.post("/api/projects/{project_id}/discovery")
def record_discovery(
    project_id: int,
    req: ArtifactRecordApiRequest,
) -> dict[str, object]:
    """Record caller-owned discovery content under host-derived lineage."""
    return _result_payload(
        _application().record_discovery(
            DiscoveryArtifactRequest(
                project_id=project_id,
                canonical_content=req.canonical_content,
                content_ref=req.content_ref,
                **_metadata(req),
            )
        )
    )


@app.get("/api/projects/{project_id}/discovery")
def get_discovery(project_id: int) -> dict[str, object]:
    """Return current durable discovery content."""
    return _read_payload(_application().reads.discovery_status(project_id=project_id))


@app.post("/api/projects/{project_id}/specifications")
def record_specification(
    project_id: int,
    req: ArtifactRecordApiRequest,
) -> dict[str, object]:
    """Record a specification candidate under host-derived lineage."""
    return _result_payload(
        _application().record_specification_candidate(
            SpecificationCandidateRequest(
                project_id=project_id,
                canonical_content=req.canonical_content,
                content_ref=req.content_ref,
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
) -> dict[str, object]:
    """Record one semantic specification review decision."""
    return _result_payload(
        _application().review_specification(
            SpecificationReviewRequest(
                project_id=project_id,
                decision=req.decision,
                rationale=req.rationale,
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
    return _artifact_history(project_id=project_id, node_id="vision.generate")


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
) -> dict[str, object]:
    """Record one human choice with server-derived authority identity."""
    return _result_payload(
        _application().decide_authority(
            build_authority_decision_request(project_id, req)
        )
    )


def _run_agentic(
    *,
    project_id: int,
    node_id: str,
    req: AgenticActionApiRequest,
) -> dict[str, object]:
    application = _application()
    request_kind = next(
        kind
        for kind, mapped_node_id in _AGENTIC_NODE_IDS.items()
        if mapped_node_id == node_id
    )
    position, decision = _current_decision(
        application,
        project_id=project_id,
        request_kind=request_kind,
        instance_key=req.instance_key,
    )
    if decision is None:
        return _result_payload(_transition_not_available(position, request_kind))
    result = application.run_agentic_action(
        AgenticActionRequest(
            project_id=project_id,
            graph_version=position.graph_version,
            fact_fingerprint=position.fact_fingerprint,
            decision_fingerprint=decision.decision_fingerprint,
            node_id=node_id,
            instance_key=decision.instance_key,
            input_payload=req.input_payload,
            model_id=req.model_id or get_model_id(AGENTIC_MODEL_ROLES[node_id]),
            idempotency_key=req.idempotency_key,
            actor=req.actor,
            correlation_id=req.correlation_id,
        )
    )
    return _result_payload(result)


@app.post("/api/projects/{project_id}/brownfield/curate")
def curate_brownfield_project(
    project_id: int,
    req: AgenticActionApiRequest,
) -> dict[str, object]:
    """Run the dedicated Brownfield curator through a durable attempt."""
    return _run_agentic(
        project_id=project_id,
        node_id="onboarding.brownfield.curation",
        req=req,
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


@app.post("/api/projects/{project_id}/vision/generate")
def generate_project_vision(
    project_id: int,
    req: AgenticActionApiRequest,
) -> dict[str, object]:
    """Generate Vision through the graph recipe."""
    return _run_agentic(project_id=project_id, node_id="vision.generate", req=req)


@app.post("/api/projects/{project_id}/backlog/generate")
def generate_project_backlog(
    project_id: int,
    req: AgenticActionApiRequest,
) -> dict[str, object]:
    """Generate Backlog through the graph recipe."""
    return _run_agentic(project_id=project_id, node_id="backlog.generate", req=req)


@app.post("/api/projects/{project_id}/roadmap/generate")
def generate_project_roadmap(
    project_id: int,
    req: AgenticActionApiRequest,
) -> dict[str, object]:
    """Generate Roadmap through the graph recipe."""
    return _run_agentic(
        project_id=project_id,
        node_id="planning.roadmap.generate",
        req=req,
    )


@app.post("/api/projects/{project_id}/story/generate")
def generate_project_story(
    project_id: int,
    req: AgenticActionApiRequest,
) -> dict[str, object]:
    """Generate Story drafts through the graph recipe."""
    return _run_agentic(
        project_id=project_id,
        node_id="planning.story.generate",
        req=req,
    )


@app.post("/api/projects/{project_id}/sprint/generate")
def generate_project_sprint(
    project_id: int,
    req: AgenticActionApiRequest,
) -> dict[str, object]:
    """Generate a Sprint plan through the graph recipe."""
    return _run_agentic(
        project_id=project_id,
        node_id="planning.sprint.plan",
        req=req,
    )


type PositionedRoute = Callable[
    [int, PositionedTransitionApiRequest],
    dict[str, object],
]


def _positioned_route(request_kind: str) -> PositionedRoute:
    """Build one FastAPI handler with a fixed closed request discriminator."""

    def route(
        project_id: int,
        req: PositionedTransitionApiRequest,
    ) -> dict[str, object]:
        application = _application()
        position, decision = _current_decision(
            application,
            project_id=project_id,
            request_kind=request_kind,
            instance_key=req.instance_key,
        )
        if decision is None:
            return _result_payload(_transition_not_available(position, request_kind))
        request = build_positioned_transition_request(
            project_id,
            request_kind,
            req,
            position,
            decision,
        )
        return _result_payload(application.transition(request))

    route.__name__ = f"transition_{request_kind}"
    route.__doc__ = f"Apply the exact {request_kind} workflow request."
    return route


for _request_kind, _route_suffix in POSITIONED_API_PATHS.items():
    app.add_api_route(
        f"/api/projects/{{project_id}}/{_route_suffix}",
        _positioned_route(_request_kind),
        methods=["POST"],
    )


async def delete_project_story(_project_id: int, _parent_requirement: str) -> None:
    """Reject the removed direct Story mutation used by the old benchmark."""
    raise HTTPException(
        status_code=410,
        detail="Direct Story deletion is not part of the workflow graph API.",
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
