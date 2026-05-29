"""Tests for host-side As-Built Assessment evidence packing."""

from __future__ import annotations

import json
import socket
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import anyio
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
    AsBuiltAssessorInput,
    CapabilityAssessment,
    EvidencePack,
    OpenSpecContext,
    OriginalSpecContext,
    RepoSnapshot,
)
from services.agent_workbench import as_built_assessment as as_built_module
from services.agent_workbench.as_built_assessment import (
    AS_BUILT_ASSESSMENT_META_STATE_KEY,
    AS_BUILT_ASSESSMENT_STATE_KEY,
    MAX_AUTHORITY_TARGETS,
    MAX_FILE_MANIFEST_ENTRIES,
    MAX_SNIPPETS_PER_TARGET,
    AsBuiltAssessmentRunner,
    build_authority_targets,
    build_evidence_pack,
    split_evidence_pack_for_assessment,
)
from services.agent_workbench.authority_projection import pending_authority_fingerprint
from utils.failure_artifacts import AgentInvocationError

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

EXPECTED_CARTOLA_TARGET_COUNT = 2
FAILING_BATCH_INDEX = 2
EXPECTED_TRANSIENT_RETRY_CALLS = 2
MIN_SKIPPED_RUNTIME_FILES = 3
OMITTED_MANIFEST_FILES = 3
OVERSIZED_FILE_BYTES = 501 * 1024
TRANSIENT_PROVIDER_JSON_ERROR = (
    "litellm.APIError: OpenrouterException - "
    "Unable to get json response - Expecting value"
)

CARTOLA_AUTHORITY: dict[str, Any] = {
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

GROUPED_INVARIANT_AUTHORITY: dict[str, Any] = {
    "invariants": [
        {
            "id": "INV-group-a",
            "type": "DATA_CONTRACT",
            "parameters": {
                "source_item_id": "REQ.grouped-capability",
                "rule": "first grouped rule",
            },
        },
        {
            "id": "INV-group-b",
            "type": "DATA_CONTRACT",
            "parameters": {
                "source_item_id": "REQ.grouped-capability",
                "rule": "second grouped rule",
            },
        },
    ],
    "source_map": [],
    "scope_themes": [],
    "gaps": [],
    "assumptions": [],
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


@pytest.fixture
def anyio_backend() -> str:
    """Run async tests on the installed backend only."""
    return "asyncio"


def _seed_authority(
    engine: Engine,
    compiled_authority: dict[str, object] | None = None,
) -> str:
    """Seed one accepted authority row for runner tests."""
    authority_json = json.dumps(compiled_authority or CARTOLA_AUTHORITY)
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
            compiled_artifact_json=authority_json,
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


def _fake_assessment(payload: AsBuiltAssessorInput) -> AsBuiltAssessment:
    """Return a schema-valid assessment for runner tests."""
    pack = payload.repo_evidence_pack
    return AsBuiltAssessment(
        schema_version=ASSESSMENT_SCHEMA_VERSION,
        project_id=payload.project_id,
        assessment_id=payload.assessment_id,
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
                authority_ref=item.authority_ref,
                invariant_refs=item.invariant_refs,
                capability_title=item.title,
                status="observed",
                confidence="medium",
                evidence=[],
                limitations=["Tests were not executed."],
                recommended_backlog_treatment="skip_new_implementation",
                reasoning="Repo evidence supports the capability.",
            )
            for item in pack.authority_targets
        ],
        cross_cutting_findings=[],
        open_questions=[],
        is_complete=True,
        clarifying_questions=[],
    )


class _RecordingBatchInvoker:
    """Fake invoker that records every batch payload."""

    def __init__(self) -> None:
        self.payloads: list[AsBuiltAssessorInput] = []

    def __call__(self, payload: AsBuiltAssessorInput) -> AsBuiltAssessment:
        self.payloads.append(payload)
        return _fake_assessment(payload)


class _SecondBatchTimeoutInvoker:
    """Fake invoker that fails on the second batch."""

    def __init__(self) -> None:
        self.call_count = 0

    def __call__(self, payload: AsBuiltAssessorInput) -> AsBuiltAssessment:
        self.call_count += 1
        if self.call_count == FAILING_BATCH_INDEX:
            msg = "As-Built assessor timed out after 120 seconds."
            raise RuntimeError(msg)
        return _fake_assessment(payload)


class _CrossBatchCoverageInvoker:
    """Fake invoker that returns capabilities assigned to the wrong batch."""

    def __call__(self, payload: AsBuiltAssessorInput) -> AsBuiltAssessment:
        wrong_target = CARTOLA_AUTHORITY["invariants"][1]
        assert isinstance(wrong_target, dict)
        parameters = wrong_target["parameters"]
        assert isinstance(parameters, dict)
        return _fake_assessment(payload).model_copy(
            update={
                "capability_assessments": [
                    CapabilityAssessment(
                        authority_ref=str(parameters["source_item_id"]),
                        invariant_refs=[str(wrong_target["id"])],
                        capability_title="Wrong Batch Capability",
                        status="observed",
                        confidence="medium",
                        evidence=[],
                        limitations=["Tests were not executed."],
                        recommended_backlog_treatment="skip_new_implementation",
                        reasoning="This capability belongs to another batch.",
                    )
                ]
            }
        )


class _GroupedInvariantCoverageInvoker:
    """Fake invoker that groups same-requirement invariants into one capability."""

    def __call__(self, payload: AsBuiltAssessorInput) -> AsBuiltAssessment:
        return _fake_assessment(payload).model_copy(
            update={
                "capability_assessments": [
                    CapabilityAssessment(
                        authority_ref="REQ.grouped-capability",
                        invariant_refs=["INV-group-a", "INV-group-b"],
                        capability_title="Grouped Capability",
                        status="observed_with_missing_evidence",
                        confidence="medium",
                        evidence=[],
                        limitations=["Tests were not executed."],
                        recommended_backlog_treatment="create_verification_item",
                        reasoning=(
                            "The agent grouped two invariants that share one "
                            "source requirement."
                        ),
                    )
                ]
            }
        )


class _ProgressContextFailureInvoker:
    """Fake invoker that emits model-level progress while inside a batch."""

    def __call__(self, payload: AsBuiltAssessorInput) -> AsBuiltAssessment:
        _ = payload
        as_built_module._emit_progress("as_built.model_call_failed", error="boom")
        msg = "boom"
        raise RuntimeError(msg)


def _mismatched_assessment(payload: AsBuiltAssessorInput) -> AsBuiltAssessment:
    """Return an assessment with stale host identity fields."""
    return _fake_assessment(payload).model_copy(
        update={"evidence_pack_fingerprint": "sha256:wrong-pack"}
    )


def _timeout_assessment(payload: AsBuiltAssessorInput) -> AsBuiltAssessment:
    """Raise the timeout error produced by default invocation."""
    _ = payload
    msg = "As-Built assessor timed out after 120 seconds."
    raise RuntimeError(msg)


def _input_payload_for_pack(pack: EvidencePack) -> AsBuiltAssessorInput:
    """Build a minimal default-invoker payload for tests."""
    return AsBuiltAssessorInput(
        project_id=2,
        assessment_id="as-built-2-timeout",
        compiled_authority=json.dumps(CARTOLA_AUTHORITY),
        original_spec=OriginalSpecContext(
            spec_mode="unknown",
            json="",
            markdown="",
        ),
        repo_evidence_pack=pack,
        openspec_context=OpenSpecContext(
            present=False,
            spec_summaries=[],
            change_summaries=[],
        ),
        prior_as_built_assessment="NO_HISTORY",
        user_input="",
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


def test_split_evidence_pack_batches_targets_in_order(tmp_path: Path) -> None:
    """Batch packs should preserve authority-target order and fingerprints."""
    repo = tmp_path / "repo"
    repo.mkdir()
    authority = {
        "invariants": [
            {
                "id": f"INV-batch-{index:04d}",
                "type": "DATA_CONTRACT",
                "parameters": {
                    "source_item_id": f"REQ.batch-{index}",
                    "rule": f"rule {index}",
                },
            }
            for index in range(5)
        ]
    }
    for index in range(5):
        (repo / f"file_{index}.py").write_text(
            f"# INV-batch-{index:04d}\n",
            encoding="utf-8",
        )
    full_pack = build_evidence_pack(
        project_id=2,
        authority_fingerprint="sha256:authority",
        compiled_authority=authority,
        repo_path=repo,
        spec_mode="unknown",
        spec_file=None,
    )

    batches = split_evidence_pack_for_assessment(full_pack, batch_size=2)

    assert [len(batch.authority_targets) for batch in batches] == [2, 2, 1]
    assert [
        target.authority_ref
        for batch in batches
        for target in batch.authority_targets
    ] == [target.authority_ref for target in full_pack.authority_targets]
    assert all(
        batch.evidence_pack_fingerprint.startswith("sha256:") for batch in batches
    )
    assert {batch.evidence_pack_fingerprint for batch in batches} != {
        full_pack.evidence_pack_fingerprint
    }


def test_split_evidence_pack_filters_snippets_to_batch_paths(
    tmp_path: Path,
) -> None:
    """Batch packs should not carry snippets unrelated to the selected targets."""
    repo = tmp_path / "repo"
    repo.mkdir()
    authority = {
        "invariants": [
            {
                "id": "INV-batch-a",
                "type": "DATA_CONTRACT",
                "parameters": {"source_item_id": "REQ.batch-a"},
            },
            {
                "id": "INV-batch-b",
                "type": "DATA_CONTRACT",
                "parameters": {"source_item_id": "REQ.batch-b"},
            },
        ]
    }
    (repo / "a.py").write_text("# INV-batch-a\n", encoding="utf-8")
    (repo / "b.py").write_text("# INV-batch-b\n", encoding="utf-8")
    full_pack = build_evidence_pack(
        project_id=2,
        authority_fingerprint="sha256:authority",
        compiled_authority=authority,
        repo_path=repo,
        spec_mode="unknown",
        spec_file=None,
    )

    first_batch = split_evidence_pack_for_assessment(full_pack, batch_size=1)[0]

    assert [target.authority_ref for target in first_batch.authority_targets] == [
        "REQ.batch-a"
    ]
    assert [snippet.path for snippet in first_batch.source_snippets] == ["a.py"]
    assert [observation.query for observation in first_batch.search_observations] == [
        "REQ.batch-a"
    ]


def test_merge_batch_assessments_restores_full_pack_identity(
    tmp_path: Path,
) -> None:
    """Merged assessment should use the full pack identity, not a batch identity."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "live.py").write_text(
        "# INV-a4b296c058e88663\n# INV-ffe2e17832c41874\n",
        encoding="utf-8",
    )
    full_pack = build_evidence_pack(
        project_id=2,
        authority_fingerprint="sha256:authority",
        compiled_authority=CARTOLA_AUTHORITY,
        repo_path=repo,
        spec_mode="unknown",
        spec_file=None,
    )
    batches = split_evidence_pack_for_assessment(full_pack, batch_size=1)
    batch_assessments = [
        _fake_assessment(
            _input_payload_for_pack(batch).model_copy(
                update={"assessment_id": f"batch-{index}"}
            )
        )
        for index, batch in enumerate(batches, start=1)
    ]

    merged = as_built_module.merge_batch_assessments(
        project_id=2,
        assessment_id="as-built-2-full",
        full_pack=full_pack,
        batch_assessments=batch_assessments,
    )

    assert merged.assessment_id == "as-built-2-full"
    assert merged.evidence_pack_fingerprint == full_pack.evidence_pack_fingerprint
    assert len(merged.capability_assessments) == len(full_pack.authority_targets)
    assert [
        item.authority_ref for item in merged.capability_assessments
    ] == [target.authority_ref for target in full_pack.authority_targets]
    assert merged.is_complete is True


def test_merge_batch_assessments_rejects_missing_capability(
    tmp_path: Path,
) -> None:
    """Batch merge must fail if an agent omits an authority target."""
    repo = tmp_path / "repo"
    repo.mkdir()
    full_pack = build_evidence_pack(
        project_id=2,
        authority_fingerprint="sha256:authority",
        compiled_authority=CARTOLA_AUTHORITY,
        repo_path=repo,
        spec_mode="unknown",
        spec_file=None,
    )
    empty_batch = _fake_assessment(_input_payload_for_pack(full_pack)).model_copy(
        update={"capability_assessments": []}
    )

    with pytest.raises(ValueError, match="coverage did not match authority targets"):
        as_built_module.merge_batch_assessments(
            project_id=2,
            assessment_id="as-built-2-full",
            full_pack=full_pack,
            batch_assessments=[empty_batch],
        )


def test_merge_batch_assessments_rejects_duplicate_capability(
    tmp_path: Path,
) -> None:
    """Batch merge must fail if an authority target appears more than once."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "live.py").write_text(
        "# INV-a4b296c058e88663\n# INV-ffe2e17832c41874\n",
        encoding="utf-8",
    )
    full_pack = build_evidence_pack(
        project_id=2,
        authority_fingerprint="sha256:authority",
        compiled_authority=CARTOLA_AUTHORITY,
        repo_path=repo,
        spec_mode="unknown",
        spec_file=None,
    )
    batches = split_evidence_pack_for_assessment(full_pack, batch_size=1)
    first_assessment = _fake_assessment(_input_payload_for_pack(batches[0]))
    second_assessment = _fake_assessment(_input_payload_for_pack(batches[1]))

    with pytest.raises(ValueError, match="coverage did not match authority targets"):
        as_built_module.merge_batch_assessments(
            project_id=2,
            assessment_id="as-built-2-full",
            full_pack=full_pack,
            batch_assessments=[first_assessment, second_assessment, first_assessment],
        )


def test_build_evidence_pack_reads_each_file_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multiple targets should not reread every repository file repeatedly."""
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "live.py"
    source.write_text(
        "# INV-a4b296c058e88663\n# INV-ffe2e17832c41874\n",
        encoding="utf-8",
    )
    read_counts: dict[str, int] = {}
    original_read_text = Path.read_text

    def counted_read_text(
        path: Path,
        encoding: str | None = None,
        errors: str | None = None,
    ) -> str:
        if path == source:
            read_counts[str(path)] = read_counts.get(str(path), 0) + 1
        return original_read_text(
            path,
            encoding=encoding,
            errors=errors,
        )

    monkeypatch.setattr(Path, "read_text", counted_read_text)

    pack = build_evidence_pack(
        project_id=2,
        authority_fingerprint="sha256:authority",
        compiled_authority=CARTOLA_AUTHORITY,
        repo_path=repo,
        spec_mode="unknown",
        spec_file=None,
    )

    assert pack.search_observations[0].match_count == 1
    assert pack.search_observations[1].match_count == 1
    assert read_counts[str(source)] == 1


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


def test_build_evidence_pack_reports_manifest_truncation(tmp_path: Path) -> None:
    """File caps should be visible to the agent and manifest summary."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for index in range(MAX_FILE_MANIFEST_ENTRIES + OMITTED_MANIFEST_FILES):
        (repo / f"file_{index:03d}.py").write_text(
            "# INV-a4b296c058e88663\n",
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

    warning_codes = {warning.code for warning in pack.warnings}
    assert "AS_BUILT_FILE_MANIFEST_TRUNCATED" in warning_codes
    assert pack.file_manifest_summary["included_files"] == MAX_FILE_MANIFEST_ENTRIES
    assert (
        pack.file_manifest_summary["skipped_counts"]["manifest_truncated"]
        == OMITTED_MANIFEST_FILES
    )
    assert any("File manifest was truncated" in item for item in pack.limitations)


def test_build_evidence_pack_skips_symlinked_files(tmp_path: Path) -> None:
    """Evidence collection should stay inside regular repo files."""
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside_secret.py"
    outside.write_text("# INV-a4b296c058e88663\n", encoding="utf-8")
    (repo / "linked.py").symlink_to(outside)

    pack = build_evidence_pack(
        project_id=2,
        authority_fingerprint="sha256:authority",
        compiled_authority=CARTOLA_AUTHORITY,
        repo_path=repo,
        spec_mode="unknown",
        spec_file=None,
    )

    assert pack.source_snippets == []
    warning = next(
        warning for warning in pack.warnings if warning.code == "AS_BUILT_SKIPPED_PATHS"
    )
    assert warning.details["counts"]["symlink"] == 1


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


def test_runner_invokes_assessor_in_batches_and_merges_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Large target sets should invoke the agent per batch and cache one result."""
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    _seed_authority(engine)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "live.py").write_text(
        "# INV-a4b296c058e88663\n# INV-ffe2e17832c41874\n",
        encoding="utf-8",
    )
    workflow = _WorkflowStub()
    invoker = _RecordingBatchInvoker()
    monkeypatch.setattr(as_built_module, "get_as_built_assessor_batch_size", lambda: 1)
    runner = AsBuiltAssessmentRunner(
        engine=engine,
        product_repo=_ProductRepoStub(),
        workflow_service=workflow,
        invoke_agent=invoker,
    )

    result = runner.assess(
        project_id=1,
        repo_path=str(repo),
        spec_file=None,
        spec_mode="unknown",
        user_input=None,
        idempotency_key="batched-key",
    )

    assert result["ok"] is True
    assert len(invoker.payloads) == EXPECTED_CARTOLA_TARGET_COUNT
    first_compiled = json.loads(invoker.payloads[0].compiled_authority)
    second_compiled = json.loads(invoker.payloads[1].compiled_authority)
    assert [item["id"] for item in first_compiled["invariants"]] == [
        "INV-a4b296c058e88663"
    ]
    assert [item["id"] for item in second_compiled["invariants"]] == [
        "INV-ffe2e17832c41874"
    ]
    assert result["data"]["batch_count"] == EXPECTED_CARTOLA_TARGET_COUNT
    assert result["data"]["batch_size"] == 1
    assert result["data"]["authority_target_count"] == EXPECTED_CARTOLA_TARGET_COUNT
    raw_cached = workflow.state[AS_BUILT_ASSESSMENT_STATE_KEY]
    assert isinstance(raw_cached, str)
    cached = AsBuiltAssessment.model_validate_json(raw_cached)
    assert (
        cached.evidence_pack_fingerprint
        == result["data"]["evidence_pack_fingerprint"]
    )
    assert len(cached.capability_assessments) == EXPECTED_CARTOLA_TARGET_COUNT


def test_runner_emits_batch_progress_to_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Long-running assessor batches should be observable through stderr."""
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    _seed_authority(engine)
    repo = tmp_path / "repo"
    repo.mkdir()
    workflow = _WorkflowStub()
    monkeypatch.setattr(as_built_module, "get_as_built_assessor_batch_size", lambda: 1)
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
        idempotency_key="progress-key",
    )

    assert result["ok"] is True
    stderr_events = [
        json.loads(line)
        for line in capsys.readouterr().err.splitlines()
        if line.strip()
    ]
    assert [event["event"] for event in stderr_events] == [
        "as_built.pack_built",
        "as_built.batch_started",
        "as_built.batch_completed",
        "as_built.batch_started",
        "as_built.batch_completed",
        "as_built.assessment_stored",
    ]
    first_batch = stderr_events[1]
    assert first_batch["project_id"] == 1
    assert first_batch["batch_index"] == 1
    assert first_batch["batch_count"] == EXPECTED_CARTOLA_TARGET_COUNT
    assert first_batch["batch_target_count"] == 1
    assert first_batch["payload_chars"] > 0
    assert "timeout_seconds" in first_batch


def test_runner_rejects_assessment_identity_mismatch(tmp_path: Path) -> None:
    """Agent output must match host evidence identity before caching."""
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
        invoke_agent=_mismatched_assessment,
    )

    result = runner.assess(
        project_id=1,
        repo_path=str(repo),
        spec_file=None,
        spec_mode="unknown",
        user_input=None,
        idempotency_key="identity-mismatch",
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "MUTATION_FAILED"
    details = result["errors"][0]["details"]
    assert "evidence_pack_fingerprint" in details["mismatches"]
    assert AS_BUILT_ASSESSMENT_STATE_KEY not in workflow.state
    assert AS_BUILT_ASSESSMENT_META_STATE_KEY not in workflow.state
    with Session(engine) as session:
        assert session.exec(select(WorkflowEvent)).all() == []


def test_runner_timeout_failure_does_not_cache_or_record_event(tmp_path: Path) -> None:
    """Timeout failures should return an envelope without mutating workflow state."""
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
        invoke_agent=_timeout_assessment,
    )

    result = runner.assess(
        project_id=1,
        repo_path=str(repo),
        spec_file=None,
        spec_mode="unknown",
        user_input=None,
        idempotency_key="timeout-key",
    )

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "MUTATION_FAILED"
    details = result["errors"][0]["details"]
    assert details["detail"] == (
        "Batch 1/1 failed after 0 completed batch(es): "
        "As-Built assessor timed out after 120 seconds."
    )
    assert details["failed_batch_index"] == 1
    assert details["completed_batches"] == 0
    assert AS_BUILT_ASSESSMENT_STATE_KEY not in workflow.state
    assert AS_BUILT_ASSESSMENT_META_STATE_KEY not in workflow.state
    with Session(engine) as session:
        assert session.exec(select(WorkflowEvent)).all() == []


def test_runner_batch_failure_does_not_cache_or_record_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Any batch failure should fail the whole command without partial cache."""
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    _seed_authority(engine)
    repo = tmp_path / "repo"
    repo.mkdir()
    workflow = _WorkflowStub()
    invoker = _SecondBatchTimeoutInvoker()
    monkeypatch.setattr(as_built_module, "get_as_built_assessor_batch_size", lambda: 1)
    runner = AsBuiltAssessmentRunner(
        engine=engine,
        product_repo=_ProductRepoStub(),
        workflow_service=workflow,
        invoke_agent=invoker,
    )

    result = runner.assess(
        project_id=1,
        repo_path=str(repo),
        spec_file=None,
        spec_mode="unknown",
        user_input=None,
        idempotency_key="batch-failure",
    )

    assert result["ok"] is False
    details = result["errors"][0]["details"]
    assert result["errors"][0]["code"] == "MUTATION_FAILED"
    assert details["batch_count"] == EXPECTED_CARTOLA_TARGET_COUNT
    assert details["batch_size"] == 1
    assert details["failed_batch_index"] == FAILING_BATCH_INDEX
    assert details["completed_batches"] == 1
    assert AS_BUILT_ASSESSMENT_STATE_KEY not in workflow.state
    assert AS_BUILT_ASSESSMENT_META_STATE_KEY not in workflow.state
    with Session(engine) as session:
        assert session.exec(select(WorkflowEvent)).all() == []


def test_runner_rejects_cross_batch_capability_coverage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Each batch assessment must cover only its own authority targets."""
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    _seed_authority(engine)
    repo = tmp_path / "repo"
    repo.mkdir()
    workflow = _WorkflowStub()
    monkeypatch.setattr(as_built_module, "get_as_built_assessor_batch_size", lambda: 1)
    runner = AsBuiltAssessmentRunner(
        engine=engine,
        product_repo=_ProductRepoStub(),
        workflow_service=workflow,
        invoke_agent=_CrossBatchCoverageInvoker(),
    )

    result = runner.assess(
        project_id=1,
        repo_path=str(repo),
        spec_file=None,
        spec_mode="unknown",
        user_input=None,
        idempotency_key="cross-batch-coverage",
    )

    assert result["ok"] is False
    details = result["errors"][0]["details"]
    assert result["errors"][0]["code"] == "MUTATION_FAILED"
    assert details["failed_batch_index"] == 1
    assert details["completed_batches"] == 0
    assert "coverage did not match batch authority targets" in details["detail"]
    assert "missing_refs" in details["detail"]
    assert "REQ.live-squad-recommendation" in details["detail"]
    assert "extra_refs" in details["detail"]
    assert "REQ.legal-roster" in details["detail"]
    stderr_events = [
        json.loads(line)
        for line in capsys.readouterr().err.splitlines()
        if line.strip()
    ]
    failed_event = stderr_events[-1]
    assert failed_event["event"] == "as_built.batch_failed"
    assert failed_event["missing_refs"] == [
        "REQ.live-squad-recommendation [INV-a4b296c058e88663]"
    ]
    assert failed_event["extra_refs"] == [
        "REQ.legal-roster [INV-ffe2e17832c41874]"
    ]
    assert failed_event["duplicate_refs"] == []
    assert AS_BUILT_ASSESSMENT_STATE_KEY not in workflow.state
    assert AS_BUILT_ASSESSMENT_META_STATE_KEY not in workflow.state
    with Session(engine) as session:
        assert session.exec(select(WorkflowEvent)).all() == []


def test_runner_splits_grouped_same_requirement_invariant_assessment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model may group same-requirement invariants; cache still needs target rows."""
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    _seed_authority(engine, GROUPED_INVARIANT_AUTHORITY)
    repo = tmp_path / "repo"
    repo.mkdir()
    workflow = _WorkflowStub()
    monkeypatch.setattr(as_built_module, "get_as_built_assessor_batch_size", lambda: 10)
    runner = AsBuiltAssessmentRunner(
        engine=engine,
        product_repo=_ProductRepoStub(),
        workflow_service=workflow,
        invoke_agent=_GroupedInvariantCoverageInvoker(),
    )

    result = runner.assess(
        project_id=1,
        repo_path=str(repo),
        spec_file=None,
        spec_mode="unknown",
        user_input=None,
        idempotency_key="grouped-invariant-coverage",
    )

    assert result["ok"] is True
    capabilities = result["data"]["assessment"]["capability_assessments"]
    assert [
        (item["authority_ref"], item["invariant_refs"]) for item in capabilities
    ] == [
        ("REQ.grouped-capability", ["INV-group-a"]),
        ("REQ.grouped-capability", ["INV-group-b"]),
    ]
    assert AS_BUILT_ASSESSMENT_STATE_KEY in workflow.state


def test_runner_adds_batch_context_to_invoker_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Deep model progress events should include current batch context."""
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    _seed_authority(engine)
    repo = tmp_path / "repo"
    repo.mkdir()
    workflow = _WorkflowStub()
    monkeypatch.setattr(as_built_module, "get_as_built_assessor_batch_size", lambda: 1)
    runner = AsBuiltAssessmentRunner(
        engine=engine,
        product_repo=_ProductRepoStub(),
        workflow_service=workflow,
        invoke_agent=_ProgressContextFailureInvoker(),
    )

    result = runner.assess(
        project_id=1,
        repo_path=str(repo),
        spec_file=None,
        spec_mode="unknown",
        user_input=None,
        idempotency_key="progress-context",
    )

    assert result["ok"] is False
    stderr_events = [
        json.loads(line)
        for line in capsys.readouterr().err.splitlines()
        if line.strip()
    ]
    model_failure = next(
        event
        for event in stderr_events
        if event["event"] == "as_built.model_call_failed"
    )
    assert model_failure["batch_index"] == 1
    assert model_failure["batch_count"] == EXPECTED_CARTOLA_TARGET_COUNT
    assert model_failure["batch_target_count"] == 1


@pytest.mark.anyio
async def test_default_invoker_times_out_with_clear_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default ADK invocation should be bounded instead of hanging forever."""
    repo = tmp_path / "repo"
    repo.mkdir()
    pack = build_evidence_pack(
        project_id=2,
        authority_fingerprint="sha256:authority",
        compiled_authority=CARTOLA_AUTHORITY,
        repo_path=repo,
        spec_mode="unknown",
        spec_file=None,
    )

    async def never_returns(**_kwargs: object) -> str:
        await anyio.sleep(10)
        return "{}"

    monkeypatch.setattr(as_built_module, "invoke_agent_to_text", never_returns)
    monkeypatch.setattr(
        as_built_module,
        "get_as_built_assessor_timeout_seconds",
        lambda: 0.01,
    )

    with anyio.fail_after(1):
        with pytest.raises(RuntimeError, match=r"timed out after 0\.01 seconds"):
            await as_built_module._invoke_agent_payload_async(
                agent=object(),
                payload=_input_payload_for_pack(pack),
            )


@pytest.mark.anyio
async def test_default_invoker_retries_transient_provider_json_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Transient OpenRouter/LiteLLM JSON transport errors should retry once."""
    repo = tmp_path / "repo"
    repo.mkdir()
    pack = build_evidence_pack(
        project_id=2,
        authority_fingerprint="sha256:authority",
        compiled_authority=CARTOLA_AUTHORITY,
        repo_path=repo,
        spec_mode="unknown",
        spec_file=None,
    )
    payload = _input_payload_for_pack(pack)
    valid = _fake_assessment(payload).model_dump_json()
    calls = 0

    async def flaky_provider(**_kwargs: object) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            msg = TRANSIENT_PROVIDER_JSON_ERROR
            raise AgentInvocationError(msg)
        return valid

    monkeypatch.setattr(as_built_module, "invoke_agent_to_text", flaky_provider)

    assessment = await as_built_module._invoke_agent_payload_async(
        agent=object(),
        payload=payload,
    )

    assert calls == EXPECTED_TRANSIENT_RETRY_CALLS
    assert assessment.assessment_id == payload.assessment_id
    stderr_events = [
        json.loads(line)
        for line in capsys.readouterr().err.splitlines()
        if line.strip()
    ]
    retry_events = [
        event
        for event in stderr_events
        if event["event"] == "as_built.model_call_retrying"
    ]
    assert retry_events[0]["attempt_index"] == 1
    assert retry_events[0]["next_attempt_index"] == EXPECTED_TRANSIENT_RETRY_CALLS


@pytest.mark.anyio
async def test_default_invoker_retries_schema_invalid_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Schema-invalid model JSON should retry before failing the whole batch."""
    repo = tmp_path / "repo"
    repo.mkdir()
    pack = build_evidence_pack(
        project_id=2,
        authority_fingerprint="sha256:authority",
        compiled_authority=CARTOLA_AUTHORITY,
        repo_path=repo,
        spec_mode="unknown",
        spec_file=None,
    )
    payload = _input_payload_for_pack(pack)
    valid = _fake_assessment(payload).model_dump_json()
    calls = 0
    payloads: list[str] = []

    async def flaky_schema(**kwargs: object) -> str:
        nonlocal calls
        calls += 1
        payloads.append(str(kwargs["payload_json"]))
        if calls == 1:
            return '{"metadata": {"bad": true}}'
        return valid

    monkeypatch.setattr(as_built_module, "invoke_agent_to_text", flaky_schema)

    assessment = await as_built_module._invoke_agent_payload_async(
        agent=object(),
        payload=payload,
    )

    assert calls == EXPECTED_TRANSIENT_RETRY_CALLS
    assert assessment.assessment_id == payload.assessment_id
    retry_input = AsBuiltAssessorInput.model_validate_json(payloads[1])
    assert "SYSTEM_FEEDBACK" in retry_input.user_input
    assert "schema-invalid JSON" in retry_input.user_input
    assert "metadata" in retry_input.user_input


@pytest.mark.anyio
async def test_default_invoker_retries_adk_schema_validation_error_with_feedback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADK output_schema validation failures should feed back before retrying."""
    repo = tmp_path / "repo"
    repo.mkdir()
    pack = build_evidence_pack(
        project_id=2,
        authority_fingerprint="sha256:authority",
        compiled_authority=CARTOLA_AUTHORITY,
        repo_path=repo,
        spec_mode="unknown",
        spec_file=None,
    )
    payload = _input_payload_for_pack(pack)
    valid = _fake_assessment(payload).model_dump_json()
    calls = 0
    payloads: list[str] = []

    async def flaky_adk_schema(**kwargs: object) -> str:
        nonlocal calls
        calls += 1
        payloads.append(str(kwargs["payload_json"]))
        if calls == 1:
            message = "13 validation errors for AsBuiltAssessment"
            raise AgentInvocationError(
                message,
                validation_errors=[
                    {
                        "loc": ("schema_version",),
                        "msg": "Field required",
                        "type": "missing",
                    }
                ],
            )
        return valid

    monkeypatch.setattr(as_built_module, "invoke_agent_to_text", flaky_adk_schema)

    assessment = await as_built_module._invoke_agent_payload_async(
        agent=object(),
        payload=payload,
    )

    assert calls == EXPECTED_TRANSIENT_RETRY_CALLS
    assert assessment.assessment_id == payload.assessment_id
    retry_input = AsBuiltAssessorInput.model_validate_json(payloads[1])
    assert "SYSTEM_FEEDBACK" in retry_input.user_input
    assert "schema_version" in retry_input.user_input


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
    assert second["data"]["authority_target_count"] == first["data"][
        "authority_target_count"
    ]
    assert second["data"]["batch_count"] == first["data"]["batch_count"]
    assert second["data"]["batch_size"] == first["data"]["batch_size"]


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


def test_runner_rejects_reused_idempotency_key_with_changed_batch_size(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Batch size affects assessor execution and must guard idempotency replay."""
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
    monkeypatch.setattr(as_built_module, "get_as_built_assessor_batch_size", lambda: 2)

    assert runner.assess(
        project_id=1,
        repo_path=str(repo),
        spec_file=None,
        spec_mode="unknown",
        user_input=None,
        idempotency_key="batch-size-key",
    )["ok"] is True
    monkeypatch.setattr(as_built_module, "get_as_built_assessor_batch_size", lambda: 1)
    second = runner.assess(
        project_id=1,
        repo_path=str(repo),
        spec_file=None,
        spec_mode="unknown",
        user_input=None,
        idempotency_key="batch-size-key",
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
