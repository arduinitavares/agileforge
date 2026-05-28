"""Tests for host-side As-Built Assessment evidence packing."""

from __future__ import annotations

import json
import socket
from contextlib import suppress
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from models.core import Product
from models.enums import WorkflowEventType
from models.events import WorkflowEvent
from models.specs import (
    CompiledSpecAuthority,
    SpecAuthorityAcceptance,
    SpecRegistry,
)
from orchestrator_agent.agent_tools.as_built_assessor.schemes import (
    AGENT_VERSION,
    ASSESSMENT_SCHEMA_VERSION,
    EVIDENCE_PACK_BUILDER_VERSION,
    AsBuiltAssessment,
    CapabilityAssessment,
    EvidencePack,
    RepoSnapshot,
)
from services.agent_workbench.as_built_assessment import (
    AS_BUILT_ASSESSMENT_META_STATE_KEY,
    AS_BUILT_ASSESSMENT_STATE_KEY,
    MAX_AUTHORITY_TARGETS,
    MAX_SNIPPETS_PER_TARGET,
    AsBuiltAssessmentRunner,
    build_authority_targets,
    build_evidence_pack,
)
from services.agent_workbench.authority_projection import pending_authority_fingerprint

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine

EXPECTED_CARTOLA_TARGET_COUNT = 2
MIN_SKIPPED_RUNTIME_FILES = 3
OVERSIZED_FILE_BYTES = 501 * 1024

CARTOLA_AUTHORITY = {
    "invariants": [
        {
            "id": "INV-a4b296c058e88663",
            "type": "STATE_TRANSITION",
            "parameters": {
                "source_item_id": "REQ.live-squad-recommendation",
                "source_level": "MUST",
                "state": "live recommendation run",
                "trigger": "market is open",
                "outcome": "exactly one operator-facing recommended squad",
            },
        },
        {
            "id": "INV-ffe2e17832c41874",
            "type": "DATA_CONTRACT",
            "parameters": {
                "source_item_id": "REQ.legal-roster",
                "subject": "selected live squad",
                "fields": ["roster_size_12", "one_tecnico", "eleven_non_tecnico"],
                "rule": "must satisfy Cartola roster rules",
            },
        },
    ],
    "source_map": [
        {
            "source_item_id": "REQ.live-squad-recommendation",
            "excerpt": "Recommend a live squad while the market is open.",
        }
    ],
    "requirement_candidates": [],
    "authority_mappings": [],
}


class _WorkflowStub:
    """Workflow state stub used by runner tests."""

    def __init__(self) -> None:
        self.state: dict[str, object] = {"fsm_state": "BACKLOG_INTERVIEW"}

    def get_session_status(self, session_id: str) -> dict[str, object]:
        """Return current workflow state."""
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


class _ProductRepoStub:
    """Product repository stub used by runner tests."""

    def get_by_id(self, product_id: int) -> object | None:
        """Return a product placeholder for positive project lookups."""
        return SimpleNamespace(product_id=product_id, name="As-Built Project")


class _MissingProductRepoStub:
    """Product repository stub for missing project tests."""

    def get_by_id(self, product_id: int) -> object | None:
        """Return no project."""
        _ = product_id
        return None


def _seed_authority(engine: Engine) -> str:
    """Seed one accepted authority row for runner tests."""
    with Session(engine) as session:
        product = Product(name="As-Built Project")
        session.add(product)
        session.commit()
        spec = SpecRegistry(
            product_id=1,
            spec_hash="spec-hash",
            content="{}",
            status="approved",
            approved_at=datetime(2026, 5, 28, tzinfo=UTC),
        )
        session.add(spec)
        session.commit()
        authority = CompiledSpecAuthority(
            spec_version_id=1,
            compiler_version="1",
            prompt_hash="prompt",
            compiled_at=datetime(2026, 5, 28, tzinfo=UTC),
            compiled_artifact_json=json.dumps(CARTOLA_AUTHORITY),
            scope_themes="[]",
            invariants="[]",
            eligible_feature_ids="[]",
        )
        session.add(authority)
        session.commit()
        authority_fingerprint = pending_authority_fingerprint(authority)
        assert authority_fingerprint is not None
        acceptance = SpecAuthorityAcceptance(
            product_id=1,
            spec_version_id=1,
            status="accepted",
            policy="test",
            decided_by="test",
            decided_at=datetime(2026, 5, 28, tzinfo=UTC),
            compiler_version="1",
            prompt_hash="prompt",
            spec_hash="spec-hash",
            pending_authority_id=1,
            authority_fingerprint=authority_fingerprint,
        )
        session.add(acceptance)
        session.commit()
        return authority_fingerprint


def _fake_assessment(input_payload: object) -> AsBuiltAssessment:
    """Return a schema-valid assessment for runner tests."""
    pack = input_payload.repo_evidence_pack
    target = pack.authority_targets[0]
    return AsBuiltAssessment(
        schema_version=ASSESSMENT_SCHEMA_VERSION,
        project_id=input_payload.project_id,
        assessment_id=input_payload.assessment_id,
        agent_version=AGENT_VERSION,
        evidence_pack_builder_version=EVIDENCE_PACK_BUILDER_VERSION,
        authority_fingerprint=pack.authority_fingerprint,
        evidence_pack_fingerprint=pack.evidence_pack_fingerprint,
        generated_at="2026-05-28T12:05:00Z",
        assessment_summary="Assessment completed.",
        repo_snapshot=RepoSnapshot(
            path=pack.repo_snapshot.path,
            git_commit=pack.repo_snapshot.git_commit,
            dirty=pack.repo_snapshot.dirty,
        ),
        capability_assessments=[
            CapabilityAssessment(
                authority_ref=target.authority_ref,
                invariant_refs=target.invariant_refs,
                capability_title=target.title,
                status="observed",
                confidence="medium",
                evidence=[],
                limitations=["Tests were not executed."],
                recommended_backlog_treatment="skip_new_implementation",
                reasoning="Repo evidence supports the capability.",
            )
        ],
        cross_cutting_findings=[],
        open_questions=[],
        is_complete=True,
        clarifying_questions=[],
    )


def test_build_authority_targets_extracts_cartola_invariants_without_items() -> None:
    """Invariant-only authority must still create assessment targets."""
    targets, warnings, limitations = build_authority_targets(CARTOLA_AUTHORITY)

    assert warnings == []
    assert limitations == []
    assert len(targets) == EXPECTED_CARTOLA_TARGET_COUNT
    first = targets[0]
    assert first.authority_ref == "REQ.live-squad-recommendation"
    assert first.invariant_refs == ["INV-a4b296c058e88663"]
    assert first.invariant_type == "STATE_TRANSITION"
    assert first.source_requirement_id == "REQ.live-squad-recommendation"
    assert "market is open" in first.terms
    assert "Recommend a live squad while the market is open." in first.terms


def test_build_authority_targets_empty_authority_records_limitation() -> None:
    """No targets is an explicit limitation, not a silent success."""
    targets, warnings, limitations = build_authority_targets({"invariants": []})

    assert targets == []
    assert [warning.code for warning in warnings] == ["AS_BUILT_NO_AUTHORITY_TARGETS"]
    assert limitations == ["No authority targets were extracted."]


def test_build_authority_targets_caps_large_target_sets() -> None:
    """Authority target caps should warn and truncate deterministically."""
    compiled = {
        "invariants": [
            {
                "id": f"INV-{index:04d}",
                "type": "DATA_CONTRACT",
                "parameters": {"source_item_id": f"REQ.item-{index}"},
            }
            for index in range(MAX_AUTHORITY_TARGETS + 3)
        ]
    }

    targets, warnings, _limitations = build_authority_targets(compiled)

    assert len(targets) == MAX_AUTHORITY_TARGETS
    assert [warning.code for warning in warnings] == ["AS_BUILT_AUTHORITY_TRUNCATED"]


def test_build_evidence_pack_uses_boundary_matching_and_classifies_snippets(
    tmp_path: Path,
) -> None:
    """ID terms should match exact tokens and snippets should be kinded."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "tests").mkdir()
    (repo / "docs").mkdir()
    (repo / "src" / "live.py").write_text(
        "def run_live():\n    return 'INV-a4b296c058e88663'\n",
        encoding="utf-8",
    )
    (repo / "src" / "false_positive.py").write_text(
        "TOKEN_INV-a4b296c058e88663_SUFFIX = True\n",
        encoding="utf-8",
    )
    (repo / "tests" / "test_live.py").write_text(
        "def test_live():\n    assert 'REQ.live-squad-recommendation'\n",
        encoding="utf-8",
    )
    (repo / "docs" / "live.md").write_text(
        "The market is open path is documented.\n",
        encoding="utf-8",
    )

    pack = build_evidence_pack(
        project_id=2,
        authority_fingerprint="sha256:authority",
        compiled_authority=CARTOLA_AUTHORITY,
        repo_path=repo,
        spec_mode="unknown",
        spec_file=None,
    )

    assert isinstance(pack, EvidencePack)
    assert pack.authority_targets
    assert pack.evidence_pack_fingerprint.startswith("sha256:")
    assert {snippet.path for snippet in pack.source_snippets} == {"src/live.py"}
    assert {snippet.path for snippet in pack.test_snippets} == {"tests/test_live.py"}
    assert {snippet.path for snippet in pack.doc_snippets} == {"docs/live.md"}


def test_build_evidence_pack_skips_unsupported_runtime_files(
    tmp_path: Path,
) -> None:
    """Unsupported files and runtime dirs should warn and continue."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".codegraph").mkdir()
    (repo / ".codegraph" / "daemon.pid").write_text("123", encoding="utf-8")
    (repo / "data.db").write_bytes(b"\x00" * 64)
    (repo / "uv.lock").write_text("lock", encoding="utf-8")
    (repo / "large.py").write_text("x" * OVERSIZED_FILE_BYTES, encoding="utf-8")
    if hasattr(socket, "AF_UNIX"):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            with suppress(OSError):
                sock.bind(str(repo / "daemon.sock"))
            pack = build_evidence_pack(
                project_id=2,
                authority_fingerprint="sha256:authority",
                compiled_authority=CARTOLA_AUTHORITY,
                repo_path=repo,
                spec_mode="unknown",
                spec_file=None,
            )
        finally:
            sock.close()
    else:
        pack = build_evidence_pack(
            project_id=2,
            authority_fingerprint="sha256:authority",
            compiled_authority=CARTOLA_AUTHORITY,
            repo_path=repo,
            spec_mode="unknown",
            spec_file=None,
        )

    warning_codes = {warning.code for warning in pack.warnings}
    assert "AS_BUILT_SKIPPED_PATHS" in warning_codes
    assert pack.file_manifest_summary["skipped_files"] >= MIN_SKIPPED_RUNTIME_FILES


def test_build_evidence_pack_preserves_empty_target_limitation(
    tmp_path: Path,
) -> None:
    """An empty target pack is complete only with explicit limitation context."""
    repo = tmp_path / "repo"
    repo.mkdir()

    pack = build_evidence_pack(
        project_id=2,
        authority_fingerprint="sha256:authority",
        compiled_authority={"invariants": []},
        repo_path=repo,
        spec_mode="unknown",
        spec_file=None,
    )

    assert pack.authority_targets == []
    assert pack.has_no_targets_limitation() is True
    assert [warning.code for warning in pack.warnings] == [
        "AS_BUILT_NO_AUTHORITY_TARGETS"
    ]


def test_build_evidence_pack_fingerprint_ignores_generated_timestamp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stable evidence inputs should have stable fingerprints across run times."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "live.py").write_text(
        "# INV-a4b296c058e88663\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "services.agent_workbench.as_built_assessment.utc_now_iso",
        lambda: "2026-05-28T12:00:00Z",
    )
    first = build_evidence_pack(
        project_id=2,
        authority_fingerprint="sha256:authority",
        compiled_authority=CARTOLA_AUTHORITY,
        repo_path=repo,
        spec_mode="unknown",
        spec_file=None,
    )
    monkeypatch.setattr(
        "services.agent_workbench.as_built_assessment.utc_now_iso",
        lambda: "2026-05-28T12:10:00Z",
    )
    second = build_evidence_pack(
        project_id=2,
        authority_fingerprint="sha256:authority",
        compiled_authority=CARTOLA_AUTHORITY,
        repo_path=repo,
        spec_mode="unknown",
        spec_file=None,
    )

    assert first.generated_at != second.generated_at
    assert first.evidence_pack_fingerprint == second.evidence_pack_fingerprint


def test_build_evidence_pack_caps_snippets_per_target(tmp_path: Path) -> None:
    """One broadly matched target cannot add unbounded snippets."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for index in range(MAX_SNIPPETS_PER_TARGET + 3):
        (repo / f"live_{index}.py").write_text(
            "# INV-a4b296c058e88663\n",
            encoding="utf-8",
        )

    pack = build_evidence_pack(
        project_id=2,
        authority_fingerprint="sha256:authority",
        compiled_authority={"invariants": [CARTOLA_AUTHORITY["invariants"][0]]},
        repo_path=repo,
        spec_mode="unknown",
        spec_file=None,
    )

    assert len(pack.source_snippets) == MAX_SNIPPETS_PER_TARGET


def test_build_evidence_pack_includes_search_observation_paths(
    tmp_path: Path,
) -> None:
    """Search observations should summarize matched paths for inspectability."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "live.py").write_text(
        "# INV-a4b296c058e88663\n",
        encoding="utf-8",
    )

    pack = build_evidence_pack(
        project_id=2,
        authority_fingerprint="sha256:authority",
        compiled_authority={"invariants": [CARTOLA_AUTHORITY["invariants"][0]]},
        repo_path=repo,
        spec_mode="unknown",
        spec_file=None,
    )

    assert pack.search_observations[0].match_count == 1
    assert pack.search_observations[0].paths == ["live.py"]


def test_build_evidence_pack_reports_skipped_runtime_directories(
    tmp_path: Path,
) -> None:
    """Skipped runtime directories should be visible even without skipped files."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".codegraph").mkdir()

    pack = build_evidence_pack(
        project_id=2,
        authority_fingerprint="sha256:authority",
        compiled_authority=CARTOLA_AUTHORITY,
        repo_path=repo,
        spec_mode="unknown",
        spec_file=None,
    )

    warning = next(
        warning for warning in pack.warnings if warning.code == "AS_BUILT_SKIPPED_PATHS"
    )
    assert warning.details["counts"]["runtime_dir"] == 1


def test_build_evidence_pack_includes_spec_file_content_hash(
    tmp_path: Path,
) -> None:
    """Spec files are context inputs and should influence the pack fingerprint."""
    repo = tmp_path / "repo"
    repo.mkdir()
    spec_file = tmp_path / "spec.md"
    spec_file.write_text("Spec mode context", encoding="utf-8")

    first = build_evidence_pack(
        project_id=2,
        authority_fingerprint="sha256:authority",
        compiled_authority=CARTOLA_AUTHORITY,
        repo_path=repo,
        spec_mode="unknown",
        spec_file=spec_file,
    )
    spec_file.write_text("Changed spec mode context", encoding="utf-8")
    second = build_evidence_pack(
        project_id=2,
        authority_fingerprint="sha256:authority",
        compiled_authority=CARTOLA_AUTHORITY,
        repo_path=repo,
        spec_mode="unknown",
        spec_file=spec_file,
    )

    assert first.evidence_pack_fingerprint != second.evidence_pack_fingerprint


def test_build_evidence_pack_rejects_unreadable_repo_path(tmp_path: Path) -> None:
    """Repository path must be a readable directory."""
    missing = tmp_path / "missing"

    with pytest.raises(ValueError, match="repo path is not a readable directory"):
        build_evidence_pack(
            project_id=2,
            authority_fingerprint="sha256:authority",
            compiled_authority=CARTOLA_AUTHORITY,
            repo_path=missing,
            spec_mode="unknown",
            spec_file=None,
        )


def test_skipped_socket_does_not_leave_runtime_file(tmp_path: Path) -> None:
    """Socket test cleanup keeps temporary repos removable on Unix."""
    repo = tmp_path / "repo"
    repo.mkdir()
    sock_path = repo / "daemon.sock"
    if not hasattr(socket, "AF_UNIX"):
        return

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        try:
            sock.bind(str(sock_path))
        except OSError:
            return
        assert not sock_path.is_file()
    finally:
        sock.close()


def test_runner_stores_assessment_cache_and_event(tmp_path: Path) -> None:
    """As-built assessment stores workflow cache and audit event."""
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    _seed_authority(engine)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "live.py").write_text("# INV-a4b296c058e88663\n", encoding="utf-8")
    workflow = _WorkflowStub()
    runner = AsBuiltAssessmentRunner(
        engine=engine,
        product_repo=_ProductRepoStub(),
        workflow_service=workflow,
        invoke_agent=_fake_assessment,
    )

    result = runner.assess(
        project_id=1,
        repo_path=str(repo),
        spec_file=None,
        spec_mode="unknown",
        user_input=None,
        idempotency_key="as-built-1",
    )

    assert result["ok"] is True
    assert AS_BUILT_ASSESSMENT_STATE_KEY in workflow.state
    assert AS_BUILT_ASSESSMENT_META_STATE_KEY in workflow.state
    data = result["data"]
    assert data["stored_state_key"] == AS_BUILT_ASSESSMENT_STATE_KEY
    assert data["stored_meta_key"] == AS_BUILT_ASSESSMENT_META_STATE_KEY
    assert data["idempotent_replay"] is False
    assert data["assessment"]["schema_version"] == ASSESSMENT_SCHEMA_VERSION
    with Session(engine) as session:
        event = session.exec(select(WorkflowEvent)).one()
        assert event.event_type == WorkflowEventType.AS_BUILT_ASSESSED


def test_runner_replays_same_idempotency_key_for_same_inputs(tmp_path: Path) -> None:
    """Same idempotency key and same request fingerprint should replay."""
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    _seed_authority(engine)
    repo = tmp_path / "repo"
    repo.mkdir()
    workflow = _WorkflowStub()
    runner = AsBuiltAssessmentRunner(
        engine=engine,
        product_repo=_ProductRepoStub(),
        workflow_service=workflow,
        invoke_agent=_fake_assessment,
    )

    first = runner.assess(
        project_id=1,
        repo_path=str(repo),
        spec_file=None,
        spec_mode="unknown",
        user_input=None,
        idempotency_key="same-key",
    )
    second = runner.assess(
        project_id=1,
        repo_path=str(repo),
        spec_file=None,
        spec_mode="unknown",
        user_input=None,
        idempotency_key="same-key",
    )

    assert first["ok"] is True
    assert second["ok"] is True
    assert second["data"]["idempotent_replay"] is True


def test_runner_rejects_reused_idempotency_key_with_changed_pack(
    tmp_path: Path,
) -> None:
    """Reused idempotency keys are guarded by request fingerprint."""
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    _seed_authority(engine)
    repo = tmp_path / "repo"
    repo.mkdir()
    workflow = _WorkflowStub()
    runner = AsBuiltAssessmentRunner(
        engine=engine,
        product_repo=_ProductRepoStub(),
        workflow_service=workflow,
        invoke_agent=_fake_assessment,
    )

    assert runner.assess(
        project_id=1,
        repo_path=str(repo),
        spec_file=None,
        spec_mode="unknown",
        user_input=None,
        idempotency_key="reused-key",
    )["ok"] is True
    (repo / "changed.py").write_text("# INV-a4b296c058e88663\n", encoding="utf-8")
    second = runner.assess(
        project_id=1,
        repo_path=str(repo),
        spec_file=None,
        spec_mode="unknown",
        user_input=None,
        idempotency_key="reused-key",
    )

    assert second["ok"] is False
    assert second["errors"][0]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_runner_rejects_reused_idempotency_key_with_changed_user_input(
    tmp_path: Path,
) -> None:
    """User input affects assessment and must participate in idempotency."""
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    _seed_authority(engine)
    repo = tmp_path / "repo"
    repo.mkdir()
    workflow = _WorkflowStub()
    runner = AsBuiltAssessmentRunner(
        engine=engine,
        product_repo=_ProductRepoStub(),
        workflow_service=workflow,
        invoke_agent=_fake_assessment,
    )

    assert runner.assess(
        project_id=1,
        repo_path=str(repo),
        spec_file=None,
        spec_mode="unknown",
        user_input="first assessment focus",
        idempotency_key="user-input-key",
    )["ok"] is True
    second = runner.assess(
        project_id=1,
        repo_path=str(repo),
        spec_file=None,
        spec_mode="unknown",
        user_input="different assessment focus",
        idempotency_key="user-input-key",
    )

    assert second["ok"] is False
    assert second["errors"][0]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_runner_rejects_missing_project(tmp_path: Path) -> None:
    """Missing projects fail closed before scanning."""
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    repo = tmp_path / "repo"
    repo.mkdir()
    runner = AsBuiltAssessmentRunner(
        engine=engine,
        product_repo=_MissingProductRepoStub(),
        workflow_service=_WorkflowStub(),
        invoke_agent=_fake_assessment,
    )

    result = runner.assess(
        project_id=404,
        repo_path=str(repo),
        spec_file=None,
        spec_mode="unknown",
        user_input=None,
        idempotency_key="missing-project",
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "PROJECT_NOT_FOUND"
