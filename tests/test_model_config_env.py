"""Tests for model config environment overrides."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import yaml
from pydantic import TypeAdapter

from adapters.adk.model_roles import AGENTIC_MODEL_ROLES, RETAINED_MODEL_ROLES
from utils import model_config, runtime_config
from utils.model_config import (
    get_model_id,
    get_openrouter_extra_body,
    get_story_pipeline_mode,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from services.contracts.provider_retry import ProviderRetryConfig

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
  product_vision: "openrouter/openai/gpt-5-mini"
  roadmap_builder: "openrouter/openai/gpt-5-mini"
  user_story_writer: "openrouter/openai/gpt-5-mini"
  spec_validator: "openrouter/openai/gpt-5-mini"
  backlog_primer: "openrouter/openai/gpt-5-mini"
  sprint_planner: "openrouter/openai/gpt-5-mini"
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
        assert get_model_id("product_vision") == "openrouter/openai/gpt-5-mini"
        assert get_story_pipeline_mode() == "single"
    finally:
        model_config.clear_config_cache()


_EXPECTED_PRODUCTION_MODELS = {
    "product_vision": "openrouter/openai/gpt-5.6-luna",
    "product_goal": "openrouter/openai/gpt-5.6-luna",
    "specification_structurer": "openrouter/openai/gpt-6-sol",
    "spec_validator": "openrouter/openai/gpt-6-sol",
    "backlog_primer": "openrouter/openai/gpt-5.6-luna",
    "roadmap_builder": "openrouter/openai/gpt-6-sol",
    "user_story_writer": "openrouter/openai/gpt-5.6-luna",
    "sprint_planner": "openrouter/openai/gpt-5.6-luna",
}


def test_default_model_config_assigns_all_eight_roles_and_max_reasoning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch a wrong role assignment or lost shared reasoning setting."""
    monkeypatch.delenv("MODEL_CONFIG_PATH", raising=False)
    model_config.clear_config_cache()

    try:
        assert {key: get_model_id(key) for key in _EXPECTED_PRODUCTION_MODELS} == (
            _EXPECTED_PRODUCTION_MODELS
        )
        assert model_config.get_model_reasoning_config() == {"effort": "max"}
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
        assert model_config.get_model_reasoning_config() == {}
    finally:
        model_config.clear_config_cache()


def test_reasoning_config_does_not_leak_between_config_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Switching profiles must not carry a cached reasoning choice forward."""
    with_reasoning = tmp_path / "with.yaml"
    with_reasoning.write_text(
        "models: {}\nreasoning:\n  effort: max\n", encoding="utf-8"
    )
    without_reasoning = tmp_path / "without.yaml"
    without_reasoning.write_text("models: {}\n", encoding="utf-8")
    try:
        monkeypatch.setenv("MODEL_CONFIG_PATH", str(with_reasoning))
        model_config.clear_config_cache()
        assert model_config.get_model_reasoning_config() == {"effort": "max"}
        monkeypatch.setenv("MODEL_CONFIG_PATH", str(without_reasoning))
        model_config.clear_config_cache()
        assert model_config.get_model_reasoning_config() == {}
    finally:
        model_config.clear_config_cache()


@pytest.mark.parametrize(
    ("raw", "error"),
    [
        ("[]", "YAML mapping"),
        ("models: []", "models must be a mapping"),
        ("models:\n  product_goal: ''", "models.product_goal"),
        ("models:\n  product_goal: 42", "models.product_goal"),
        ("reasoning: max", "reasoning must be a mapping"),
        ("reasoning:\n  effort: low", "reasoning.effort"),
        ("reasoning:\n  effort: 1", "reasoning.effort"),
    ],
)
def test_pure_model_parser_rejects_invalid_config(raw: str, error: str) -> None:
    """Reject malformed maintenance inputs before publishing them."""
    with pytest.raises((TypeError, ValueError), match=error):
        model_config.parse_model_config(raw)


def test_pure_model_parser_requires_all_roles_only_when_requested() -> None:
    """Maintenance enforces complete role coverage while old test configs stay valid."""
    assert model_config.parse_model_config("models:\n  product_goal: test/model\n") == {
        "models": {"product_goal": "test/model"}
    }
    with pytest.raises(ValueError, match="product_vision"):
        model_config.parse_model_config(
            "models:\n  product_goal: test/model\n", require_all_roles=True
        )


def test_model_configs_exactly_match_live_production_roles() -> None:
    """Keep production and test config equal to the retained recipe roles."""
    expected = frozenset(_RUNTIME_MODEL_KEYS)

    assert _configured_model_roles(_PRODUCTION_MODEL_CONFIG_PATH) == expected
    assert _configured_model_roles(_TEST_MODEL_CONFIG_PATH) == expected


def test_specification_structure_uses_the_structurer_model_role() -> None:
    """Hard-break the provider role instead of retaining an author alias."""
    assert AGENTIC_MODEL_ROLES["specification.structure"] == (
        "specification_structurer"
    )
    assert "specification_author" not in RETAINED_MODEL_ROLES


def test_relax_zdr_for_tests_toggles_privacy(monkeypatch: pytest.MonkeyPatch) -> None:
    """RELAX_ZDR_FOR_TESTS should relax the OpenRouter privacy routing."""
    monkeypatch.setenv("RELAX_ZDR_FOR_TESTS", "true")

    extra_body = get_openrouter_extra_body()
    provider = extra_body["provider"]

    assert provider["zdr"] is False
    assert provider["data_collection"] == "allow"
    assert provider["allow_fallbacks"] is True
    assert provider["require_parameters"] is False


_RETRY_ENV_KEYS: tuple[str, ...] = (
    "OPENROUTER_RETRY_MAX_ATTEMPTS",
    "OPENROUTER_RETRY_BASE_DELAY_SECONDS",
    "OPENROUTER_RETRY_MAX_DELAY_SECONDS",
    "OPENROUTER_RETRY_MAX_ELAPSED_SECONDS",
    "OPENROUTER_RETRY_MIN_REMAINING_SECONDS",
)


def _retry_config() -> ProviderRetryConfig:
    return runtime_config.get_provider_retry_config()


@pytest.fixture
def clean_retry_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep retry policy inputs synthetic and independent of local .env."""
    for key in _RETRY_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    runtime_config.clear_runtime_config_cache()
    yield
    runtime_config.clear_runtime_config_cache()


@pytest.mark.usefixtures("clean_retry_env")
def test_retry_configuration_defaults_are_safe() -> None:
    """Catch lost defaults or accidental disabling of bounded retries."""
    config = _retry_config()
    assert config.model_dump() == {
        "max_attempts": 3,
        "base_delay_seconds": 1.0,
        "max_delay_seconds": 8.0,
        "max_elapsed_seconds": 60.0,
        "min_remaining_seconds": 1.0,
    }


@pytest.mark.usefixtures("clean_retry_env")
def test_retry_configuration_overrides_are_cached_and_clearable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch ignored overrides or a retry cache omitted from the existing reset."""
    for key, value in zip(_RETRY_ENV_KEYS, ("1", "2.5", "9", "90", "3"), strict=True):
        monkeypatch.setenv(key, value)
    config = _retry_config()
    assert config.model_dump() == {
        "max_attempts": 1,
        "base_delay_seconds": 2.5,
        "max_delay_seconds": 9.0,
        "max_elapsed_seconds": 90.0,
        "min_remaining_seconds": 3.0,
    }
    monkeypatch.setenv("OPENROUTER_RETRY_MAX_ATTEMPTS", "4")
    assert _retry_config() is config
    runtime_config.clear_runtime_config_cache()
    assert _retry_config().model_dump() == {
        "max_attempts": 4,
        "base_delay_seconds": 2.5,
        "max_delay_seconds": 9.0,
        "max_elapsed_seconds": 90.0,
        "min_remaining_seconds": 3.0,
    }


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("OPENROUTER_RETRY_MAX_ATTEMPTS", "true"),
        ("OPENROUTER_RETRY_MAX_ATTEMPTS", "1.5"),
        ("OPENROUTER_RETRY_MAX_ATTEMPTS", "0"),
        ("OPENROUTER_RETRY_MAX_ATTEMPTS", "11"),
        ("OPENROUTER_RETRY_BASE_DELAY_SECONDS", "nan"),
        ("OPENROUTER_RETRY_BASE_DELAY_SECONDS", "0"),
        ("OPENROUTER_RETRY_BASE_DELAY_SECONDS", "61"),
        ("OPENROUTER_RETRY_MAX_DELAY_SECONDS", "inf"),
        ("OPENROUTER_RETRY_MAX_DELAY_SECONDS", "0.5"),
        ("OPENROUTER_RETRY_MAX_ELAPSED_SECONDS", "121"),
        ("OPENROUTER_RETRY_MIN_REMAINING_SECONDS", "61"),
        ("OPENROUTER_RETRY_MIN_REMAINING_SECONDS", "false"),
        ("OPENROUTER_RETRY_MIN_REMAINING_SECONDS", ""),
    ],
)
@pytest.mark.usefixtures("clean_retry_env")
def test_retry_configuration_rejects_bad_environment_values(
    monkeypatch: pytest.MonkeyPatch, key: str, value: str
) -> None:
    """Invalid retry inputs fail locally before a client can send anything."""
    monkeypatch.setenv(key, value)
    with pytest.raises(runtime_config.RuntimeConfigError, match=key):
        _retry_config()
