"""Role effort must reach real ADK models, including secondary agents."""

from __future__ import annotations

from pathlib import Path
from runpy import run_path
from typing import TYPE_CHECKING

from utils import model_config

if TYPE_CHECKING:
    from typing import Any

    import pytest


def test_every_adk_builder_uses_its_own_role_reasoning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch a missing/wrong role argument in any primary or repair builder."""
    cases = [
        ("vision", "root_agent", "medium"),
        ("vision", "repair_agent", "medium"),
        ("product_goal", "root_agent", "medium"),
        ("specification_author", "root_agent", "xhigh"),
        ("spec_validator", "root_agent", "high"),
        ("backlog", "root_agent", "high"),
        ("roadmap", "root_agent", "xhigh"),
        ("story", "root_agent", "high"),
        ("story", "create_user_story_patch_agent", "high"),
        ("sprint", "root_agent", "high"),
    ]
    agents_dir = Path(__file__).resolve().parents[2] / "adapters" / "adk" / "agents"
    modules: dict[str, dict[str, Any]] = {}
    try:
        with monkeypatch.context() as scoped:
            scoped.delenv("MODEL_CONFIG_PATH", raising=False)
            model_config.clear_config_cache()
            for name, agent_name, effort in cases:
                if name not in modules:
                    # Keep collected tests' canonical modules and callbacks intact.
                    modules[name] = run_path(str(agents_dir / f"{name}.py"))
                agent = modules[name][agent_name]
                if callable(agent):
                    agent = agent()
                assert agent.model.model == "openrouter/openai/gpt-6.1-sol"
                assert agent.model._additional_args["extra_body"]["reasoning"] == {
                    "effort": effort
                }
    finally:
        model_config.clear_config_cache()
