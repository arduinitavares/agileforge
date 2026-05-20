"""Phase 1 integration tests for the agent workbench CLI."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess  # nosec B404
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

from sqlalchemy import create_engine

from cli.main import main
from models.core import Product, Sprint, SprintStory, Team, UserStory
from models.enums import SprintStatus, StoryStatus
from models.specs import (
    CompiledSpecAuthority,
    SpecAuthorityAcceptance,
    SpecRegistry,
)
from services.agent_workbench.application import AgentWorkbenchApplication
from services.agent_workbench.authority_projection import AuthorityProjectionService
from services.agent_workbench.read_projection import ReadProjectionService
from tests.typing_helpers import require_id
from utils.agileforge_spec_profile import (
    TechnicalSpecArtifact,
    canonical_spec_hash,
    canonical_spec_json,
)

if TYPE_CHECKING:
    import pytest
    from sqlalchemy.engine import Engine
    from sqlmodel import Session

    from services.agent_workbench.session_reader import ReadOnlySessionReader

type JsonObject = dict[str, object]

SCHEMA_VERSION = "agileforge.cli.v1"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMPILER_VERSION = "1.0.0"
PROMPT_HASH = "a" * 64
SEEDED_STORY_COUNT = 2
SCHEMA_NOT_READY_EXIT_CODE = 5
PHASE_1_GROUPS = (
    "project",
    "workflow",
    "authority",
    "story",
    "sprint",
    "context",
    "status",
)


def _structured_spec_content() -> str:
    """Return canonical structured spec content for Phase 1 disk checks."""
    artifact = TechnicalSpecArtifact.model_validate(
        {
            "schema_version": "agileforge.spec.v1",
            "artifact_id": "SPEC.phase1",
            "title": "Phase 1 Spec",
            "status": "draft",
            "version": "0.1",
            "created_at": "2026-05-20",
            "updated_at": "2026-05-20",
            "summary": "Exercise the Phase 1 CLI.",
            "problem_statement": "The CLI must expose read-only project context.",
            "items": [
                {
                    "id": "REQ.phase1-context",
                    "type": "REQ",
                    "status": "accepted",
                    "title": "Read-only context",
                    "statement": "The CLI must expose read-only project context.",
                    "level": "MUST",
                    "verification": "inspection",
                    "acceptance": [
                        "The CLI exposes read-only project context."
                    ],
                }
            ],
        }
    )
    return canonical_spec_json(artifact)


SPEC_CONTENT = _structured_spec_content()


class _SprintPlanningSessionReader:
    """Read-only session reader that returns sprint-planning workflow state."""

    def __init__(self) -> None:
        self.project_ids: list[int] = []

    def get_project_state(self, project_id: int) -> JsonObject:
        """Return deterministic sprint-planning state."""
        self.project_ids.append(project_id)
        return {
            "fsm_state": "SPRINT_SETUP",
            "setup_status": "passed",
            "setup_error": None,
        }


def _spec_hash(content: str) -> str:
    """Return SHA-256 hash for persisted spec content."""
    try:
        artifact = TechnicalSpecArtifact.model_validate(json.loads(content))
    except (json.JSONDecodeError, ValueError):
        return hashlib.sha256(content.encode("utf-8")).hexdigest()
    return canonical_spec_hash(artifact)


def _seed_phase1_project(
    session: Session,
    *,
    repo_root: Path,
) -> tuple[int, int, int]:
    """Seed a project with current authority, sprint, and candidate data."""
    spec_path = repo_root / "specs" / "phase1.json"
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(SPEC_CONTENT, encoding="utf-8")
    spec_hash = _spec_hash(SPEC_CONTENT)

    product = Product(
        name="Phase 1 Project",
        description="Seeded integration project",
        vision="Inspect read-only context",
        roadmap="Ship CLI workbench",
        spec_file_path="specs/phase1.json",
    )
    session.add(product)
    session.commit()
    session.refresh(product)
    project_id = require_id(product.product_id, "product_id")

    spec = SpecRegistry(
        product_id=project_id,
        spec_hash=spec_hash,
        content=SPEC_CONTENT,
        content_ref="specs/phase1.json",
        status="approved",
        approved_at=datetime(2026, 5, 14, 12, tzinfo=UTC),
        approved_by="integration-test",
        approval_notes="Approved for Phase 1 integration.",
    )
    session.add(spec)
    session.commit()
    session.refresh(spec)
    spec_version_id = require_id(spec.spec_version_id, "spec_version_id")

    authority = CompiledSpecAuthority(
        spec_version_id=spec_version_id,
        compiler_version=COMPILER_VERSION,
        prompt_hash=PROMPT_HASH,
        compiled_at=datetime(2026, 5, 14, 13, tzinfo=UTC),
        compiled_artifact_json=json.dumps(
            {"invariants": [{"id": "INV-1", "text": "Keep CLI read-only."}]}
        ),
        scope_themes=json.dumps(["read-only cli"]),
        invariants=json.dumps([{"id": "INV-1", "text": "Keep CLI read-only."}]),
        eligible_feature_ids=json.dumps([]),
        rejected_features=json.dumps([]),
        spec_gaps=json.dumps([]),
    )
    session.add(authority)
    session.flush()
    authority_id = require_id(authority.authority_id, "authority_id")

    acceptance = SpecAuthorityAcceptance(
        product_id=project_id,
        spec_version_id=spec_version_id,
        status="accepted",
        policy="human",
        decided_by="integration-test",
        decided_at=datetime(2026, 5, 14, 14, tzinfo=UTC),
        rationale="Accepted for integration test.",
        compiler_version=COMPILER_VERSION,
        prompt_hash=PROMPT_HASH,
        spec_hash=spec_hash,
        pending_authority_id=authority_id,
        terminal_decision_key=f"{project_id}:{spec_version_id}:{authority_id}",
    )
    session.add(acceptance)

    blocked_story = UserStory(
        product_id=project_id,
        title="Already planned story",
        story_description="This story is already in an open sprint.",
        acceptance_criteria="- It remains excluded from candidates.",
        status=StoryStatus.TO_DO,
        story_points=2,
        rank="1",
        is_refined=True,
        accepted_spec_version_id=spec_version_id,
    )
    candidate_story = UserStory(
        product_id=project_id,
        title="Ready candidate story",
        story_description="This story is ready for sprint planning.",
        acceptance_criteria="- It appears in sprint candidates.",
        status=StoryStatus.TO_DO,
        story_points=3,
        rank="2",
        is_refined=True,
        accepted_spec_version_id=spec_version_id,
    )
    session.add_all([blocked_story, candidate_story])
    session.commit()
    session.refresh(blocked_story)
    session.refresh(candidate_story)
    blocked_story_id = require_id(blocked_story.story_id, "blocked_story_id")
    candidate_story_id = require_id(candidate_story.story_id, "candidate_story_id")

    team = Team(name="Phase 1 Team")
    session.add(team)
    session.commit()
    session.refresh(team)

    sprint = Sprint(
        product_id=project_id,
        team_id=require_id(team.team_id, "team_id"),
        goal="Keep current work visible",
        start_date=date(2026, 5, 18),
        end_date=date(2026, 6, 1),
        status=SprintStatus.PLANNED,
    )
    session.add(sprint)
    session.commit()
    session.refresh(sprint)
    sprint_id = require_id(sprint.sprint_id, "sprint_id")

    session.add(SprintStory(sprint_id=sprint_id, story_id=blocked_story_id))
    session.commit()
    return project_id, candidate_story_id, spec_version_id


def _app_for_engine(
    *,
    engine: Engine,
    repo_root: Path,
    session_reader: _SprintPlanningSessionReader | None = None,
) -> AgentWorkbenchApplication:
    """Build the real application facade over injected read-only dependencies."""
    read_projection = ReadProjectionService(
        engine=engine,
        session_reader=cast(
            "ReadOnlySessionReader",
            session_reader or _SprintPlanningSessionReader(),
        ),
    )
    authority_projection = AuthorityProjectionService(
        engine=engine,
        repo_root=repo_root,
    )
    return AgentWorkbenchApplication(
        read_projection=read_projection,
        authority_projection=authority_projection,
    )


def _payload_from_stdout(capsys: pytest.CaptureFixture[str]) -> JsonObject:
    """Return captured CLI stdout as a JSON object and assert stderr is clean."""
    captured = capsys.readouterr()
    assert captured.err == ""
    return cast("JsonObject", json.loads(captured.out))


def _mapping(value: object) -> JsonObject:
    """Return a JSON object field from a payload."""
    assert isinstance(value, dict)
    return cast("JsonObject", value)


def _sequence(value: object) -> list[object]:
    """Return a JSON array field from a payload."""
    assert isinstance(value, list)
    return cast("list[object]", value)


def _cli_payload(
    argv: list[str],
    *,
    app: AgentWorkbenchApplication,
    capsys: pytest.CaptureFixture[str],
) -> JsonObject:
    """Invoke CLI transport and return a successful JSON envelope."""
    rc = main(argv, application=app)
    payload = _payload_from_stdout(capsys)

    assert rc == 0
    assert payload["ok"] is True
    assert payload["errors"] == []
    meta = _mapping(payload["meta"])
    assert meta["schema_version"] == SCHEMA_VERSION
    assert meta["command_version"] == "1"
    assert isinstance(meta["agileforge_version"], str)
    assert meta["agileforge_version"]
    assert meta["storage_schema_version"] == "3"
    assert isinstance(meta["correlation_id"], str)
    assert meta["correlation_id"]
    assert isinstance(meta["generated_at"], str)
    assert meta["generated_at"]
    return payload


def test_phase1_cli_drives_real_application_facade(
    session: Session,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify actual CLI transport drives the real Phase 1 application facade."""
    project_id, story_id, spec_version_id = _seed_phase1_project(
        session,
        repo_root=tmp_path,
    )
    engine = cast("Engine", session.get_bind())
    session_reader = _SprintPlanningSessionReader()
    app = _app_for_engine(
        engine=engine,
        repo_root=tmp_path,
        session_reader=session_reader,
    )

    command_cases = [
        (
            ["project", "list"],
            "agileforge project list",
            lambda data: (
                data["count"] == 1
                and _mapping(_sequence(data["items"])[0])["product_id"] == project_id
            ),
        ),
        (
            ["project", "show", "--project-id", str(project_id)],
            "agileforge project show",
            lambda data: (
                data["product_id"] == project_id
                and _mapping(data["structure_counts"])["user_stories"]
                == SEEDED_STORY_COUNT
                and _mapping(data["latest_approved_spec"])["spec_version_id"]
                == spec_version_id
            ),
        ),
        (
            ["workflow", "state", "--project-id", str(project_id)],
            "agileforge workflow state",
            lambda data: _mapping(data["state"])["fsm_state"] == "SPRINT_SETUP",
        ),
        (
            ["workflow", "next", "--project-id", str(project_id)],
            "agileforge workflow next",
            lambda data: (
                data["next_valid_commands"]
                == [f"agileforge sprint candidates --project-id {project_id}"]
            ),
        ),
        (
            ["authority", "status", "--project-id", str(project_id)],
            "agileforge authority status",
            lambda data: (
                data["status"] == "current"
                and _mapping(data["disk_spec"])["matches_accepted"] is True
            ),
        ),
        (
            ["authority", "invariants", "--project-id", str(project_id)],
            "agileforge authority invariants",
            lambda data: (
                data["count"] == 1
                and _mapping(_sequence(data["invariants"])[0])["id"] == "INV-1"
            ),
        ),
        (
            ["story", "show", "--story-id", str(story_id)],
            "agileforge story show",
            lambda data: (
                data["story_id"] == story_id
                and data["accepted_spec_version_id"] == spec_version_id
            ),
        ),
        (
            ["sprint", "candidates", "--project-id", str(project_id)],
            "agileforge sprint candidates",
            lambda data: (
                data["count"] == 1
                and _mapping(_sequence(data["items"])[0])["story_id"] == story_id
            ),
        ),
        (
            [
                "context",
                "pack",
                "--project-id",
                str(project_id),
                "--phase",
                "sprint-planning",
            ],
            "agileforge context pack",
            lambda data: (
                data["next_valid_commands"]
                == [f"agileforge sprint candidates --project-id {project_id}"]
                and data["blocked_future_commands"]
                == [
                    f"agileforge sprint generate --project-id {project_id} "
                    "--selected-story-ids 1,2,3"
                ]
                and data["blocked_commands"] == []
                and _mapping(_mapping(data["phase_data"])["sprint_candidates"])["count"]
                == 1
            ),
        ),
        (
            ["status", "--project-id", str(project_id)],
            "agileforge status",
            lambda data: (
                _mapping(data["project"])["product_id"] == project_id
                and _mapping(data["authority"])["status"] == "current"
            ),
        ),
    ]

    for argv, expected_command, assert_data in command_cases:
        payload = _cli_payload(argv, app=app, capsys=capsys)
        meta = _mapping(payload["meta"])
        data = _mapping(payload["data"])
        assert meta["command"] == expected_command
        assert assert_data(data)

    assert session_reader.project_ids


def test_phase1_cli_preserves_schema_not_ready_error_envelope(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify schema-not-ready errors stay structured through CLI transport."""
    db_path = tmp_path / "missing.sqlite3"
    app = _app_for_engine(
        engine=create_engine(f"sqlite:///{db_path.as_posix()}"),
        repo_root=tmp_path,
    )

    rc = main(["project", "list"], application=app)
    payload = _payload_from_stdout(capsys)

    assert rc == SCHEMA_NOT_READY_EXIT_CODE
    assert payload["ok"] is False
    assert payload["data"] is None
    meta = _mapping(payload["meta"])
    assert meta["schema_version"] == SCHEMA_VERSION
    assert meta["command"] == "agileforge project list"
    error = _mapping(_sequence(payload["errors"])[0])
    assert error["code"] == "SCHEMA_NOT_READY"
    assert error["exit_code"] == SCHEMA_NOT_READY_EXIT_CODE
    assert error["retryable"] is True
    assert "products" in _mapping(_mapping(error["details"])["missing"])
    assert not db_path.exists()


def test_phase1_console_script_help_is_wired(tmp_path: Path) -> None:
    """Verify installed console script is available through uv project run."""
    uv_path = shutil.which("uv")
    assert uv_path is not None

    result = subprocess.run(  # noqa: S603  # nosec B603
        [
            uv_path,
            "run",
            "--project",
            str(PROJECT_ROOT),
            "--frozen",
            "agileforge",
            "--help",
        ],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "usage: agileforge" in result.stdout
    for group in PHASE_1_GROUPS:
        assert group in result.stdout
