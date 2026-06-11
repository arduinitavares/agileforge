"""Transitional sprint route registration.

This module keeps route declarations out of ``api.py`` while the handler
implementations still live there during the first extraction phase.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypedDict, Unpack

if TYPE_CHECKING:
    from fastapi import FastAPI

Handler = Callable[..., Any]


class _ManualSprintRouteHandlers(TypedDict):
    get_sprint_close: Handler
    post_sprint_close: Handler
    get_project_task_packet: Handler
    get_project_story_packet: Handler
    get_task_execution: Handler
    post_task_execution: Handler
    get_story_close: Handler
    post_story_close: Handler


class _SprintRouteHandlers(_ManualSprintRouteHandlers):
    get_project_sprint_candidates: Handler
    generate_project_sprint: Handler
    get_project_sprint_history: Handler
    get_project_sprint_metrics: Handler
    reset_project_sprint_planner: Handler
    list_project_sprints: Handler
    get_project_sprint: Handler
    save_project_sprint: Handler
    start_project_sprint: Handler


def register_manual_sprint_routes(
    app: FastAPI,
    **handlers: Unpack[_ManualSprintRouteHandlers],
) -> None:
    """Register the manual sprint close/execution/packet routes."""
    app.add_api_route(
        "/api/projects/{project_id}/sprints/{sprint_id}/close",
        handlers["get_sprint_close"],
        methods=["GET"],
    )
    app.add_api_route(
        "/api/projects/{project_id}/sprints/{sprint_id}/close",
        handlers["post_sprint_close"],
        methods=["POST"],
    )
    app.add_api_route(
        "/api/projects/{project_id}/sprints/{sprint_id}/tasks/{task_id}/packet",
        handlers["get_project_task_packet"],
        methods=["GET"],
    )
    app.add_api_route(
        "/api/projects/{project_id}/sprints/{sprint_id}/stories/{story_id}/packet",
        handlers["get_project_story_packet"],
        methods=["GET"],
    )
    app.add_api_route(
        "/api/projects/{project_id}/sprints/{sprint_id}/tasks/{task_id}/execution",
        handlers["get_task_execution"],
        methods=["GET"],
    )
    app.add_api_route(
        "/api/projects/{project_id}/sprints/{sprint_id}/tasks/{task_id}/execution",
        handlers["post_task_execution"],
        methods=["POST"],
    )
    app.add_api_route(
        "/api/projects/{project_id}/sprints/{sprint_id}/stories/{story_id}/close",
        handlers["get_story_close"],
        methods=["GET"],
    )
    app.add_api_route(
        "/api/projects/{project_id}/sprints/{sprint_id}/stories/{story_id}/close",
        handlers["post_story_close"],
        methods=["POST"],
    )


def register_sprint_routes(
    app: FastAPI,
    **handlers: Unpack[_SprintRouteHandlers],
) -> None:
    """Register the full sprint HTTP surface for the transitional router split."""
    app.add_api_route(
        "/api/projects/{project_id}/sprint/candidates",
        handlers["get_project_sprint_candidates"],
        methods=["GET"],
    )
    app.add_api_route(
        "/api/projects/{project_id}/sprint/generate",
        handlers["generate_project_sprint"],
        methods=["POST"],
    )
    app.add_api_route(
        "/api/projects/{project_id}/sprint/history",
        handlers["get_project_sprint_history"],
        methods=["GET"],
    )
    app.add_api_route(
        "/api/projects/{project_id}/sprint/metrics",
        handlers["get_project_sprint_metrics"],
        methods=["GET"],
    )
    app.add_api_route(
        "/api/projects/{project_id}/sprint/planner/reset",
        handlers["reset_project_sprint_planner"],
        methods=["POST"],
    )
    app.add_api_route(
        "/api/projects/{project_id}/sprints",
        handlers["list_project_sprints"],
        methods=["GET"],
    )
    app.add_api_route(
        "/api/projects/{project_id}/sprints/{sprint_id}",
        handlers["get_project_sprint"],
        methods=["GET"],
    )
    app.add_api_route(
        "/api/projects/{project_id}/sprint/save",
        handlers["save_project_sprint"],
        methods=["POST"],
    )
    app.add_api_route(
        "/api/projects/{project_id}/sprints/{sprint_id}/start",
        handlers["start_project_sprint"],
        methods=["PATCH"],
    )
    register_manual_sprint_routes(
        app,
        get_sprint_close=handlers["get_sprint_close"],
        post_sprint_close=handlers["post_sprint_close"],
        get_project_task_packet=handlers["get_project_task_packet"],
        get_project_story_packet=handlers["get_project_story_packet"],
        get_task_execution=handlers["get_task_execution"],
        post_task_execution=handlers["post_task_execution"],
        get_story_close=handlers["get_story_close"],
        post_story_close=handlers["post_story_close"],
    )
