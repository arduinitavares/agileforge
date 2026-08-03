"""Tests for model config environment overrides."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import TypeAdapter

from adapters.adk.model_roles import RETAINED_MODEL_ROLES
from utils import model_config
from utils.model_config import (
    get_model_id,
    get_openrouter_extra_body,
    get_story_pipeline_mode,
)

_RUNTIME_MODEL_KEYS = tuple(sorted(RETAINED_MODEL_ROLES))
_TEST_MODEL_CONFIG_PATH: Path = (
    Path(__file__).resolve().parents[1] / "config" / "models.test.yaml"
)
_PRODUCTION_MODEL_CONFIG_PATH: Path = (
    Path(__file__).resolve().parents[1] / "config" / "models.yaml"
)
_CONFIG_OBJECT = TypeAdapter(dict[str, object])
_MODEL_MAPPING = TypeAdapter(dict[str, str])


def _configured_model_roles(path: Path) -> frozenset[str]:
    payload = _CONFIG_OBJECT.validate_python(
        yaml.safe_load(path.read_text(encoding="utf-8"))
    )
    models = _MODEL_MAPPING.validate_python(payload.get("models"))
    return frozenset(models)


@pytest.fixture
def temp_model_config(tmp_path: Path) -> Path:
    """Create a temporary model config file for tests."""
    config_path = tmp_path / "models.test.yaml"
    config_path.write_text(
        """
models:
  spec_authority_compiler: "openrouter/openai/gpt-5-mini"
  product_vision: "openrouter/openai/gpt-5-mini"
  roadmap_builder: "openrouter/openai/gpt-5-mini"
  user_story_writer: "openrouter/openai/gpt-5-mini"
  spec_validator: "openrouter/openai/gpt-5-mini"
  backlog_primer: "openrouter/openai/gpt-5-mini"
  sprint_planner: "openrouter/openai/gpt-5-mini"
  brownfield_curator: "openrouter/openai/gpt-5-mini"

story_pipeline:
  mode: "single"
""".lstrip(),
        encoding="utf-8",
    )
    return config_path


def test_model_config_path_env_overrides(
    monkeypatch: pytest.MonkeyPatch, temp_model_config: Path
) -> None:
    """MODEL_CONFIG_PATH should override the default config file."""
    monkeypatch.setenv("MODEL_CONFIG_PATH", str(temp_model_config))
    model_config.clear_config_cache()

    try:
        assert get_model_id("spec_authority_compiler") == "openrouter/openai/gpt-5-mini"
        assert get_story_pipeline_mode() == "single"
    finally:
        model_config.clear_config_cache()


def test_default_model_config_uses_cheapest_gpt_5_6_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production agent roles should default to GPT-5.6 Luna."""
    monkeypatch.delenv("MODEL_CONFIG_PATH", raising=False)
    model_config.clear_config_cache()

    try:
        assert {get_model_id(key) for key in _RUNTIME_MODEL_KEYS} == {
            "openrouter/openai/gpt-5.6-luna"
        }
    finally:
        model_config.clear_config_cache()


def test_test_model_config_uses_pinned_free_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All runtime agent roles should use the pinned free model under pytest."""
    monkeypatch.setenv("MODEL_CONFIG_PATH", str(_TEST_MODEL_CONFIG_PATH))
    model_config.clear_config_cache()

    try:
        assert {get_model_id(key) for key in _RUNTIME_MODEL_KEYS} == {
            "openrouter/openai/gpt-oss-20b:free"
        }
    finally:
        model_config.clear_config_cache()


def test_model_configs_exactly_match_live_production_roles() -> None:
    """Keep production and test config equal to the retained recipe roles."""
    expected = frozenset(_RUNTIME_MODEL_KEYS)

    assert _configured_model_roles(_PRODUCTION_MODEL_CONFIG_PATH) == expected
    assert _configured_model_roles(_TEST_MODEL_CONFIG_PATH) == expected


def test_relax_zdr_for_tests_toggles_privacy(monkeypatch: pytest.MonkeyPatch) -> None:
    """RELAX_ZDR_FOR_TESTS should relax the OpenRouter privacy routing."""
    monkeypatch.setenv("RELAX_ZDR_FOR_TESTS", "true")

    extra_body = get_openrouter_extra_body()
    provider = extra_body["provider"]

    assert provider["zdr"] is False
    assert provider["data_collection"] == "allow"
    assert provider["allow_fallbacks"] is True
    assert provider["require_parameters"] is False
