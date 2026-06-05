"""Single source of truth for Spec Authority Compiler instructions.

Policy:
- The agent and host-side normalizer MUST use the exact same instruction string.
- prompt_hash MUST be computed from this exact string.

Note on retries:
- The ADK runtime may retry internally to satisfy JSON schema constraints.
- Host-side normalization is the final authority for determinism (IDs/prompt_hash).
"""

from __future__ import annotations

from pathlib import Path

from orchestrator_agent.agent_tools.spec_authority_compiler_agent.compiler_contract import (
    compute_prompt_hash,
)
from utils.helper import load_instruction

SPEC_AUTHORITY_COMPILER_VERSION = "2.0.0"

_INSTRUCTIONS_PATH = Path(__file__).with_name("instructions.txt")

SPEC_AUTHORITY_COMPILER_INSTRUCTIONS: str = load_instruction(_INSTRUCTIONS_PATH)

SPEC_AUTHORITY_COMPILER_PROMPT_HASH: str = compute_prompt_hash(
    SPEC_AUTHORITY_COMPILER_INSTRUCTIONS
)
