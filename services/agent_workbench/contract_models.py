"""Pydantic models for agent workbench command contract schemas."""

from typing import Any

from pydantic import BaseModel, ConfigDict


class CommandInputSchema(BaseModel):
    """Input fields accepted by a command."""

    model_config = ConfigDict(extra="forbid")

    required: list[str]
    optional: list[str]
    option_count: int
    options: list[dict[str, Any]]


class CommandOutputSchema(BaseModel):
    """Output schemas returned by a command."""

    model_config = ConfigDict(extra="forbid")

    data_schema: dict[str, Any]
    envelope_schema: dict[str, Any]


class CommandContractSchema(BaseModel):
    """Stable contract documentation for one discoverable command."""

    model_config = ConfigDict(extra="forbid")

    name: str
    command_version: str
    installed: bool
    stable: bool
    mutates: bool
    destructive: bool
    input: CommandInputSchema
    output: CommandOutputSchema
    guard_policy: list[str]
    guard_policy_is_authoritative: bool
    legacy_guard_flags: dict[str, bool]
    idempotency_required: bool
    idempotency_policy: dict[str, str]
    errors: list[str]
    exit_codes: dict[str, int]
