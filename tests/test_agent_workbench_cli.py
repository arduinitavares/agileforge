"""Tests for the agileforge CLI transport."""

from __future__ import annotations

import json
import shutil
import subprocess  # nosec B404
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from cli.main import main
from services.agent_workbench.error_codes import ErrorCode

type JsonObject = dict[str, object]
PROJECT_ID = 7
SPEC_VERSION_ID = 3
STORY_ID = 42
ERROR_EXIT_CODE = 5
INVALID_COMMAND_EXIT_CODE = 2
COMMAND_EXCEPTION_EXIT_CODE = 1


class _FakeApplication:
    """Fake application facade used to verify CLI routing."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def __bool__(self) -> bool:
        """Return false to catch truthiness-based dependency selection."""
        return False

    def project_list(self) -> JsonObject:
        """Return a project list payload."""
        self.calls.append(("project_list", {}))
        return {"ok": True, "data": {"items": []}, "warnings": [], "errors": []}

    def project_show(self, *, project_id: int) -> JsonObject:
        """Return a project detail payload."""
        self.calls.append(("project_show", {"project_id": project_id}))
        return {
            "ok": True,
            "data": {"project_id": project_id},
            "warnings": [],
            "errors": [],
        }

    def project_create(  # noqa: PLR0913
        self,
        *,
        name: str,
        spec_file: str,
        idempotency_key: str | None = None,
        dry_run: bool = False,
        dry_run_id: str | None = None,
        correlation_id: str | None = None,
        changed_by: str = "cli-agent",
    ) -> JsonObject:
        """Return a project create payload."""
        self.calls.append(
            (
                "project_create",
                {
                    "name": name,
                    "spec_file": spec_file,
                    "idempotency_key": idempotency_key,
                    "dry_run": dry_run,
                    "dry_run_id": dry_run_id,
                    "correlation_id": correlation_id,
                    "changed_by": changed_by,
                },
            )
        )
        return {"ok": True, "data": {"project_id": 1}, "warnings": [], "errors": []}

    def project_setup_retry(  # noqa: PLR0913
        self,
        *,
        project_id: int,
        spec_file: str,
        expected_state: str,
        expected_context_fingerprint: str,
        recovery_mutation_event_id: int | None = None,
        idempotency_key: str | None = None,
        dry_run: bool = False,
        dry_run_id: str | None = None,
        correlation_id: str | None = None,
        changed_by: str = "cli-agent",
    ) -> JsonObject:
        """Return a project setup retry payload."""
        self.calls.append(
            (
                "project_setup_retry",
                {
                    "project_id": project_id,
                    "spec_file": spec_file,
                    "expected_state": expected_state,
                    "expected_context_fingerprint": expected_context_fingerprint,
                    "recovery_mutation_event_id": recovery_mutation_event_id,
                    "idempotency_key": idempotency_key,
                    "dry_run": dry_run,
                    "dry_run_id": dry_run_id,
                    "correlation_id": correlation_id,
                    "changed_by": changed_by,
                },
            )
        )
        return {
            "ok": True,
            "data": {"project_id": project_id},
            "warnings": [],
            "errors": [],
        }

    def workflow_state(self, *, project_id: int) -> JsonObject:
        """Return a workflow state payload."""
        self.calls.append(("workflow_state", {"project_id": project_id}))
        return {
            "ok": True,
            "data": {"project_id": project_id, "state": {}},
            "warnings": [],
            "errors": [],
        }

    def workflow_next(self, *, project_id: int) -> JsonObject:
        """Return a workflow next payload."""
        self.calls.append(("workflow_next", {"project_id": project_id}))
        return {
            "ok": True,
            "data": {"project_id": project_id, "next_valid_commands": []},
            "warnings": [],
            "errors": [],
        }

    def authority_status(self, *, project_id: int) -> JsonObject:
        """Return an authority status payload."""
        self.calls.append(("authority_status", {"project_id": project_id}))
        return {
            "ok": True,
            "data": {"project_id": project_id, "status": "missing"},
            "warnings": [],
            "errors": [],
        }

    def authority_invariants(
        self,
        *,
        project_id: int,
        spec_version_id: int | None = None,
    ) -> JsonObject:
        """Return an authority invariants payload."""
        self.calls.append(
            (
                "authority_invariants",
                {
                    "project_id": project_id,
                    "spec_version_id": spec_version_id,
                },
            )
        )
        return {
            "ok": True,
            "data": {
                "project_id": project_id,
                "spec_version_id": spec_version_id,
            },
            "warnings": [],
            "errors": [],
        }

    def story_show(self, *, story_id: int) -> JsonObject:
        """Return a story detail payload."""
        self.calls.append(("story_show", {"story_id": story_id}))
        return {
            "ok": True,
            "data": {"story_id": story_id},
            "warnings": [],
            "errors": [],
        }

    def sprint_candidates(self, *, project_id: int) -> JsonObject:
        """Return a sprint candidates payload."""
        self.calls.append(("sprint_candidates", {"project_id": project_id}))
        return {
            "ok": True,
            "data": {"project_id": project_id, "items": []},
            "warnings": [],
            "errors": [],
        }

    def context_pack(
        self,
        *,
        project_id: int,
        phase: str = "overview",
    ) -> JsonObject:
        """Return a context pack payload."""
        self.calls.append(
            ("context_pack", {"project_id": project_id, "phase": phase})
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "phase": phase},
            "warnings": [],
            "errors": [],
        }

    def status(self, *, project_id: int) -> JsonObject:
        """Return a root status payload."""
        self.calls.append(("status", {"project_id": project_id}))
        return {
            "ok": True,
            "data": {"project_id": project_id, "status": "ok"},
            "warnings": [],
            "errors": [],
        }

    def doctor(self) -> JsonObject:
        """Return a doctor diagnostics payload."""
        self.calls.append(("doctor", {}))
        return {"ok": True, "data": {"checks": []}, "warnings": [], "errors": []}

    def schema_check(self) -> JsonObject:
        """Return a schema check diagnostics payload."""
        self.calls.append(("schema_check", {}))
        return {"ok": True, "data": {"stores": []}, "warnings": [], "errors": []}

    def capabilities(self) -> JsonObject:
        """Return a capabilities payload."""
        self.calls.append(("capabilities", {}))
        return {"ok": True, "data": {"commands": []}, "warnings": [], "errors": []}

    def command_schema(self, *, command_name: str) -> JsonObject:
        """Return a command schema payload."""
        self.calls.append(("command_schema", {"command_name": command_name}))
        return {
            "ok": True,
            "data": {"name": command_name},
            "warnings": [],
            "errors": [],
        }

    def mutation_show(self, *, mutation_event_id: int) -> JsonObject:
        """Return a mutation ledger row payload."""
        self.calls.append(("mutation_show", {"mutation_event_id": mutation_event_id}))
        return {
            "ok": True,
            "data": {"mutation_event_id": mutation_event_id},
            "warnings": [],
            "errors": [],
        }

    def mutation_list(
        self,
        *,
        project_id: int | None = None,
        status: str | None = None,
    ) -> JsonObject:
        """Return mutation ledger rows."""
        self.calls.append(
            ("mutation_list", {"project_id": project_id, "status": status})
        )
        return {"ok": True, "data": {"items": []}, "warnings": [], "errors": []}

    def mutation_resume(
        self,
        *,
        mutation_event_id: int,
        correlation_id: str | None = None,
    ) -> JsonObject:
        """Return a mutation resume payload."""
        self.calls.append(
            (
                "mutation_resume",
                {
                    "mutation_event_id": mutation_event_id,
                    "correlation_id": correlation_id,
                },
            )
        )
        return {
            "ok": True,
            "data": {"mutation_event_id": mutation_event_id},
            "warnings": [],
            "errors": [],
        }


class _FailingApplication(_FakeApplication):
    """Fake application that returns a structured command failure."""

    def project_show(self, *, project_id: int) -> JsonObject:
        """Return a structured project show failure."""
        self.calls.append(("project_show", {"project_id": project_id}))
        return {
            "ok": False,
            "data": None,
            "warnings": [
                {
                    "code": "CACHE_STALE",
                    "message": "Cached projection is stale.",
                    "details": {"project_id": project_id},
                    "remediation": ["Retry after refresh."],
                }
            ],
            "errors": [
                {
                    "code": "PROJECT_NOT_FOUND",
                    "message": "Project does not exist.",
                    "details": {"project_id": project_id},
                    "remediation": ["agileforge project list"],
                    "exit_code": ERROR_EXIT_CODE,
                    "retryable": False,
                }
            ],
        }


class _ExplodingApplication(_FakeApplication):
    """Fake application that raises an unexpected exception."""

    def project_list(self) -> JsonObject:
        """Raise an unexpected runtime error."""
        self.calls.append(("project_list", {}))
        msg = "projection exploded"
        raise RuntimeError(msg)


def _stdout_payload(capsys: pytest.CaptureFixture[str]) -> JsonObject:
    """Return captured stdout as a JSON object."""
    captured = capsys.readouterr()
    assert captured.err == ""
    return cast("JsonObject", json.loads(captured.out))


def _mapping(value: object) -> JsonObject:
    """Return a JSON object field from a payload."""
    assert isinstance(value, dict)
    return cast("JsonObject", value)


def _sequence(value: object) -> list[object]:
    """Return a JSON list field from a payload."""
    assert isinstance(value, list)
    return cast("list[object]", value)


def _first_mapping(value: object) -> JsonObject:
    """Return the first JSON object from a list field."""
    items = _sequence(value)
    assert items
    first = items[0]
    assert isinstance(first, dict)
    return cast("JsonObject", first)


def _agileforge_spec_profile_payload() -> dict[str, object]:
    """Return a minimal valid AgileForge spec profile payload."""
    return {
        "schema_version": "agileforge.spec.v1",
        "artifact_id": "SPEC.test",
        "title": "Test Spec",
        "status": "draft",
        "version": "0.1",
        "created_at": "2026-05-18",
        "updated_at": "2026-05-18",
        "summary": "Exercise the spec profile CLI.",
        "problem_statement": "Agents need a structured spec profile smoke test.",
        "items": [
            {
                "id": "GOAL.test.profile-cli",
                "type": "GOAL",
                "status": "proposed",
                "title": "Profile CLI",
                "statement": "Expose spec profile utilities through the CLI.",
            },
            {
                "id": "REQ.test.render-markdown",
                "type": "REQ",
                "status": "proposed",
                "title": "Render markdown",
                "statement": "The CLI MUST render deterministic Markdown.",
                "level": "MUST",
                "verification": "unit-test",
                "acceptance": [
                    "Given a valid spec profile, when validation renders Markdown, "
                    "then the target file starts with the spec title."
                ],
            },
        ],
        "relations": [
            {
                "from": "REQ.test.render-markdown",
                "type": "satisfies",
                "to": "GOAL.test.profile-cli",
            }
        ],
        "controlled_terms": [],
        "external_references": [],
        "rendering": {
            "markdown_profile": "agileforge.spec_markdown.v1",
            "rendered_markdown_sha256": None,
        },
    }


def test_cli_writes_success_json_to_stdout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify CLI emits a success envelope to stdout only."""
    app = _FakeApplication()

    rc = main(["project", "list"], application=app)

    payload = _stdout_payload(capsys)
    assert rc == 0
    assert payload["ok"] is True
    assert payload["data"] == {"items": []}
    assert payload["warnings"] == []
    assert payload["errors"] == []
    assert _mapping(payload["meta"])["command"] == "agileforge project list"
    assert app.calls == [("project_list", {})]


def test_spec_profile_schema_command_outputs_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Expose the AgileForge spec profile JSON Schema through the CLI."""
    rc = main(["spec", "profile", "schema"], application=_FakeApplication())

    payload = _stdout_payload(capsys)
    assert rc == 0
    assert payload["ok"] is True
    schema_id = _mapping(_mapping(payload["data"])["schema"])["$id"]
    assert isinstance(schema_id, str)
    assert schema_id.endswith("agileforge.spec.v1.json")
    assert _mapping(payload["meta"])["command"] == "agileforge spec profile schema"


def test_spec_profile_validate_can_render_markdown(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Validate a spec profile JSON file and write its Markdown rendering."""
    spec_path = tmp_path / "spec.json"
    render_path = tmp_path / "spec.md"
    spec_path.write_text(
        json.dumps(_agileforge_spec_profile_payload()),
        encoding="utf-8",
    )

    rc = main(
        [
            "spec",
            "profile",
            "validate",
            "--spec-file",
            str(spec_path),
            "--render-md",
            str(render_path),
        ],
        application=_FakeApplication(),
    )

    payload = _stdout_payload(capsys)
    data = _mapping(payload["data"])
    assert rc == 0
    assert payload["ok"] is True
    assert data["format"] == "agileforge.spec.v1"
    assert str(data["spec_sha256"]).startswith("sha256:")
    assert str(data["rendered_markdown_sha256"]).startswith("sha256:")
    assert render_path.read_text(encoding="utf-8").startswith("# Test Spec")
    assert _mapping(payload["meta"])["command"] == "agileforge spec profile validate"


def test_spec_profile_validate_missing_spec_file_returns_registered_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Return a registered error code when the profile input file is missing."""
    missing_spec_path = tmp_path / "missing-spec.json"

    rc = main(
        [
            "spec",
            "profile",
            "validate",
            "--spec-file",
            str(missing_spec_path),
        ],
        application=_FakeApplication(),
    )

    payload = _stdout_payload(capsys)
    error = _first_mapping(payload["errors"])
    assert rc == INVALID_COMMAND_EXIT_CODE
    assert payload["ok"] is False
    assert error["code"] == ErrorCode.SPEC_FILE_NOT_FOUND.value
    assert _mapping(error["details"])["spec_file"] == str(missing_spec_path.resolve())


def test_spec_profile_validate_invalid_spec_file_returns_registered_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Return a registered error code when profile JSON validation fails."""
    spec_path = tmp_path / "invalid-spec.json"
    spec_path.write_text('{"schema_version": "agileforge.spec.v1"}', encoding="utf-8")

    rc = main(
        [
            "spec",
            "profile",
            "validate",
            "--spec-file",
            str(spec_path),
        ],
        application=_FakeApplication(),
    )

    payload = _stdout_payload(capsys)
    error = _first_mapping(payload["errors"])
    assert rc == INVALID_COMMAND_EXIT_CODE
    assert payload["ok"] is False
    assert error["code"] == ErrorCode.SPEC_FILE_INVALID.value
    assert _mapping(error["details"])["spec_file"] == str(spec_path.resolve())


def test_spec_profile_validate_render_write_failure_returns_invalid_command(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Report Markdown render write failures separately from input validation."""
    spec_path = tmp_path / "spec.json"
    render_path = tmp_path / "render-target"
    render_path.mkdir()
    spec_path.write_text(
        json.dumps(_agileforge_spec_profile_payload()),
        encoding="utf-8",
    )

    rc = main(
        [
            "spec",
            "profile",
            "validate",
            "--spec-file",
            str(spec_path),
            "--render-md",
            str(render_path),
        ],
        application=_FakeApplication(),
    )

    payload = _stdout_payload(capsys)
    error = _first_mapping(payload["errors"])
    assert rc == INVALID_COMMAND_EXIT_CODE
    assert payload["ok"] is False
    assert error["code"] == ErrorCode.INVALID_COMMAND.value
    assert _mapping(error["details"])["render_md"] == str(render_path.resolve())


def test_cli_redirects_application_stdout_away_from_json_envelope(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify lower-layer stdout noise is suppressed, not moved to stderr."""
    app = _FakeApplication()

    def noisy_project_list() -> JsonObject:
        sys.stdout.write("LiteLLM completion() model=openai/example\n")
        app.calls.append(("project_list", {}))
        return {"ok": True, "data": {"items": []}, "warnings": [], "errors": []}

    cast("Any", app).project_list = noisy_project_list

    rc = main(["project", "list"], application=app)

    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.startswith("{")
    assert "LiteLLM" not in captured.out
    assert captured.err == ""
    payload = cast("JsonObject", json.loads(captured.out))
    assert payload["ok"] is True
    assert app.calls == [("project_list", {})]


def test_cli_wraps_success_source_fingerprint_in_meta(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Expose successful result source fingerprints in envelope metadata."""
    source_fingerprint = "sha256:" + "b" * 64
    app = _FakeApplication()

    def project_list_with_source() -> JsonObject:
        app.calls.append(("project_list", {}))
        return {
            "ok": True,
            "data": {"items": [], "source_fingerprint": source_fingerprint},
            "warnings": [],
            "errors": [],
        }

    cast("Any", app).project_list = project_list_with_source

    rc = main(["project", "list"], application=app)

    payload = _stdout_payload(capsys)
    assert rc == 0
    assert _mapping(payload["data"])["source_fingerprint"] == source_fingerprint
    assert _mapping(payload["meta"])["source_fingerprint"] == source_fingerprint


def test_cli_routes_project_create_to_application(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify project create routes mutation args to the application facade."""
    app = _FakeApplication()

    rc = main(
        [
            "project",
            "create",
            "--name",
            "CLI Project",
            "--spec-file",
            "specs/app.md",
            "--idempotency-key",
            "create-cli-project-001",
            "--changed-by",
            "test-agent",
        ],
        application=app,
    )

    payload = _stdout_payload(capsys)
    assert rc == 0
    assert _mapping(payload["meta"])["command"] == "agileforge project create"
    assert app.calls == [
        (
            "project_create",
            {
                "name": "CLI Project",
                "spec_file": "specs/app.md",
                "idempotency_key": "create-cli-project-001",
                "dry_run": False,
                "dry_run_id": None,
                "correlation_id": None,
                "changed_by": "test-agent",
            },
        )
    ]


def test_cli_routes_project_create_dry_run_without_idempotency_key(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify project create dry-run routes without consuming idempotency."""
    app = _FakeApplication()

    rc = main(
        [
            "project",
            "create",
            "--name",
            "CLI Project",
            "--spec-file",
            "specs/app.md",
            "--dry-run",
            "--dry-run-id",
            "preview-001",
        ],
        application=app,
    )

    payload = _stdout_payload(capsys)
    assert rc == 0
    assert _mapping(payload["meta"])["command"] == "agileforge project create"
    assert app.calls == [
        (
            "project_create",
            {
                "name": "CLI Project",
                "spec_file": "specs/app.md",
                "idempotency_key": None,
                "dry_run": True,
                "dry_run_id": "preview-001",
                "correlation_id": None,
                "changed_by": "cli-agent",
            },
        )
    ]


@pytest.mark.parametrize(
    "argv",
    [
        [
            "project",
            "create",
            "--name",
            "CLI Project",
            "--spec-file",
            "specs/app.md",
            "--dry-run",
            "--dry-run-id",
            "preview-001",
            "--idempotency-key",
            "create-001",
        ],
    ],
)
def test_cli_rejects_invalid_project_create_idempotency_args(
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify project create enforces dry-run/idempotency CLI contract."""
    app = _FakeApplication()

    rc = main(argv, application=app)

    payload = _stdout_payload(capsys)
    assert rc == INVALID_COMMAND_EXIT_CODE
    assert _mapping(payload["meta"])["command"] == "agileforge project create"
    assert _first_mapping(payload["errors"])["code"] == "INVALID_COMMAND"
    assert app.calls == []


def test_cli_generates_auto_idempotency_key_when_omitted() -> None:
    """Verify project create automatically generates an idempotency key if omitted."""
    app = _FakeApplication()

    rc = main(
        [
            "project",
            "create",
            "--name",
            "CLI Project",
            "--spec-file",
            "specs/app.md",
        ],
        application=app,
    )

    assert rc == 0
    assert len(app.calls) == 1
    call_args = app.calls[0][1]
    key = call_args["idempotency_key"]
    assert isinstance(key, str)
    assert key.startswith("auto-")


def test_cli_routes_project_setup_retry_to_application(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify project setup retry routes stale guards and recovery id."""
    app = _FakeApplication()

    rc = main(
        [
            "project",
            "setup",
            "retry",
            "--project-id",
            str(PROJECT_ID),
            "--spec-file",
            "specs/app.md",
            "--expected-state",
            "SETUP_REQUIRED",
            "--expected-context-fingerprint",
            "ctx123",
            "--recovery-mutation-event-id",
            "42",
            "--idempotency-key",
            "retry-cli-project-001",
        ],
        application=app,
    )

    payload = _stdout_payload(capsys)
    assert rc == 0
    assert _mapping(payload["meta"])["command"] == "agileforge project setup retry"
    assert app.calls == [
        (
            "project_setup_retry",
            {
                "project_id": PROJECT_ID,
                "spec_file": "specs/app.md",
                "expected_state": "SETUP_REQUIRED",
                "expected_context_fingerprint": "ctx123",
                "recovery_mutation_event_id": 42,
                "idempotency_key": "retry-cli-project-001",
                "dry_run": False,
                "dry_run_id": None,
                "correlation_id": None,
                "changed_by": "cli-agent",
            },
        )
    ]


def test_cli_routes_project_setup_retry_dry_run_without_idempotency_key(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify setup retry dry-run routes without consuming idempotency."""
    app = _FakeApplication()

    rc = main(
        [
            "project",
            "setup",
            "retry",
            "--project-id",
            str(PROJECT_ID),
            "--spec-file",
            "specs/app.md",
            "--expected-state",
            "SETUP_REQUIRED",
            "--expected-context-fingerprint",
            "ctx123",
            "--recovery-mutation-event-id",
            "42",
            "--dry-run",
            "--dry-run-id",
            "retry-preview-001",
        ],
        application=app,
    )

    payload = _stdout_payload(capsys)
    assert rc == 0
    assert _mapping(payload["meta"])["command"] == "agileforge project setup retry"
    assert app.calls == [
        (
            "project_setup_retry",
            {
                "project_id": PROJECT_ID,
                "spec_file": "specs/app.md",
                "expected_state": "SETUP_REQUIRED",
                "expected_context_fingerprint": "ctx123",
                "recovery_mutation_event_id": 42,
                "idempotency_key": None,
                "dry_run": True,
                "dry_run_id": "retry-preview-001",
                "correlation_id": None,
                "changed_by": "cli-agent",
            },
        )
    ]


def test_cli_rejects_project_setup_retry_dry_run_with_idempotency_key(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify setup retry dry-run rejects idempotency keys."""
    app = _FakeApplication()

    rc = main(
        [
            "project",
            "setup",
            "retry",
            "--project-id",
            str(PROJECT_ID),
            "--spec-file",
            "specs/app.md",
            "--expected-state",
            "SETUP_REQUIRED",
            "--expected-context-fingerprint",
            "ctx123",
            "--dry-run",
            "--dry-run-id",
            "retry-preview-001",
            "--idempotency-key",
            "retry-001",
        ],
        application=app,
    )

    payload = _stdout_payload(capsys)
    assert rc == INVALID_COMMAND_EXIT_CODE
    assert _mapping(payload["meta"])["command"] == "agileforge project setup retry"
    assert _first_mapping(payload["errors"])["code"] == "INVALID_COMMAND"
    assert app.calls == []


def test_cli_routes_authority_status(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify authority status command routes project id."""
    app = _FakeApplication()

    rc = main(
        ["authority", "status", "--project-id", str(PROJECT_ID)],
        application=app,
    )

    payload = _stdout_payload(capsys)
    assert rc == 0
    data = _mapping(payload["data"])
    assert data["project_id"] == PROJECT_ID
    assert data["status"] == "missing"
    assert _mapping(payload["meta"])["command"] == "agileforge authority status"
    assert app.calls == [("authority_status", {"project_id": PROJECT_ID})]


@pytest.mark.parametrize(
    ("argv", "expected_call", "expected_command"),
    [
        (["doctor"], ("doctor", {}), "agileforge doctor"),
        (["schema", "check"], ("schema_check", {}), "agileforge schema check"),
        (["capabilities"], ("capabilities", {}), "agileforge capabilities"),
        (
            ["command", "schema", "agileforge status"],
            ("command_schema", {"command_name": "agileforge status"}),
            "agileforge command schema",
        ),
        (
            ["mutation", "show", "--mutation-event-id", "101"],
            ("mutation_show", {"mutation_event_id": 101}),
            "agileforge mutation show",
        ),
        (
            [
                "mutation",
                "list",
                "--project-id",
                "7",
                "--status",
                "recovery_required",
            ],
            ("mutation_list", {"project_id": 7, "status": "recovery_required"}),
            "agileforge mutation list",
        ),
        (
            [
                "mutation",
                "resume",
                "--mutation-event-id",
                "101",
                "--correlation-id",
                "corr-1",
            ],
            (
                "mutation_resume",
                {"mutation_event_id": 101, "correlation_id": "corr-1"},
            ),
            "agileforge mutation resume",
        ),
    ],
)
def test_cli_routes_phase_2a_operational_commands(
    argv: list[str],
    expected_call: tuple[str, dict[str, object]],
    expected_command: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify Phase 2A operational commands route to the application facade."""
    app = _FakeApplication()

    rc = main(argv, application=app)

    payload = _stdout_payload(capsys)
    assert rc == 0
    assert _mapping(payload["meta"])["command"] == expected_command
    assert app.calls == [expected_call]


def test_cli_uses_error_exit_code_and_preserves_warnings(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify structured errors are enveloped with their first exit code."""
    app = _FailingApplication()

    rc = main(
        ["project", "show", "--project-id", str(PROJECT_ID)],
        application=app,
    )

    payload = _stdout_payload(capsys)
    assert rc == ERROR_EXIT_CODE
    assert payload["ok"] is False
    assert payload["data"] is None
    assert _mapping(payload["meta"])["command"] == "agileforge project show"
    assert payload["warnings"] == [
        {
            "code": "CACHE_STALE",
            "message": "Cached projection is stale.",
            "details": {"project_id": PROJECT_ID},
            "remediation": ["Retry after refresh."],
        }
    ]
    assert payload["errors"] == [
        {
            "code": "PROJECT_NOT_FOUND",
            "message": "Project does not exist.",
            "details": {"project_id": PROJECT_ID},
            "remediation": ["agileforge project list"],
            "exit_code": ERROR_EXIT_CODE,
            "retryable": False,
        }
    ]


def test_cli_preserves_error_data_from_service_result(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Keep structured failure data when wrapping service errors."""
    app = _FakeApplication()

    def mutation_resume_conflict(
        *,
        mutation_event_id: int,
        correlation_id: str | None = None,
    ) -> JsonObject:
        app.calls.append(
            (
                "mutation_resume",
                {
                    "mutation_event_id": mutation_event_id,
                    "correlation_id": correlation_id,
                },
            )
        )
        return {
            "ok": False,
            "data": {"mutation_event_id": mutation_event_id, "status": "pending"},
            "warnings": [],
            "errors": [
                {
                    "code": "MUTATION_RESUME_CONFLICT",
                    "message": "Another worker acquired recovery.",
                    "details": {"mutation_event_id": mutation_event_id},
                    "remediation": [],
                    "exit_code": 1,
                    "retryable": True,
                }
            ],
        }

    cast("Any", app).mutation_resume = mutation_resume_conflict

    rc = main(
        ["mutation", "resume", "--mutation-event-id", "101"],
        application=app,
    )

    payload = _stdout_payload(capsys)
    assert rc == 1
    assert payload["ok"] is False
    assert payload["data"] == {"mutation_event_id": 101, "status": "pending"}
    assert app.calls == [
        (
            "mutation_resume",
            {"mutation_event_id": 101, "correlation_id": None},
        )
    ]


def test_cli_unexpected_exceptions_return_json_envelope(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify unexpected application errors stay inside the CLI envelope."""
    app = _ExplodingApplication()

    rc = main(["project", "list"], application=app)

    payload = _stdout_payload(capsys)
    assert rc == COMMAND_EXCEPTION_EXIT_CODE
    assert payload["ok"] is False
    assert payload["data"] is None
    assert payload["warnings"] == []
    assert _mapping(payload["meta"])["command"] == "agileforge"
    error = _first_mapping(payload["errors"])
    assert error["code"] == "COMMAND_EXCEPTION"
    assert error["message"] == "projection exploded"
    assert error["exit_code"] == COMMAND_EXCEPTION_EXIT_CODE
    assert error["retryable"] is False
    assert error["details"] == {"exception_type": "RuntimeError"}


def test_cli_parse_errors_return_json_envelope(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify direct main parse errors return structured JSON."""
    rc = main(["project", "show"])

    payload = _stdout_payload(capsys)
    assert rc == INVALID_COMMAND_EXIT_CODE
    assert payload["ok"] is False
    assert payload["data"] is None
    assert payload["warnings"] == []
    assert _mapping(payload["meta"])["command"] == "agileforge"
    error = _first_mapping(payload["errors"])
    assert error["code"] == "INVALID_COMMAND"
    assert error["exit_code"] == INVALID_COMMAND_EXIT_CODE
    assert "--project-id" in str(error["message"])


def test_module_parse_errors_return_json_envelope() -> None:
    """Verify python -m parse errors return structured JSON."""
    result = subprocess.run(  # nosec B603
        [sys.executable, "-m", "cli.main", "project", "show"],
        cwd=Path.cwd(),
        text=True,
        capture_output=True,
        check=False,
    )

    payload = cast("JsonObject", json.loads(result.stdout))
    assert result.returncode == INVALID_COMMAND_EXIT_CODE
    assert result.stderr == ""
    assert payload["ok"] is False
    assert _mapping(payload["meta"])["command"] == "agileforge"
    error = _first_mapping(payload["errors"])
    assert error["code"] == "INVALID_COMMAND"
    assert error["exit_code"] == INVALID_COMMAND_EXIT_CODE


def test_top_level_help_describes_agent_workbench_commands(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify help output is useful for agents and developers."""
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])

    captured = capsys.readouterr()
    assert exc_info.value.code == 0
    assert captured.err == ""
    assert "AgileForge" in captured.out
    assert "agent-facing CLI" in captured.out
    assert "read-only" not in captured.out
    assert "agileforge project list" in captured.out
    assert "agileforge authority review --project-id 1" in captured.out
    assert "agileforge authority accept --project-id 1" in captured.out
    assert (
        'agileforge authority reject --project-id 1 --review-token <review_token> '
        '--reason "..."'
    ) in captured.out
    assert (
        "agileforge context pack --project-id 1 --phase sprint-planning"
        in captured.out
    )


def test_cli_configures_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify CLI startup configures file logging without console logging."""
    calls: list[dict[str, object]] = []

    def fake_configure_logging(**kwargs: object) -> None:
        calls.append(dict(kwargs))

    monkeypatch.setattr("cli.main.configure_logging", fake_configure_logging)

    exit_code = main(["project", "list"], application=_FakeApplication())

    assert exit_code == 0
    assert calls == [{"console": False}]


def test_packaged_project_exposes_api_module_from_other_cwd(
    tmp_path: Path,
) -> None:
    """Verify package metadata keeps top-level api importable outside repo cwd."""
    uv_path = shutil.which("uv")
    assert uv_path is not None

    result = subprocess.run(  # noqa: S603  # nosec B603
        [
            uv_path,
            "run",
            "--project",
            str(Path.cwd()),
            "--frozen",
            "python",
            "-c",
            (
                "import importlib.util; "
                "spec = importlib.util.find_spec('api'); "
                "print(spec.origin if spec else 'MISSING')"
            ),
        ],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stderr == ""
    assert result.stdout.rstrip().endswith("api.py")


@pytest.mark.parametrize(
    ("argv", "expected_command", "expected_call"),
    [
        (
            ["project", "show", "--project-id", str(PROJECT_ID)],
            "agileforge project show",
            ("project_show", {"project_id": PROJECT_ID}),
        ),
        (
            ["workflow", "state", "--project-id", str(PROJECT_ID)],
            "agileforge workflow state",
            ("workflow_state", {"project_id": PROJECT_ID}),
        ),
        (
            ["workflow", "next", "--project-id", str(PROJECT_ID)],
            "agileforge workflow next",
            ("workflow_next", {"project_id": PROJECT_ID}),
        ),
        (
            [
                "authority",
                "invariants",
                "--project-id",
                str(PROJECT_ID),
                "--spec-version-id",
                str(SPEC_VERSION_ID),
            ],
            "agileforge authority invariants",
            (
                "authority_invariants",
                {"project_id": PROJECT_ID, "spec_version_id": SPEC_VERSION_ID},
            ),
        ),
        (
            ["story", "show", "--story-id", str(STORY_ID)],
            "agileforge story show",
            ("story_show", {"story_id": STORY_ID}),
        ),
        (
            ["sprint", "candidates", "--project-id", str(PROJECT_ID)],
            "agileforge sprint candidates",
            ("sprint_candidates", {"project_id": PROJECT_ID}),
        ),
        (
            [
                "context",
                "pack",
                "--project-id",
                str(PROJECT_ID),
                "--phase",
                "sprint-planning",
            ],
            "agileforge context pack",
            (
                "context_pack",
                {"project_id": PROJECT_ID, "phase": "sprint-planning"},
            ),
        ),
        (
            ["status", "--project-id", str(PROJECT_ID)],
            "agileforge status",
            ("status", {"project_id": PROJECT_ID}),
        ),
    ],
)
def test_cli_routes_phase_1_command_surface(
    argv: list[str],
    expected_command: str,
    expected_call: tuple[str, dict[str, object]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify every Phase 1 command is routed through the CLI transport."""
    app = _FakeApplication()

    rc = main(argv, application=app)

    payload = _stdout_payload(capsys)
    assert rc == 0
    assert payload["ok"] is True
    assert _mapping(payload["meta"])["command"] == expected_command
    assert app.calls == [expected_call]
