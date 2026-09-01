"""Platform-selection tests for secure Vision repository evidence readers."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import services.vision_evidence_reader as reader_module

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


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
