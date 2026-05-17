"""Command capability and schema payload builders."""

from typing import Any

from services.agent_workbench.command_registry import (
    CommandMetadata,
    command_contracts,
)
from services.agent_workbench.contract_models import (
    CommandContractSchema,
    CommandInputSchema,
    CommandOutputSchema,
)
from services.agent_workbench.error_codes import error_metadata
from services.agent_workbench.version import COMMAND_VERSION, STORAGE_SCHEMA_VERSION

CAPABILITIES_SCHEMA_VERSION: str = "agileforge.cli.capabilities.v1"


def capabilities_payload() -> dict[str, Any]:
    """Return command capability metadata for installed and future contracts."""
    commands = [
        {
            "name": command.name,
            "command_version": command.command_version,
            "installed": command.installed,
            "phase": command.phase,
            "stable": command.stable,
            "mutates": command.mutates,
            "destructive": command.destructive,
            "requires_idempotency_key": command.requires_idempotency_key,
            "idempotency_policy": command.idempotency_policy,
            "accepts_expected_state": command.accepts_expected_state,
            "accepts_expected_artifact_fingerprint": (
                command.accepts_expected_artifact_fingerprint
            ),
            "accepts_expected_context_fingerprint": (
                command.accepts_expected_context_fingerprint
            ),
            "accepts_expected_authority_version": (
                command.accepts_expected_authority_version
            ),
            "guard_policy": _guard_policy(command),
            "guard_policy_is_authoritative": _guard_policy_is_authoritative(command),
            "legacy_guard_flags": _legacy_guard_flags(command),
            "input": {
                "required": list(command.input_required),
                "optional": list(command.input_optional),
            },
            "errors": list(command.errors),
        }
        for command in command_contracts()
    ]
    return {
        "schema_version": CAPABILITIES_SCHEMA_VERSION,
        "command_version": COMMAND_VERSION,
        "storage_schema_version": STORAGE_SCHEMA_VERSION,
        "installed_command_count": sum(
            1 for command in commands if command["installed"] is True
        ),
        "commands": commands,
    }


def command_schema_payload(command_name: str) -> dict[str, Any]:
    """Return a stable command contract payload for a known command."""
    command = _command_metadata(command_name)
    errors = list(command.errors)
    contract = CommandContractSchema(
        name=command.name,
        command_version=command.command_version,
        installed=command.installed,
        stable=command.stable,
        mutates=command.mutates,
        destructive=command.destructive,
        input=CommandInputSchema(
            required=list(command.input_required),
            optional=list(command.input_optional),
        ),
        output=CommandOutputSchema(
            data_schema={"type": "object"},
            envelope_schema=_envelope_schema(),
        ),
        guard_policy=_guard_policy(command),
        guard_policy_is_authoritative=_guard_policy_is_authoritative(command),
        legacy_guard_flags=_legacy_guard_flags(command),
        idempotency_required=command.requires_idempotency_key,
        idempotency_policy=command.idempotency_policy,
        errors=errors,
        exit_codes={
            error_code: error_metadata(error_code).default_exit_code
            for error_code in errors
        },
    )
    return contract.model_dump(mode="python")


def _command_metadata(command_name: str) -> CommandMetadata:
    """Return metadata for one discoverable command contract."""
    for command in command_contracts():
        if command.name == command_name:
            return command
    msg = f"Unknown command: {command_name}"
    raise ValueError(msg)


def _guard_policy(command: CommandMetadata) -> list[str]:
    """Return enabled guard field names for a command contract."""
    if command.guard_policy:
        return list(command.guard_policy)

    guard_fields = [
        ("expected_state", command.accepts_expected_state),
        (
            "expected_artifact_fingerprint",
            command.accepts_expected_artifact_fingerprint,
        ),
        (
            "expected_context_fingerprint",
            command.accepts_expected_context_fingerprint,
        ),
        (
            "expected_authority_version",
            command.accepts_expected_authority_version,
        ),
    ]
    return [name for name, enabled in guard_fields if enabled]


def _guard_policy_is_authoritative(command: CommandMetadata) -> bool:
    """Return whether guard_policy supersedes legacy guard booleans."""
    return bool(command.guard_policy)


def _legacy_guard_flags(command: CommandMetadata) -> dict[str, bool]:
    """Return legacy guard booleans grouped to avoid confusing partial coverage."""
    return {
        "accepts_expected_state": command.accepts_expected_state,
        "accepts_expected_artifact_fingerprint": (
            command.accepts_expected_artifact_fingerprint
        ),
        "accepts_expected_context_fingerprint": (
            command.accepts_expected_context_fingerprint
        ),
        "accepts_expected_authority_version": (
            command.accepts_expected_authority_version
        ),
    }


def _envelope_schema() -> dict[str, Any]:
    """Return the shared CLI envelope schema skeleton."""
    return {
        "type": "object",
        "required": ["ok", "data", "warnings", "errors", "meta"],
        "properties": {
            "ok": {"type": "boolean"},
            "data": {},
            "warnings": {"type": "array"},
            "errors": {"type": "array"},
            "meta": {
                "type": "object",
                "required": [
                    "schema_version",
                    "command",
                    "command_version",
                    "agileforge_version",
                    "storage_schema_version",
                    "generated_at",
                    "correlation_id",
                ],
            },
        },
    }
