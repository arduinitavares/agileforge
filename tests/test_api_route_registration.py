"""API tests for sprint route registration helpers."""

from fastapi import FastAPI
from fastapi.routing import APIRoute

import api as api_module
from routers.sprint import register_manual_sprint_routes, register_sprint_routes


def test_manual_sprint_execution_and_packet_routes_are_registered() -> None:
    """Confirm the API module exposes the manual sprint routes."""
    routes = {
        (route.path, tuple(sorted(route.methods or ())))
        for route in api_module.app.routes
        if isinstance(route, APIRoute)
    }

    expected = {
        ("/api/projects/{project_id}/sprints/{sprint_id}/close", ("GET",)),
        ("/api/projects/{project_id}/sprints/{sprint_id}/close", ("POST",)),
        (
            "/api/projects/{project_id}/sprints/{sprint_id}/tasks/{task_id}/packet",
            ("GET",),
        ),
        (
            "/api/projects/{project_id}/sprints/{sprint_id}/stories/{story_id}/packet",
            ("GET",),
        ),
        (
            "/api/projects/{project_id}/sprints/{sprint_id}/tasks/{task_id}/execution",
            ("GET",),
        ),
        (
            "/api/projects/{project_id}/sprints/{sprint_id}/tasks/{task_id}/execution",
            ("POST",),
        ),
        (
            "/api/projects/{project_id}/sprints/{sprint_id}/stories/{story_id}/close",
            ("GET",),
        ),
        (
            "/api/projects/{project_id}/sprints/{sprint_id}/stories/{story_id}/close",
            ("POST",),
        ),
    }

    missing = expected - routes
    assert not missing, f"Missing route registrations: {sorted(missing)}"


async def _async_stub(**_kwargs: object) -> dict[str, str]:
    """Return a simple success payload for async route handlers."""
    return {"status": "success"}


def _sync_stub(**_kwargs: object) -> dict[str, str]:
    """Return a simple success payload for sync route handlers."""
    return {"status": "success"}


def test_sprint_router_api_app_registers_metrics_route() -> None:
    """Confirm the API module exposes the Sprint metrics route."""
    routes = {
        (route.path, tuple(sorted(route.methods or ())))
        for route in api_module.app.routes
        if isinstance(route, APIRoute)
    }

    assert (
        "/api/projects/{project_id}/sprint/metrics",
        ("GET",),
    ) in routes


def test_manual_sprint_router_module_registers_routes() -> None:
    """Confirm the manual sprint router helper registers its route set."""
    app = FastAPI()

    register_manual_sprint_routes(
        app,
        get_sprint_close=_sync_stub,
        post_sprint_close=_sync_stub,
        get_project_task_packet=_async_stub,
        get_project_story_packet=_async_stub,
        get_task_execution=_sync_stub,
        post_task_execution=_sync_stub,
        get_story_close=_sync_stub,
        post_story_close=_sync_stub,
    )

    routes = {
        (route.path, tuple(sorted(route.methods or ())))
        for route in app.routes
        if isinstance(route, APIRoute)
    }

    expected = {
        ("/api/projects/{project_id}/sprints/{sprint_id}/close", ("GET",)),
        ("/api/projects/{project_id}/sprints/{sprint_id}/close", ("POST",)),
        (
            "/api/projects/{project_id}/sprints/{sprint_id}/tasks/{task_id}/packet",
            ("GET",),
        ),
        (
            "/api/projects/{project_id}/sprints/{sprint_id}/stories/{story_id}/packet",
            ("GET",),
        ),
        (
            "/api/projects/{project_id}/sprints/{sprint_id}/tasks/{task_id}/execution",
            ("GET",),
        ),
        (
            "/api/projects/{project_id}/sprints/{sprint_id}/tasks/{task_id}/execution",
            ("POST",),
        ),
        (
            "/api/projects/{project_id}/sprints/{sprint_id}/stories/{story_id}/close",
            ("GET",),
        ),
        (
            "/api/projects/{project_id}/sprints/{sprint_id}/stories/{story_id}/close",
            ("POST",),
        ),
    }

    missing = expected - routes
    assert not missing, f"Missing router module registrations: {sorted(missing)}"


def test_sprint_router_module_registers_full_sprint_surface() -> None:
    """Confirm the full sprint router helper registers every sprint endpoint."""
    app = FastAPI()

    register_sprint_routes(
        app,
        get_project_sprint_candidates=_async_stub,
        generate_project_sprint=_async_stub,
        get_project_sprint_history=_async_stub,
        get_project_sprint_metrics=_async_stub,
        reset_project_sprint_planner=_async_stub,
        list_project_sprints=_async_stub,
        get_project_sprint=_async_stub,
        save_project_sprint=_async_stub,
        start_project_sprint=_async_stub,
        get_sprint_close=_sync_stub,
        post_sprint_close=_sync_stub,
        get_project_task_packet=_async_stub,
        get_project_story_packet=_async_stub,
        get_task_execution=_sync_stub,
        post_task_execution=_sync_stub,
        get_story_close=_sync_stub,
        post_story_close=_sync_stub,
    )

    routes = {
        (route.path, tuple(sorted(route.methods or ())))
        for route in app.routes
        if isinstance(route, APIRoute)
    }

    expected = {
        ("/api/projects/{project_id}/sprint/candidates", ("GET",)),
        ("/api/projects/{project_id}/sprint/generate", ("POST",)),
        ("/api/projects/{project_id}/sprint/history", ("GET",)),
        ("/api/projects/{project_id}/sprint/metrics", ("GET",)),
        ("/api/projects/{project_id}/sprint/planner/reset", ("POST",)),
        ("/api/projects/{project_id}/sprint/save", ("POST",)),
        ("/api/projects/{project_id}/sprints", ("GET",)),
        ("/api/projects/{project_id}/sprints/{sprint_id}", ("GET",)),
        ("/api/projects/{project_id}/sprints/{sprint_id}/start", ("PATCH",)),
        ("/api/projects/{project_id}/sprints/{sprint_id}/close", ("GET",)),
        ("/api/projects/{project_id}/sprints/{sprint_id}/close", ("POST",)),
        (
            "/api/projects/{project_id}/sprints/{sprint_id}/tasks/{task_id}/packet",
            ("GET",),
        ),
        (
            "/api/projects/{project_id}/sprints/{sprint_id}/stories/{story_id}/packet",
            ("GET",),
        ),
        (
            "/api/projects/{project_id}/sprints/{sprint_id}/tasks/{task_id}/execution",
            ("GET",),
        ),
        (
            "/api/projects/{project_id}/sprints/{sprint_id}/tasks/{task_id}/execution",
            ("POST",),
        ),
        (
            "/api/projects/{project_id}/sprints/{sprint_id}/stories/{story_id}/close",
            ("GET",),
        ),
        (
            "/api/projects/{project_id}/sprints/{sprint_id}/stories/{story_id}/close",
            ("POST",),
        ),
    }

    missing = expected - routes
    assert not missing, f"Missing full sprint registrations: {sorted(missing)}"
