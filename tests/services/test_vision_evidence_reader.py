"""Platform-selection tests for secure Vision repository evidence readers."""

from __future__ import annotations

import ctypes
import struct
import sys
from typing import TYPE_CHECKING, cast

import pytest

import services.vision_evidence_reader as reader_module

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any, Never

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


def test_windows_api_accepts_empty_name_for_retained_root_reopen() -> None:
    """Pass an empty relative name to NtCreateFile when reopening its root."""
    from services.vision_evidence_windows import (  # noqa: PLC0415
        _ObjectAttributes,
        _WindowsApi,
        _WindowsNativeError,
    )

    observed_names: list[str] = []

    def unavailable_native(*args: object) -> int:
        del args
        return 1

    def record_relative_name(
        file_handle: object,
        desired_access: object,
        attributes_pointer: ctypes.c_void_p,
        *args: object,
    ) -> int:
        del file_handle, desired_access, args
        attributes = ctypes.cast(
            attributes_pointer,
            ctypes.POINTER(_ObjectAttributes),
        ).contents
        observed_names.append(attributes.ObjectName.contents.Buffer)
        return -1

    native = cast("Any", unavailable_native)
    api = _WindowsApi(
        _create_file=native,
        _close_handle=native,
        _get_file_information=native,
        _get_final_path=native,
        _get_volume_information=native,
        _read_file=native,
        _nt_create_file=cast("Any", record_relative_name),
        _nt_close=native,
        _rtl_status_to_error=native,
        _get_last_error=lambda: 1,
    )

    with pytest.raises(_WindowsNativeError):
        api.open_relative(17, "", directory=True)
    with pytest.raises(_WindowsNativeError):
        api.open_relative(17, ".", directory=True)

    assert observed_names == [""]


def test_windows_native_opens_preserve_read_write_delete_sharing(
    tmp_path: Path,
) -> None:
    """Keep ordinary Git operations compatible with every retained handle."""
    from services.vision_evidence_windows import (  # noqa: PLC0415
        _FILE_SHARE_DELETE,
        _FILE_SHARE_READ,
        _FILE_SHARE_WRITE,
        _WindowsApi,
        _WindowsNativeError,
    )

    observed_share_modes: list[int] = []
    test_handle = 17

    def record_path_open(*args: object) -> int:
        observed_share_modes.append(int(cast("Any", args[2])))
        return test_handle

    def record_relative_open(*args: object) -> int:
        observed_share_modes.append(int(cast("Any", args[6])))
        return -1

    def unavailable_native(*args: object) -> int:
        del args
        return 1

    native = cast("Any", unavailable_native)
    api = _WindowsApi(
        _create_file=cast("Any", record_path_open),
        _close_handle=native,
        _get_file_information=native,
        _get_final_path=native,
        _get_volume_information=native,
        _read_file=native,
        _nt_create_file=cast("Any", record_relative_open),
        _nt_close=native,
        _rtl_status_to_error=native,
        _get_last_error=lambda: 1,
    )

    assert api.open_root(tmp_path) == test_handle
    with pytest.raises(_WindowsNativeError):
        api.open_relative(test_handle, "README.md", directory=False)

    expected = _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE
    assert observed_share_modes == [expected, expected]


def test_windows_bind_rejects_unc_reparse_before_opening_its_target() -> None:
    """Inspect a reparse point by root handle and never open its UNC target."""
    from services.vision_evidence_windows import (  # noqa: PLC0415
        _FILE_ATTRIBUTE_REPARSE_POINT,
        WindowsRepositoryEvidenceReader,
        _FileAttributeTagInfo,
        _ReparseTarget,
        _WindowsSourceBinding,
    )

    reparse_handle = 19

    class ReparseApi:
        def __init__(self) -> None:
            self.relative_opens: list[tuple[int, str, bool]] = []
            self.closed: list[tuple[int, bool]] = []

        def open_relative(
            self,
            parent_handle: int,
            component: str,
            *,
            directory: bool,
        ) -> int:
            self.relative_opens.append((parent_handle, component, directory))
            return reparse_handle

        def attributes(self, handle: int) -> _FileAttributeTagInfo:
            assert handle == reparse_handle
            return _FileAttributeTagInfo(
                FileAttributes=_FILE_ATTRIBUTE_REPARSE_POINT,
                ReparseTag=0xA000000C,
            )

        def reparse_target(self, handle: int) -> _ReparseTarget:
            assert handle == reparse_handle
            return _ReparseTarget(
                path=r"\??\UNC\server\share\README.md",
                relative=False,
            )

        def close(self, handle: int, *, native: bool) -> None:
            self.closed.append((handle, native))

    api = ReparseApi()
    reader = WindowsRepositoryEvidenceReader(api=cast("Any", api))
    warnings = []

    binding = reader._bind_source(
        root_handle=11,
        root_final_path=r"\\?\C:\repo",
        source_path="README.md",
        warnings=warnings,
    )

    assert isinstance(binding, _WindowsSourceBinding)
    assert binding.state == "unreadable"
    assert api.relative_opens == [(11, "README.md", False)]
    assert api.closed == [(reparse_handle, True)]
    assert [warning.code for warning in warnings] == ["SYMLINK_ESCAPE"]


def test_windows_decodes_retained_symlink_payload_without_target_open() -> None:
    """Decode the substitute name returned by FSCTL_GET_REPARSE_POINT."""
    from services.vision_evidence_windows import (  # noqa: PLC0415
        _IO_REPARSE_TAG_SYMLINK,
        _decode_reparse_target,
    )

    target = r"\??\UNC\server\share\README.md"
    encoded_target = target.encode("utf-16-le")
    reparse_data_length = 12 + len(encoded_target)
    content = b"".join(
        (
            struct.pack("<IHH", _IO_REPARSE_TAG_SYMLINK, reparse_data_length, 0),
            struct.pack("<HHHHI", 0, len(encoded_target), 0, 0, 0),
            encoded_target,
        )
    )

    decoded = _decode_reparse_target(content)

    assert decoded.path == target
    assert decoded.relative is False


def test_windows_bind_resolves_compatible_internal_reparse_by_root_handle() -> None:
    """Preserve internal-link semantics without an absolute target open."""
    from services.vision_evidence_windows import (  # noqa: PLC0415
        _FILE_ATTRIBUTE_DIRECTORY,
        _FILE_ATTRIBUTE_REPARSE_POINT,
        WindowsRepositoryEvidenceReader,
        _FileAttributeTagInfo,
        _FileIdentity,
        _ReparseTarget,
    )

    docs_handle = 20
    link_handle = 21
    target_parent_handle = 22
    leaf_handle = 23

    identity = _FileIdentity(
        volume_serial=1,
        file_id=b"internal-spec",
        size=12,
        creation_time=10,
        last_write_time=20,
        change_time=30,
        attributes=0,
    )

    class InternalReparseApi:
        def __init__(self) -> None:
            self.opens: list[tuple[int, str, bool]] = []
            self.closed: list[tuple[int, bool]] = []
            self._handles = iter(
                (docs_handle, link_handle, target_parent_handle, leaf_handle)
            )

        def open_relative(
            self,
            parent_handle: int,
            component: str,
            *,
            directory: bool,
        ) -> int:
            self.opens.append((parent_handle, component, directory))
            return next(self._handles)

        def attributes(self, handle: int) -> _FileAttributeTagInfo:
            values = {
                docs_handle: _FILE_ATTRIBUTE_DIRECTORY,
                link_handle: (
                    _FILE_ATTRIBUTE_DIRECTORY | _FILE_ATTRIBUTE_REPARSE_POINT
                ),
                target_parent_handle: _FILE_ATTRIBUTE_DIRECTORY,
                leaf_handle: 0,
            }
            return _FileAttributeTagInfo(
                FileAttributes=values[handle],
                ReparseTag=0xA0000003 if handle == link_handle else 0,
            )

        def reparse_target(self, handle: int) -> _ReparseTarget:
            assert handle == link_handle
            return _ReparseTarget(path=r"\??\C:\repo\specs", relative=False)

        def identity(self, handle: int) -> _FileIdentity:
            assert handle == leaf_handle
            return identity

        def final_path(self, handle: int) -> str:
            assert handle == leaf_handle
            return r"\\?\C:\repo\specs\spec.md"

        def close(self, handle: int, *, native: bool) -> None:
            self.closed.append((handle, native))

    api = InternalReparseApi()
    reader = WindowsRepositoryEvidenceReader(api=cast("Any", api))

    binding = reader._bind_source(
        root_handle=11,
        root_final_path=r"\\?\C:\repo",
        source_path="docs/spec/spec.md",
        warnings=[],
    )

    assert binding.state == "open"
    assert binding.resolved_path == "specs/spec.md"
    assert api.opens == [
        (11, "docs", True),
        (docs_handle, "spec", True),
        (11, "specs", True),
        (target_parent_handle, "spec.md", False),
    ]
    assert api.closed == [
        (link_handle, True),
        (docs_handle, True),
        (target_parent_handle, True),
    ]
    binding.close()
    assert api.closed[-1] == (leaf_handle, True)


def test_windows_bound_source_disappearance_is_reported_as_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not downgrade a bound source race to optional-file absence."""
    from services.vision_evidence_reader import (  # noqa: PLC0415
        RepositoryEvidenceChangedError,
    )
    from services.vision_evidence_windows import (  # noqa: PLC0415
        _ERROR_PATH_NOT_FOUND,
        WindowsRepositoryEvidenceReader,
        _FileIdentity,
        _WindowsApi,
        _WindowsNativeError,
        _WindowsSourceBinding,
    )

    identity = _FileIdentity(
        volume_serial=1,
        file_id=b"source-id",
        size=7,
        creation_time=10,
        last_write_time=20,
        change_time=30,
        attributes=0,
    )
    binding = _WindowsSourceBinding(
        state="open",
        identity=identity,
        final_path=r"\\?\C:\repo\README.md",
    )
    reader = WindowsRepositoryEvidenceReader(api=cast("_WindowsApi", object()))

    def disappear_after_binding(*args: object, **kwargs: object) -> Never:
        del args, kwargs
        raise _WindowsNativeError(_ERROR_PATH_NOT_FOUND)

    monkeypatch.setattr(
        WindowsRepositoryEvidenceReader,
        "_open_approved_leaf",
        disappear_after_binding,
    )

    with pytest.raises(RepositoryEvidenceChangedError):
        reader._read_relative(
            root_handle=1,
            root_final_path=r"\\?\C:\repo",
            resolved_path="README.md",
            source_path="README.md",
            warnings=[],
            byte_limit=1024,
            binding=binding,
        )


def test_windows_bound_source_access_denied_remains_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep stable ACL denial distinct from a source replacement race."""
    from services.vision_evidence_windows import (  # noqa: PLC0415
        WindowsRepositoryEvidenceReader,
        _FileIdentity,
        _WindowsApi,
        _WindowsNativeError,
        _WindowsSourceBinding,
    )

    identity = _FileIdentity(
        volume_serial=1,
        file_id=b"source-id",
        size=7,
        creation_time=10,
        last_write_time=20,
        change_time=30,
        attributes=0,
    )
    binding = _WindowsSourceBinding(
        state="open",
        identity=identity,
        final_path=r"\\?\C:\repo\README.md",
    )
    reader = WindowsRepositoryEvidenceReader(api=cast("_WindowsApi", object()))

    def deny_content_read(*args: object, **kwargs: object) -> Never:
        del args, kwargs
        raise _WindowsNativeError(5)

    monkeypatch.setattr(
        WindowsRepositoryEvidenceReader,
        "_open_approved_leaf",
        deny_content_read,
    )
    warnings = []

    content = reader._read_relative(
        root_handle=1,
        root_final_path=r"\\?\C:\repo",
        resolved_path="README.md",
        source_path="README.md",
        warnings=warnings,
        byte_limit=1024,
        binding=binding,
    )

    assert content is None
    assert [warning.code for warning in warnings] == ["EVIDENCE_UNREADABLE"]
