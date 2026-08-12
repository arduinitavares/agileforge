"""Tool-surface regressions for typed Specification Authority compilation."""

from __future__ import annotations

from tools import spec_tools


def test_spec_tools_have_no_raw_compiler_bypass() -> None:
    """Tools can select an approved version but cannot upload specification bytes."""
    retired = {
        "CompileSpecAuthorityInput",
        "PreviewSpecAuthorityInput",
        "UpdateSpecAndCompileAuthorityInput",
        "UpdateSpecAndCompileAuthorityToolInput",
        "compile_spec_authority",
        "preview_spec_authority",
        "update_spec_and_compile_authority",
        "_invoke_spec_authority_compiler",
    }
    assert all(not hasattr(spec_tools, name) for name in retired)

    fields = spec_tools.CompileSpecAuthorityForVersionToolInput.model_fields
    assert set(fields) == {"spec_version_id", "force_recompile"}
