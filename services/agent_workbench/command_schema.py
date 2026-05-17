"""Command capability and schema payload builders."""

from typing import Any

from services.agent_workbench.command_registry import (
    CommandMetadata,
    installed_commands,
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
    """Return installed command capability metadata."""
    commands = [
        {
            "name": command.name,
            "command_version": command.command_version,
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
            "input": {
                "required": list(command.input_required),
                "optional": list(command.input_optional),
            },
            "errors": list(command.errors),
        }
        for command in installed_commands()
    ]
    return {
        "schema_version": CAPABILITIES_SCHEMA_VERSION,
        "command_version": COMMAND_VERSION,
        "storage_schema_version": STORAGE_SCHEMA_VERSION,
        "installed_command_count": len(commands),
        "commands": commands,
    }


def command_schema_payload(command_name: str) -> dict[str, Any]:
    """Return a stable command contract payload for a known command."""
    command = _command_metadata(command_name)
    errors = list(command.errors)
    contract = CommandContractSchema(
        name=command.name,
        command_version=command.command_version,
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
    """Return metadata for one installed command."""
    for command in installed_commands():
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
