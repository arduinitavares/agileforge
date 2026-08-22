"""Top-level direct-Specification lifecycle seam for issue #210."""

from __future__ import annotations

import json
from pathlib import Path

from tests.workflow.test_vision_backlog_graph import _decision, _snapshot
from workflow.contracts import NodeCategory

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "issue_210"
GOLD_SPECIFICATION_CANDIDATE_ID = 2


def test_accepted_specification_is_the_complete_backlog_gate() -> None:
    """Specification acceptance is the complete human gate for Backlog access."""
    manifest = json.loads(
        (FIXTURE_ROOT / "gold" / "manifest.json").read_text(encoding="utf-8")
    )
    snapshot = _snapshot()
    accepted_specification = snapshot.spec_versions[0].model_copy(
        update={
            "spec_hash": manifest["spec_hash"],
            "source_specification_candidate_id": manifest["specification_candidate_id"],
            "source_specification_candidate_fingerprint": manifest[
                "candidate_fingerprint"
            ],
        }
    )
    snapshot = snapshot.model_copy(update={"spec_versions": (accepted_specification,)})
    decision = _decision(snapshot, "backlog.generate")

    assert (
        accepted_specification.source_specification_candidate_id
        == manifest["specification_candidate_id"]
        == GOLD_SPECIFICATION_CANDIDATE_ID
    )
    assert (
        accepted_specification.source_specification_candidate_fingerprint
        == manifest["candidate_fingerprint"]
    )
    assert accepted_specification.spec_hash == manifest["spec_hash"]
    assert decision.category is NodeCategory.AVAILABLE
    assert {
        (reference.fact_type, reference.fact_id, reference.fingerprint)
        for reference in decision.fact_references
    } == {
        ("product_goal", "301", "sha256:goal-current"),
        ("specification", "101", manifest["spec_hash"]),
    }
