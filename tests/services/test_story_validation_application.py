# tests/services/test_story_validation_application.py
"""Tests for explicit provider-free Story structural-eligibility reconciliation."""

from __future__ import annotations

import concurrent.futures
import json
from datetime import UTC, datetime
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, col, create_engine, select

import api
from models.core import UserStory
from models.workflow import WorkflowTransitionReceipt
from services import application as application_service
from services.application import AgileForgeApplication, StoryEligibilityReconcileRequest
from services.read_projections import DurableReadProjectionService
from services.specs.story_validation_service import StoryValidationReadinessError
from tests.test_create_user_story import (
    _decide_story,
    _record_story,
    _seed_story_parent,
)
from tests.test_story_validation_service import _accepted_story, _validate
from utils.spec_schemas import ValidationEvidence
from workflow.clock import FixedClock
from workflow.definitions.root import project_graph
from workflow.domain import WorkflowDomain

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from sqlalchemy import Engine

    from services.specs.story_validation_service import (
        StorySemanticReview,
        ValidateStoryInput,
    )

NOW = datetime(2026, 8, 25, 12, tzinfo=UTC)
_SECOND_EVALUATION = 2


def _build_application(engine: Engine) -> AgileForgeApplication:
    return AgileForgeApplication(
        workflow_domain=WorkflowDomain(
            engine=engine,
            graph=project_graph(),
            clock=FixedClock(now_value=NOW),
        ),
        read_projection=DurableReadProjectionService(engine=engine),
    )


def _request(
    project_id: int,
    *,
    story_ids: tuple[int, ...] | None = None,
    key: str = "reconcile-key-1",
    actor: str = "test-operator",
    correlation_id: str | None = None,
) -> StoryEligibilityReconcileRequest:
    return StoryEligibilityReconcileRequest(
        project_id=project_id,
        story_ids=story_ids,
        idempotency_key=key,
        actor=actor,
        correlation_id=correlation_id,
    )


def _story(engine: Engine, story_id: int) -> UserStory:
    with Session(engine) as session:
        story = session.get(UserStory, story_id)
        assert story is not None
        session.expunge(story)
        return story


def _clear_evidence(engine: Engine, story_id: int) -> None:
    with Session(engine) as session:
        story = session.get(UserStory, story_id)
        assert story is not None
        story.validation_evidence = None
        session.add(story)
        session.commit()


def _data(result: dict[str, Any]) -> dict[str, Any]:
    data = result.get("data")
    assert isinstance(data, dict)
    return data


def _accepted_stories(
    engine: Engine,
    *,
    requirements: tuple[str, ...] = ("Plan immutable work",),
) -> tuple[int, int, int]:
    project_id, roadmap_id = _seed_story_parent(engine, requirements=requirements)
    with Session(engine) as session:
        artifact = _record_story(
            session,
            project_id=project_id,
            roadmap_id=roadmap_id,
            title="Reconciliation batch targets",
            item_count=2,
            recorded_offset=1,
        )
        accepted = _decide_story(session, artifact, decision="accepted", offset=2)
        session.commit()
    first_id, second_id = accepted.activated_story_ids
    return project_id, first_id, second_id


def _receipt(engine: Engine, key: str) -> WorkflowTransitionReceipt | None:
    with Session(engine) as session:
        return session.exec(
            select(WorkflowTransitionReceipt).where(
                col(WorkflowTransitionReceipt.request_kind)
                == "reconcile_story_structural_eligibility",
                col(WorkflowTransitionReceipt.idempotency_key) == key,
            )
        ).one_or_none()


def test_reconcile_all_active_stories_replaces_missing_evidence(engine: Engine) -> None:
    """Missing evidence is persisted for every active accepted Story."""
    story_id = _accepted_story(engine)
    story = _story(engine, story_id)
    _clear_evidence(engine, story_id)

    result = _build_application(engine).reconcile_story_eligibility(
        _request(story.project_id)
    )

    assert result["ok"] is True
    data = cast("dict[str, Any]", result["data"])
    assert data["story_ids"] == [story_id]
    assert data["reconciled_story_ids"] == [story_id]
    assert data["unchanged_story_ids"] == []
    item = data["stories"][0]
    assert item["story_id"] == story_id
    assert item["structurally_eligible"] is True
    assert item["structural_failures"] == []
    assert item["validated_at"]
    assert item["evidence_fingerprint"].startswith("sha256:")
    assert "Sprint selection." in data["does_not_prove"]
    assert "Dependency safety." in data["does_not_prove"]


def test_reconciliation_api_and_cli_disclose_the_exact_structural_proof_boundary(
    engine: Engine,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every operator surface states the same exact proof and non-proof list."""
    from cli.main import main  # noqa: PLC0415

    story_id = _accepted_story(engine)
    story = _story(engine, story_id)
    expected_scope = {
        "proves": [
            "exact Story identity",
            "immutable accepted Story artifact/item binding",
            "accepted Backlog and Specification lineage",
            "parent-bounded Specification references",
            "required Story shape",
            "non-empty acceptance criteria",
            "current evidence and input fingerprints",
        ],
        "does_not_prove": [
            "semantic/model quality",
            "product value",
            "human Sprint selection",
            "dependency safety",
            "Sprint candidacy",
            "Sprint-generation readiness",
        ],
    }
    app = _build_application(engine)
    client = TestClient(api.app)

    with patch("api._application", return_value=app):
        response = client.post(
            f"/api/projects/{story.project_id}/story/structural-eligibility/reconcile",
            json={"idempotency_key": "exact-proof-api", "actor": "api-operator"},
        )
    assert response.status_code == HTTPStatus.OK
    assert response.json()["data"]["structural_evidence_scope"] == expected_scope

    assert main(
        [
            "story",
            "eligibility",
            "reconcile",
            "--project-id",
            str(story.project_id),
            "--story-id",
            str(story_id),
            "--idempotency-key",
            "exact-proof-cli",
            "--actor",
            "cli-operator",
        ],
        application=app,
    ) == 0
    assert json.loads(capsys.readouterr().out)["data"][
        "structural_evidence_scope"
    ] == expected_scope


def test_reconcile_explicit_subset_is_canonical_and_rejects_duplicate_ids(
    engine: Engine,
) -> None:
    """Explicit subsets use sorted unique Story IDs."""
    story_id = _accepted_story(engine)
    story = _story(engine, story_id)

    result = _build_application(engine).reconcile_story_eligibility(
        _request(story.project_id, story_ids=(story_id,), key="subset-key")
    )

    assert result["ok"] is True
    assert _data(result)["story_ids"] == [story_id]
    with pytest.raises(ValueError, match="duplicate Story IDs"):
        _request(story.project_id, story_ids=(story_id, story_id))


def test_reconcile_batch_canonicalizes_reversed_ids_and_cli_omission_uses_all(
    engine: Engine,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A batch uses sorted identities and omitted CLI IDs select every active Story."""
    from cli.main import main  # noqa: PLC0415

    project_id, first_id, second_id = _accepted_stories(engine)
    _clear_evidence(engine, first_id)
    _clear_evidence(engine, second_id)
    app = _build_application(engine)

    result = app.reconcile_story_eligibility(
        _request(project_id, story_ids=(second_id, first_id), key="reversed-key")
    )
    assert _data(result)["story_ids"] == [first_id, second_id]

    assert main(
        [
            "story", "eligibility", "reconcile", "--project-id", str(project_id),
            "--idempotency-key", "all-cli-key", "--actor", "cli-operator",
        ],
        application=app,
    ) == 0
    assert json.loads(capsys.readouterr().out)["data"]["story_ids"] == [
        first_id,
        second_id,
    ]


def test_reconcile_rejects_every_invalid_batch_target_before_evidence_mutation(
    engine: Engine,
) -> None:
    """Invalid, foreign, and superseded targets cannot partially reconcile a batch."""
    project_id, first_id, second_id = _accepted_stories(engine)
    _foreign_project_id, foreign_id, _foreign_second_id = _accepted_stories(
        engine,
        requirements=("Foreign reconciliation work",),
    )
    _clear_evidence(engine, first_id)
    _clear_evidence(engine, second_id)
    with Session(engine) as session:
        superseded = session.get(UserStory, second_id)
        assert superseded is not None
        superseded.is_superseded = True
        session.add(superseded)
        session.commit()

    app = _build_application(engine)
    for key, target_id in (
        ("unknown-target", 999999),
        ("foreign-target", foreign_id),
        ("superseded-target", second_id),
    ):
        result = app.reconcile_story_eligibility(
            _request(project_id, story_ids=(first_id, target_id), key=key)
        )
        assert result["ok"] is False
        assert _story(engine, first_id).validation_evidence is None
        assert _receipt(engine, key) is not None


def test_reconcile_batch_rolls_back_first_evidence_when_second_evaluation_fails(
    engine: Engine,
) -> None:
    """An evaluator exception on later Story removes all earlier batch writes."""
    project_id, first_id, second_id = _accepted_stories(engine)
    _clear_evidence(engine, first_id)
    _clear_evidence(engine, second_id)
    original = application_service.validate_story_with_specification_in_session
    calls = 0

    def fail_second(
        session: Session,
        params: ValidateStoryInput | Mapping[str, object],
        *,
        semantic_review: StorySemanticReview | None = None,
        now: Callable[[], datetime] = datetime.now,
    ) -> object:
        nonlocal calls
        calls += 1
        if calls == _SECOND_EVALUATION:
            message = "second evaluator failure"
            raise RuntimeError(message)
        return original(
            session,
            params,
            semantic_review=semantic_review,
            now=now,
        )

    with (
        patch(
            "services.application.validate_story_with_specification_in_session",
            fail_second,
        ),
        pytest.raises(RuntimeError, match="second evaluator failure"),
    ):
        _build_application(engine).reconcile_story_eligibility(
            _request(project_id, story_ids=(first_id, second_id), key="batch-rollback")
        )
    assert _story(engine, first_id).validation_evidence is None
    assert _story(engine, second_id).validation_evidence is None
    assert _receipt(engine, "batch-rollback") is None


def test_reconcile_rechecks_refreshed_evidence_before_writing_receipt(
    engine: Engine,
) -> None:
    """Evaluator success without current evidence cannot receipt stale data."""
    story_id = _accepted_story(engine)
    story = _story(engine, story_id)
    _validate(engine, story_id)
    with Session(engine) as session:
        row = session.get(UserStory, story_id)
        assert row is not None
        assert row.validation_evidence is not None
        stale_evidence = row.validation_evidence
        row.story_description = "stale structural context"
        session.add(row)
        session.commit()

    with (
        patch(
            "services.application.validate_story_with_specification_in_session",
            return_value={"success": True},
        ),
        pytest.raises(StoryValidationReadinessError),
    ):
        _build_application(engine).reconcile_story_eligibility(
            _request(story.project_id, key="stale-success-key")
        )
    assert _story(engine, story_id).validation_evidence == stale_evidence
    assert _receipt(engine, "stale-success-key") is None


def test_reconcile_concurrent_identical_requests_replay_one_receipt(
    tmp_path: Path,
) -> None:
    """SQLite writer serialization yields an exact replay without evidence rewrites."""
    database = tmp_path / "reconcile-race.sqlite3"
    engine = create_engine(
        f"sqlite:///{database}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    try:
        story_id = _accepted_story(engine)
        story = _story(engine, story_id)
        _clear_evidence(engine, story_id)
        request = _request(story.project_id, key="concurrent-key")
        app = _build_application(engine)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(app.reconcile_story_eligibility, request)
            second = executor.submit(app.reconcile_story_eligibility, request)
            first_result = first.result(timeout=10)
            second_result = second.result(timeout=10)
        assert first_result == second_result
        assert _receipt(engine, "concurrent-key") is not None
        raw = _story(engine, story_id).validation_evidence
        assert raw is not None
        evidence = ValidationEvidence.model_validate_json(raw, strict=True)
        assert _data(first_result)["stories"][0]["validated_at"] == (
            evidence.validated_at.isoformat()
        )
    finally:
        engine.dispose()


def test_reconcile_replaces_legacy_v2_and_preserves_current_failed_v3_bytes(
    engine: Engine,
) -> None:
    """Legacy evidence is replaced while current failed evidence is retained."""
    story_id = _accepted_story(engine)
    story = _story(engine, story_id)
    _validate(engine, story_id)
    with Session(engine) as session:
        row = session.get(UserStory, story_id)
        assert row is not None
        assert row.validation_evidence is not None
        legacy = json.loads(row.validation_evidence)
        legacy["schema_version"] = "agileforge.story-validation-evidence.v2"
        row.validation_evidence = json.dumps(
            legacy,
            sort_keys=True,
            separators=(",", ":"),
        )
        row.story_description = "invalid statement"
        session.add(row)
        session.commit()

    first = _build_application(engine).reconcile_story_eligibility(
        _request(story.project_id, key="legacy-key")
    )
    assert first["ok"] is True
    first_data = _data(first)
    assert first_data["reconciled_story_ids"] == [story_id]
    assert first_data["stories"][0]["structurally_eligible"] is False
    raw_after_replacement = _story(engine, story_id).validation_evidence
    assert raw_after_replacement is not None
    original_validated_at = ValidationEvidence.model_validate_json(
        raw_after_replacement, strict=True
    ).validated_at

    second = _build_application(engine).reconcile_story_eligibility(
        _request(story.project_id, key="current-failed-key")
    )
    assert second["ok"] is True
    assert _data(second)["unchanged_story_ids"] == [story_id]
    assert _story(engine, story_id).validation_evidence == raw_after_replacement
    assert ValidationEvidence.model_validate_json(
        raw_after_replacement, strict=True
    ).validated_at == original_validated_at


def test_reconcile_replays_exact_result_and_rejects_changed_metadata(
    engine: Engine,
) -> None:
    """A matching key replays while changed metadata conflicts."""
    story_id = _accepted_story(engine)
    story = _story(engine, story_id)
    app = _build_application(engine)
    request = _request(story.project_id, key="replay-key", correlation_id="trace-1")

    first = app.reconcile_story_eligibility(request)
    replay = app.reconcile_story_eligibility(request)
    conflict = app.reconcile_story_eligibility(
        _request(story.project_id, key="replay-key", correlation_id="trace-2")
    )

    assert replay == first
    assert conflict["ok"] is False
    errors = cast("list[dict[str, Any]]", conflict["errors"])
    assert errors[0]["code"] == "IDEMPOTENCY_CONFLICT"


def test_reconcile_rolls_back_evidence_and_receipt_on_unexpected_failure(
    engine: Engine,
) -> None:
    """Unexpected evaluator failures leave no evidence or receipt behind."""
    story_id = _accepted_story(engine)
    story = _story(engine, story_id)
    _clear_evidence(engine, story_id)
    request = _request(story.project_id, key="rollback-key")

    with (
        patch(
            "services.application.validate_story_with_specification_in_session",
            side_effect=RuntimeError("simulated evaluator failure"),
        ),
        pytest.raises(RuntimeError, match="simulated evaluator failure"),
    ):
        _build_application(engine).reconcile_story_eligibility(request)

    assert _story(engine, story_id).validation_evidence is None
    with Session(engine) as session:
        receipt = session.exec(
            select(WorkflowTransitionReceipt).where(
                col(WorkflowTransitionReceipt.request_kind)
                == "reconcile_story_structural_eligibility",
                col(WorkflowTransitionReceipt.idempotency_key) == "rollback-key",
            )
        ).one_or_none()
    assert receipt is None


def test_reconcile_api_and_cli_expose_only_the_renamed_operator_surface(
    engine: Engine,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Only the reconciliation API route and CLI command are exposed."""
    from cli.main import main  # noqa: PLC0415

    story_id = _accepted_story(engine)
    story = _story(engine, story_id)
    app = _build_application(engine)
    client = TestClient(api.app)

    with patch("api._application", return_value=app):
        response = client.post(
            f"/api/projects/{story.project_id}/story/structural-eligibility/reconcile",
            json={"idempotency_key": "api-key", "actor": "api-operator"},
        )
        removed = client.post(
            f"/api/projects/{story.project_id}/story/validate",
            json={"idempotency_key": "legacy-key", "actor": "api-operator"},
        )
        duplicate_ids = client.post(
            f"/api/projects/{story.project_id}/story/structural-eligibility/reconcile",
            json={
                "story_ids": [story_id, story_id],
                "idempotency_key": "duplicate-key",
                "actor": "api-operator",
            },
        )
    assert response.status_code == HTTPStatus.OK
    assert response.json()["data"]["story_ids"] == [story_id]
    assert removed.status_code == HTTPStatus.NOT_FOUND
    assert duplicate_ids.status_code == HTTPStatus.UNPROCESSABLE_ENTITY

    assert main(
        [
            "story", "eligibility", "reconcile", "--project-id",
            str(story.project_id), "--story-id", str(story_id),
            "--idempotency-key", "cli-key", "--actor", "cli-operator",
        ],
        application=app,
    ) == 0
    assert json.loads(capsys.readouterr().out)["data"]["story_ids"] == [story_id]
    assert main(["story", "validate", "--project-id", str(story.project_id)]) == 2  # noqa: PLR2004
