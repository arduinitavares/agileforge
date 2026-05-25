"""Tests for agent workbench command schema contracts."""

from services.agent_workbench.command_registry import (
    CommandMetadata,
    command_is_available,
    installed_command_names,
    installed_commands,
)
from services.agent_workbench.command_schema import (
    _guard_policy,
    capabilities_payload,
    command_schema_payload,
)
from services.agent_workbench.error_codes import ErrorCode, error_metadata
from services.agent_workbench.version import COMMAND_VERSION, STORAGE_SCHEMA_VERSION

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

EXPECTED_PHASE_2B_COMMAND_NAMES = {
    "agileforge project create",
    "agileforge project setup retry",
}

EXPECTED_PHASE_2C_COMMAND_NAMES = {
    "agileforge authority review",
    "agileforge authority accept",
    "agileforge authority reject",
}

EXPECTED_PHASE_2D_COMMAND_NAMES = {
    "agileforge vision generate",
    "agileforge vision history",
    "agileforge vision save",
    "agileforge backlog generate",
    "agileforge backlog history",
    "agileforge backlog save",
    "agileforge backlog reconcile",
    "agileforge roadmap generate",
    "agileforge roadmap history",
    "agileforge roadmap save",
    "agileforge story pending",
    "agileforge story generate",
    "agileforge story retry",
    "agileforge story history",
    "agileforge story save",
    "agileforge story complete",
    "agileforge story reopen",
    "agileforge story repair-readiness",
    "agileforge story dependencies inspect",
    "agileforge story dependencies propose",
    "agileforge story dependencies apply",
    "agileforge sprint generate",
    "agileforge sprint history",
    "agileforge sprint save",
}

EXPECTED_PHASE_2E_COMMAND_NAMES = {
    "agileforge spec profile schema",
    "agileforge spec profile validate",
}

EXPECTED_PHASE_1_INPUTS = {
    "agileforge status": (["project_id"], []),
    "agileforge project list": ([], []),
    "agileforge project show": (["project_id"], []),
    "agileforge workflow state": (["project_id"], []),
    "agileforge workflow next": (["project_id"], []),
    "agileforge authority status": (["project_id"], []),
    "agileforge authority invariants": (["project_id"], ["spec_version_id"]),
    "agileforge story show": (["story_id"], []),
    "agileforge sprint candidates": (["project_id"], []),
    "agileforge context pack": (["project_id"], ["phase"]),
}

DRY_RUN_IDEMPOTENCY_POLICY = {
    "non_dry_run": "required",
    "dry_run": "forbidden",
    "dry_run_trace_field": "dry_run_id",
}


def _capability_by_name() -> dict[str, dict[str, object]]:
    """Return capabilities keyed by command name."""
    payload = capabilities_payload()
    commands = payload["commands"]

    assert isinstance(commands, list)
    return {str(command["name"]): command for command in commands}


def test_installed_commands_include_contract_metadata_for_phase_1() -> None:
    """Expose stable contract metadata for existing Phase 1 commands."""
    commands = {
        command.name: command
        for command in installed_commands()
        if command.name in EXPECTED_PHASE_1_COMMAND_NAMES
    }

    assert set(commands) == EXPECTED_PHASE_1_COMMAND_NAMES
    for command in commands.values():
        assert command.command_version == COMMAND_VERSION
        assert command.stable is True
        assert command.mutates is False
        assert command.destructive is False


def test_capabilities_expose_mutation_command_mutability() -> None:
    """Expose read-only versus mutating mutation commands."""
    commands = _capability_by_name()

    assert commands["agileforge mutation show"]["mutates"] is False
    assert commands["agileforge mutation list"]["mutates"] is False
    assert commands["agileforge mutation resume"]["mutates"] is True
    assert commands["agileforge mutation resume"]["destructive"] is False


def test_capabilities_include_top_level_contract_metadata() -> None:
    """Expose capabilities payload metadata useful to agents."""
    payload = capabilities_payload()
    commands = payload["commands"]

    assert isinstance(commands, list)
    assert payload["schema_version"] == "agileforge.cli.capabilities.v1"
    assert payload["command_version"] == COMMAND_VERSION
    assert payload["storage_schema_version"] == STORAGE_SCHEMA_VERSION
    assert payload["installed_command_count"] == sum(
        1 for command in commands if command["installed"] is True
    )


def test_phase_1_command_schema_payloads_publish_real_inputs() -> None:
    """Expose real Phase 1 CLI input contracts in command schemas."""
    for command_name, (required, optional) in EXPECTED_PHASE_1_INPUTS.items():
        payload = command_schema_payload(command_name)

        assert payload["input"]["required"] == required
        assert payload["input"]["optional"] == optional


def test_phase_1_capabilities_publish_real_inputs() -> None:
    """Expose real Phase 1 CLI input contracts in capabilities."""
    commands = _capability_by_name()

    for command_name, (required, optional) in EXPECTED_PHASE_1_INPUTS.items():
        assert commands[command_name]["input"] == {
            "required": required,
            "optional": optional,
        }


def test_command_schema_payload_describes_mutation_resume_contract() -> None:
    """Describe mutation resume inputs, errors, and envelope output."""
    payload = command_schema_payload("agileforge mutation resume")

    assert payload["name"] == "agileforge mutation resume"
    assert payload["command_version"] == COMMAND_VERSION
    assert payload["mutates"] is True
    assert payload["guard_policy"] == []
    assert payload["input"]["required"] == ["mutation_event_id"]
    assert payload["input"]["optional"] == ["correlation_id"]
    assert ErrorCode.SCHEMA_NOT_READY.value in payload["errors"]
    assert ErrorCode.MUTATION_NOT_FOUND.value in payload["errors"]
    assert ErrorCode.MUTATION_RESUME_CONFLICT.value in payload["errors"]
    assert ErrorCode.MUTATION_IN_PROGRESS.value not in payload["errors"]
    assert payload["output"]["envelope_schema"]["type"] == "object"


def test_command_schema_payload_describes_mutation_show_errors() -> None:
    """Describe reachable mutation inspection errors."""
    payload = command_schema_payload("agileforge mutation show")

    assert payload["errors"] == [
        ErrorCode.SCHEMA_NOT_READY.value,
        ErrorCode.MUTATION_NOT_FOUND.value,
    ]


def test_command_schema_exit_codes_match_error_registry() -> None:
    """Derive command schema exit codes from registered error metadata."""
    payload = command_schema_payload("agileforge mutation resume")

    assert payload["exit_codes"] == {
        ErrorCode.SCHEMA_NOT_READY.value: error_metadata(
            ErrorCode.SCHEMA_NOT_READY
        ).default_exit_code,
        ErrorCode.MUTATION_NOT_FOUND.value: error_metadata(
            ErrorCode.MUTATION_NOT_FOUND
        ).default_exit_code,
        ErrorCode.MUTATION_RESUME_CONFLICT.value: error_metadata(
            ErrorCode.MUTATION_RESUME_CONFLICT
        ).default_exit_code,
    }


def test_command_schema_guard_policy_lists_enabled_guard_fields() -> None:
    """Return only enabled guard field names in command schema contracts."""
    command = CommandMetadata(
        name="agileforge future guarded command",
        mutates=True,
        phase="phase_future",
        accepts_expected_state=True,
        accepts_expected_context_fingerprint=True,
    )

    assert _guard_policy(command) == [
        "expected_state",
        "expected_context_fingerprint",
    ]


def test_phase_2a_commands_are_registered_and_available() -> None:
    """Expose Phase 2A operational command names through the registry."""
    names = installed_command_names()

    assert EXPECTED_PHASE_2A_COMMAND_NAMES.issubset(names)
    for command_name in EXPECTED_PHASE_2A_COMMAND_NAMES:
        assert command_is_available(command_name) is True


def test_project_create_is_registered_as_mutating_idempotent_command() -> None:
    """Publish the project create mutation contract for agents."""
    project_create_schema = command_schema_payload("agileforge project create")

    assert project_create_schema["mutates"] is True
    assert project_create_schema["idempotency_required"] is True
    assert project_create_schema["idempotency_policy"] == DRY_RUN_IDEMPOTENCY_POLICY
    assert project_create_schema["input"]["required"] == ["name", "spec_file"]
    assert "idempotency_key" in project_create_schema["input"]["optional"]
    assert "dry_run" in project_create_schema["input"]["optional"]
    assert "dry_run_id" in project_create_schema["input"]["optional"]
    assert ErrorCode.PROJECT_ALREADY_EXISTS.value in project_create_schema["errors"]
    assert (
        ErrorCode.SPEC_SOURCE_FORMAT_UNSUPPORTED.value
        in project_create_schema["errors"]
    )
    assert ErrorCode.MUTATION_FAILED.value in project_create_schema["errors"]


def test_project_setup_retry_is_registered_as_guarded_mutation() -> None:
    """Publish the setup retry mutation contract for agents."""
    schema = command_schema_payload("agileforge project setup retry")

    assert schema["mutates"] is True
    assert schema["idempotency_required"] is True
    assert schema["idempotency_policy"] == DRY_RUN_IDEMPOTENCY_POLICY
    assert schema["guard_policy"] == [
        "expected_state",
        "expected_context_fingerprint",
    ]
    assert schema["input"]["required"] == [
        "project_id",
        "spec_file",
        "expected_state",
        "expected_context_fingerprint",
    ]
    assert "recovery_mutation_event_id" in schema["input"]["optional"]
    assert ErrorCode.MUTATION_FAILED.value in schema["errors"]
    assert ErrorCode.MUTATION_RESUME_CONFLICT.value in schema["errors"]


def test_phase_2b_commands_are_registered_and_available() -> None:
    """Expose Phase 2B project setup command names through the registry."""
    names = installed_command_names()

    assert EXPECTED_PHASE_2B_COMMAND_NAMES.issubset(names)
    for command_name in EXPECTED_PHASE_2B_COMMAND_NAMES:
        assert command_is_available(command_name) is True


def test_authority_review_is_registered_as_read_only_command() -> None:
    """Publish the authority review read-only contract for agents."""
    schema = command_schema_payload("agileforge authority review")
    capabilities = _capability_by_name()

    assert schema["mutates"] is False
    assert schema["installed"] is True
    assert capabilities["agileforge authority review"]["installed"] is True
    assert schema["input"]["required"] == ["project_id"]
    assert schema["input"]["optional"] == ["include_spec", "format", "open"]


def test_authority_accept_is_registered_as_guarded_mutation() -> None:
    """Publish the authority accept mutation contract for agents."""
    accept_schema = command_schema_payload("agileforge authority accept")
    capabilities = _capability_by_name()
    input_required = accept_schema["input"]["required"]

    assert accept_schema["mutates"] is True
    assert accept_schema["installed"] is True
    assert accept_schema["idempotency_required"] is False
    assert accept_schema["idempotency_policy"]["non_dry_run"] == "not_applicable"
    assert accept_schema["idempotency_policy"]["dry_run"] == "not_applicable"
    assert "project_id" in input_required
    assert "review_token" not in input_required
    assert "idempotency_key" not in input_required
    assert "review_token" in accept_schema["input"]["optional"]
    assert "expected_authority_fingerprint" in accept_schema["input"]["optional"]
    assert "expected_source_spec_hash" in accept_schema["input"]["optional"]
    assert "expected_disk_spec_hash" in accept_schema["input"]["optional"]
    assert "expected_state" in accept_schema["input"]["optional"]
    assert "expected_setup_status" in accept_schema["input"]["optional"]
    assert "expected_coverage_summary_fingerprint" in accept_schema["input"]["optional"]
    assert "review_token" in accept_schema["guard_policy"]
    assert "expected_coverage_summary_fingerprint" in accept_schema["guard_policy"]
    assert accept_schema["guard_policy_is_authoritative"] is True
    legacy_guard_flags = accept_schema["legacy_guard_flags"]
    assert legacy_guard_flags["accepts_expected_state"] is True
    assert legacy_guard_flags["accepts_expected_artifact_fingerprint"] is False
    assert capabilities["agileforge authority accept"]["guard_policy_is_authoritative"]
    assert capabilities["agileforge authority accept"]["accepts_expected_state"] is True
    assert ErrorCode.AUTHORITY_REVIEW_INCOMPLETE.value in accept_schema["errors"]
    assert ErrorCode.AUTHORITY_ALREADY_DECIDED.value in accept_schema["errors"]
    assert ErrorCode.AUTHORITY_SOURCE_CHANGED.value in accept_schema["errors"]
    assert ErrorCode.AUTHORITY_GUARD_INCOMPLETE.value in accept_schema["errors"]


def test_authority_reject_is_registered_as_guarded_mutation_with_reason() -> None:
    """Publish the authority reject mutation contract for agents."""
    schema = command_schema_payload("agileforge authority reject")
    capabilities = _capability_by_name()

    assert schema["mutates"] is True
    assert schema["installed"] is True
    assert schema["idempotency_required"] is True
    assert schema["idempotency_policy"]["non_dry_run"] == "required"
    assert schema["idempotency_policy"]["dry_run"] != "not_applicable"
    assert schema["input"]["required"] == ["project_id", "reason", "idempotency_key"]
    assert "review_token" in schema["input"]["optional"]
    assert "expected_source_spec_hash" in schema["input"]["optional"]
    assert "expected_disk_spec_hash" in schema["input"]["optional"]
    assert "expected_state" in schema["input"]["optional"]
    assert "expected_setup_status" in schema["input"]["optional"]
    assert "review_token" in schema["guard_policy"]
    assert "expected_source_spec_hash" in schema["guard_policy"]
    assert schema["guard_policy_is_authoritative"] is True
    assert schema["legacy_guard_flags"]["accepts_expected_state"] is True
    assert schema["legacy_guard_flags"]["accepts_expected_artifact_fingerprint"] is (
        False
    )
    assert capabilities["agileforge authority reject"]["guard_policy_is_authoritative"]
    assert capabilities["agileforge authority reject"]["accepts_expected_state"] is True
    assert ErrorCode.AUTHORITY_ALREADY_DECIDED.value in schema["errors"]
    assert ErrorCode.AUTHORITY_SOURCE_CHANGED.value in schema["errors"]
    assert ErrorCode.AUTHORITY_REVIEW_INCOMPLETE.value in schema["errors"]


def test_phase_2c_authority_commands_are_registered_and_available() -> None:
    """Expose Task 5 authority commands as installed CLI capabilities."""
    names = installed_command_names()
    capabilities = _capability_by_name()

    assert EXPECTED_PHASE_2C_COMMAND_NAMES.issubset(names)
    for command_name in EXPECTED_PHASE_2C_COMMAND_NAMES:
        assert command_is_available(command_name) is True
        assert command_name in capabilities
        assert capabilities[command_name]["installed"] is True


def test_vision_commands_are_registered_and_available() -> None:
    """Expose Vision phase commands as installed CLI capabilities."""
    names = installed_command_names()
    capabilities = _capability_by_name()

    vision_command_names = {
        "agileforge vision generate",
        "agileforge vision history",
        "agileforge vision save",
    }
    assert vision_command_names.issubset(names)
    for command_name in vision_command_names:
        assert command_is_available(command_name) is True
        assert command_name in capabilities
        assert capabilities[command_name]["installed"] is True

    generate = command_schema_payload("agileforge vision generate")
    history = command_schema_payload("agileforge vision history")
    save = command_schema_payload("agileforge vision save")

    assert generate["mutates"] is True
    assert generate["input"]["required"] == ["project_id"]
    assert generate["input"]["optional"] == ["input"]
    assert history["mutates"] is False
    assert history["input"]["required"] == ["project_id"]
    assert save["mutates"] is True
    assert save["input"]["required"] == ["project_id"]


def test_backlog_commands_are_registered_and_available() -> None:
    """Expose Backlog phase commands as installed CLI capabilities."""
    names = installed_command_names()
    capabilities = _capability_by_name()
    backlog_command_names = {
        "agileforge backlog generate",
        "agileforge backlog history",
        "agileforge backlog save",
        "agileforge backlog reconcile",
    }

    assert backlog_command_names.issubset(names)
    for command_name in backlog_command_names:
        assert command_is_available(command_name) is True
        assert command_name in capabilities
        assert capabilities[command_name]["installed"] is True

    generate = command_schema_payload("agileforge backlog generate")
    history = command_schema_payload("agileforge backlog history")
    save = command_schema_payload("agileforge backlog save")
    reconcile = command_schema_payload("agileforge backlog reconcile")

    assert generate["mutates"] is True
    assert generate["input"]["required"] == ["project_id"]
    assert generate["input"]["optional"] == ["input"]
    assert ErrorCode.MUTATION_FAILED.value in generate["errors"]
    assert history["mutates"] is False
    assert history["input"]["required"] == ["project_id"]
    assert save["mutates"] is True
    assert save["input"]["required"] == [
        "project_id",
        "attempt_id",
        "expected_artifact_fingerprint",
        "expected_state",
        "idempotency_key",
    ]
    assert save["idempotency_required"] is True
    assert ErrorCode.MUTATION_FAILED.value in save["errors"]
    assert reconcile["mutates"] is True
    assert reconcile["input"]["required"] == ["project_id", "idempotency_key"]
    assert reconcile["idempotency_required"] is True
    assert ErrorCode.MUTATION_FAILED.value in reconcile["errors"]


def test_roadmap_commands_are_registered_and_available() -> None:
    """Expose Roadmap phase commands as installed CLI capabilities."""
    names = installed_command_names()
    capabilities = _capability_by_name()
    roadmap_command_names = {
        "agileforge roadmap generate",
        "agileforge roadmap history",
        "agileforge roadmap save",
    }

    assert roadmap_command_names.issubset(names)
    for command_name in roadmap_command_names:
        assert command_is_available(command_name) is True
        assert command_name in capabilities
        assert capabilities[command_name]["installed"] is True

    generate = command_schema_payload("agileforge roadmap generate")
    history = command_schema_payload("agileforge roadmap history")
    save = command_schema_payload("agileforge roadmap save")

    assert generate["mutates"] is True
    assert generate["input"]["required"] == ["project_id"]
    assert generate["input"]["optional"] == ["input"]
    assert ErrorCode.MUTATION_FAILED.value in generate["errors"]
    assert history["mutates"] is False
    assert history["input"]["required"] == ["project_id"]
    assert save["mutates"] is True
    assert save["input"]["required"] == [
        "project_id",
        "attempt_id",
        "expected_artifact_fingerprint",
        "expected_state",
        "idempotency_key",
    ]
    assert save["idempotency_required"] is True
    assert ErrorCode.MUTATION_FAILED.value in save["errors"]


def test_story_dependency_commands_are_registered_and_available() -> None:
    """Expose Story dependency review commands as installed CLI capabilities."""
    names = installed_command_names()
    dependency_command_names = {
        "agileforge story dependencies inspect",
        "agileforge story dependencies propose",
        "agileforge story dependencies apply",
    }

    assert dependency_command_names.issubset(names)
    for command_name in dependency_command_names:
        assert command_is_available(command_name) is True

    inspect_payload = command_schema_payload("agileforge story dependencies inspect")
    propose_payload = command_schema_payload("agileforge story dependencies propose")
    apply_payload = command_schema_payload("agileforge story dependencies apply")

    assert inspect_payload["mutates"] is False
    assert inspect_payload["input"]["required"] == ["project_id"]
    assert propose_payload["mutates"] is True
    assert propose_payload["input"]["required"] == [
        "project_id",
        "expected_state",
        "idempotency_key",
    ]
    assert propose_payload["idempotency_required"] is True
    assert apply_payload["mutates"] is True
    assert apply_payload["input"]["required"] == [
        "project_id",
        "attempt_id",
        "expected_artifact_fingerprint",
        "expected_state",
        "idempotency_key",
    ]
    assert apply_payload["idempotency_required"] is True


def test_story_phase_commands_are_registered_and_available() -> None:
    """Expose Story phase commands as installed CLI capabilities."""
    names = installed_command_names()
    capabilities = _capability_by_name()
    story_command_names = {
        "agileforge story pending",
        "agileforge story generate",
        "agileforge story retry",
        "agileforge story history",
        "agileforge story save",
        "agileforge story complete",
        "agileforge story reopen",
        "agileforge story repair-readiness",
    }

    assert story_command_names.issubset(names)
    for command_name in story_command_names:
        assert command_is_available(command_name) is True
        assert command_name in capabilities
        assert capabilities[command_name]["installed"] is True

    pending = command_schema_payload("agileforge story pending")
    generate = command_schema_payload("agileforge story generate")
    retry = command_schema_payload("agileforge story retry")
    history = command_schema_payload("agileforge story history")
    save = command_schema_payload("agileforge story save")
    complete = command_schema_payload("agileforge story complete")
    reopen = command_schema_payload("agileforge story reopen")
    repair_readiness = command_schema_payload("agileforge story repair-readiness")

    assert pending["mutates"] is False
    assert pending["input"]["required"] == ["project_id"]
    assert generate["mutates"] is True
    assert generate["input"]["required"] == ["project_id", "parent_requirement"]
    assert generate["input"]["optional"] == ["input"]
    assert retry["mutates"] is True
    assert retry["input"]["required"] == ["project_id", "parent_requirement"]
    assert history["mutates"] is False
    assert history["input"]["required"] == ["project_id", "parent_requirement"]
    assert save["mutates"] is True
    assert save["input"]["required"] == [
        "project_id",
        "parent_requirement",
        "attempt_id",
        "expected_artifact_fingerprint",
        "expected_state",
        "idempotency_key",
    ]
    assert save["idempotency_required"] is True
    assert complete["mutates"] is True
    assert complete["input"]["required"] == [
        "project_id",
        "expected_state",
        "idempotency_key",
    ]
    assert complete["idempotency_required"] is True
    assert reopen["mutates"] is True
    assert reopen["input"]["required"] == [
        "project_id",
        "parent_requirement",
        "expected_state",
        "idempotency_key",
    ]
    assert reopen["idempotency_required"] is True
    assert repair_readiness["mutates"] is True
    assert repair_readiness["input"]["required"] == [
        "project_id",
        "expected_state",
        "idempotency_key",
    ]
    assert repair_readiness["idempotency_required"] is True
    for schema in (generate, retry, save, complete, reopen, repair_readiness):
        assert ErrorCode.PROJECT_NOT_FOUND.value in schema["errors"]
        assert ErrorCode.AUTHORITY_NOT_ACCEPTED.value in schema["errors"]
        assert ErrorCode.INVALID_COMMAND.value in schema["errors"]
        assert ErrorCode.WORKFLOW_SESSION_FAILED.value in schema["errors"]
        assert ErrorCode.MUTATION_FAILED.value in schema["errors"]


def test_sprint_phase_commands_are_registered_and_available() -> None:
    """Expose Sprint phase generation commands as installed CLI capabilities."""
    names = installed_command_names()
    capabilities = _capability_by_name()
    sprint_command_names = {
        "agileforge sprint generate",
        "agileforge sprint history",
        "agileforge sprint save",
    }

    assert sprint_command_names.issubset(names)
    for command_name in sprint_command_names:
        assert command_is_available(command_name) is True
        assert command_name in capabilities
        assert capabilities[command_name]["installed"] is True

    generate = command_schema_payload("agileforge sprint generate")
    history = command_schema_payload("agileforge sprint history")
    save = command_schema_payload("agileforge sprint save")

    assert generate["mutates"] is True
    assert generate["input"]["required"] == ["project_id"]
    assert generate["input"]["optional"] == [
        "input",
        "selected_story_ids",
        "team_velocity_assumption",
        "sprint_duration_days",
        "max_story_points",
        "include_task_decomposition",
    ]
    assert history["mutates"] is False
    assert history["input"]["required"] == ["project_id"]
    assert save["mutates"] is True
    assert save["input"]["required"] == [
        "project_id",
        "team_name",
        "sprint_start_date",
        "attempt_id",
        "expected_artifact_fingerprint",
        "expected_state",
        "idempotency_key",
    ]
    assert save["idempotency_required"] is True
    for schema in (generate, history, save):
        assert ErrorCode.PROJECT_NOT_FOUND.value in schema["errors"]
        assert ErrorCode.INVALID_COMMAND.value in schema["errors"]
        assert ErrorCode.WORKFLOW_SESSION_FAILED.value in schema["errors"]
    assert ErrorCode.AUTHORITY_NOT_ACCEPTED.value in generate["errors"]
    assert ErrorCode.MUTATION_FAILED.value in generate["errors"]
    assert ErrorCode.AUTHORITY_NOT_ACCEPTED.value in save["errors"]
    assert ErrorCode.MUTATION_FAILED.value in save["errors"]


def test_spec_profile_commands_are_registered_with_expected_inputs() -> None:
    """Publish spec profile schema and validation command contracts."""
    names = installed_command_names()
    capabilities = _capability_by_name()

    assert EXPECTED_PHASE_2E_COMMAND_NAMES.issubset(names)
    for command_name in EXPECTED_PHASE_2E_COMMAND_NAMES:
        assert command_is_available(command_name) is True
        assert command_name in capabilities
        assert capabilities[command_name]["installed"] is True

    schema = command_schema_payload("agileforge spec profile schema")
    validate = command_schema_payload("agileforge spec profile validate")

    assert schema["mutates"] is False
    assert schema["input"]["required"] == []
    assert schema["input"]["optional"] == []
    assert validate["mutates"] is False
    assert validate["input"]["required"] == ["spec_file"]
    assert validate["input"]["optional"] == ["render_md"]
    assert ErrorCode.SPEC_FILE_NOT_FOUND.value in validate["errors"]
    assert ErrorCode.SPEC_FILE_INVALID.value in validate["errors"]
    assert ErrorCode.INVALID_COMMAND.value in validate["errors"]
