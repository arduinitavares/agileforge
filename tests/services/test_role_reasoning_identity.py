"""Effective effort belongs to its own workflow attempt identity."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from services.application import _agentic_execution_settings
from utils import model_config
from workflow.fingerprints import canonical_hash

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(
    ("node_id", "effort"),
    [
        ("vision.bootstrap", "medium"),
        ("vision.interview", "medium"),
        ("goal.interview", "medium"),
        ("specification.structure", "xhigh"),
        ("backlog.generate", "high"),
        ("planning.roadmap.generate", "xhigh"),
        ("planning.story.generate", "high"),
        ("planning.sprint.plan", "high"),
    ],
)
def test_node_attempt_records_its_effective_role_effort(
    monkeypatch: pytest.MonkeyPatch, node_id: str, effort: str
) -> None:
    """Catch a shared global effort being recorded for a role-specific request."""
    monkeypatch.delenv("MODEL_CONFIG_PATH", raising=False)
    model_config.clear_config_cache()
    try:
        assert _agentic_execution_settings(node_id)["reasoning"] == {"effort": effort}
    finally:
        model_config.clear_config_cache()


def test_effort_change_updates_only_matching_attempt_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An effort edit must not reuse old work or invalidate unrelated nodes."""
    config = tmp_path / "models.yaml"
    original = (
        "models: {}\nreasoning:\n  effort: high\n  by_role:\n    product_goal: medium\n"
    )
    config.write_text(original, encoding="utf-8")
    monkeypatch.setenv("MODEL_CONFIG_PATH", str(config))
    model_config.clear_config_cache()
    try:
        goal_before = canonical_hash(_agentic_execution_settings("goal.interview"))
        roadmap_before = canonical_hash(
            _agentic_execution_settings("planning.roadmap.generate")
        )
        config.write_text(original.replace("medium", "xhigh"), encoding="utf-8")
        model_config.clear_config_cache()
        assert canonical_hash(_agentic_execution_settings("goal.interview")) != (
            goal_before
        )
        assert (
            canonical_hash(_agentic_execution_settings("planning.roadmap.generate"))
            == roadmap_before
        )
    finally:
        model_config.clear_config_cache()
