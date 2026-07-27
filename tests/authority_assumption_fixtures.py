"""Shared fixtures for current typed assumptions and historical v2 authority."""


def free_text_assumption(text: str) -> dict[str, str]:
    """Return one current v3 free-text assumption."""
    return {"kind": "free_text", "text": text}


def historical_v2_compiled_authority(
    *,
    prompt_hash: str,
    assumptions: list[str] | None = None,
) -> dict[str, object]:
    """Return one immutable historical v2 authority payload."""
    return {
        "schema_version": "agileforge.compiled_authority.v2",
        "scope_themes": [],
        "domain": None,
        "invariants": [],
        "eligible_feature_rules": [],
        "rejected_features": [],
        "gaps": [],
        "assumptions": assumptions or [],
        "source_map": [],
        "compiler_version": "2.0.0",
        "prompt_hash": prompt_hash,
        "ir_schema_version": None,
        "ir_provenance": None,
    }
