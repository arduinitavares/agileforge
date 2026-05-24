"""Phase-scoped context pack projections for agents."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final, Protocol

from services.agent_workbench.command_registry import command_is_available
from services.agent_workbench.fingerprints import canonical_hash
from services.agent_workbench.project_setup_fingerprints import (
    setup_retry_context_fingerprint,
)
from services.specs.profile_content import SpecContentNormalizationError

JsonDict = dict[str, Any]

CONTEXT_PACK_COMMAND: Final[str] = "agileforge context pack"
SPRINT_CANDIDATES_COMMAND: Final[str] = "agileforge sprint candidates"
SPRINT_GENERATE_COMMAND: Final[str] = "agileforge sprint generate"
SPRINT_PLANNING_STATES: Final[frozenset[str]] = frozenset(
    {"SPRINT_SETUP", "SPRINT_DRAFT", "SPRINT_PLANNING"}
)
SETUP_READY_STATUSES: Final[frozenset[str]] = frozenset({"passed", "ready"})
AUTHORITY_BLOCKING_STATUSES: Final[frozenset[str]] = frozenset(
    {"missing", "not_compiled", "pending_acceptance", "stale"}
)


class _ReadProjection(Protocol):
    """Read projections needed by context pack composition."""

    def workflow_state(self, *, project_id: int) -> JsonDict:
        """Return workflow state projection."""
        ...

    def sprint_candidates(self, *, project_id: int) -> JsonDict:
        """Return sprint candidate projection."""
        ...


class _AuthorityProjection(Protocol):
    """Authority projections needed by context pack composition."""

    def status(self, *, project_id: int) -> JsonDict:
        """Return authority status projection."""
        ...


class ContextPackService:
    """Compose bounded context packs from read-only projections."""

    def __init__(
        self,
        *,
        read_projection: _ReadProjection,
        authority_projection: _AuthorityProjection,
    ) -> None:
        """Initialize with already-configured projection services."""
        self._read_projection = read_projection
        self._authority_projection = authority_projection

    def pack(self, *, project_id: int, phase: str = "overview") -> JsonDict:
        """Return a bounded context pack for a project and phase."""
        workflow = self._read_projection.workflow_state(project_id=project_id)
        if not workflow.get("ok"):
            return workflow

        authority = self._authority_projection.status(project_id=project_id)
        if not authority.get("ok"):
            return authority

        workflow_data = _envelope_data(workflow)
        authority_data = _envelope_data(authority)
        warnings = [
            *_section_warnings(
                section="workflow",
                source="workflow_state",
                envelope=workflow,
            ),
            *_section_warnings(
                section="authority",
                source="authority_status",
                envelope=authority,
            ),
        ]
        included_sections = ["workflow", "authority"]
        omitted_sections = ["raw_spec", "authority_full"]
        truncation: list[JsonDict] = []
        phase_data: JsonDict = {}
        guard_tokens = _guard_tokens(
            project_id=project_id,
            workflow_data=workflow_data,
            authority_data=authority_data,
        )
        next_valid_commands: list[str] = []
        blocked_commands: list[JsonDict] = []
        blocked_future_commands: list[str] = []

        if phase == "sprint-planning":
            candidate_block = _candidate_blocker(
                workflow_data=workflow_data,
                authority_data=authority_data,
            )
            if candidate_block is None:
                candidates = self._read_projection.sprint_candidates(
                    project_id=project_id
                )
                if not candidates.get("ok"):
                    return candidates

                candidate_data = _envelope_data(candidates)
                warnings.extend(
                    _section_warnings(
                        section="sprint_candidates",
                        source="sprint_candidates",
                        envelope=candidates,
                    )
                )
                phase_data["sprint_candidates"] = candidate_data
                included_sections.append("sprint_candidates")
            (
                next_valid_commands,
                blocked_commands,
                blocked_future_commands,
            ) = _sprint_planning_commands(
                project_id=project_id,
                candidate_block=candidate_block,
            )

        authority_fingerprint = authority_data.get("authority_fingerprint")
        data = {
            "project_id": project_id,
            "phase": phase,
            "fsm_state": _fsm_state(workflow_data),
            "workflow": workflow_data,
            "authority": authority_data,
            "authority_fingerprint": authority_fingerprint,
            "next_valid_commands": next_valid_commands,
            "blocked_commands": blocked_commands,
            "blocked_future_commands": blocked_future_commands,
            "included_sections": included_sections,
            "omitted_sections": omitted_sections,
            "truncation": truncation,
            "guard_tokens": guard_tokens,
            "phase_data": phase_data,
            "warnings": warnings,
        }
        data["source_fingerprint"] = canonical_hash(
            {
                "command": CONTEXT_PACK_COMMAND,
                "project_id": project_id,
                "phase": phase,
                "included_sections": included_sections,
                "omitted_sections": omitted_sections,
                "truncation": truncation,
                "workflow": _fingerprint_or_data(workflow_data),
                "authority": authority_fingerprint or authority_data,
                "phase_data": _fingerprinted_phase_data(phase_data),
                "guard_tokens": guard_tokens,
                "next_valid_commands": next_valid_commands,
                "blocked_commands": blocked_commands,
                "blocked_future_commands": blocked_future_commands,
                "warnings": warnings,
            }
        )

        return {"ok": True, "data": data, "warnings": warnings, "errors": []}


def _envelope_data(envelope: JsonDict) -> JsonDict:
    """Return dictionary data from a successful child projection."""
    data = envelope.get("data")
    return data if isinstance(data, dict) else {}


def _fingerprint_or_data(data: JsonDict) -> object:
    """Return the source fingerprint when present, otherwise the data payload."""
    return data.get("source_fingerprint") or data


def _section_warnings(
    *,
    section: str,
    source: str,
    envelope: JsonDict,
) -> list[JsonDict]:
    """Return child warnings with context pack section labels."""
    warnings = envelope.get("warnings", [])
    if not isinstance(warnings, list):
        return []

    labeled: list[JsonDict] = []
    for warning in warnings:
        if not isinstance(warning, dict):
            continue
        labeled.append({"section": section, "source": source, **warning})
    return labeled


def _fingerprinted_phase_data(phase_data: JsonDict) -> JsonDict:
    """Return compact phase data inputs for context pack hashing."""
    fingerprinted: JsonDict = {}
    for section, data in phase_data.items():
        fingerprinted[section] = (
            _fingerprint_or_data(data) if isinstance(data, dict) else data
        )
    return fingerprinted


def _fsm_state(workflow_data: JsonDict) -> object:
    """Return workflow FSM state from a workflow projection payload."""
    state = workflow_data.get("state")
    if not isinstance(state, dict):
        return None
    return state.get("fsm_state")


def _guard_tokens(
    *,
    project_id: int,
    workflow_data: JsonDict,
    authority_data: JsonDict,
) -> JsonDict:
    """Return command guard tokens that can be derived from this context pack."""
    expected_context_fingerprint = _setup_retry_context_guard_token(
        project_id=project_id,
        workflow_data=workflow_data,
        authority_data=authority_data,
    )
    if expected_context_fingerprint is None:
        return {}
    return {"expected_context_fingerprint": expected_context_fingerprint}


def _setup_retry_context_guard_token(
    *,
    project_id: int,
    workflow_data: JsonDict,
    authority_data: JsonDict,
) -> str | None:
    """Return the setup retry context token when the disk spec is readable."""
    state = workflow_data.get("state")
    workflow_state = state if isinstance(state, dict) else {}
    disk_spec = authority_data.get("disk_spec")
    if not isinstance(disk_spec, dict):
        return None
    if disk_spec.get("status") != "readable":
        return None
    resolved_path = disk_spec.get("resolved_path")
    if not isinstance(resolved_path, str) or not resolved_path:
        return None
    try:
        return setup_retry_context_fingerprint(
            project_id=project_id,
            resolved_spec_path=Path(resolved_path),
            workflow_state=workflow_state,
        )
    except (OSError, SpecContentNormalizationError, UnicodeError):
        return None


def _sprint_planning_commands(
    *,
    project_id: int,
    candidate_block: JsonDict | None,
) -> tuple[list[str], list[JsonDict], list[str]]:
    """Return installed and future commands relevant to sprint planning."""
    candidate_command = f"{SPRINT_CANDIDATES_COMMAND} --project-id {project_id}"
    generate_command = f"{SPRINT_GENERATE_COMMAND} --project-id {project_id}"
    next_valid: list[str] = []
    blocked_commands: list[JsonDict] = []
    blocked_future: list[str] = []

    if candidate_block is not None:
        if command_is_available(SPRINT_CANDIDATES_COMMAND):
            blocked_commands.append({"command": candidate_command, **candidate_block})
        return next_valid, blocked_commands, blocked_future

    if command_is_available(SPRINT_CANDIDATES_COMMAND):
        next_valid.append(candidate_command)
    else:
        blocked_future.append(candidate_command)

    if command_is_available(SPRINT_GENERATE_COMMAND):
        next_valid.append(generate_command)
    else:
        blocked_future.append(generate_command)

    return next_valid, blocked_commands, blocked_future


def _candidate_blocker(
    *,
    workflow_data: JsonDict,
    authority_data: JsonDict,
) -> JsonDict | None:
    """Return a conservative reason when candidates should not be advertised."""
    state = workflow_data.get("state")
    state_data = state if isinstance(state, dict) else {}
    fsm_state = _normalized_upper(state_data.get("fsm_state"))
    if fsm_state not in SPRINT_PLANNING_STATES:
        return {
            "reason_code": "WORKFLOW_STATE_NOT_SPRINT_PLANNING",
            "details": {"fsm_state": state_data.get("fsm_state")},
        }

    setup_error = state_data.get("setup_error")
    if setup_error not in (None, ""):
        return {
            "reason_code": "SETUP_ERROR",
            "details": {"setup_error": setup_error},
        }

    setup_status = _normalized_lower(state_data.get("setup_status"))
    if setup_status not in SETUP_READY_STATUSES:
        return {
            "reason_code": "SETUP_STATUS_NOT_READY",
            "details": {"setup_status": state_data.get("setup_status")},
        }

    authority_status = _normalized_lower(authority_data.get("status"))
    if authority_status in AUTHORITY_BLOCKING_STATUSES:
        return {
            "reason_code": "AUTHORITY_BLOCKING_STATE",
            "details": {"authority_status": authority_data.get("status")},
        }
    if authority_status != "current":
        return {
            "reason_code": "AUTHORITY_STATUS_UNCERTAIN",
            "details": {"authority_status": authority_data.get("status")},
        }

    return None


def _normalized_upper(value: object) -> str | None:
    """Return an upper-case normalized string value when present."""
    if value is None:
        return None
    return str(value).strip().upper()


def _normalized_lower(value: object) -> str | None:
    """Return a lower-case normalized string value when present."""
    if value is None:
        return None
    return str(value).strip().lower()
