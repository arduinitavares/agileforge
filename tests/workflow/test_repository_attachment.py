"""Repository binding workflow request contracts."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from services.repository_probe import RepositoryProbeResult
from workflow.requests import RecordRepositoryBinding, RepositoryBindingInput

_REPOSITORY_PATH = "repository"


def test_repository_binding_request_has_no_decision_fingerprint() -> None:
    """Keep repository attachment outside graph decision guards."""
    probe = RepositoryProbeResult(
        worktree_path=_REPOSITORY_PATH,
        common_git_dir=f"{_REPOSITORY_PATH}/.git",
        head_sha="a" * 40,
        branch_name="main",
        detached_head=False,
        dirty=False,
        status_entries=(),
        status_fingerprint="status-1",
        remotes=(),
        probe_version="agileforge.repository-probe.v1",
        inspected_at=datetime(2026, 8, 9, 12, tzinfo=UTC),
        warnings=(),
    )

    binding = RepositoryBindingInput.from_probe(
        probe,
        recorded_by="operator@example.com",
    )
    request = RecordRepositoryBinding(
        project_id=1,
        operation="attach",
        requested_repository_path=_REPOSITORY_PATH,
        graph_version="agileforge.workflow.v2",
        fact_fingerprint="fact-1",
        expected_active_binding_fingerprint=None,
        binding=binding,
        idempotency_key="attach-1",
        actor="operator@example.com",
    )

    assert "decision_fingerprint" not in request.model_dump()
    assert request.binding.worktree_path == _REPOSITORY_PATH

    payload = request.model_dump()
    payload["requested_repository_path"] = None
    with pytest.raises(ValidationError):
        RecordRepositoryBinding.model_validate(payload)

    payload["operation"] = "refresh"
    with pytest.raises(ValidationError):
        RecordRepositoryBinding.model_validate(payload)
