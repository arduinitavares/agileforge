"""Tests for durable authority curation trace artifacts."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

import utils.authority_curation_trace as trace_mod
from utils.authority_curation_trace import (
    TRACE_SCHEMA_VERSION,
    append_trace_event,
    summarize_trace,
    trace_artifact_id,
    trace_artifact_path,
    trace_step,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

EXPECTED_EVENT_COUNT = 2
CANDIDATE_AUTHORITY_ID = 7
TRACE_WRITE_ERROR = "trace write failed"


def test_append_and_summarize_trace_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Trace events are JSONL and produce a bounded summary."""
    monkeypatch.setattr(trace_mod, "TRACE_DIR", tmp_path / "traces")

    append_trace_event(
        mutation_event_id=647,
        project_id=3,
        step="mutation_lease_acquired",
        status="completed",
        curation_attempt_id=None,
        correlation_id="corr-1",
        attributes={"source_authority_id": 6},
    )
    append_trace_event(
        mutation_event_id=647,
        project_id=3,
        step="candidate_publication_completed",
        status="completed",
        curation_attempt_id="curation-1",
        correlation_id="corr-1",
        attributes={
            "candidate_authority_id": CANDIDATE_AUTHORITY_ID,
            "candidate_authority_fingerprint": "sha256:" + ("a" * 64),
        },
    )

    path = trace_artifact_path(647)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == EXPECTED_EVENT_COUNT
    first = json.loads(lines[0])
    assert first["schema_version"] == TRACE_SCHEMA_VERSION
    assert first["trace_artifact_id"] == "authority_curation_trace-647"

    summary = summarize_trace(mutation_event_id=647)
    assert summary["trace_artifact_id"] == trace_artifact_id(647)
    assert summary["event_count"] == EXPECTED_EVENT_COUNT
    assert summary["last_trace_step"] == "candidate_publication_completed"
    assert summary["last_trace_status"] == "completed"
    assert summary["candidate_published"] is True
    assert summary["candidate_authority_id"] == CANDIDATE_AUTHORITY_ID


def test_trace_rejects_unknown_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Trace steps are constrained to the approved enum."""
    monkeypatch.setattr(trace_mod, "TRACE_DIR", tmp_path / "traces")

    with pytest.raises(ValueError, match="unknown authority curation trace step"):
        append_trace_event(
            mutation_event_id=1,
            project_id=1,
            step="made_up_step",
            status="completed",
        )


def test_trace_redacts_unallowlisted_and_oversized_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Trace attributes keep ids/counts/hashes but remove raw payloads."""
    monkeypatch.setattr(trace_mod, "TRACE_DIR", tmp_path / "traces")

    append_trace_event(
        mutation_event_id=2,
        project_id=1,
        step="adk_invocation_failed",
        status="failed",
        attributes={
            "requested_model_id": "openrouter/deepseek/deepseek-v4-pro",
            "source_authority_json": {"raw": "must-not-appear"},
            "long_text": "x" * 2000,
        },
        error={
            "code": "SPEC_COMPILE_FAILED",
            "message": "x" * 2000,
            "retryable": False,
            "failure_artifact_id": "authority_curation-failed",
            "details": {"raw_output": "must-not-appear", "validation_error_count": 2},
        },
    )

    payload = trace_artifact_path(2).read_text(encoding="utf-8")
    assert "openrouter/deepseek/deepseek-v4-pro" in payload
    assert "source_authority_json" not in payload
    assert "must-not-appear" not in payload
    event = json.loads(payload)
    assert len(event["error"]["message"]) <= trace_mod.MAX_TRACE_STRING_CHARS
    assert event["error"]["details"] == {"validation_error_count": 2}


def test_trace_drops_arbitrary_hash_like_attributes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hash-shaped attributes do not persist arbitrary prompt or model text."""
    monkeypatch.setattr(trace_mod, "TRACE_DIR", tmp_path / "traces")

    append_trace_event(
        mutation_event_id=5,
        project_id=1,
        step="adk_gate_parse_completed",
        status="completed",
        attributes={
            "source_authority_fingerprint": "raw prompt body",
            "candidate_authority_fingerprint": "model output",
            "prompt_hash": "raw prompt body",
            "requested_model_id": "openrouter/deepseek/deepseek-v4-pro",
        },
    )

    payload = trace_artifact_path(5).read_text(encoding="utf-8")
    event = json.loads(payload)
    assert event["attributes"] == {
        "requested_model_id": "openrouter/deepseek/deepseek-v4-pro",
    }
    assert "raw prompt body" not in payload
    assert "model output" not in payload


def test_authority_curation_trace_records_rejected_selection_vector(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rejected v2 repair selections keep debug vectors without raw text."""
    monkeypatch.setattr(trace_mod, "TRACE_DIR", tmp_path / "traces")

    append_trace_event(
        mutation_event_id=660,
        project_id=3,
        step="repair_selection_rejected",
        status="failed",
        curation_attempt_id="curation-1",
        attributes={
            "feedback_id": "AFB-1",
            "target_handle": "R1",
            "target_kind": "assumption",
            "target_id": "ASM-11",
            "target_field": "text",
            "repair_kind": "replace_text",
            "reject_reason": "target_handle_unknown",
            "requested_model_id": "openrouter/deepseek/deepseek-v4-pro",
            "selection_fingerprint": "sha256:" + ("a" * 64),
            "replacement_text": "must-not-appear",
        },
    )

    payload = trace_artifact_path(660).read_text(encoding="utf-8")
    event = json.loads(payload)
    assert event["attributes"] == {
        "feedback_id": "AFB-1",
        "target_handle": "R1",
        "target_kind": "assumption",
        "target_id": "ASM-11",
        "target_field": "text",
        "repair_kind": "replace_text",
        "reject_reason": "target_handle_unknown",
        "requested_model_id": "openrouter/deepseek/deepseek-v4-pro",
        "selection_fingerprint": "sha256:" + ("a" * 64),
    }
    assert "must-not-appear" not in payload


def test_trace_step_records_completed_and_failed_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Context manager writes start plus terminal event."""
    monkeypatch.setattr(trace_mod, "TRACE_DIR", tmp_path / "traces")

    with trace_step(
        mutation_event_id=3,
        project_id=1,
        step="input_load_started",
        completed_step="input_load_completed",
        curation_attempt_id="curation-3",
    ):
        pass

    boom_message = "boom"
    with (
        pytest.raises(RuntimeError, match=boom_message),
        trace_step(
            mutation_event_id=4,
            project_id=1,
            step="adk_invocation_started",
            completed_step="adk_invocation_completed",
            failed_step="adk_invocation_failed",
        ),
    ):
        raise RuntimeError(boom_message)

    assert summarize_trace(mutation_event_id=3)["last_trace_status"] == "completed"
    failed_summary = summarize_trace(mutation_event_id=4)
    assert failed_summary["last_trace_step"] == "adk_invocation_failed"
    assert failed_summary["last_trace_status"] == "failed"

    failed_event = trace_mod.read_trace_events(mutation_event_id=4)[-1]
    assert failed_event["error"]["message"] == "Authority curation trace step failed."
    assert boom_message not in trace_artifact_path(4).read_text(encoding="utf-8")


def test_trace_step_preserves_original_exception_when_failed_trace_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed terminal trace write never masks the workflow exception."""
    monkeypatch.setattr(trace_mod, "TRACE_DIR", tmp_path / "traces")
    original_append_trace_event = trace_mod.append_trace_event

    def fail_failed_trace_write(  # noqa: PLR0913
        *,
        mutation_event_id: int,
        project_id: int,
        step: str,
        status: str,
        curation_attempt_id: str | None = None,
        correlation_id: str | None = None,
        attributes: Mapping[str, object] | None = None,
        error: Mapping[str, object] | None = None,
    ) -> dict[str, trace_mod.JsonValue]:
        if status == "failed":
            raise OSError(TRACE_WRITE_ERROR)
        return original_append_trace_event(
            mutation_event_id=mutation_event_id,
            project_id=project_id,
            step=step,
            status=status,
            curation_attempt_id=curation_attempt_id,
            correlation_id=correlation_id,
            attributes=attributes,
            error=error,
        )

    monkeypatch.setattr(trace_mod, "append_trace_event", fail_failed_trace_write)

    boom_message = "raw workflow secret"
    with (
        pytest.raises(RuntimeError, match=boom_message),
        trace_step(
            mutation_event_id=6,
            project_id=1,
            step="adk_invocation_started",
            completed_step="adk_invocation_completed",
            failed_step="adk_invocation_failed",
        ),
    ):
        raise RuntimeError(boom_message)


def test_trace_step_swallow_completed_trace_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Completed terminal trace write never converts successful work to failure."""
    monkeypatch.setattr(trace_mod, "TRACE_DIR", tmp_path / "traces")
    original_append_trace_event = trace_mod.append_trace_event
    reached_body = False

    def fail_completed_trace_write(  # noqa: PLR0913
        *,
        mutation_event_id: int,
        project_id: int,
        step: str,
        status: str,
        curation_attempt_id: str | None = None,
        correlation_id: str | None = None,
        attributes: Mapping[str, object] | None = None,
        error: Mapping[str, object] | None = None,
    ) -> dict[str, trace_mod.JsonValue]:
        if status == "completed":
            raise OSError(TRACE_WRITE_ERROR)
        return original_append_trace_event(
            mutation_event_id=mutation_event_id,
            project_id=project_id,
            step=step,
            status=status,
            curation_attempt_id=curation_attempt_id,
            correlation_id=correlation_id,
            attributes=attributes,
            error=error,
        )

    monkeypatch.setattr(trace_mod, "append_trace_event", fail_completed_trace_write)

    with trace_step(
        mutation_event_id=7,
        project_id=1,
        step="input_load_started",
        completed_step="input_load_completed",
    ):
        reached_body = True

    assert reached_body is True


def test_summarize_trace_counts_invalid_jsonl_lines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Summary skips corrupt and non-object JSONL records without crashing."""
    monkeypatch.setattr(trace_mod, "TRACE_DIR", tmp_path / "traces")
    trace_path = trace_artifact_path(8)
    trace_path.parent.mkdir(parents=True)
    valid_event = append_trace_event(
        mutation_event_id=8,
        project_id=1,
        step="mutation_lease_acquired",
        status="completed",
    )
    trace_path.write_text(
        "\n".join(
            [
                "{bad-json",
                json.dumps(["not", "an", "object"]),
                json.dumps(valid_event),
            ]
        ),
        encoding="utf-8",
    )

    summary = summarize_trace(mutation_event_id=8)
    assert summary["event_count"] == 1
    assert summary["invalid_event_count"] == EXPECTED_EVENT_COUNT
    assert summary["last_trace_step"] == "mutation_lease_acquired"
