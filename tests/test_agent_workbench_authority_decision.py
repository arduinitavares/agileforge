"""Tests for guarded authority accept/reject decisions."""

# ruff: noqa: ANN401, D102, D103, D107, EM101, TC002, TC003, TRY003

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, cast

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlmodel import select

from models.core import Product
from models.specs import (
    CompiledSpecAuthority,
    SpecAuthorityAcceptance,
    SpecRegistry,
)
from services.agent_workbench.authority_decision import (
    AuthorityAcceptRequest,
    AuthorityDecisionRunner,
    AuthorityRejectRequest,
    terminal_decision_key,
)
from services.agent_workbench.authority_projection import AuthorityProjectionService
from services.agent_workbench.authority_review import (
    build_authority_review_snapshot,
    sha256_prefixed,
)
from services.agent_workbench.version import STORAGE_SCHEMA_VERSION
from tests.typing_helpers import require_id
from utils.spec_schemas import (
    Invariant,
    InvariantType,
    RequiredFieldParams,
    SourceMapEntry,
    SpecAuthorityCompilationSuccess,
    SpecAuthorityCompilerOutput,
)

if TYPE_CHECKING:
    from sqlmodel import Session

PROMPT_HASH: Final[str] = "a" * 64
COMPILER_VERSION: Final[str] = "1.0.0"
INVARIANT_ID: Final[str] = "INV-0123456789abcdef"


class FakeWorkflowPort:
    """Workflow test double used by authority decision tests."""

    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, Any]] = {}
        self.fail_update: bool = False

    def get_session_status(self, session_id: str) -> dict[str, Any]:
        return dict(self.sessions.get(session_id, {}))

    def update_session_status(
        self,
        session_id: str,
        partial_update: dict[str, Any],
    ) -> None:
        if self.fail_update:
            raise RuntimeError("Injected workflow update failure.")
        self.sessions[session_id] = {
            **self.sessions.get(session_id, {}),
            **partial_update,
        }


def _engine(session: Session) -> Engine:
    return cast("Engine", session.get_bind())


def _make_schema_v3_ready(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS
                    uq_spec_authority_terminal_decision_key
                ON spec_authority_acceptance (terminal_decision_key)
                WHERE terminal_decision_key IS NOT NULL
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS agent_workbench_schema_versions (
                    component VARCHAR PRIMARY KEY,
                    version VARCHAR NOT NULL,
                    recorded_at DATETIME NOT NULL
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO agent_workbench_schema_versions (
                    component, version, recorded_at
                )
                VALUES ('agent_workbench', :version, :recorded_at)
                ON CONFLICT(component) DO UPDATE SET
                    version = excluded.version,
                    recorded_at = excluded.recorded_at
                """
            ),
            {
                "version": STORAGE_SCHEMA_VERSION,
                "recorded_at": datetime(2026, 5, 17, tzinfo=UTC).isoformat(),
            },
        )


def _compiled_success_json(
    *,
    source_excerpt: str,
    source_location: str | None = "Submission Contract",
) -> str:
    success = SpecAuthorityCompilationSuccess(
        scope_themes=["Authority review"],
        domain="agent workbench",
        invariants=[
            Invariant(
                id=INVARIANT_ID,
                type=InvariantType.REQUIRED_FIELD,
                parameters=RequiredFieldParams(field_name="guard_tokens"),
            )
        ],
        eligible_feature_rules=[],
        gaps=[],
        assumptions=[],
        source_map=[
            SourceMapEntry(
                invariant_id=INVARIANT_ID,
                excerpt=source_excerpt,
                location=source_location,
            )
        ],
        compiler_version=COMPILER_VERSION,
        prompt_hash=PROMPT_HASH,
    )
    return SpecAuthorityCompilerOutput(root=success).model_dump_json()


def _seed_pending_review_project(
    session: Session,
    *,
    tmp_path: Path,
    spec_content: str | None = None,
    source_excerpt: str | None = None,
    spec_filename: str = "spec.md",
) -> tuple[int, int, int, Path]:
    content = spec_content or _complete_spec()
    spec_path = tmp_path / spec_filename
    raw_bytes = content.encode("utf-8")
    spec_path.write_bytes(raw_bytes)
    spec_hash = sha256_prefixed(raw_bytes)

    product = Product(
        name=f"Authority Decision {spec_filename}",
        description="Seeded authority decision project",
        spec_file_path=str(spec_path),
    )
    session.add(product)
    session.commit()
    session.refresh(product)
    project_id = require_id(product.product_id, "product_id")

    spec = SpecRegistry(
        product_id=project_id,
        spec_hash=spec_hash,
        content=content,
        content_ref=str(spec_path),
        status="approved",
        approved_at=datetime(2026, 5, 17, 12, tzinfo=UTC),
        approved_by="decision-test",
        approval_notes="Approved for decision test.",
    )
    session.add(spec)
    session.commit()
    session.refresh(spec)
    spec_version_id = require_id(spec.spec_version_id, "spec_version_id")

    authority = CompiledSpecAuthority(
        spec_version_id=spec_version_id,
        compiler_version=COMPILER_VERSION,
        prompt_hash=PROMPT_HASH,
        compiled_at=datetime(2026, 5, 17, 13, tzinfo=UTC),
        compiled_artifact_json=_compiled_success_json(
            source_excerpt=source_excerpt
            or "The review output must include guard tokens."
        ),
        scope_themes=json.dumps(["Authority review"]),
        invariants=json.dumps(
            [{"id": INVARIANT_ID, "text": "guard_tokens are required"}]
        ),
        eligible_feature_ids=json.dumps([]),
        rejected_features=json.dumps([]),
        spec_gaps=json.dumps([]),
    )
    session.add(authority)
    session.commit()
    session.refresh(authority)
    authority_id = require_id(authority.authority_id, "authority_id")
    return project_id, spec_version_id, authority_id, spec_path


def _complete_spec() -> str:
    return (
        "# Submission Contract\n\n"
        "The review output must include guard tokens.\n\n"
        "## Background\n\n"
        "This section is descriptive background.\n"
    )


def _incomplete_spec() -> str:
    return (
        "# Submission Contract\n\n"
        "The review output must include guard tokens.\n\n"
        "## Mandatory Additional Rules\n\n"
        "The runner must record exactly one terminal decision.\n"
    )


def _workflow_for(project_id: int) -> FakeWorkflowPort:
    workflow = FakeWorkflowPort()
    workflow.sessions[str(project_id)] = {
        "fsm_state": "SETUP_REQUIRED",
        "setup_status": "authority_pending_review",
        "setup_error": "pending",
        "setup_error_code": "AUTHORITY_REVIEW_REQUIRED",
    }
    return workflow


def _runner(session: Session, workflow: FakeWorkflowPort) -> AuthorityDecisionRunner:
    return AuthorityDecisionRunner(engine=_engine(session), workflow=workflow)


def _snapshot(
    session: Session,
    project_id: int,
) -> Any:
    result = build_authority_review_snapshot(
        project_id=project_id,
        engine=_engine(session),
    )
    assert not isinstance(result, dict)
    return result


def _accept_request(
    *,
    project_id: int,
    review_token: str,
    idempotency_key: str = "accept-key",
    **kwargs: Any,
) -> AuthorityAcceptRequest:
    values = {"changed_by": "decision-test", **kwargs}
    return AuthorityAcceptRequest(
        project_id=project_id,
        review_token=review_token,
        idempotency_key=idempotency_key,
        **values,
    )


def _reject_request(
    *,
    project_id: int,
    review_token: str,
    idempotency_key: str = "reject-key",
    reason: str = "Spec needs revision.",
    **kwargs: Any,
) -> AuthorityRejectRequest:
    return AuthorityRejectRequest(
        project_id=project_id,
        review_token=review_token,
        idempotency_key=idempotency_key,
        changed_by="decision-test",
        reason=reason,
        **kwargs,
    )


def _explicit_accept_request(
    snapshot: Any,
    *,
    idempotency_key: str = "explicit-accept-key",
    **kwargs: Any,
) -> AuthorityAcceptRequest:
    values = snapshot.guard_tokens | {
        "project_id": snapshot.project_id,
        "idempotency_key": idempotency_key,
        "changed_by": "decision-test",
    }
    values.pop("review_token")
    values.update(kwargs)
    return AuthorityAcceptRequest(**values)


def _explicit_reject_request(
    snapshot: Any,
    *,
    idempotency_key: str = "explicit-reject-key",
    **kwargs: Any,
) -> AuthorityRejectRequest:
    values = {
        "project_id": snapshot.project_id,
        "pending_authority_id": snapshot.pending_authority_id,
        "expected_authority_fingerprint": snapshot.authority_fingerprint,
        "expected_source_spec_hash": snapshot.source_spec_hash,
        "expected_disk_spec_hash": snapshot.disk_spec_hash,
        "expected_resolved_spec_path": snapshot.resolved_spec_path,
        "expected_state": snapshot.fsm_state,
        "expected_setup_status": snapshot.setup_status,
        "idempotency_key": idempotency_key,
        "changed_by": "decision-test",
        "reason": "Spec needs revision.",
    }
    values.update(kwargs)
    return AuthorityRejectRequest(**values)


def _terminal_rows(session: Session) -> list[SpecAuthorityAcceptance]:
    return list(
        session.exec(
            select(SpecAuthorityAcceptance).where(
                SpecAuthorityAcceptance.status.in_(["accepted", "rejected"])
            )
        ).all()
    )


def test_accept_with_review_token_promotes_authority_and_advances_to_vision(
    session: Session,
    tmp_path: Path,
) -> None:
    _make_schema_v3_ready(_engine(session))
    project_id, spec_version_id, authority_id, _path = _seed_pending_review_project(
        session,
        tmp_path=tmp_path,
    )
    snapshot = _snapshot(session, project_id)
    workflow = _workflow_for(project_id)

    result = _runner(session, workflow).accept(
        _accept_request(project_id=project_id, review_token=snapshot.review_token)
    )

    assert result["ok"] is True
    data = result["data"]
    assert data == {
        "project_id": project_id,
        "authority_id": authority_id,
        "accepted_decision_id": data["accepted_decision_id"],
        "accepted_spec_version_id": spec_version_id,
        "authority_fingerprint": snapshot.authority_fingerprint,
        "setup_status": "passed",
        "fsm_state": "VISION_INTERVIEW",
        "next_actions": [
            {
                "command": f"agileforge vision generate --project-id {project_id}",
                "reason": "Authority is accepted and Vision is unlocked.",
            }
        ],
    }
    decision = session.get(SpecAuthorityAcceptance, data["accepted_decision_id"])
    assert decision is not None
    assert decision.status == "accepted"
    assert decision.pending_authority_id == authority_id
    assert decision.authority_fingerprint == snapshot.authority_fingerprint
    assert decision.review_token == snapshot.review_token
    assert decision.review_fingerprint == snapshot.coverage_summary_fingerprint
    assert decision.disk_spec_hash == snapshot.disk_spec_hash
    assert decision.resolved_spec_path == snapshot.resolved_spec_path
    assert decision.actor_mode == "cli-agent"
    assert decision.review_completeness == "complete"
    assert decision.terminal_decision_key == terminal_decision_key(
        project_id=project_id,
        spec_version_id=spec_version_id,
        pending_authority_id=authority_id,
    )
    assert workflow.get_session_status(str(project_id))["setup_status"] == "passed"
    assert workflow.get_session_status(str(project_id))["fsm_state"] == (
        "VISION_INTERVIEW"
    )
    assert workflow.get_session_status(str(project_id))["setup_error"] is None


def test_reject_with_review_token_records_rejection_and_keeps_setup_required(
    session: Session,
    tmp_path: Path,
) -> None:
    _make_schema_v3_ready(_engine(session))
    project_id, spec_version_id, authority_id, _path = _seed_pending_review_project(
        session,
        tmp_path=tmp_path,
    )
    snapshot = _snapshot(session, project_id)
    workflow = _workflow_for(project_id)

    result = _runner(session, workflow).reject(
        _reject_request(project_id=project_id, review_token=snapshot.review_token)
    )

    assert result["ok"] is True
    data = result["data"]
    assert data["project_id"] == project_id
    assert data["pending_authority_id"] == authority_id
    assert data["setup_status"] == "authority_rejected"
    assert data["fsm_state"] == "SETUP_REQUIRED"
    assert data["reason"] == "Spec needs revision."
    assert data["next_actions"] == [
        {
            "command": (
                f"agileforge project spec update --project-id {project_id} "
                f"--spec-file {snapshot.resolved_spec_path}"
            ),
            "installed": False,
            "reason": (
                "Spec update/recompile is required after rejection and is a later "
                "workflow slice."
            ),
        }
    ]
    decision = session.get(SpecAuthorityAcceptance, data["rejected_decision_id"])
    assert decision is not None
    assert decision.status == "rejected"
    assert decision.rationale == "Spec needs revision."
    assert decision.terminal_decision_key == terminal_decision_key(
        project_id=project_id,
        spec_version_id=spec_version_id,
        pending_authority_id=authority_id,
    )
    state = workflow.get_session_status(str(project_id))
    assert state["setup_status"] == "authority_rejected"
    assert state["setup_error_code"] == "AUTHORITY_REJECTED"
    assert state["setup_error"] == "Spec needs revision."


def test_accept_fails_when_review_is_incomplete_without_override(
    session: Session,
    tmp_path: Path,
) -> None:
    _make_schema_v3_ready(_engine(session))
    project_id, _spec_version_id, _authority_id, _path = (
        _seed_pending_review_project(
            session,
            tmp_path=tmp_path,
            spec_content=_incomplete_spec(),
            source_excerpt="The review output must include guard tokens.",
        )
    )
    snapshot = _snapshot(session, project_id)

    result = _runner(session, _workflow_for(project_id)).accept(
        _accept_request(project_id=project_id, review_token=snapshot.review_token)
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "AUTHORITY_REVIEW_INCOMPLETE"
    assert _terminal_rows(session) == []


def test_accept_override_persists_rationale_policy_and_actor_mode(
    session: Session,
    tmp_path: Path,
) -> None:
    _make_schema_v3_ready(_engine(session))
    project_id, _spec_version_id, _authority_id, _path = (
        _seed_pending_review_project(
            session,
            tmp_path=tmp_path,
            spec_content=_incomplete_spec(),
            source_excerpt="The review output must include guard tokens.",
        )
    )
    snapshot = _snapshot(session, project_id)

    result = _runner(session, _workflow_for(project_id)).accept(
        _accept_request(
            project_id=project_id,
            review_token=snapshot.review_token,
            allow_incomplete_review=True,
            incomplete_review_rationale="Reviewed uncovered rule manually.",
            policy="manual",
            actor_mode="human",
        )
    )

    assert result["ok"] is True
    decision = session.get(
        SpecAuthorityAcceptance,
        result["data"]["accepted_decision_id"],
    )
    assert decision is not None
    assert decision.policy == "manual"
    assert decision.actor_mode == "human"
    assert decision.review_completeness == "incomplete"
    assert decision.incomplete_review_override is True
    assert decision.incomplete_review_rationale == (
        "Reviewed uncovered rule manually."
    )


def test_explicit_accept_missing_completeness_guards_fails(
    session: Session,
    tmp_path: Path,
) -> None:
    _make_schema_v3_ready(_engine(session))
    project_id, _spec_version_id, _authority_id, _path = _seed_pending_review_project(
        session,
        tmp_path=tmp_path,
    )
    snapshot = _snapshot(session, project_id)

    request = _explicit_accept_request(
        snapshot,
        expected_content_included=None,
        expected_omission_assessment=None,
        expected_coverage_summary_fingerprint=None,
    )
    result = _runner(session, _workflow_for(project_id)).accept(request)

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "AUTHORITY_GUARD_INCOMPLETE"


def test_explicit_accept_fabricated_completeness_guards_fails(
    session: Session,
    tmp_path: Path,
) -> None:
    _make_schema_v3_ready(_engine(session))
    project_id, _spec_version_id, _authority_id, _path = _seed_pending_review_project(
        session,
        tmp_path=tmp_path,
    )
    snapshot = _snapshot(session, project_id)

    request = _explicit_accept_request(
        snapshot,
        expected_coverage_summary_fingerprint="sha256:bad",
    )
    result = _runner(session, _workflow_for(project_id)).accept(request)

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "STALE_ARTIFACT_FINGERPRINT"


def test_explicit_reject_allows_missing_completeness_guards(
    session: Session,
    tmp_path: Path,
) -> None:
    _make_schema_v3_ready(_engine(session))
    project_id, _spec_version_id, _authority_id, _path = _seed_pending_review_project(
        session,
        tmp_path=tmp_path,
    )
    snapshot = _snapshot(session, project_id)

    result = _runner(session, _workflow_for(project_id)).reject(
        _explicit_reject_request(snapshot)
    )

    assert result["ok"] is True


def test_accept_after_reject_fails_authority_already_decided(
    session: Session,
    tmp_path: Path,
) -> None:
    _make_schema_v3_ready(_engine(session))
    project_id, _spec_version_id, _authority_id, _path = _seed_pending_review_project(
        session,
        tmp_path=tmp_path,
    )
    snapshot = _snapshot(session, project_id)
    runner = _runner(session, _workflow_for(project_id))
    assert runner.reject(
        _reject_request(project_id=project_id, review_token=snapshot.review_token)
    )["ok"]

    result = runner.accept(
        _accept_request(
            project_id=project_id,
            review_token=snapshot.review_token,
            idempotency_key="accept-after-reject",
        )
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "AUTHORITY_ALREADY_DECIDED"
    assert len(_terminal_rows(session)) == 1


def test_reject_after_accept_fails_authority_already_decided(
    session: Session,
    tmp_path: Path,
) -> None:
    _make_schema_v3_ready(_engine(session))
    project_id, _spec_version_id, _authority_id, _path = _seed_pending_review_project(
        session,
        tmp_path=tmp_path,
    )
    snapshot = _snapshot(session, project_id)
    runner = _runner(session, _workflow_for(project_id))
    assert runner.accept(
        _accept_request(project_id=project_id, review_token=snapshot.review_token)
    )["ok"]

    result = runner.reject(
        _reject_request(
            project_id=project_id,
            review_token=snapshot.review_token,
            idempotency_key="reject-after-accept",
        )
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "AUTHORITY_ALREADY_DECIDED"
    assert len(_terminal_rows(session)) == 1


def test_idempotency_same_key_replays_same_accept_response(
    session: Session,
    tmp_path: Path,
) -> None:
    _make_schema_v3_ready(_engine(session))
    project_id, _spec_version_id, _authority_id, _path = _seed_pending_review_project(
        session,
        tmp_path=tmp_path,
    )
    snapshot = _snapshot(session, project_id)
    runner = _runner(session, _workflow_for(project_id))
    request = _accept_request(project_id=project_id, review_token=snapshot.review_token)

    first = runner.accept(request)
    second = runner.accept(request)

    assert second == first
    assert len(_terminal_rows(session)) == 1


def test_idempotency_same_key_different_request_fails(
    session: Session,
    tmp_path: Path,
) -> None:
    _make_schema_v3_ready(_engine(session))
    project_id, _spec_version_id, _authority_id, _path = _seed_pending_review_project(
        session,
        tmp_path=tmp_path,
    )
    snapshot = _snapshot(session, project_id)
    runner = _runner(session, _workflow_for(project_id))
    assert runner.accept(
        _accept_request(project_id=project_id, review_token=snapshot.review_token)
    )["ok"]

    result = runner.accept(
        _accept_request(
            project_id=project_id,
            review_token=snapshot.review_token,
            policy="manual",
        )
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_idempotency_same_key_different_changed_by_fails(
    session: Session,
    tmp_path: Path,
) -> None:
    _make_schema_v3_ready(_engine(session))
    project_id, _spec_version_id, _authority_id, _path = _seed_pending_review_project(
        session,
        tmp_path=tmp_path,
    )
    snapshot = _snapshot(session, project_id)
    runner = _runner(session, _workflow_for(project_id))
    assert runner.accept(
        _accept_request(
            project_id=project_id,
            review_token=snapshot.review_token,
            changed_by="first-actor",
        )
    )["ok"]

    result = runner.accept(
        _accept_request(
            project_id=project_id,
            review_token=snapshot.review_token,
            changed_by="second-actor",
        )
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_decision_replay_runs_before_current_pending_state_validation(
    session: Session,
    tmp_path: Path,
) -> None:
    _make_schema_v3_ready(_engine(session))
    project_id, _spec_version_id, _authority_id, _path = _seed_pending_review_project(
        session,
        tmp_path=tmp_path,
    )
    snapshot = _snapshot(session, project_id)
    runner = _runner(session, _workflow_for(project_id))
    request = _accept_request(project_id=project_id, review_token=snapshot.review_token)
    first = runner.accept(request)
    assert first["ok"]

    result = runner.accept(request)

    assert result == first


def test_changed_disk_hash_after_review_fails_authority_source_changed(
    session: Session,
    tmp_path: Path,
) -> None:
    _make_schema_v3_ready(_engine(session))
    project_id, _spec_version_id, _authority_id, spec_path = (
        _seed_pending_review_project(session, tmp_path=tmp_path)
    )
    snapshot = _snapshot(session, project_id)
    spec_path.write_text(_complete_spec() + "\nchanged\n", encoding="utf-8")

    result = _runner(session, _workflow_for(project_id)).accept(
        _accept_request(project_id=project_id, review_token=snapshot.review_token)
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "AUTHORITY_SOURCE_CHANGED"


def test_missing_disk_spec_at_decision_fails_specific_error(
    session: Session,
    tmp_path: Path,
) -> None:
    _make_schema_v3_ready(_engine(session))
    project_id, _spec_version_id, _authority_id, spec_path = (
        _seed_pending_review_project(session, tmp_path=tmp_path)
    )
    snapshot = _snapshot(session, project_id)
    spec_path.unlink()

    result = _runner(session, _workflow_for(project_id)).accept(
        _accept_request(project_id=project_id, review_token=snapshot.review_token)
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "SPEC_FILE_NOT_FOUND"


def test_concurrent_accept_reject_records_one_terminal_decision(
    session: Session,
    tmp_path: Path,
) -> None:
    engine = _engine(session)
    _make_schema_v3_ready(engine)
    project_id, _spec_version_id, _authority_id, _path = _seed_pending_review_project(
        session,
        tmp_path=tmp_path,
    )
    snapshot = _snapshot(session, project_id)
    workflow = _workflow_for(project_id)

    def accept() -> dict[str, Any]:
        return AuthorityDecisionRunner(engine=engine, workflow=workflow).accept(
            _accept_request(
                project_id=project_id,
                review_token=snapshot.review_token,
                idempotency_key="concurrent-accept",
            )
        )

    def reject() -> dict[str, Any]:
        return AuthorityDecisionRunner(engine=engine, workflow=workflow).reject(
            _reject_request(
                project_id=project_id,
                review_token=snapshot.review_token,
                idempotency_key="concurrent-reject",
            )
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda fn: fn(), [accept, reject]))

    assert sum(1 for result in results if result["ok"]) == 1
    assert sum(
        1
        for result in results
        if not result["ok"]
        and result["errors"][0]["code"] == "AUTHORITY_ALREADY_DECIDED"
    ) == 1
    assert len(_terminal_rows(session)) == 1


def test_rejected_decision_never_satisfies_accepted_authority_projection(
    session: Session,
    tmp_path: Path,
) -> None:
    engine = _engine(session)
    _make_schema_v3_ready(engine)
    project_id, _spec_version_id, _authority_id, _path = _seed_pending_review_project(
        session,
        tmp_path=tmp_path,
    )
    snapshot = _snapshot(session, project_id)

    result = _runner(session, _workflow_for(project_id)).reject(
        _reject_request(project_id=project_id, review_token=snapshot.review_token)
    )
    assert result["ok"]

    projection = AuthorityProjectionService(engine=engine, repo_root=tmp_path).status(
        project_id=project_id
    )

    assert projection["ok"] is True
    assert projection["data"]["accepted_decision_id"] is None
    assert projection["data"]["accepted_spec_version_id"] is None
    assert projection["data"]["status"] == "pending_acceptance"
