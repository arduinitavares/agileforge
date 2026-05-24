"""Tests for agent workbench envelope and registry contracts."""

import pytest

import services.agent_workbench.envelope as envelope_module
from services.agent_workbench.command_registry import (
    command_is_available,
    installed_command_names,
    installed_commands,
)
from services.agent_workbench.envelope import (
    WorkbenchError,
    WorkbenchWarning,
    error_envelope,
    success_envelope,
)

UUID4_TEXT_LENGTH = 36
UUID4_HYPHEN_COUNT = 4

EXPECTED_PHASE_1_COMMAND_NAMES = {
    "agileforge status",
    "agileforge project list",
    "agileforge project show",
    "agileforge workflow state",
    "agileforge workflow next",
    "agileforge authority status",
    "agileforge authority invariants",
    "agileforge story show",
    "agileforge sprint candidates",
    "agileforge context pack",
}

EXPECTED_PHASE_2A_COMMAND_NAMES = {
    "agileforge doctor",
    "agileforge schema check",
    "agileforge capabilities",
    "agileforge command schema",
    "agileforge mutation show",
    "agileforge mutation list",
    "agileforge mutation resume",
}


def test_success_envelope_has_stable_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serialize success responses with stable top-level keys."""
    monkeypatch.setattr(envelope_module, "agileforge_version", lambda: "dev")

    envelope = success_envelope(
        command="agileforge project list",
        data={"items": []},
        warnings=[
            WorkbenchWarning(
                code="EMPTY_PROJECTS",
                message="No projects exist.",
                details={"count": 0},
                remediation=[
                    "agileforge project create --name Example --spec-file specs/app.md"
                ],
            )
        ],
        generated_at="2026-05-14T00:00:00Z",
        correlation_id="corr-123",
    )

    assert envelope == {
        "ok": True,
        "data": {"items": []},
        "warnings": [
            {
                "code": "EMPTY_PROJECTS",
                "message": "No projects exist.",
                "details": {"count": 0},
                "remediation": [
                    "agileforge project create --name Example --spec-file specs/app.md"
                ],
            }
        ],
        "errors": [],
        "meta": {
            "schema_version": "agileforge.cli.v1",
            "command": "agileforge project list",
            "command_version": "1",
            "agileforge_version": "dev",
            "storage_schema_version": envelope_module.STORAGE_SCHEMA_VERSION,
            "generated_at": "2026-05-14T00:00:00Z",
            "correlation_id": "corr-123",
        },
    }


def test_error_envelope_has_retryable_exit_code_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Serialize a singular command error with warning context."""
    monkeypatch.setattr(envelope_module, "agileforge_version", lambda: "dev")

    envelope = error_envelope(
        command="agileforge sprint candidates",
        error=WorkbenchError(
            code="PROJECT_NOT_FOUND",
            message="Project does not exist.",
            details={"project_id": "missing"},
            remediation=["agileforge project list"],
            exit_code=2,
            retryable=True,
        ),
        warnings=[WorkbenchWarning(code="STALE_INDEX", message="Index is stale.")],
        generated_at="2026-05-14T00:00:00Z",
        command_version="2",
        correlation_id="corr-456",
    )

    assert envelope == {
        "ok": False,
        "data": None,
        "warnings": [
            {
                "code": "STALE_INDEX",
                "message": "Index is stale.",
                "details": {},
                "remediation": [],
            }
        ],
        "errors": [
            {
                "code": "PROJECT_NOT_FOUND",
                "message": "Project does not exist.",
                "details": {"project_id": "missing"},
                "remediation": ["agileforge project list"],
                "exit_code": 2,
                "retryable": True,
            }
        ],
        "meta": {
            "schema_version": "agileforge.cli.v1",
            "command": "agileforge sprint candidates",
            "command_version": "2",
            "agileforge_version": "dev",
            "storage_schema_version": envelope_module.STORAGE_SCHEMA_VERSION,
            "generated_at": "2026-05-14T00:00:00Z",
            "correlation_id": "corr-456",
        },
    }


def test_success_envelope_generates_default_correlation_id() -> None:
    """Generate a UUID4 correlation ID when callers do not supply one."""
    envelope = success_envelope(
        command="agileforge status",
        data={},
        generated_at="2026-05-14T00:00:00Z",
    )

    correlation_id = envelope["meta"]["correlation_id"]

    assert isinstance(correlation_id, str)
    assert len(correlation_id) == UUID4_TEXT_LENGTH
    assert correlation_id.count("-") == UUID4_HYPHEN_COUNT


def test_success_envelope_includes_optional_source_fingerprint() -> None:
    """Include source fingerprint metadata only when callers supply it."""
    source_fingerprint = "sha256:" + "a" * 64

    envelope = success_envelope(
        command="agileforge status",
        data={},
        generated_at="2026-05-14T00:00:00Z",
        correlation_id="corr-source",
        source_fingerprint=source_fingerprint,
    )
    envelope_without_source = success_envelope(
        command="agileforge status",
        data={},
        generated_at="2026-05-14T00:00:00Z",
        correlation_id="corr-no-source",
    )

    assert envelope["meta"]["source_fingerprint"] == source_fingerprint
    assert "source_fingerprint" not in envelope_without_source["meta"]


def test_problem_to_dict_returns_shallow_copies() -> None:
    """Keep dataclass internals isolated from serialized payload mutation."""
    warning = WorkbenchWarning(
        code="WARN",
        message="Warning.",
        details={"count": 1},
        remediation=["retry"],
    )
    error = WorkbenchError(
        code="ERR",
        message="Error.",
        details={"count": 2},
        remediation=["fix"],
    )

    warning_payload = warning.to_dict()
    error_payload = error.to_dict()

    assert warning_payload["details"] == {"count": 1}
    assert warning_payload["details"] is not warning.details
    assert warning_payload["remediation"] == ["retry"]
    assert warning_payload["remediation"] is not warning.remediation
    assert error_payload["details"] == {"count": 2}
    assert error_payload["details"] is not error.details
    assert error_payload["remediation"] == ["fix"]
    assert error_payload["remediation"] is not error.remediation


def test_registry_exposes_phase_1_and_phase_2a_commands() -> None:
    """Expose current installed command names."""
    names = installed_command_names()

    assert isinstance(names, set)
    assert EXPECTED_PHASE_1_COMMAND_NAMES.issubset(names)
    assert EXPECTED_PHASE_2A_COMMAND_NAMES.issubset(names)
    assert command_is_available("agileforge sprint candidates") is True
    assert command_is_available("agileforge sprint generate") is True
    assert command_is_available("agileforge context pack") is True
    assert command_is_available("agileforge capabilities") is True


def test_registry_metadata_has_phase_1_shape() -> None:
    """Keep registry metadata stable for every installed Phase 1 command."""
    commands = [
        command
        for command in installed_commands()
        if command.name in EXPECTED_PHASE_1_COMMAND_NAMES
    ]

    assert commands
    assert {command.name for command in commands} == EXPECTED_PHASE_1_COMMAND_NAMES
    assert all(command.phase == "phase_1" for command in commands)
    assert all(command.mutates is False for command in commands)
