"""Tests for authority review/decision CLI commands."""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING, Any, cast

import pytest

from cli.main import main

if TYPE_CHECKING:
    from services.agent_workbench.authority_decision import AuthorityAcceptRequest

type JsonObject = dict[str, object]

PROJECT_ID = 7
INVALID_COMMAND_EXIT_CODE = 2
AUTHORITY_REVIEW_REQUIRED_EXIT_CODE = 4


class _AuthorityDecisionCliApplication:
    """Fake application facade used to verify authority decision routing."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.noisy_review_stdout = False

    def authority_review(
        self,
        *,
        project_id: int,
        include_spec: str = "auto",
        output_format: str = "json",
    ) -> JsonObject:
        """Return a review packet with guard tokens."""
        if self.noisy_review_stdout:
            sys.stdout.write("LiteLLM completion() model=openai/example\n")
        self.calls.append(
            (
                "authority_review",
                {
                    "project_id": project_id,
                    "include_spec": include_spec,
                    "output_format": output_format,
                },
            )
        )
        return {
            "ok": True,
            "data": {
                **(
                    {"text": "Authority review\nProject: 7"}
                    if output_format == "text"
                    else {}
                ),
                "project": {
                    "project_id": project_id,
                    "fsm_state": "SETUP_REQUIRED",
                    "setup_status": "authority_pending_review",
                },
                "spec": {
                    "resolved_path": "/repo/specs/app.md",
                    "spec_hash": "sha256:source",
                    "disk_sha256": "sha256:disk",
                    "content_included": True,
                    "coverage_summary_fingerprint": "sha256:coverage",
                },
                "pending_authority": {
                    "authority_id": 99,
                    "authority_fingerprint": "sha256:authority",
                },
                "guard_tokens": _guard_payload(),
            },
            "warnings": [],
            "errors": [],
        }

    def authority_accept(self, request: object) -> JsonObject:
        """Record the accept request payload."""
        self.calls.append(("authority_accept", _request_payload(request)))
        return {"ok": True, "data": {"accepted": True}, "warnings": [], "errors": []}

    def authority_reject(self, request: object) -> JsonObject:
        """Record the reject request payload."""
        self.calls.append(("authority_reject", _request_payload(request)))
        return {"ok": True, "data": {"rejected": True}, "warnings": [], "errors": []}


def _request_payload(request: object) -> dict[str, object]:
    """Return a JSON-like request payload from a pydantic model."""
    model_dump = cast("Any", request).model_dump
    dumped = model_dump()
    assert isinstance(dumped, dict)
    return cast("dict[str, object]", dumped)


def _stdout_payload(capsys: pytest.CaptureFixture[str]) -> JsonObject:
    """Return captured stdout as a JSON object."""
    captured = capsys.readouterr()
    assert captured.err == ""
    return cast("JsonObject", json.loads(captured.out))


def _first_error(payload: JsonObject) -> JsonObject:
    """Return the first error object from an envelope."""
    errors = payload["errors"]
    assert isinstance(errors, list)
    assert errors
    error = errors[0]
    assert isinstance(error, dict)
    return cast("JsonObject", error)


def _guard_payload() -> dict[str, object]:
    """Return a complete authority guard token set."""
    return {
        "review_token": "agileforge.authority_review.v1:sha256:" + ("a" * 64),
        "pending_authority_id": 99,
        "expected_authority_fingerprint": "sha256:authority",
        "expected_source_spec_hash": "sha256:source",
        "expected_disk_spec_hash": "sha256:disk",
        "expected_resolved_spec_path": "/repo/specs/app.md",
        "expected_state": "SETUP_REQUIRED",
        "expected_setup_status": "authority_pending_review",
        "expected_content_included": True,
        "expected_omission_assessment": "complete",
        "expected_coverage_summary_fingerprint": "sha256:coverage",
    }


def _explicit_guard_args(*, include_completeness: bool = True) -> list[str]:
    """Return CLI args for explicit authority guards."""
    guards = _guard_payload()
    args = [
        "--pending-authority-id",
        str(guards["pending_authority_id"]),
        "--expected-authority-fingerprint",
        str(guards["expected_authority_fingerprint"]),
        "--expected-source-spec-hash",
        str(guards["expected_source_spec_hash"]),
        "--expected-disk-spec-hash",
        str(guards["expected_disk_spec_hash"]),
        "--expected-resolved-spec-path",
        str(guards["expected_resolved_spec_path"]),
        "--expected-state",
        str(guards["expected_state"]),
        "--expected-setup-status",
        str(guards["expected_setup_status"]),
    ]
    if include_completeness:
        args.extend(
            [
                "--expected-content-included",
                "true",
                "--expected-omission-assessment",
                str(guards["expected_omission_assessment"]),
                "--expected-coverage-summary-fingerprint",
                str(guards["expected_coverage_summary_fingerprint"]),
            ]
        )
    return args


def test_authority_review_parser_calls_application(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify authority review routes parser args to the application."""
    app = _AuthorityDecisionCliApplication()

    rc = main(
        [
            "authority",
            "review",
            "--project-id",
            str(PROJECT_ID),
            "--include-spec",
            "full",
            "--format",
            "json",
        ],
        application=app,
    )

    payload = _stdout_payload(capsys)
    assert rc == 0
    assert payload["ok"] is True
    assert cast("JsonObject", payload["meta"])["command"] == (
        "agileforge authority review"
    )
    assert app.calls == [
        (
            "authority_review",
            {
                "project_id": PROJECT_ID,
                "include_spec": "full",
                "output_format": "json",
            },
        )
    ]


def test_authority_review_parser_passes_text_format_to_application(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify authority review text format writes plain text to stdout."""
    app = _AuthorityDecisionCliApplication()

    rc = main(
        [
            "authority",
            "review",
            "--project-id",
            str(PROJECT_ID),
            "--format",
            "text",
        ],
        application=app,
    )

    captured = capsys.readouterr()
    assert rc == 0
    assert captured.err == ""
    assert captured.out == "Authority review\nProject: 7\n"
    assert app.calls == [
        (
            "authority_review",
            {
                "project_id": PROJECT_ID,
                "include_spec": "auto",
                "output_format": "text",
            },
        )
    ]


def test_authority_accept_with_review_token_does_not_require_idempotency_key(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify human token mode generates idempotency when omitted."""
    app = _AuthorityDecisionCliApplication()
    review_token = cast("str", _guard_payload()["review_token"])

    rc = main(
        [
            "authority",
            "accept",
            "--project-id",
            str(PROJECT_ID),
            "--review-token",
            review_token,
        ],
        application=app,
    )

    payload = _stdout_payload(capsys)
    assert rc == 0
    assert payload["ok"] is True
    assert app.calls[0][0] == "authority_accept"
    request = app.calls[0][1]
    assert request["project_id"] == PROJECT_ID
    assert request["review_token"] == review_token
    assert str(request["idempotency_key"]).startswith("human-token:")
    assert request["actor_mode"] == "cli-human"


def test_authority_accept_repeated_incomplete_review_override_flag(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify candidate-scoped incomplete-review overrides reach the service."""
    app = _AuthorityDecisionCliApplication()

    rc = main(
        [
            "authority",
            "accept",
            "--project-id",
            str(PROJECT_ID),
            "--review-token",
            str(_guard_payload()["review_token"]),
            "--incomplete-review-override",
            "REQ-1:AUTHORITY_CANDIDATE_UNCOVERED:Reviewed uncovered text.",
            "--incomplete-review-override",
            "REQ-2:AUTHORITY_CANDIDATE_WEAK_MAPPING:Reviewed weak mapping.",
        ],
        application=app,
    )

    payload = _stdout_payload(capsys)
    assert rc == 0
    assert payload["ok"] is True
    request = app.calls[0][1]
    assert request["incomplete_review_overrides"] == [
        {
            "candidate_id": "REQ-1",
            "finding_code": "AUTHORITY_CANDIDATE_UNCOVERED",
            "rationale": "Reviewed uncovered text.",
        },
        {
            "candidate_id": "REQ-2",
            "finding_code": "AUTHORITY_CANDIDATE_WEAK_MAPPING",
            "rationale": "Reviewed weak mapping.",
        },
    ]


def test_authority_accept_forwards_broad_incomplete_review_fields(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The legacy broad override flags still reach the service."""
    app = _AuthorityDecisionCliApplication()

    rc = main(
        [
            "authority",
            "accept",
            "--project-id",
            str(PROJECT_ID),
            "--review-token",
            str(_guard_payload()["review_token"]),
            "--allow-incomplete-review",
            "--incomplete-review-rationale",
            "Reviewed manually.",
        ],
        application=app,
    )

    payload = _stdout_payload(capsys)
    assert rc == 0
    assert payload["ok"] is True
    assert app.calls[0][0] == "authority_accept"
    request = app.calls[0][1]
    assert request["allow_incomplete_review"] is True
    assert request["incomplete_review_rationale"] == "Reviewed manually."


def test_authority_accept_without_token_uses_latest_review(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Human CLI can accept with project id only when latest review is fresh."""
    expected_project_id = 42
    latest_review_value = "review-token-123"
    captured_requests: list[AuthorityAcceptRequest] = []

    class FakeApplication:
        """Fake default application facade for tokenless accept routing."""

        def authority_review(
            self,
            *,
            project_id: int,
            include_spec: str,
            output_format: str,
        ) -> dict[str, Any]:
            """Return the latest fresh review for a project."""
            assert project_id == expected_project_id
            assert include_spec == "auto"
            assert output_format == "json"
            return {
                "ok": True,
                "data": {
                    "guard_tokens": {"review_token": latest_review_value},
                    "review_summary": {"acceptance_status": "accept_ready"},
                },
                "errors": [],
                "warnings": [],
            }

        def authority_accept(
            self,
            request: AuthorityAcceptRequest,
        ) -> dict[str, Any]:
            """Record the accept request built by CLI tokenless accept."""
            captured_requests.append(request)
            return {
                "ok": True,
                "data": {"accepted_decision_id": 7},
                "errors": [],
                "warnings": [],
            }

    monkeypatch.setattr(
        "services.agent_workbench.application.AgentWorkbenchApplication",
        lambda **_kwargs: FakeApplication(),
    )

    exit_code = main(["authority", "accept", "--project-id", str(expected_project_id)])

    payload = _stdout_payload(capsys)
    assert exit_code == 0
    assert payload["ok"] is True
    assert len(captured_requests) == 1
    request = captured_requests[0]
    assert request.review_token == latest_review_value
    assert isinstance(request.idempotency_key, str)
    assert request.idempotency_key.strip()


def test_authority_accept_without_token_requires_latest_review_token(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify tokenless accept fails when latest review has no review token."""
    expected_project_id = PROJECT_ID

    class FakeApplication:
        """Fake default application facade for missing-token review routing."""

        def authority_review(
            self,
            *,
            project_id: int,
            include_spec: str,
            output_format: str,
        ) -> dict[str, Any]:
            """Return a latest review packet without a guard review token."""
            del project_id, include_spec, output_format
            return {
                "ok": True,
                "data": {
                    "guard_tokens": {},
                    "review_summary": {"acceptance_status": "accept_ready"},
                },
                "errors": [],
                "warnings": [],
            }

        def authority_accept(
            self,
            request: AuthorityAcceptRequest,
        ) -> dict[str, Any]:
            """Record any unexpected accept request."""
            del request
            return {
                "ok": True,
                "data": {"accepted_decision_id": 7},
                "errors": [],
                "warnings": [],
            }

    monkeypatch.setattr(
        "services.agent_workbench.application.AgentWorkbenchApplication",
        lambda **_kwargs: FakeApplication(),
    )

    rc = main(
        ["authority", "accept", "--project-id", str(expected_project_id)],
    )

    payload = _stdout_payload(capsys)
    assert rc == AUTHORITY_REVIEW_REQUIRED_EXIT_CODE
    assert _first_error(payload)["code"] == "AUTHORITY_REVIEW_REQUIRED"


def test_authority_accept_explicit_agent_mode_requires_idempotency_key(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify explicit accept mode requires idempotency."""
    app = _AuthorityDecisionCliApplication()

    rc = main(
        [
            "authority",
            "accept",
            "--project-id",
            str(PROJECT_ID),
            *_explicit_guard_args(),
        ],
        application=app,
    )

    payload = _stdout_payload(capsys)
    assert rc == INVALID_COMMAND_EXIT_CODE
    assert cast("JsonObject", payload["meta"])["command"] == (
        "agileforge authority accept"
    )
    assert _first_error(payload)["code"] == "INVALID_COMMAND"
    assert app.calls == []


def test_authority_accept_explicit_agent_mode_requires_completeness_guards(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify explicit accept mode requires completeness guard fields."""
    app = _AuthorityDecisionCliApplication()

    rc = main(
        [
            "authority",
            "accept",
            "--project-id",
            str(PROJECT_ID),
            *_explicit_guard_args(include_completeness=False),
            "--idempotency-key",
            "explicit-accept-001",
        ],
        application=app,
    )

    payload = _stdout_payload(capsys)
    assert rc == INVALID_COMMAND_EXIT_CODE
    error = _first_error(payload)
    assert error["code"] == "INVALID_COMMAND"
    details = cast("JsonObject", error["details"])
    assert details["missing"] == [
        "expected_content_included",
        "expected_omission_assessment",
        "expected_coverage_summary_fingerprint",
    ]
    assert app.calls == []


def test_authority_reject_requires_reason(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify reject requires a rationale in token mode."""
    app = _AuthorityDecisionCliApplication()

    rc = main(
        [
            "authority",
            "reject",
            "--project-id",
            str(PROJECT_ID),
            "--review-token",
            str(_guard_payload()["review_token"]),
        ],
        application=app,
    )

    payload = _stdout_payload(capsys)
    assert rc == INVALID_COMMAND_EXIT_CODE
    assert _first_error(payload)["code"] == "INVALID_COMMAND"
    assert app.calls == []


def test_authority_reject_without_token_non_tty_requires_review(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify non-interactive reject requires a review token."""
    app = _AuthorityDecisionCliApplication()

    rc = main(
        [
            "authority",
            "reject",
            "--project-id",
            str(PROJECT_ID),
            "--reason",
            "Spec needs revision.",
        ],
        application=app,
    )

    payload = _stdout_payload(capsys)
    assert rc == AUTHORITY_REVIEW_REQUIRED_EXIT_CODE
    assert _first_error(payload)["code"] == "AUTHORITY_REVIEW_REQUIRED"
    assert app.calls == []


def test_authority_reject_explicit_mode_requires_resolved_path_guard(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify explicit reject mode requires source path guards."""
    app = _AuthorityDecisionCliApplication()
    args = _explicit_guard_args()
    path_index = args.index("--expected-resolved-spec-path")
    del args[path_index : path_index + 2]

    rc = main(
        [
            "authority",
            "reject",
            "--project-id",
            str(PROJECT_ID),
            *args,
            "--reason",
            "Spec needs more detail.",
            "--idempotency-key",
            "explicit-reject-001",
        ],
        application=app,
    )

    payload = _stdout_payload(capsys)
    assert rc == INVALID_COMMAND_EXIT_CODE
    error = _first_error(payload)
    assert error["code"] == "INVALID_COMMAND"
    assert cast("JsonObject", error["details"])["missing"] == [
        "expected_resolved_spec_path"
    ]
    assert app.calls == []


def test_authority_help_shows_review_accept_reject_examples(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify help shows authority decision commands and examples."""
    with pytest.raises(SystemExit) as top_level:
        main(["--help"])

    captured = capsys.readouterr()
    assert top_level.value.code == 0
    assert "agileforge authority review --project-id 1" in captured.out
    assert (
        "agileforge authority accept --project-id 1 --review-token <review_token>"
        in captured.out
    )
    assert (
        'agileforge authority reject --project-id 1 --review-token <review_token> '
        '--reason "..."'
    ) in captured.out

    with pytest.raises(SystemExit) as authority:
        main(["authority", "--help"])

    captured = capsys.readouterr()
    assert authority.value.code == 0
    assert "review" in captured.out
    assert "accept" in captured.out
    assert "reject" in captured.out


def test_authority_review_keeps_stdout_json_clean_when_service_logs(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify authority review suppresses lower-layer stdout noise."""
    app = _AuthorityDecisionCliApplication()
    app.noisy_review_stdout = True

    rc = main(
        ["authority", "review", "--project-id", str(PROJECT_ID)],
        application=app,
    )

    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.startswith("{")
    assert "LiteLLM" not in captured.out
    assert captured.err == ""
    payload = cast("JsonObject", json.loads(captured.out))
    assert payload["ok"] is True
    assert app.calls == [
        (
            "authority_review",
            {
                "project_id": PROJECT_ID,
                "include_spec": "auto",
                "output_format": "json",
            },
        )
    ]
