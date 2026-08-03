"""Parser-backed regressions for production compiler remediation commands."""

from __future__ import annotations

import shlex

from cli.main import build_parser
from models.specs import SpecRegistry
from services.specs.compiler_service import (
    _source_metadata_retry_commands,
    compiled_authority_schema_unsupported_remediation,
)


def _assert_workflow_next_parses(command: str, *, project_id: int) -> None:
    parser = build_parser()
    arguments = parser.parse_args(shlex.split(command)[1:])
    assert arguments.workflow_action == "next"
    assert arguments.project_id == project_id


def test_all_compiler_remediation_uses_the_registered_workflow_next_command() -> None:
    """Keep compiler recovery orientation owned by the live workflow graph."""
    project_id = 17
    commands = [
        *compiled_authority_schema_unsupported_remediation(
            project_id=project_id,
            spec_version_id=None,
        ),
        *compiled_authority_schema_unsupported_remediation(
            project_id=project_id,
            spec_version_id=23,
        ),
        *_source_metadata_retry_commands(
            SpecRegistry(
                project_id=project_id,
                spec_hash="a" * 64,
                content="# Current spec",
                status="approved",
            )
        ),
    ]

    assert commands
    assert set(commands) == {f"agileforge workflow next --project-id {project_id}"}
    for command in commands:
        _assert_workflow_next_parses(command, project_id=project_id)
