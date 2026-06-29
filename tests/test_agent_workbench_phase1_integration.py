"""Phase 1 integration tests for the agent workbench CLI."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess  # nosec B404
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import create_engine
from sqlmodel import Session as SQLModelSession
from sqlmodel import select

from cli.main import main
from models.core import Product, Sprint, SprintStory, Team, UserStory
from models.enums import SprintStatus, StoryStatus
from models.specs import (
    CompiledSpecAuthority,
    SpecAuthorityAcceptance,
    SpecRegistry,
)
from services.agent_workbench.application import AgentWorkbenchApplication
from services.agent_workbench.authority_projection import (
    AuthorityProjectionService,
    pending_authority_fingerprint,
)
from services.agent_workbench.evidence_collect import (
    IMPLEMENTATION_EVIDENCE_STATE_KEY,
    EvidenceCollectionRunner,
)
from services.agent_workbench.post_sprint_triage import build_triage_payload
from services.agent_workbench.read_projection import ReadProjectionService
from services.agent_workbench.scope_discovery import ScopeDiscoveryRunner
from services.agent_workbench.scope_extension import (
    ScopeExtensionPreconditions,
    ScopeExtensionRunner,
    ScopeExtensionStartRequest,
    ScopeExtensionValidateRequest,
    evaluate_scope_extension_preconditions,
)
from services.agent_workbench.version import STORAGE_SCHEMA_VERSION
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

    from services.agent_workbench.application import (
        _BacklogPhaseRunner,
        _StoryPhaseRunner,
    )
    from services.agent_workbench.authority_decision import (
        AuthorityAcceptRequest,
        AuthorityRejectRequest,
    )
    from services.agent_workbench.session_reader import ReadOnlySessionReader

type JsonObject = dict[str, object]

SCHEMA_VERSION = "agileforge.cli.v1"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMPILER_VERSION = "1.0.0"
PROMPT_HASH = "a" * 64
SEEDED_STORY_COUNT = 2
SCHEMA_NOT_READY_EXIT_CODE = 5
PHASE1_INVARIANT_ID = "INV-0000000000000001"
PHASE_1_GROUPS = (
    "project",
    "workflow",
    "authority",
    "evidence",
    "story",
    "sprint",
    "context",
    "status",
)
EXTENSION_REQUIREMENT = "Scope Extension Follow-up"
EXTENSION_STORY_TITLE = "Implement extension workflow follow-up"
EXTENSION_AUTHORITY_ID = 1001
EXTENSION_REVIEW_TOKEN = "scope-extension-review-token"  # noqa: S105  # nosec B105


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
                    "acceptance": ["The CLI exposes read-only project context."],
                }
            ],
        }
    )
    return canonical_spec_json(artifact)


SPEC_CONTENT = _structured_spec_content()


def _scope_extension_spec_content() -> str:
    """Return canonical amended spec content with one additive requirement."""
    payload = json.loads(SPEC_CONTENT)
    items = payload.setdefault("items", [])
    assert isinstance(items, list)
    items.append(
        {
            "id": "REQ.scope-extension-follow-up",
            "type": "REQ",
            "status": "accepted",
            "title": EXTENSION_REQUIREMENT,
            "statement": "The CLI must support additive extension follow-up work.",
            "level": "MUST",
            "verification": "system-test",
            "acceptance": [
                "Extension follow-up work can appear in sprint candidates.",
            ],
        }
    )
    artifact = TechnicalSpecArtifact.model_validate(payload)
    return canonical_spec_json(artifact)


def _phase1_compiled_authority_json(
    *,
    source_item_id: str = "REQ.phase1-context",
) -> str:
    """Return a supported v2 compiled-authority artifact for Phase 1 fixtures."""
    return json.dumps(
        {
            "schema_version": "agileforge.compiled_authority.v2",
            "scope_themes": ["read-only cli"],
            "domain": None,
            "invariants": [
                {
                    "id": PHASE1_INVARIANT_ID,
                    "type": "REQUIRED_FIELD",
                    "source_item_id": source_item_id,
                    "source_level": "MUST",
                    "parameters": {"field_name": "phase1_context"},
                }
            ],
            "eligible_feature_rules": [],
            "rejected_features": [],
            "gaps": [],
            "assumptions": [],
            "source_map": [],
            "compiler_version": COMPILER_VERSION,
            "prompt_hash": PROMPT_HASH,
            "ir_schema_version": None,
            "ir_provenance": None,
            "source_units": [],
            "requirement_candidates": [],
            "authority_mappings": [],
            "ir_packet_limits": None,
        }
    )


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


class _EvidenceWorkflowStub:
    """Workflow-state stub used by evidence CLI integration tests."""

    def __init__(self) -> None:
        self.state: dict[str, object] = {}

    def get_session_status(self, session_id: str) -> dict[str, object]:
        """Return cached workflow state."""
        _ = session_id
        return dict(self.state)

    def update_session_status(
        self,
        session_id: str,
        partial_update: dict[str, object],
    ) -> None:
        """Merge cached workflow state."""
        _ = session_id
        self.state.update(partial_update)


class _MutableWorkflowState:
    """Shared workflow state for read projection and mutation runner fakes."""

    def __init__(self, state: dict[str, object]) -> None:
        self.state = dict(state)
        self.project_ids: list[int] = []

    def get_project_state(self, project_id: int) -> JsonObject:
        """Return deterministic mutable workflow state."""
        self.project_ids.append(project_id)
        return dict(self.state)

    def get_session_status(self, session_id: str) -> dict[str, object]:
        """Return cached workflow state for mutation runners."""
        _ = session_id
        return dict(self.state)

    def update_session_status(
        self,
        session_id: str,
        partial_update: dict[str, object],
    ) -> None:
        """Merge workflow state updates."""
        _ = session_id
        self.state.update(partial_update)


class _ScopeExtensionIntegrationRunner:
    """Use real scope-extension validation/start with test workflow ports."""

    def __init__(
        self,
        *,
        session: Session,
        workflow: _MutableWorkflowState,
    ) -> None:
        self._session = session
        self._workflow = workflow

    def preconditions(
        self,
        *,
        project_id: int,
        workflow: dict[str, Any],
        sprint_candidate_count: int,
    ) -> ScopeExtensionPreconditions | None:
        """Evaluate scope-extension availability against real DB state."""
        state = workflow.get("state")
        workflow_state = (
            cast("dict[str, Any]", state) if isinstance(state, dict) else {}
        )
        return evaluate_scope_extension_preconditions(
            session=self._session,
            product_id=project_id,
            workflow_state=workflow_state,
            sprint_candidate_count=sprint_candidate_count,
        )

    def validate(self, request: ScopeExtensionValidateRequest) -> dict[str, Any]:
        """Delegate validation to the real scope-extension runner."""
        return self._runner().validate(request)

    def start(self, request: ScopeExtensionStartRequest) -> dict[str, Any]:
        """Delegate start to the real scope-extension runner."""
        return self._runner().start(request)

    def _runner(self) -> ScopeExtensionRunner:
        """Build a real runner over the shared test session."""
        return ScopeExtensionRunner(
            session=self._session,
            workflow_service=self._workflow,
            sprint_candidate_count_resolver=lambda _project_id: 0,
        )


class _ExtensionProjectSetupRunner:
    """Stub authority compiler while recording pending authority rows."""

    def __init__(
        self,
        *,
        session: Session,
        workflow: _MutableWorkflowState,
    ) -> None:
        self._session = session
        self._workflow = workflow
        self.calls: list[object] = []

    def create_project(self, request: object) -> dict[str, Any]:
        """Project create is outside this regression path."""
        self.calls.append(request)
        return {"ok": False, "data": None, "warnings": [], "errors": []}

    def retry_setup(self, request: object) -> dict[str, Any]:
        """Return unsupported setup retry for this regression path."""
        self.calls.append(request)
        return {"ok": False, "data": None, "warnings": [], "errors": []}

    def compile_authority(self, request: object) -> dict[str, Any]:
        """Compile pending authority and route workflow to review."""
        self.calls.append(request)
        request_data = cast("Any", request)
        project_id = int(request_data.project_id)
        spec_version_id = int(request_data.spec_version_id)
        authority = CompiledSpecAuthority(
            authority_id=EXTENSION_AUTHORITY_ID,
            spec_version_id=spec_version_id,
            compiler_version=COMPILER_VERSION,
            prompt_hash=PROMPT_HASH,
            compiled_at=datetime(2026, 6, 15, 12, tzinfo=UTC),
            compiled_artifact_json=_phase1_compiled_authority_json(
                source_item_id="REQ.scope-extension-follow-up"
            ),
            scope_themes=json.dumps(["scope extension"]),
            invariants=json.dumps(
                [{"id": PHASE1_INVARIANT_ID, "text": "Keep extension additive."}]
            ),
            eligible_feature_ids=json.dumps([]),
            rejected_features=json.dumps([]),
            spec_gaps=json.dumps([]),
        )
        self._session.add(authority)
        self._session.commit()
        self._workflow.update_session_status(
            str(project_id),
            {
                "fsm_state": "SETUP_REQUIRED",
                "setup_status": "authority_pending_review",
                "pending_authority_id": EXTENSION_AUTHORITY_ID,
                "pending_compiled_spec_version_id": spec_version_id,
            },
        )
        return {
            "ok": True,
            "data": {
                "project_id": project_id,
                "spec_version_id": spec_version_id,
                "status": "authority_pending_review",
                "authority_id": EXTENSION_AUTHORITY_ID,
            },
            "warnings": [],
            "errors": [],
        }


class _ExtensionAuthorityProjection:
    """State-backed authority projection for pending/current routing."""

    def __init__(self, workflow: _MutableWorkflowState) -> None:
        self._workflow = workflow

    def status(self, *, project_id: int) -> dict[str, Any]:
        """Return pending authority while review is active, current afterward."""
        data: dict[str, Any] = {"project_id": project_id, "status": "current"}
        if self._workflow.state.get("setup_status") == "authority_pending_review":
            data.update(
                {
                    "status": "pending_review",
                    "pending_authority_id": EXTENSION_AUTHORITY_ID,
                    "pending_compiled_spec_version_id": self._workflow.state.get(
                        "pending_compiled_spec_version_id"
                    ),
                }
            )
        return {"ok": True, "data": data, "warnings": [], "errors": []}

    def invariants(
        self,
        *,
        project_id: int,
        spec_version_id: int | None = None,
    ) -> dict[str, Any]:
        """Return a minimal invariant projection."""
        return {
            "ok": True,
            "data": {
                "project_id": project_id,
                "spec_version_id": spec_version_id,
                "invariants": [],
            },
            "warnings": [],
            "errors": [],
        }


class _ExtensionAuthorityReview:
    """Stub review packet after authority compile."""

    def review(
        self,
        *,
        project_id: int,
        include_spec: str = "auto",
        output_format: str = "json",
    ) -> dict[str, Any]:
        """Return an accept-ready review token."""
        _ = include_spec, output_format
        return {
            "ok": True,
            "data": {
                "project_id": project_id,
                "review_token": EXTENSION_REVIEW_TOKEN,
                "review_summary": {"acceptance_status": "accepted"},
            },
            "warnings": [],
            "errors": [],
        }


class _ExtensionAuthorityDecisionRunner:
    """Stub authority accept while recording amended authority acceptance."""

    def __init__(
        self,
        *,
        session: Session,
        workflow: _MutableWorkflowState,
    ) -> None:
        self._session = session
        self._workflow = workflow

    def accept(self, request: AuthorityAcceptRequest) -> dict[str, Any]:
        """Accept pending amended authority and resume phase generation."""
        pending_spec_version_id = self._workflow.state.get(
            "pending_compiled_spec_version_id"
        )
        assert isinstance(pending_spec_version_id, int)
        spec_version_id = pending_spec_version_id
        spec = self._session.get(SpecRegistry, spec_version_id)
        assert spec is not None
        spec.status = "approved"
        spec.approved_by = request.changed_by or "integration-test"
        spec.approval_notes = "Accepted amended authority for extension regression."
        self._session.add(spec)
        self._session.add(
            SpecAuthorityAcceptance(
                product_id=request.project_id,
                spec_version_id=spec_version_id,
                status="accepted",
                policy="human",
                decided_by=request.changed_by or "integration-test",
                decided_at=datetime(2026, 6, 15, 13, tzinfo=UTC),
                rationale="Accepted scope extension authority.",
                compiler_version=COMPILER_VERSION,
                prompt_hash=PROMPT_HASH,
                spec_hash=spec.spec_hash,
                pending_authority_id=EXTENSION_AUTHORITY_ID,
                terminal_decision_key=(
                    f"{request.project_id}:{spec_version_id}:{EXTENSION_AUTHORITY_ID}"
                ),
            )
        )
        self._session.commit()
        self._workflow.update_session_status(
            str(request.project_id),
            {
                "fsm_state": "BACKLOG_INTERVIEW",
                "setup_status": "passed",
                "accepted_spec_version_id": spec_version_id,
            },
        )
        return {
            "ok": True,
            "data": {
                "decision": "accepted",
                "project_id": request.project_id,
                "spec_version_id": spec_version_id,
            },
            "warnings": [],
            "errors": [],
        }

    def reject(self, request: AuthorityRejectRequest) -> dict[str, Any]:
        """Reject is outside this regression path."""
        _ = request
        return {"ok": False, "data": None, "warnings": [], "errors": []}


class _ExtensionBacklogRunner:
    """Stub Backlog generation/save and append extension work on save."""

    def __init__(
        self,
        *,
        session: Session,
        workflow: _MutableWorkflowState,
    ) -> None:
        self._session = session
        self._workflow = workflow

    def generate(
        self,
        *,
        project_id: int,
        user_input: str | None = None,
    ) -> dict[str, Any]:
        """Return a complete extension Backlog attempt."""
        _ = user_input
        self._workflow.update_session_status(
            str(project_id),
            {
                "fsm_state": "BACKLOG_REVIEW",
                "backlog_attempts": [
                    {
                        "attempt_id": "extension-backlog-attempt",
                        "artifact_fingerprint": "sha256:" + "b" * 64,
                        "is_complete": True,
                    }
                ],
            },
        )
        return {
            "ok": True,
            "data": {
                "project_id": project_id,
                "is_complete": True,
                "attempt_id": "extension-backlog-attempt",
                "artifact_fingerprint": "sha256:" + "b" * 64,
            },
            "warnings": [],
            "errors": [],
        }

    def save(
        self,
        *,
        project_id: int,
        attempt_id: str,
        expected_artifact_fingerprint: str,
        expected_state: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Persist one extension story candidate without touching completed work."""
        _ = attempt_id, expected_artifact_fingerprint, expected_state, idempotency_key
        accepted_spec_version_id = self._workflow.state["accepted_spec_version_id"]
        assert isinstance(accepted_spec_version_id, int)
        spec_version_id = accepted_spec_version_id
        story = UserStory(
            product_id=project_id,
            title=EXTENSION_STORY_TITLE,
            story_description="Extension story created by the stubbed Backlog phase.",
            acceptance_criteria="- Extension story is eligible for sprint planning.",
            status=StoryStatus.TO_DO,
            story_points=3,
            rank="100",
            source_requirement=EXTENSION_REQUIREMENT,
            story_origin="scope_extension",
            is_refined=True,
            accepted_spec_version_id=spec_version_id,
        )
        self._session.add(story)
        self._session.commit()
        self._workflow.update_session_status(
            str(project_id),
            {"fsm_state": "BACKLOG_PERSISTENCE"},
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "fsm_state": "BACKLOG_PERSISTENCE"},
            "warnings": [],
            "errors": [],
        }

    def history(self, *, project_id: int) -> dict[str, Any]:
        """Return no backlog history for this regression."""
        return {
            "ok": True,
            "data": {"project_id": project_id, "items": []},
            "warnings": [],
            "errors": [],
        }


class _ExtensionRoadmapRunner:
    """Stub Roadmap generation/save and append extension phase metadata."""

    def __init__(self, workflow: _MutableWorkflowState) -> None:
        self._workflow = workflow

    def generate(
        self,
        *,
        project_id: int,
        user_input: str | None = None,
    ) -> dict[str, Any]:
        """Return a complete extension Roadmap attempt."""
        _ = user_input
        self._workflow.update_session_status(
            str(project_id),
            {
                "fsm_state": "ROADMAP_REVIEW",
                "roadmap_attempts": [
                    {
                        "attempt_id": "extension-roadmap-attempt",
                        "artifact_fingerprint": "sha256:" + "c" * 64,
                        "is_complete": True,
                    }
                ],
            },
        )
        return {
            "ok": True,
            "data": {
                "project_id": project_id,
                "is_complete": True,
                "attempt_id": "extension-roadmap-attempt",
                "artifact_fingerprint": "sha256:" + "c" * 64,
            },
            "warnings": [],
            "errors": [],
        }

    def save(
        self,
        *,
        project_id: int,
        attempt_id: str,
        expected_artifact_fingerprint: str,
        expected_state: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Persist one extension Roadmap phase."""
        _ = attempt_id, expected_artifact_fingerprint, expected_state, idempotency_key
        self._workflow.update_session_status(
            str(project_id),
            {
                "fsm_state": "STORY_PERSISTENCE",
                "roadmap_releases": [
                    {
                        "title": "Extension Phase",
                        "assigned_backlog_items": [EXTENSION_REQUIREMENT],
                        "accepted_spec_version_id": self._workflow.state[
                            "accepted_spec_version_id"
                        ],
                    }
                ],
                "saved_story_requirements": {EXTENSION_REQUIREMENT: True},
            },
        )
        return {
            "ok": True,
            "data": {"project_id": project_id, "fsm_state": "STORY_PERSISTENCE"},
            "warnings": [],
            "errors": [],
        }

    def history(self, *, project_id: int) -> dict[str, Any]:
        """Return no Roadmap history for this regression."""
        return {
            "ok": True,
            "data": {"project_id": project_id, "items": []},
            "warnings": [],
            "errors": [],
        }


class _ExtensionStoryRunner:
    """Stub Story pending over the saved extension Roadmap phase."""

    def __init__(self, workflow: _MutableWorkflowState) -> None:
        self._workflow = workflow

    def pending(self, *, project_id: int) -> dict[str, Any]:
        """Expose the extension phase in Story pending output."""
        return {
            "ok": True,
            "data": {
                "project_id": project_id,
                "pending": [],
                "phases": [
                    {
                        "scope_id": "milestone_0",
                        "title": "Extension Phase",
                        "requirements": [EXTENSION_REQUIREMENT],
                        "covered": True,
                    }
                ],
                "roadmap_releases": self._workflow.state.get("roadmap_releases", []),
            },
            "warnings": [],
            "errors": [],
        }


class _EvidenceProductRepo:
    """Product repository facade over the test engine."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def get_by_id(self, product_id: int) -> object | None:
        """Return the seeded product when present."""
        with SQLModelSession(self._engine) as local_session:
            return local_session.get(Product, product_id)


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
        compiled_artifact_json=_phase1_compiled_authority_json(),
        scope_themes=json.dumps(["read-only cli"]),
        invariants=json.dumps(
            [{"id": PHASE1_INVARIANT_ID, "text": "Keep CLI read-only."}]
        ),
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


def _seed_completed_scope_extension_project(
    session: Session,
    *,
    repo_root: Path,
) -> tuple[int, int, int]:
    """Seed a completed project whose original execution scope is exhausted."""
    spec_path = repo_root / "specs" / "phase1.json"
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(SPEC_CONTENT, encoding="utf-8")
    spec_hash = _spec_hash(SPEC_CONTENT)

    product = Product(
        name="Scope Extension E2E Project",
        description="Completed integration project",
        vision="Extend completed scope",
        roadmap="Ship completed baseline",
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
        approval_notes="Approved baseline.",
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
        compiled_artifact_json=_phase1_compiled_authority_json(),
        scope_themes=json.dumps(["read-only cli"]),
        invariants=json.dumps(
            [{"id": PHASE1_INVARIANT_ID, "text": "Keep CLI read-only."}]
        ),
        eligible_feature_ids=json.dumps([]),
        rejected_features=json.dumps([]),
        spec_gaps=json.dumps([]),
    )
    session.add(authority)
    session.flush()
    authority_id = require_id(authority.authority_id, "authority_id")
    session.add(
        SpecAuthorityAcceptance(
            product_id=project_id,
            spec_version_id=spec_version_id,
            status="accepted",
            policy="human",
            decided_by="integration-test",
            decided_at=datetime(2026, 5, 14, 14, tzinfo=UTC),
            rationale="Accepted baseline.",
            compiler_version=COMPILER_VERSION,
            prompt_hash=PROMPT_HASH,
            spec_hash=spec_hash,
            pending_authority_id=authority_id,
            terminal_decision_key=f"{project_id}:{spec_version_id}:{authority_id}",
        )
    )

    completed_stories = [
        UserStory(
            product_id=project_id,
            title="Completed baseline story A",
            story_description="Completed baseline work.",
            acceptance_criteria="- Baseline work is complete.",
            status=StoryStatus.DONE,
            story_points=2,
            rank="1",
            is_refined=True,
            accepted_spec_version_id=spec_version_id,
        ),
        UserStory(
            product_id=project_id,
            title="Completed baseline story B",
            story_description="Completed baseline work.",
            acceptance_criteria="- Baseline work is complete.",
            status=StoryStatus.DONE,
            story_points=3,
            rank="2",
            is_refined=True,
            accepted_spec_version_id=spec_version_id,
        ),
    ]
    session.add_all(completed_stories)
    session.commit()
    for story in completed_stories:
        session.refresh(story)

    team = Team(name="Scope Extension Team")
    session.add(team)
    session.commit()
    session.refresh(team)

    sprint = Sprint(
        product_id=project_id,
        team_id=require_id(team.team_id, "team_id"),
        goal="Complete baseline scope",
        start_date=date(2026, 5, 18),
        end_date=date(2026, 6, 1),
        status=SprintStatus.COMPLETED,
        completed_at=datetime(2026, 6, 1, 12, tzinfo=UTC),
    )
    session.add(sprint)
    session.commit()
    session.refresh(sprint)
    sprint_id = require_id(sprint.sprint_id, "sprint_id")
    for story in completed_stories:
        session.add(
            SprintStory(
                sprint_id=sprint_id,
                story_id=require_id(story.story_id, "story_id"),
            )
        )
    session.commit()
    return project_id, spec_version_id, sprint_id


def _completed_counts(session: Session, project_id: int) -> dict[str, int]:
    """Return completed work counters protected by the regression."""
    return {
        "completed_sprints": len(
            session.exec(
                select(Sprint).where(
                    Sprint.product_id == project_id,
                    Sprint.status == SprintStatus.COMPLETED,
                )
            ).all()
        ),
        "completed_stories": len(
            session.exec(
                select(UserStory).where(
                    UserStory.product_id == project_id,
                    UserStory.status == StoryStatus.DONE,
                )
            ).all()
        ),
        "total_stories": len(
            session.exec(
                select(UserStory).where(UserStory.product_id == project_id)
            ).all()
        ),
    }


def _app_for_engine(
    *,
    engine: Engine,
    repo_root: Path,
    session_reader: _SprintPlanningSessionReader | None = None,
    evidence_runner: EvidenceCollectionRunner | None = None,
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
        evidence_runner=evidence_runner,
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
    assert meta["storage_schema_version"] == STORAGE_SCHEMA_VERSION
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
                == [
                    (
                        "agileforge story dependencies inspect "
                        f"--project-id {project_id}"
                    ),
                    (
                        "agileforge story dependencies propose "
                        f"--project-id {project_id} "
                        "--expected-state SPRINT_SETUP "
                        "--idempotency-key <idempotency_key>"
                    ),
                    (
                        "agileforge story dependencies apply "
                        f"--project-id {project_id} "
                        "--attempt-id <attempt_id> "
                        "--expected-artifact-fingerprint <artifact_fingerprint> "
                        "--expected-state SPRINT_SETUP "
                        "--idempotency-key <idempotency_key>"
                    ),
                    f"agileforge sprint candidates --project-id {project_id}",
                    f"agileforge sprint generate --project-id {project_id}",
                ]
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
                    and _mapping(_sequence(data["invariants"])[0])["id"]
                    == PHASE1_INVARIANT_ID
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
                == [
                    f"agileforge sprint candidates --project-id {project_id}",
                    f"agileforge sprint generate --project-id {project_id}",
                ]
                and data["blocked_future_commands"] == []
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


def test_scope_extension_cli_drives_completed_project_end_to_end(  # noqa: PLR0915
    session: Session,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Regress completed-project scope extension across CLI/app boundaries."""
    project_id, base_spec_version_id, sprint_id = (
        _seed_completed_scope_extension_project(
            session,
            repo_root=tmp_path,
        )
    )
    amended_spec_path = tmp_path / "specs" / "phase1-extension.json"
    amended_spec_path.write_text(_scope_extension_spec_content(), encoding="utf-8")
    engine = cast("Engine", session.get_bind())
    triage = build_triage_payload(
        project_id=project_id,
        sprint_id=sprint_id,
        impact="none",
        affected_requirements=[],
        affected_task_ids=[],
        affected_story_ids=[],
        affected_backlog_item_ids=[],
        affected_roadmap_item_ids=[],
        affected_layers=[],
        learning_summary="Baseline scope complete.",
        decision_reason="No follow-up from completed sprint.",
        idempotency_key="triage-scope-extension-e2e",
        replace_existing=False,
        recorded_at="2026-06-01T12:00:00Z",
        recorded_by="integration-test",
    )
    workflow = _MutableWorkflowState(
        {
            "fsm_state": "SPRINT_COMPLETE",
            "setup_status": "passed",
            "setup_error": None,
            "latest_completed_sprint_id": sprint_id,
            "post_sprint_triage": triage,
        }
    )
    read_projection = ReadProjectionService(
        engine=engine,
        session_reader=cast("ReadOnlySessionReader", workflow),
    )
    app = AgentWorkbenchApplication(
        read_projection=read_projection,
        authority_projection=_ExtensionAuthorityProjection(workflow),
        project_setup_runner=_ExtensionProjectSetupRunner(
            session=session,
            workflow=workflow,
        ),
        authority_review=_ExtensionAuthorityReview(),
        authority_decision_runner=_ExtensionAuthorityDecisionRunner(
            session=session,
            workflow=workflow,
        ),
        scope_extension_runner=_ScopeExtensionIntegrationRunner(
            session=session,
            workflow=workflow,
        ),
        scope_discovery_runner=ScopeDiscoveryRunner(session=session),
        backlog_runner=cast(
            "_BacklogPhaseRunner",
            _ExtensionBacklogRunner(session=session, workflow=workflow),
        ),
        roadmap_runner=_ExtensionRoadmapRunner(workflow),
        story_runner=cast("_StoryPhaseRunner", _ExtensionStoryRunner(workflow)),
    )
    before_counts = _completed_counts(session, project_id)

    candidates_before = _cli_payload(
        ["sprint", "candidates", "--project-id", str(project_id)],
        app=app,
        capsys=capsys,
    )
    assert _mapping(candidates_before["data"])["count"] == 0

    next_before = _cli_payload(
        ["workflow", "next", "--project-id", str(project_id)],
        app=app,
        capsys=capsys,
    )
    next_before_data = _mapping(next_before["data"])
    validate_command = (
        f"agileforge scope extension validate --project-id {project_id} "
        "--spec-file <amended_spec_file>"
    )
    assert next_before_data["status"] == "project_scope_extension_available"
    assert validate_command in _sequence(next_before_data["next_valid_commands"])

    validate_payload = _cli_payload(
        [
            "scope",
            "extension",
            "validate",
            "--project-id",
            str(project_id),
            "--spec-file",
            str(amended_spec_path),
            "--base-spec-version-id",
            str(base_spec_version_id),
        ],
        app=app,
        capsys=capsys,
    )
    validate_data = _mapping(validate_payload["data"])
    assert validate_data["valid"] is True
    assert validate_data["added_source_item_ids"] == [
        "REQ.scope-extension-follow-up"
    ]

    challenge_file = tmp_path / "artifacts" / "challenge.json"
    challenge_file.parent.mkdir(parents=True, exist_ok=True)
    challenge_file.write_text(
        json.dumps(
            {
                "producer": "grill-with-docs",
                "readiness": "ready_for_prd",
                "original_idea": "Add follow-up scope after completing baseline.",
                "content": {
                    "questions": [{"question": "What changed?", "answer": "Scope."}],
                    "reviewed_evidence": [
                        {
                            "source": "CONTEXT.md",
                            "summary": "Completed scope needs additive extension.",
                        }
                    ],
                    "evidence_conflicts": [],
                    "assumptions": [],
                    "non_goals": [],
                    "risks": [],
                    "open_questions": [],
                    "glossary_changes": [
                        {
                            "term": "Scope Extension",
                            "change": "Use accepted amendments as the start source.",
                            "committed_to_project_glossary": True,
                            "evidence": "CONTEXT.md",
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    challenge_payload = _cli_payload(
        [
            "discovery",
            "challenge",
            "record",
            "--project-id",
            str(project_id),
            "--artifact-file",
            str(challenge_file),
            "--idempotency-key",
            "scope-extension-e2e-challenge",
        ],
        app=app,
        capsys=capsys,
    )
    challenge_artifact_id_value = _mapping(challenge_payload["data"])[
        "challenge_artifact_id"
    ]
    assert isinstance(challenge_artifact_id_value, int)
    challenge_artifact_id = challenge_artifact_id_value
    prd_file = tmp_path / "artifacts" / "prd.json"
    prd_file.write_text(
        json.dumps(
            {
                "producer": "to-prd",
                "source_challenge_artifact_id": challenge_artifact_id,
                "title": "Scope Extension PRD",
                "content": {
                    "problem_statement": "Completed project needs additive scope.",
                    "solution": "Add follow-up requirement through a spec amendment.",
                    "user_stories": ["As a user, I can use the new follow-up scope."],
                    "implementation_decisions": [],
                    "testing_decisions": [],
                    "out_of_scope": [],
                    "further_notes": [],
                    "major_modules": [],
                },
            }
        ),
        encoding="utf-8",
    )
    prd_payload = _cli_payload(
        [
            "discovery",
            "prd",
            "draft",
            "record",
            "--project-id",
            str(project_id),
            "--challenge-artifact-id",
            str(challenge_artifact_id),
            "--prd-file",
            str(prd_file),
            "--idempotency-key",
            "scope-extension-e2e-prd",
        ],
        app=app,
        capsys=capsys,
    )
    prd_id_value = _mapping(prd_payload["data"])["prd_id"]
    assert isinstance(prd_id_value, int)
    prd_id = prd_id_value
    _cli_payload(
        [
            "discovery",
            "prd",
            "accept",
            "--project-id",
            str(project_id),
            "--prd-id",
            str(prd_id),
            "--reviewer",
            "integration-test",
            "--acceptance-notes",
            "Accepted for Spec Amendment drafting.",
            "--idempotency-key",
            "scope-extension-e2e-prd-accept",
        ],
        app=app,
        capsys=capsys,
    )
    amendment_payload = _cli_payload(
        [
            "discovery",
            "spec-amendment",
            "draft",
            "record",
            "--project-id",
            str(project_id),
            "--prd-id",
            str(prd_id),
            "--amendment-file",
            str(amended_spec_path),
            "--base-spec-version-id",
            str(base_spec_version_id),
            "--idempotency-key",
            "scope-extension-e2e-amendment",
        ],
        app=app,
        capsys=capsys,
    )
    spec_amendment_draft_id_value = _mapping(amendment_payload["data"])[
        "spec_amendment_draft_id"
    ]
    assert isinstance(spec_amendment_draft_id_value, int)
    spec_amendment_draft_id = spec_amendment_draft_id_value
    _cli_payload(
        [
            "discovery",
            "spec-amendment",
            "accept",
            "--project-id",
            str(project_id),
            "--spec-amendment-draft-id",
            str(spec_amendment_draft_id),
            "--reviewer",
            "integration-test",
            "--acceptance-notes",
            "Accepted for scope extension start.",
            "--idempotency-key",
            "scope-extension-e2e-amendment-accept",
        ],
        app=app,
        capsys=capsys,
    )
    start_payload = _cli_payload(
        [
            "scope",
            "extension",
            "start",
            "--project-id",
            str(project_id),
            "--spec-amendment-draft-id",
            str(spec_amendment_draft_id),
            "--expected-state",
            "SPRINT_COMPLETE",
            "--idempotency-key",
            "scope-extension-e2e-start",
        ],
        app=app,
        capsys=capsys,
    )
    start_data = _mapping(start_payload["data"])
    assert start_data["setup_status"] == "authority_compile_required"
    spec_version_value = start_data["spec_version_id"]
    assert isinstance(spec_version_value, int)
    amended_spec_version_id = spec_version_value
    amended_spec_hash = str(
        _mapping(start_data["scope_extension_context"])["amended_spec_hash"]
    )

    compile_payload = _cli_payload(
        [
            "authority",
            "compile",
            "--project-id",
            str(project_id),
            "--spec-version-id",
            str(amended_spec_version_id),
            "--expected-spec-hash",
            amended_spec_hash,
            "--expected-state",
            "SETUP_REQUIRED",
            "--expected-setup-status",
            "authority_compile_required",
            "--idempotency-key",
            "scope-extension-e2e-compile",
        ],
        app=app,
        capsys=capsys,
    )
    assert _mapping(compile_payload["data"])["status"] == "authority_pending_review"

    next_review = _cli_payload(
        ["workflow", "next", "--project-id", str(project_id)],
        app=app,
        capsys=capsys,
    )
    next_review_data = _mapping(next_review["data"])
    assert next_review_data["status"] == "authority_pending_review"
    assert _mapping(
        _sequence(next_review_data["decision_actions_after_review"])[0]
    )["command"] == f"agileforge authority accept --project-id {project_id}"

    accept_payload = _cli_payload(
        [
            "authority",
            "accept",
            "--project-id",
            str(project_id),
            "--review-token",
            EXTENSION_REVIEW_TOKEN,
            "--idempotency-key",
            "scope-extension-e2e-accept",
        ],
        app=app,
        capsys=capsys,
    )
    assert (
        _mapping(accept_payload["data"])["spec_version_id"] == amended_spec_version_id
    )
    accepted = session.exec(
        select(SpecAuthorityAcceptance).where(
            SpecAuthorityAcceptance.product_id == project_id,
            SpecAuthorityAcceptance.spec_version_id == amended_spec_version_id,
            SpecAuthorityAcceptance.status == "accepted",
        )
    ).one()
    assert accepted.pending_authority_id == EXTENSION_AUTHORITY_ID

    backlog_generate = _cli_payload(
        ["backlog", "generate", "--project-id", str(project_id)],
        app=app,
        capsys=capsys,
    )
    backlog_data = _mapping(backlog_generate["data"])
    assert backlog_data["attempt_id"] == "extension-backlog-attempt"
    _cli_payload(
        [
            "backlog",
            "save",
            "--project-id",
            str(project_id),
            "--attempt-id",
            "extension-backlog-attempt",
            "--expected-artifact-fingerprint",
            "sha256:" + "b" * 64,
            "--expected-state",
            "BACKLOG_REVIEW",
            "--idempotency-key",
            "scope-extension-e2e-backlog-save",
        ],
        app=app,
        capsys=capsys,
    )

    roadmap_generate = _cli_payload(
        ["roadmap", "generate", "--project-id", str(project_id)],
        app=app,
        capsys=capsys,
    )
    roadmap_data = _mapping(roadmap_generate["data"])
    assert roadmap_data["attempt_id"] == "extension-roadmap-attempt"
    _cli_payload(
        [
            "roadmap",
            "save",
            "--project-id",
            str(project_id),
            "--attempt-id",
            "extension-roadmap-attempt",
            "--expected-artifact-fingerprint",
            "sha256:" + "c" * 64,
            "--expected-state",
            "ROADMAP_REVIEW",
            "--idempotency-key",
            "scope-extension-e2e-roadmap-save",
        ],
        app=app,
        capsys=capsys,
    )

    pending_payload = _cli_payload(
        ["story", "pending", "--project-id", str(project_id)],
        app=app,
        capsys=capsys,
    )
    pending_data = _mapping(pending_payload["data"])
    phase = _mapping(_sequence(pending_data["phases"])[0])
    assert phase["title"] == "Extension Phase"
    assert phase["requirements"] == [EXTENSION_REQUIREMENT]

    candidates_after = _cli_payload(
        ["sprint", "candidates", "--project-id", str(project_id)],
        app=app,
        capsys=capsys,
    )
    candidate_data = _mapping(candidates_after["data"])
    candidate_items = _sequence(candidate_data["items"])
    assert candidate_data["count"] == 1
    candidate = _mapping(candidate_items[0])
    assert candidate["story_title"] == EXTENSION_STORY_TITLE
    assert candidate["source_requirement"] == EXTENSION_REQUIREMENT
    assert candidate["accepted_spec_version_id"] == amended_spec_version_id

    after_counts = _completed_counts(session, project_id)
    assert after_counts["completed_sprints"] == before_counts["completed_sprints"]
    assert after_counts["completed_stories"] == before_counts["completed_stories"]
    assert after_counts["total_stories"] == before_counts["total_stories"] + 1


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


def test_evidence_collect_cli_writes_workflow_state(
    session: Session,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify evidence collect routes through the CLI and caches the report."""
    project_id, _story_id, _spec_version_id = _seed_phase1_project(
        session,
        repo_root=tmp_path,
    )
    authority = session.get(CompiledSpecAuthority, 1)
    assert authority is not None
    authority.compiled_artifact_json = _phase1_compiled_authority_json()
    session.add(authority)
    session.flush()
    acceptance = session.get(SpecAuthorityAcceptance, 1)
    assert acceptance is not None
    acceptance.authority_fingerprint = pending_authority_fingerprint(authority)
    session.add(acceptance)
    session.commit()

    repo = tmp_path / "cartola"
    repo.mkdir()
    (repo / "phase1.py").write_text("# REQ.phase1-context\n", encoding="utf-8")
    engine = cast("Engine", session.get_bind())
    workflow = _EvidenceWorkflowStub()
    evidence_runner = EvidenceCollectionRunner(
        engine=engine,
        product_repo=_EvidenceProductRepo(engine),
        workflow_service=workflow,
    )
    app = _app_for_engine(
        engine=engine,
        repo_root=tmp_path,
        evidence_runner=evidence_runner,
    )

    payload = _cli_payload(
        [
            "evidence",
            "collect",
            "--project-id",
            str(project_id),
            "--repo-path",
            str(repo),
            "--idempotency-key",
            "evidence-phase1",
        ],
        app=app,
        capsys=capsys,
    )

    assert _mapping(payload["meta"])["command"] == "agileforge evidence collect"
    data = _mapping(payload["data"])
    assert data["stored_state_key"] == IMPLEMENTATION_EVIDENCE_STATE_KEY
    report = _mapping(data["report"])
    assert report["schema_version"] == "agileforge.reconciliation_report.v1"
    assert IMPLEMENTATION_EVIDENCE_STATE_KEY in workflow.state


def test_phase1_console_script_help_is_wired(tmp_path: Path) -> None:
    """Verify installed console script is available through uv project run."""
    uv_path = shutil.which("uv")
    assert uv_path is not None

    result = subprocess.run(  # nosec B603  # noqa: S603
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
