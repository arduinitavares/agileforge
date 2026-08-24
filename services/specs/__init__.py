"""Public spec-related service boundaries."""

from __future__ import annotations

from importlib import import_module

_EXPORT_MODULES: dict[str, str] = {
    "AcceptedSpecification": "services.specs.accepted_specification",
    "AcceptedSpecificationIntegrityError": "services.specs.accepted_specification",
    "compute_story_validation_input_fingerprint": (
        "services.specs.story_validation_service"
    ),
    "load_accepted_specification": "services.specs.accepted_specification",
    "load_current_accepted_specification": "services.specs.accepted_specification",
    "require_current_accepted_specification": "services.specs.accepted_specification",
    "require_story_ready_for_sprint": "services.specs.story_validation_service",
    "validate_story_with_specification": "services.specs.story_validation_service",
}

__all__: list[str] = [
    "AcceptedSpecification",
    "AcceptedSpecificationIntegrityError",
    "compute_story_validation_input_fingerprint",
    "load_accepted_specification",
    "load_current_accepted_specification",
    "require_current_accepted_specification",
    "require_story_ready_for_sprint",
    "validate_story_with_specification",
]


def __getattr__(name: str) -> object:
    """Lazily load spec service exports without importing agent runtimes."""
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(name)
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value
