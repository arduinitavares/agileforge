"""Public spec-related service boundaries."""

from __future__ import annotations

from importlib import import_module

_EXPORT_MODULES: dict[str, str] = {
    "check_spec_authority_status": "services.specs.compiler_service",
    "compile_spec_authority_for_version": "services.specs.compiler_service",
    "compute_story_input_hash": "services.specs.story_validation_service",
    "ensure_accepted_spec_authority": "services.specs.compiler_service",
    "get_compiled_authority_by_version": "services.specs.compiler_service",
    "load_compiled_artifact": "services.specs.compiler_service",
    "validate_story_with_spec_authority": "services.specs.story_validation_service",
}

__all__: list[str] = [
    "check_spec_authority_status",
    "compile_spec_authority_for_version",
    "compute_story_input_hash",
    "ensure_accepted_spec_authority",
    "get_compiled_authority_by_version",
    "load_compiled_artifact",
    "validate_story_with_spec_authority",
]


def __getattr__(name: str) -> object:
    """Lazily load spec service exports without importing agent runtimes."""
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(name)
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value
