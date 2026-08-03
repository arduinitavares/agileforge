"""Single source of truth for Spec Authority Compiler instructions.

Policy:
- The agent and host-side normalizer MUST use the exact same instruction string.
- prompt_hash MUST be computed from this exact string.

Note on retries:
- The ADK runtime may retry internally to satisfy JSON schema constraints.
- Host-side normalization is the final authority for determinism (IDs/prompt_hash).
"""

from __future__ import annotations

from adapters.adk.prompts import load_prompt
from services.contracts.specification import (
    SPEC_AUTHORITY_COMPILER_PROMPT_HASH as EXPECTED_SPEC_AUTHORITY_COMPILER_PROMPT_HASH,
)
from services.contracts.specification import (
    SPEC_AUTHORITY_COMPILER_VERSION,
    compute_prompt_hash,
)

SPEC_AUTHORITY_COMPILER_INSTRUCTIONS: str = load_prompt("specification.txt")

SPEC_AUTHORITY_COMPILER_PROMPT_HASH: str = compute_prompt_hash(
    SPEC_AUTHORITY_COMPILER_INSTRUCTIONS
)

if SPEC_AUTHORITY_COMPILER_PROMPT_HASH != EXPECTED_SPEC_AUTHORITY_COMPILER_PROMPT_HASH:
    msg = "Spec Authority Compiler prompt does not match its service contract."
    raise RuntimeError(msg)

__all__ = [
    "SPEC_AUTHORITY_COMPILER_INSTRUCTIONS",
    "SPEC_AUTHORITY_COMPILER_PROMPT_HASH",
    "SPEC_AUTHORITY_COMPILER_VERSION",
]
