"""Adapter-selection tests for secure Vision repository evidence readers."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

import services.vision_evidence_reader as reader_module
from services.vision_evidence_posix import PosixRepositoryEvidenceReader

if TYPE_CHECKING:
    from services.vision_evidence_reader import RepositoryEvidenceCapabilityCode


def test_reader_factory_returns_the_posix_adapter() -> None:
    """The Linux container runtime has exactly one evidence adapter."""
    reader = reader_module.repository_evidence_reader()

    assert isinstance(reader, PosixRepositoryEvidenceReader)


@pytest.mark.parametrize(
    ("available", "code", "message"),
    [
        (True, "REPOSITORY_EVIDENCE_CAPABILITY_UNAVAILABLE", "contradiction"),
        (False, None, "missing code"),
        (False, "REPOSITORY_EVIDENCE_CAPABILITY_UNAVAILABLE", None),
    ],
)
def test_capability_result_rejects_contradictory_or_incomplete_states(
    available: bool,
    code: str | None,
    message: str | None,
) -> None:
    """Keep capability projection finite and internally consistent."""
    with pytest.raises(ValueError, match="capability"):
        reader_module.RepositoryEvidenceCapability(
            available=available,
            code=cast("RepositoryEvidenceCapabilityCode | None", code),
            message=message,
        )
