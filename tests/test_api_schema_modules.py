"""Boundary tests for extracted API-facing schema modules."""

from __future__ import annotations

from services import story_close_service, task_execution_service
from tests import legacy_api as api_module
from utils import api_schemas, schemes


def test_api_schema_module_exports_task_and_close_models() -> None:
    """Confirm the API schema module exports the task and close models."""
    assert api_schemas.TaskExecutionWriteRequest.__module__ == "utils.api_schemas"
    assert api_schemas.TaskExecutionLogEntry.__module__ == "utils.api_schemas"
    assert api_schemas.TaskExecutionReadResponse.__module__ == "utils.api_schemas"
    assert api_schemas.StoryTaskProgressSummary.__module__ == "utils.api_schemas"
    assert api_schemas.StoryCloseReadResponse.__module__ == "utils.api_schemas"
    assert api_schemas.StoryCloseWriteRequest.__module__ == "utils.api_schemas"
    assert api_schemas.SprintCloseStorySummary.__module__ == "utils.api_schemas"
    assert api_schemas.SprintCloseReadiness.__module__ == "utils.api_schemas"
    assert api_schemas.SprintCloseReadResponse.__module__ == "utils.api_schemas"
    assert api_schemas.SprintCloseWriteRequest.__module__ == "utils.api_schemas"

    assert schemes.TaskExecutionWriteRequest is api_schemas.TaskExecutionWriteRequest
    assert schemes.TaskExecutionLogEntry is api_schemas.TaskExecutionLogEntry
    assert schemes.TaskExecutionReadResponse is api_schemas.TaskExecutionReadResponse
    assert schemes.StoryTaskProgressSummary is api_schemas.StoryTaskProgressSummary
    assert schemes.StoryCloseReadResponse is api_schemas.StoryCloseReadResponse
    assert schemes.StoryCloseWriteRequest is api_schemas.StoryCloseWriteRequest
    assert schemes.SprintCloseStorySummary is api_schemas.SprintCloseStorySummary
    assert schemes.SprintCloseReadiness is api_schemas.SprintCloseReadiness
    assert schemes.SprintCloseReadResponse is api_schemas.SprintCloseReadResponse
    assert schemes.SprintCloseWriteRequest is api_schemas.SprintCloseWriteRequest


def test_services_import_api_schema_module_boundary() -> None:
    """Confirm API-facing services import models from the extracted schema module."""
    assert (
        story_close_service.StoryTaskProgressSummary.__module__ == "utils.api_schemas"
    )
    assert (
        task_execution_service.TaskExecutionLogEntry.__module__ == "utils.api_schemas"
    )
    assert api_module.SprintCloseReadiness.__module__ == "utils.api_schemas"
    assert api_module.SprintCloseStorySummary.__module__ == "utils.api_schemas"
