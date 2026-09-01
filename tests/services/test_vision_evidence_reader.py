"""Platform-selection tests for secure Vision repository evidence readers."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, cast

import pytest

import services.vision_evidence_reader as reader_module

if TYPE_CHECKING:
    from pathlib import Path

    from services.vision_evidence_reader import RepositoryEvidenceCapabilityCode


def test_reader_factory_selects_posix_without_loading_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep non-Windows collection independent of the native Windows adapter."""
    sys.modules.pop("services.vision_evidence_windows", None)
    monkeypatch.setattr(reader_module.sys, "platform", "darwin")

    reader = reader_module.repository_evidence_reader()

    from services.vision_evidence_posix import (  # noqa: PLC0415
        PosixRepositoryEvidenceReader,
    )

    assert isinstance(reader, PosixRepositoryEvidenceReader)
    assert "services.vision_evidence_windows" not in sys.modules


def test_reader_factory_selects_windows_lazily(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Select the isolated Windows adapter only for the Windows platform."""
    sys.modules.pop("services.vision_evidence_windows", None)
    monkeypatch.setattr(reader_module.sys, "platform", "win32")

    reader = reader_module.repository_evidence_reader()

    from services.vision_evidence_windows import (  # noqa: PLC0415
        WindowsRepositoryEvidenceReader,
    )

    assert isinstance(reader, WindowsRepositoryEvidenceReader)


def test_windows_reader_reports_native_loader_failure_as_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Map a missing native function table to the closed capability result."""
    from services.vision_evidence_windows import (  # noqa: PLC0415
        WindowsRepositoryEvidenceReader,
        _WindowsApi,
        _WindowsCapabilityError,
    )

    def unavailable(cls: type[_WindowsApi]) -> _WindowsApi:
        del cls
        message = "native API unavailable"
        raise _WindowsCapabilityError(message)

    monkeypatch.setattr(_WindowsApi, "load", classmethod(unavailable))

    capability = WindowsRepositoryEvidenceReader().capability(tmp_path)

    assert capability.available is False
    assert capability.code == "REPOSITORY_EVIDENCE_CAPABILITY_UNAVAILABLE"


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


def test_windows_root_object_identity_ignores_child_entry_timestamps() -> None:
    """Retained-parent traversal may change root content without replacing root."""
    from services.vision_evidence_windows import (  # noqa: PLC0415
        _FileIdentity,
        _same_file_object,
    )

    before = _FileIdentity(
        volume_serial=1,
        file_id=b"root-id",
        size=0,
        creation_time=10,
        last_write_time=20,
        change_time=30,
        attributes=16,
    )
    after_child_change = _FileIdentity(
        volume_serial=1,
        file_id=b"root-id",
        size=4096,
        creation_time=10,
        last_write_time=21,
        change_time=31,
        attributes=16,
    )

    assert _same_file_object(before, after_child_change)
