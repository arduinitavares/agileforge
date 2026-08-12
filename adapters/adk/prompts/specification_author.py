"""Single source of truth for packaged to-spec instructions."""

from adapters.adk.prompts import load_prompt
from services.contracts.specification_authoring import (
    SPECIFICATION_AUTHOR_PROMPT_HASH as EXPECTED_SPECIFICATION_AUTHOR_PROMPT_HASH,
)
from services.contracts.specification_authoring import (
    compute_specification_author_prompt_hash,
)

SPECIFICATION_AUTHOR_INSTRUCTIONS: str = load_prompt("specification_author.txt")
SPECIFICATION_AUTHOR_PROMPT_HASH: str = compute_specification_author_prompt_hash(
    SPECIFICATION_AUTHOR_INSTRUCTIONS
)

if SPECIFICATION_AUTHOR_PROMPT_HASH != EXPECTED_SPECIFICATION_AUTHOR_PROMPT_HASH:
    message = "Specification author prompt does not match its service contract."
    raise RuntimeError(message)

__all__ = [
    "SPECIFICATION_AUTHOR_INSTRUCTIONS",
    "SPECIFICATION_AUTHOR_PROMPT_HASH",
]
