"""Shared Story scope-extension metadata helpers."""

from __future__ import annotations

from typing import Any, cast

from orchestrator_agent.agent_tools.story_linkage import normalize_requirement_key


def _coerce_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(cast("Any", value))
    except (TypeError, ValueError):
        return None


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _scope_extension_context(state: dict[str, Any]) -> dict[str, Any] | None:
    context = state.get("scope_extension_context")
    return context if isinstance(context, dict) else None


def _release_extension_metadata(
    release: dict[str, Any],
    *,
    extension_context: dict[str, Any],
) -> dict[str, Any] | None:
    amended_spec_version_id = _coerce_int(
        extension_context.get("amended_spec_version_id")
    )
    release_spec_version_id = _coerce_int(release.get("accepted_spec_version_id"))
    source_item_ids = _string_list(release.get("source_item_ids"))
    extension_source_ids = set(
        _string_list(extension_context.get("added_source_item_ids"))
    )
    is_extension = bool(
        release.get("extension_of_spec_version_id") is not None
        or (
            amended_spec_version_id is not None
            and release_spec_version_id == amended_spec_version_id
        )
        or set(source_item_ids).intersection(extension_source_ids)
    )
    if not is_extension:
        return None

    metadata: dict[str, Any] = {"extension_scope": True}
    if release_spec_version_id is not None:
        metadata["accepted_spec_version_id"] = release_spec_version_id
    elif amended_spec_version_id is not None:
        metadata["accepted_spec_version_id"] = amended_spec_version_id
    if source_item_ids:
        metadata["source_item_ids"] = source_item_ids
    return metadata


def _requirement_extension_metadata(
    state: dict[str, Any],
    *,
    parent_requirement: str,
) -> dict[str, Any] | None:
    requirement_key = normalize_requirement_key(parent_requirement)
    context = _scope_extension_context(state)
    releases = state.get("roadmap_releases")
    if context is None or not isinstance(releases, list):
        return None

    for release in releases:
        if not isinstance(release, dict):
            continue
        items = release.get("items")
        if not isinstance(items, list):
            continue
        if not any(
            isinstance(item, str)
            and normalize_requirement_key(item) == requirement_key
            for item in items
        ):
            continue
        metadata = _release_extension_metadata(release, extension_context=context)
        if metadata is not None:
            return metadata
    return None


def _metadata_matches_extension_scope(
    metadata: dict[str, Any] | None,
    expected_metadata: dict[str, Any] | None,
) -> bool:
    if expected_metadata is None:
        return True
    if not isinstance(metadata, dict) or metadata.get("extension_scope") is not True:
        return False

    expected_spec_version_id = _coerce_int(
        expected_metadata.get("accepted_spec_version_id")
    )
    if expected_spec_version_id is not None and _coerce_int(
        metadata.get("accepted_spec_version_id")
    ) != expected_spec_version_id:
        return False

    expected_source_item_ids = set(
        _string_list(expected_metadata.get("source_item_ids"))
    )
    return not expected_source_item_ids or expected_source_item_ids.issubset(
        set(_string_list(metadata.get("source_item_ids")))
    )


def _scope_metadata_from_record(record: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(record, dict) or record.get("extension_scope") is not True:
        return None

    metadata: dict[str, Any] = {"extension_scope": True}
    accepted_spec_version_id = _coerce_int(record.get("accepted_spec_version_id"))
    if accepted_spec_version_id is not None:
        metadata["accepted_spec_version_id"] = accepted_spec_version_id
    source_item_ids = _string_list(record.get("source_item_ids"))
    if source_item_ids:
        metadata["source_item_ids"] = source_item_ids
    return metadata


def _record_matches_story_scope(
    record: dict[str, Any] | None,
    expected_metadata: dict[str, Any] | None,
) -> bool:
    if expected_metadata is None:
        return True
    return _metadata_matches_extension_scope(
        _scope_metadata_from_record(record),
        expected_metadata,
    )
