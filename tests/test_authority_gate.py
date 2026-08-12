# tests/test_authority_gate.py
"""Tests for the independent human Authority acceptance gate."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from models.core import Project
from models.specs import CompiledSpecAuthority, SpecAuthorityAcceptance, SpecRegistry
from services.contracts.specification import (
    SPEC_AUTHORITY_COMPILER_PROMPT_HASH,
    SPEC_AUTHORITY_COMPILER_VERSION,
)
from services.specs.authority_selection import pending_authority_fingerprint
from services.specs.compiler_service import SpecAuthorityGateError
from tests.typing_helpers import require_id
from tests.workflow.lifecycle_fixtures import seed_accepted_specification
from tools import spec_tools
from utils.spec_schemas import (
    SpecAuthorityCompilationFailure,
    SpecAuthorityCompilationSuccess,
    SpecAuthorityCompilerOutput,
)

if TYPE_CHECKING:
    from google.adk.tools import ToolContext
    from sqlmodel import Session


@pytest.fixture
def sample_project(session: Session) -> Project:
    """Create one project for Authority gate tests."""
    project = Project(
        name="Authority Gate Project",
        description="Project for Authority gate tests",
        vision="Keep Authority review explicit",
    )
    session.add(project)
    session.commit()
    session.refresh(project)
    return project


def _success_artifact_json() -> str:
    """Return one valid current compiled-Authority envelope."""
    success = SpecAuthorityCompilationSuccess(
        scope_themes=["Scope"],
        invariants=[],
        eligible_feature_rules=[],
        gaps=[],
        assumptions=[],
        source_map=[],
        compiler_version=SPEC_AUTHORITY_COMPILER_VERSION,
        prompt_hash=SPEC_AUTHORITY_COMPILER_PROMPT_HASH,
    )
    return SpecAuthorityCompilerOutput(root=success).model_dump_json()


def _failure_artifact_json() -> str:
    """Return one valid current compiler-failure envelope."""
    failure = SpecAuthorityCompilationFailure(
        error="COMPILATION_FAILED",
        reason="Specification lacks mandatory detail.",
        blocking_gaps=["Missing testable requirement"],
    )
    return SpecAuthorityCompilerOutput(root=failure).model_dump_json()


def _seed_compiled_authority(
    session: Session,
    *,
    project_id: int,
    artifact_json: str | None = None,
    accepted: bool,
) -> tuple[SpecRegistry, CompiledSpecAuthority]:
    """Persist exact v2 candidate/registry lineage plus one Authority row."""
    spec = seed_accepted_specification(
        session,
        project_id=project_id,
        content=json.dumps({"title": "Authority gate fixture"}),
    ).spec
    authority = CompiledSpecAuthority(
        spec_version_id=require_id(spec.spec_version_id, "spec_version_id"),
        compiler_version=SPEC_AUTHORITY_COMPILER_VERSION,
        prompt_hash=SPEC_AUTHORITY_COMPILER_PROMPT_HASH,
        compiled_at=datetime.now(UTC),
        compiled_artifact_json=artifact_json or _success_artifact_json(),
        scope_themes=json.dumps(["Scope"]),
        invariants="[]",
        eligible_feature_ids="[]",
        rejected_features="[]",
        spec_gaps="[]",
    )
    session.add(authority)
    session.commit()
    session.refresh(authority)

    if accepted:
        session.add(
            SpecAuthorityAcceptance(
                project_id=project_id,
                spec_version_id=require_id(
                    spec.spec_version_id,
                    "spec_version_id",
                ),
                status="accepted",
                policy="manual",
                decided_by="reviewer",
                decided_at=datetime.now(UTC),
                rationale="Accepted after separate human review.",
                compiler_version=authority.compiler_version,
                prompt_hash=authority.prompt_hash,
                spec_hash=spec.spec_hash,
                pending_authority_id=authority.authority_id,
                authority_fingerprint=pending_authority_fingerprint(authority),
            )
        )
        session.commit()

    return spec, authority


def test_tool_adapter_preserves_current_gate_arguments(
    sample_project: Project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tool adapter delegates without accepting raw Specification content."""
    captured: dict[str, object] = {}

    def fake_gate(
        project_id: int,
        *,
        recompile: bool = False,
        tool_context: ToolContext | None = None,
    ) -> int:
        captured.update(
            project_id=project_id,
            recompile=recompile,
            tool_context=tool_context,
        )
        return 321

    monkeypatch.setattr(spec_tools, "_ensure_accepted_spec_authority", fake_gate)

    result = spec_tools.ensure_accepted_spec_authority(
        require_id(sample_project.project_id, "project_id"),
        recompile=True,
    )

    assert result == 321  # noqa: PLR2004
    assert captured == {
        "project_id": require_id(sample_project.project_id, "project_id"),
        "recompile": True,
        "tool_context": None,
    }


def test_gate_returns_exact_separately_accepted_spec_version(
    session: Session,
    sample_project: Project,
) -> None:
    """An accepted Authority unlocks only its exact approved v2 registry row."""
    spec, _authority = _seed_compiled_authority(
        session,
        project_id=require_id(sample_project.project_id, "project_id"),
        accepted=True,
    )

    result = spec_tools.ensure_accepted_spec_authority(
        require_id(sample_project.project_id, "project_id")
    )

    assert result == require_id(spec.spec_version_id, "spec_version_id")
    assert spec.source_specification_candidate_id is not None
    assert spec.source_specification_candidate_fingerprint is not None


def test_compiled_but_unaccepted_authority_stays_at_human_review_gate(
    session: Session,
    sample_project: Project,
) -> None:
    """Compilation alone never substitutes for the separate human decision."""
    project_id = require_id(sample_project.project_id, "project_id")
    _seed_compiled_authority(
        session,
        project_id=project_id,
        accepted=False,
    )

    with pytest.raises(SpecAuthorityGateError, match="separate Authority review"):
        spec_tools.ensure_accepted_spec_authority(project_id, recompile=True)


def test_missing_authority_stays_at_human_review_gate(
    sample_project: Project,
) -> None:
    """The gate orients recovery through workflow review, not raw uploads."""
    project_id = require_id(sample_project.project_id, "project_id")

    with pytest.raises(SpecAuthorityGateError) as exc_info:
        spec_tools.ensure_accepted_spec_authority(project_id)

    assert f"workflow next --project-id {project_id}" in str(exc_info.value)


def test_accepted_compiler_failure_does_not_bypass_review_gate(
    session: Session,
    sample_project: Project,
) -> None:
    """A terminal acceptance cannot turn compiler failure bytes into Authority."""
    project_id = require_id(sample_project.project_id, "project_id")
    _seed_compiled_authority(
        session,
        project_id=project_id,
        artifact_json=_failure_artifact_json(),
        accepted=True,
    )

    with pytest.raises(SpecAuthorityGateError, match="separate Authority review"):
        spec_tools.ensure_accepted_spec_authority(project_id)
