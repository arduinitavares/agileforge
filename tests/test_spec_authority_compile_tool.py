"""Tests for the typed Specification Authority tool adapters."""

from __future__ import annotations

from typing import Any

import pytest

from services.specs import compiler_service
from tools import spec_tools


def test_tool_exports_service_owned_typed_input_models() -> None:
    """Tool callers share the active service contracts by identity."""
    assert (
        spec_tools.CompileSpecAuthorityForVersionInput
        is compiler_service.CompileSpecAuthorityForVersionInput
    )
    assert (
        spec_tools.CheckSpecAuthorityStatusInput
        is compiler_service.CheckSpecAuthorityStatusInput
    )
    assert (
        spec_tools.GetCompiledAuthorityInput
        is compiler_service.GetCompiledAuthorityInput
    )


def test_compile_tool_delegates_only_version_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tool forwards an approved version selector and no Specification bytes."""
    captured: dict[str, Any] = {}
    expected = {"success": True, "authority_id": 19}

    def compile_stub(
        params: object,
        *,
        tool_context: object | None,
    ) -> dict[str, Any]:
        captured["params"] = params
        captured["tool_context"] = tool_context
        return expected

    monkeypatch.setattr(
        spec_tools,
        "_compile_spec_authority_for_version",
        compile_stub,
    )
    result = spec_tools.compile_spec_authority_for_version(
        spec_tools.CompileSpecAuthorityForVersionToolInput(
            spec_version_id=17,
            force_recompile=True,
        ),
        tool_context=None,
    )

    assert result is expected
    assert captured == {
        "params": {"spec_version_id": 17, "force_recompile": True},
        "tool_context": None,
    }


def test_authority_gate_adapter_delegates_review_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The downstream gate preserves its review request parameters."""
    captured: dict[str, Any] = {}

    def ensure_stub(
        project_id: int,
        *,
        recompile: bool,
        tool_context: object | None,
    ) -> int:
        captured.update(
            project_id=project_id,
            recompile=recompile,
            tool_context=tool_context,
        )
        return 23

    monkeypatch.setattr(spec_tools, "_ensure_accepted_spec_authority", ensure_stub)
    expected_authority_id = 23

    result = spec_tools.ensure_accepted_spec_authority(
        11,
        recompile=True,
        tool_context=None,
    )

    assert result == expected_authority_id
    assert captured == {
        "project_id": 11,
        "recompile": True,
        "tool_context": None,
    }


@pytest.mark.parametrize(
    ("adapter_name", "delegate_name", "params"),
    [
        (
            "check_spec_authority_status",
            "_check_spec_authority_status",
            {"project_id": 7},
        ),
        (
            "get_compiled_authority_by_version",
            "_get_compiled_authority_by_version",
            {"project_id": 7, "spec_version_id": 13},
        ),
    ],
)
def test_read_tools_delegate_without_mutating_payload(
    monkeypatch: pytest.MonkeyPatch,
    adapter_name: str,
    delegate_name: str,
    params: dict[str, int],
) -> None:
    """Read adapters preserve exact typed selection parameters."""
    captured: dict[str, Any] = {}

    def read_stub(
        received: object,
        *,
        tool_context: object | None,
    ) -> dict[str, Any]:
        captured.update(params=received, tool_context=tool_context)
        return {"success": True}

    monkeypatch.setattr(spec_tools, delegate_name, read_stub)
    context = object()

    result = getattr(spec_tools, adapter_name)(params, tool_context=context)

    assert result == {"success": True}
    assert captured == {"params": params, "tool_context": context}
