"""Single source of truth for packaged Specification structuring instructions."""

from adapters.adk.prompts import load_prompt
from services.contracts.specification_authoring import (
    SPECIFICATION_STRUCTURER_PROMPT_HASH as EXPECTED_STRUCTURER_PROMPT_HASH,
)
from services.contracts.specification_authoring import (
    compute_specification_structurer_prompt_hash,
)

SPECIFICATION_STRUCTURER_INSTRUCTIONS: str = load_prompt("specification_author.txt")
SPECIFICATION_STRUCTURER_PROMPT_HASH: str = (
    compute_specification_structurer_prompt_hash(SPECIFICATION_STRUCTURER_INSTRUCTIONS)
)

if SPECIFICATION_STRUCTURER_PROMPT_HASH != EXPECTED_STRUCTURER_PROMPT_HASH:
    message = "Specification structurer prompt does not match its service contract."
    raise RuntimeError(message)

__all__ = [
    "SPECIFICATION_STRUCTURER_INSTRUCTIONS",
    "SPECIFICATION_STRUCTURER_PROMPT_HASH",
]
