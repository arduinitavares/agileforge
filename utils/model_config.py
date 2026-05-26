"""Model configuration loader for ADK agents."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from utils.runtime_config import get_bool_env, load_runtime_env

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_CONFIG_PATH = _REPO_ROOT / "config" / "models.yaml"

load_runtime_env()

OPENROUTER_PROVIDER: dict[str, Any] = {
    "data_collection": "deny",
    "zdr": True,
    "sort": "price",
    "allow_fallbacks": True,
    "require_parameters": True,
}

OPENROUTER_PRIVACY_ERROR_MESSAGE = (
    "No ZDR/data_collection=deny provider available for this model"
)

# ZDR retry configuration
ZDR_MAX_RETRIES = 5
ZDR_MAX_BACKOFF_SECONDS = 10.0


class ModelConfigError(RuntimeError):
    """Base error for invalid model configuration data."""


class ModelConfigNotFoundError(FileNotFoundError):
    """Raised when the configured models.yaml file cannot be found."""

    def __init__(self, config_path: Path) -> None:
        """Store the missing config path in the exception message."""
        super().__init__(f"Model config not found: {config_path}")


class ModelConfigMappingError(TypeError):
    """Raised when the root models.yaml payload is not a mapping."""

    def __init__(self) -> None:
        """Describe the expected root YAML structure."""
        super().__init__("Model config must be a YAML mapping")


class ModelsSectionMappingError(TypeError):
    """Raised when the models section is not a mapping."""

    def __init__(self) -> None:
        """Describe the expected type for the models section."""
        super().__init__("models must be a mapping in models.yaml")


class ModelKeyNotFoundError(KeyError):
    """Raised when a requested model key is absent from models.yaml."""

    def __init__(self, key: str) -> None:
        """Store the missing model key in the exception message."""
        super().__init__(f"Model key not found in models.yaml: {key}")


class StoryPipelineMappingError(TypeError):
    """Raised when the story_pipeline section is not a mapping."""

    def __init__(self) -> None:
        """Describe the expected type for the story_pipeline section."""
        super().__init__("story_pipeline must be a mapping in models.yaml")


class StoryPipelineModeError(ValueError):
    """Raised when story_pipeline.mode is neither batch nor single."""

    def __init__(self) -> None:
        """Describe the allowed story pipeline modes."""
        super().__init__("story_pipeline.mode must be 'batch' or 'single'")


def is_zdr_routing_error(exception: BaseException) -> bool:
    """Check if an exception is a ZDR/privacy routing failure.

    These are transient errors when no privacy-compliant provider is available.
    They can be retried after a short delay.
    """
    message = str(exception).lower()
    return (
        "zdr" in message
        or "data_collection" in message
        or ("provider" in message and "no" in message)
        or "no providers" in message
    )


def _get_config_path() -> Path:
    env_path = os.getenv("MODEL_CONFIG_PATH")
    if not env_path:
        return _DEFAULT_CONFIG_PATH

    candidate = Path(env_path)
    if not candidate.is_absolute():
        candidate = (_REPO_ROOT / candidate).resolve()
    return candidate


@lru_cache(maxsize=1)
def _load_config() -> dict[str, Any]:
    config_path = _get_config_path()
    if not config_path.exists():
        raise ModelConfigNotFoundError(config_path)
    raw = config_path.read_text(encoding="utf-8")
    data = yaml.safe_load(raw) or {}
    if not isinstance(data, dict):
        raise ModelConfigMappingError
    return data


def clear_config_cache() -> None:
    """Clear cached model configuration (useful for tests)."""
    _load_config.cache_clear()


def get_model_id(key: str) -> str:
    """Get model identifier by key.

    Args:
        key: Model key under models.

    Returns:
        Model identifier string.
    """
    data = _load_config()
    models = data.get("models", {})
    if not isinstance(models, dict):
        raise ModelsSectionMappingError
    model_id = models.get(key)
    if not model_id:
        raise ModelKeyNotFoundError(key)
    return str(model_id)


def _get_provider_config() -> dict[str, Any]:
    relax_privacy = get_bool_env("RELAX_ZDR_FOR_TESTS", default=False)
    if not relax_privacy:
        return dict(OPENROUTER_PROVIDER)

    relaxed_provider = dict(OPENROUTER_PROVIDER)
    relaxed_provider.update(
        {
            "data_collection": "allow",
            "zdr": False,
            "allow_fallbacks": True,
            "require_parameters": False,
        }
    )
    return relaxed_provider


def get_openrouter_extra_body() -> dict[str, Any]:
    """Return extra_body for OpenRouter requests with privacy routing."""
    return {"provider": _get_provider_config()}


def get_story_pipeline_mode() -> str:
    """Return story pipeline mode from config.

    Allowed values: "batch" or "single".
    Defaults to "batch" when not configured.
    """
    data = _load_config()
    pipeline = data.get("story_pipeline", {})
    if pipeline is None:
        pipeline = {}
    if not isinstance(pipeline, dict):
        raise StoryPipelineMappingError

    mode = str(pipeline.get("mode", "batch")).strip().lower()
    if mode not in {"batch", "single"}:
        raise StoryPipelineModeError
    return mode


def get_story_pipeline_negation_tolerance() -> bool:
    """Return whether LLM negation tolerance is enabled for alignment checks."""
    data = _load_config()
    pipeline = data.get("story_pipeline", {})
    if pipeline is None:
        pipeline = {}
    if not isinstance(pipeline, dict):
        raise StoryPipelineMappingError

    enabled = pipeline.get("negation_tolerance_llm", False)
    return bool(enabled)
