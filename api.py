"""FastAPI transport backed exclusively by the durable workflow graph."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from services.application import (
    AgenticActionRequest,
    AgileForgeApplication,
    production_application,
)
from utils.api_schemas import WorkflowPositionGuards
from utils.model_config import get_model_id
from workflow.contracts import JsonObject, TransitionResult
from workflow.requests import DecideAuthority, OpenProjectShell, TransitionRequest

_TRANSITION_REQUEST = TypeAdapter(TransitionRequest)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Initialize the business schema without creating routing sessions."""
    from models.db import ensure_business_db_ready  # noqa: PLC0415

    ensure_business_db_ready()
    yield


app = FastAPI(title="AgileForge API", lifespan=lifespan)
app.mount("/dashboard", StaticFiles(directory="frontend", html=True), name="frontend")


class CreateProjectRequest(BaseModel):
    """Request body for opening a Project Shell."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    origin: Literal["greenfield", "brownfield"]
    idempotency_key: str = Field(min_length=1, max_length=200)
    changed_by: str = Field(default="dashboard-ui", min_length=1, max_length=200)
    correlation_id: str | None = Field(default=None, min_length=1)


class AuthorityDecisionApiRequest(WorkflowPositionGuards):
    """Exact guarded authority decision payload."""

    pending_authority_id: int
    authority_fingerprint: str = Field(min_length=1)
    review_fingerprint: str = Field(min_length=1)
    decision: Literal["accepted", "rejected"]
    rationale: str = Field(min_length=1)


class AgenticActionApiRequest(WorkflowPositionGuards):
    """Exact position guards and normalized input for one task-specific leaf."""

    instance_key: str | None = None
    input_payload: JsonObject
    model_id: str | None = Field(default=None, min_length=1)


class PositionedTransitionApiRequest(WorkflowPositionGuards):
    """Exact guards plus task-specific fields for one fixed API route."""

    instance_key: str | None = None
    input_payload: JsonObject


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


_AGENTIC_MODEL_KEYS: dict[str, str] = {
    "onboarding.brownfield.curation": "brownfield_curator",
    "authority.compile": "spec_authority_compiler",
    "authority.repair": "spec_authority_compiler",
    "vision.generate": "product_vision",
    "backlog.generate": "backlog_primer",
    "planning.roadmap.generate": "roadmap_builder",
    "planning.story.generate": "user_story_writer",
    "planning.sprint.plan": "sprint_planner",
}


def _application() -> AgileForgeApplication:
    return production_application()


def build_project_shell_request(req: CreateProjectRequest) -> OpenProjectShell:
    """Translate one API payload into the exact Project Shell request."""
    return OpenProjectShell(
        name=req.name,
        origin=req.origin,
        idempotency_key=req.idempotency_key,
        actor=req.changed_by,
        correlation_id=req.correlation_id,
    )


def build_authority_decision_request(
    project_id: int,
    req: AuthorityDecisionApiRequest,
) -> DecideAuthority:
    """Translate one API payload into the exact guarded decision request."""
    return DecideAuthority(
        project_id=project_id,
        graph_version=req.graph_version,
        fact_fingerprint=req.expected_fact_fingerprint,
        decision_fingerprint=req.expected_decision_fingerprint,
        idempotency_key=req.idempotency_key,
        actor=req.changed_by,
        correlation_id=req.correlation_id,
        pending_authority_id=req.pending_authority_id,
        authority_fingerprint=req.authority_fingerprint,
        review_fingerprint=req.review_fingerprint,
        decision=req.decision,
        rationale=req.rationale,
    )


def build_positioned_transition_request(
    project_id: int,
    request_kind: str,
    req: PositionedTransitionApiRequest,
) -> TransitionRequest:
    """Build an exact typed request for one route-fixed request kind."""
    payload = dict(req.input_payload)
    payload.update(
        {
            "kind": request_kind,
            "project_id": project_id,
            "graph_version": req.graph_version,
            "fact_fingerprint": req.expected_fact_fingerprint,
            "decision_fingerprint": req.expected_decision_fingerprint,
            "instance_key": req.instance_key,
            "idempotency_key": req.idempotency_key,
            "actor": req.changed_by,
            "correlation_id": req.correlation_id,
        }
    )
    return _TRANSITION_REQUEST.validate_python(payload)


def _result_payload(result: TransitionResult) -> dict[str, object]:
    if not isinstance(result, TransitionResult):
        raise TypeError(type(result).__name__)
    if not result.ok:
        status = 409 if result.error is not None else 400
        raise HTTPException(
            status_code=status,
            detail=result.model_dump(mode="json"),
        )
    return {"status": "success", "data": result.model_dump(mode="json")}


@app.get("/")
def root() -> RedirectResponse:
    """Redirect to the workflow dashboard."""
    return RedirectResponse(url="/dashboard")


@app.post("/api/projects")
def create_project(req: CreateProjectRequest) -> dict[str, object]:
    """Open a greenfield or Brownfield Project Shell."""
    return _result_payload(_application().transition(build_project_shell_request(req)))


@app.get("/api/projects")
def get_projects() -> dict[str, object]:
    """Return Project identity without a routing projection."""
    projects = [item.model_dump(mode="json") for item in _application().projects()]
    return {"status": "success", "data": projects}


@app.get("/api/projects/{project_id}/position")
def get_project_position(project_id: int) -> dict[str, object]:
    """Return the only workflow routing projection."""
    position = _application().position(project_id=project_id)
    return {"status": "success", "data": position.model_dump(mode="json")}


@app.post("/api/projects/{project_id}/authority/decision")
def decide_project_authority(
    project_id: int,
    req: AuthorityDecisionApiRequest,
) -> dict[str, object]:
    """Record one exact human authority decision."""
    return _result_payload(
        _application().transition(build_authority_decision_request(project_id, req))
    )


def _run_agentic(
    *,
    project_id: int,
    node_id: str,
    req: AgenticActionApiRequest,
) -> dict[str, object]:
    result = _application().run_agentic_action(
        AgenticActionRequest(
            project_id=project_id,
            graph_version=req.graph_version,
            fact_fingerprint=req.expected_fact_fingerprint,
            decision_fingerprint=req.expected_decision_fingerprint,
            node_id=node_id,
            instance_key=req.instance_key,
            input_payload=req.input_payload,
            model_id=req.model_id or get_model_id(_AGENTIC_MODEL_KEYS[node_id]),
            idempotency_key=req.idempotency_key,
            actor=req.changed_by,
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
    req: AgenticActionApiRequest,
) -> dict[str, object]:
    """Compile authority through the graph recipe."""
    return _run_agentic(project_id=project_id, node_id="authority.compile", req=req)


@app.post("/api/projects/{project_id}/authority/repair")
def repair_project_authority(
    project_id: int,
    req: AgenticActionApiRequest,
) -> dict[str, object]:
    """Repair rejected authority through the graph recipe."""
    return _run_agentic(project_id=project_id, node_id="authority.repair", req=req)


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
        request = build_positioned_transition_request(project_id, request_kind, req)
        return _result_payload(_application().transition(request))

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
