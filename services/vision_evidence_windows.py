"""Windows handle adapter for secure Vision repository evidence reads."""

from __future__ import annotations

import ctypes
import ntpath
import sys
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Protocol, cast

from services.contracts.vision_evidence import VisionEvidenceWarning
from services.vision_evidence_reader import (
    RepositoryEvidenceCapability,
    RepositoryEvidenceCapabilityError,
    RepositoryEvidenceChangedError,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from types import TracebackType
    from typing import ClassVar, Literal

    from services.vision_evidence_reader import RepositoryEvidenceBinding

_ULONG = ctypes.c_uint32
_DWORD = ctypes.c_uint32
_USHORT = ctypes.c_uint16
_UCHAR = ctypes.c_ubyte
_BOOLEAN = ctypes.c_ubyte
_ULONGLONG = ctypes.c_uint64
_LARGE_INTEGER = ctypes.c_int64
_NTSTATUS = ctypes.c_int32
_HANDLE = ctypes.c_void_p
_PVOID = ctypes.c_void_p
_ULONG_PTR = ctypes.c_size_t

_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_OPEN_EXISTING = 3
_FILE_READ_DATA = 0x0001
_FILE_TRAVERSE = 0x0020
_FILE_READ_ATTRIBUTES = 0x0080
_SYNCHRONIZE = 0x00100000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_SHARE_DELETE = 0x00000004
_FILE_OPEN = 0x00000001
_FILE_DIRECTORY_FILE = 0x00000001
_FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
_FILE_NON_DIRECTORY_FILE = 0x00000040
_FILE_OPEN_REPARSE_POINT = 0x00200000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_OBJ_CASE_INSENSITIVE = 0x00000040
_OBJ_DONT_REPARSE = 0x00001000
_FILE_BASIC_INFO_CLASS = 0
_FILE_STANDARD_INFO_CLASS = 1
_FILE_ATTRIBUTE_TAG_INFO_CLASS = 9
_FILE_REMOTE_PROTOCOL_INFO_CLASS = 13
_FILE_ID_INFO_CLASS = 18
_ERROR_INVALID_FUNCTION = 1
_ERROR_FILE_NOT_FOUND = 2
_ERROR_PATH_NOT_FOUND = 3
_ERROR_NOT_SUPPORTED = 50
_ERROR_INVALID_PARAMETER = 87
_SUPPORTED_FILESYSTEMS = frozenset({"NTFS", "REFS"})
_EXPECTED_POINTER_SIZE = 8
_CHANGED_DURING_READ = "Approved evidence file changed while it was read."
_WORKTREE_CHANGED_BEFORE_READ = (
    "Repository worktree changed before evidence files were read."
)
_WORKTREE_CHANGED_DURING_COLLECTION = (
    "Repository worktree changed during evidence collection."
)


class _NativeFunction(Protocol):
    argtypes: list[object]
    restype: object

    def __call__(self, *args: object) -> int: ...


class _UnicodeString(ctypes.Structure):
    _fields_: ClassVar[list[tuple[str, object]]] = [
        ("Length", _USHORT),
        ("MaximumLength", _USHORT),
        ("Buffer", ctypes.c_wchar_p),
    ]


class _ObjectAttributes(ctypes.Structure):
    _fields_: ClassVar[list[tuple[str, object]]] = [
        ("Length", _ULONG),
        ("RootDirectory", _HANDLE),
        ("ObjectName", ctypes.POINTER(_UnicodeString)),
        ("Attributes", _ULONG),
        ("SecurityDescriptor", _PVOID),
        ("SecurityQualityOfService", _PVOID),
    ]


class _IoStatusValue(ctypes.Union):
    _fields_: ClassVar[list[tuple[str, object]]] = [
        ("Status", _NTSTATUS),
        ("Pointer", _PVOID),
    ]


class _IoStatusBlock(ctypes.Structure):
    _anonymous_: ClassVar[tuple[str, ...]] = ("value",)
    _fields_: ClassVar[list[tuple[str, object]]] = [
        ("value", _IoStatusValue),
        ("Information", _ULONG_PTR),
    ]


class _FileId128(ctypes.Structure):
    _fields_: ClassVar[list[tuple[str, object]]] = [("Identifier", _UCHAR * 16)]


class _FileIdInfo(ctypes.Structure):
    _fields_: ClassVar[list[tuple[str, object]]] = [
        ("VolumeSerialNumber", _ULONGLONG),
        ("FileId", _FileId128),
    ]


class _FileBasicInfo(ctypes.Structure):
    _fields_: ClassVar[list[tuple[str, object]]] = [
        ("CreationTime", _LARGE_INTEGER),
        ("LastAccessTime", _LARGE_INTEGER),
        ("LastWriteTime", _LARGE_INTEGER),
        ("ChangeTime", _LARGE_INTEGER),
        ("FileAttributes", _DWORD),
    ]


class _FileStandardInfo(ctypes.Structure):
    _fields_: ClassVar[list[tuple[str, object]]] = [
        ("AllocationSize", _LARGE_INTEGER),
        ("EndOfFile", _LARGE_INTEGER),
        ("NumberOfLinks", _DWORD),
        ("DeletePending", _BOOLEAN),
        ("Directory", _BOOLEAN),
    ]


class _FileAttributeTagInfo(ctypes.Structure):
    _fields_: ClassVar[list[tuple[str, object]]] = [
        ("FileAttributes", _DWORD),
        ("ReparseTag", _DWORD),
    ]


class _FileRemoteProtocolInfo(ctypes.Structure):
    _fields_: ClassVar[list[tuple[str, object]]] = [
        ("StructureVersion", _USHORT),
        ("StructureSize", _USHORT),
        ("Protocol", _ULONG),
        ("ProtocolMajorVersion", _USHORT),
        ("ProtocolMinorVersion", _USHORT),
        ("ProtocolRevision", _USHORT),
        ("Reserved", _USHORT),
        ("Flags", _ULONG),
        ("GenericReserved", _ULONG * 8),
        ("ProtocolSpecificReserved", _ULONG * 16),
        ("ProtocolSpecific", _ULONG * 16),
    ]


@dataclass(frozen=True)
class _FileIdentity:
    """Stable handle metadata used to detect replacement and modification."""

    volume_serial: int
    file_id: bytes
    size: int
    creation_time: int
    last_write_time: int
    change_time: int
    attributes: int


class _WindowsCapabilityError(RuntimeError):
    """Raised when Windows cannot provide the required native safety contract."""


class _WindowsNativeError(OSError):
    """Closed internal Windows error carrying only a numeric system code."""

    def __init__(self, error_code: int) -> None:
        self.error_code = error_code
        super().__init__(error_code, "Windows file operation failed")


@dataclass(frozen=True)
class _WindowsApi:
    """Validated Win32 and NT native API table used only by the Windows adapter."""

    _create_file: _NativeFunction
    _close_handle: _NativeFunction
    _get_file_information: _NativeFunction
    _get_final_path: _NativeFunction
    _get_volume_information: _NativeFunction
    _read_file: _NativeFunction
    _nt_create_file: _NativeFunction
    _nt_close: _NativeFunction
    _rtl_status_to_error: _NativeFunction
    _get_last_error: Callable[[], int]

    @classmethod
    def load(cls) -> _WindowsApi:
        """Load every required function and assign its exact Windows signature."""
        if (
            sys.platform != "win32"
            or ctypes.sizeof(ctypes.c_void_p) != _EXPECTED_POINTER_SIZE
        ):
            message = "Secure Windows evidence requires 64-bit Windows Python."
            raise _WindowsCapabilityError(message)
        win_dll = getattr(ctypes, "WinDLL", None)
        get_last_error = getattr(ctypes, "get_last_error", None)
        if not callable(win_dll) or not callable(get_last_error):
            message = "Windows native API loading is unavailable."
            raise _WindowsCapabilityError(message)
        try:
            kernel32 = win_dll("kernel32", use_last_error=True)
            ntdll = win_dll("ntdll", use_last_error=True)
        except OSError as exc:
            message = "Required Windows system libraries are unavailable."
            raise _WindowsCapabilityError(message) from exc

        create_file = _function(kernel32, "CreateFileW")
        create_file.argtypes = [
            ctypes.c_wchar_p,
            _DWORD,
            _DWORD,
            _PVOID,
            _DWORD,
            _DWORD,
            _HANDLE,
        ]
        create_file.restype = _HANDLE

        close_handle = _function(kernel32, "CloseHandle")
        close_handle.argtypes = [_HANDLE]
        close_handle.restype = ctypes.c_int

        get_file_information = _function(
            kernel32,
            "GetFileInformationByHandleEx",
        )
        get_file_information.argtypes = [_HANDLE, ctypes.c_int, _PVOID, _DWORD]
        get_file_information.restype = ctypes.c_int

        get_final_path = _function(kernel32, "GetFinalPathNameByHandleW")
        get_final_path.argtypes = [
            _HANDLE,
            ctypes.c_wchar_p,
            _DWORD,
            _DWORD,
        ]
        get_final_path.restype = _DWORD

        get_volume_information = _function(
            kernel32,
            "GetVolumeInformationByHandleW",
        )
        get_volume_information.argtypes = [
            _HANDLE,
            ctypes.c_wchar_p,
            _DWORD,
            ctypes.POINTER(_DWORD),
            ctypes.POINTER(_DWORD),
            ctypes.POINTER(_DWORD),
            ctypes.c_wchar_p,
            _DWORD,
        ]
        get_volume_information.restype = ctypes.c_int

        read_file = _function(kernel32, "ReadFile")
        read_file.argtypes = [
            _HANDLE,
            _PVOID,
            _DWORD,
            ctypes.POINTER(_DWORD),
            _PVOID,
        ]
        read_file.restype = ctypes.c_int

        nt_create_file = _function(ntdll, "NtCreateFile")
        nt_create_file.argtypes = [
            ctypes.POINTER(_HANDLE),
            _DWORD,
            ctypes.POINTER(_ObjectAttributes),
            ctypes.POINTER(_IoStatusBlock),
            ctypes.POINTER(_LARGE_INTEGER),
            _ULONG,
            _ULONG,
            _ULONG,
            _ULONG,
            _PVOID,
            _ULONG,
        ]
        nt_create_file.restype = _NTSTATUS

        nt_close = _function(ntdll, "NtClose")
        nt_close.argtypes = [_HANDLE]
        nt_close.restype = _NTSTATUS

        rtl_status_to_error = _function(ntdll, "RtlNtStatusToDosError")
        rtl_status_to_error.argtypes = [_NTSTATUS]
        rtl_status_to_error.restype = _ULONG

        return cls(
            _create_file=create_file,
            _close_handle=close_handle,
            _get_file_information=get_file_information,
            _get_final_path=get_final_path,
            _get_volume_information=get_volume_information,
            _read_file=read_file,
            _nt_create_file=nt_create_file,
            _nt_close=nt_close,
            _rtl_status_to_error=rtl_status_to_error,
            _get_last_error=cast("Callable[[], int]", get_last_error),
        )

    def open_root(self, worktree: Path) -> int:
        """Open one resolved worktree without following a final reparse point."""
        raw_handle = self._create_file(
            str(worktree),
            _FILE_TRAVERSE | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
            None,
            _OPEN_EXISTING,
            _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        handle = _handle_value(raw_handle)
        if handle in {None, _INVALID_HANDLE_VALUE}:
            raise _WindowsNativeError(self._get_last_error())
        return handle

    def open_source_sentinel(self, path: str) -> int:
        """Follow one logical source into a retained metadata-only sentinel."""
        raw_handle = self._create_file(
            path,
            _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
            None,
            _OPEN_EXISTING,
            _FILE_FLAG_BACKUP_SEMANTICS,
            None,
        )
        handle = _handle_value(raw_handle)
        if handle in {None, _INVALID_HANDLE_VALUE}:
            raise _WindowsNativeError(self._get_last_error())
        return handle

    def open_relative(
        self, parent_handle: int, component: str, *, directory: bool
    ) -> int:
        """Open one component relative to a retained directory handle."""
        if (
            not component
            or component == ".."
            or (component == "." and not directory)
            or "\\" in component
            or "/" in component
        ):
            raise _WindowsNativeError(_ERROR_PATH_NOT_FOUND)
        name_buffer = ctypes.create_unicode_buffer(component)
        name = _UnicodeString(
            Length=len(component.encode("utf-16-le")),
            MaximumLength=len(name_buffer) * ctypes.sizeof(ctypes.c_wchar),
            Buffer=ctypes.cast(name_buffer, ctypes.c_wchar_p),
        )
        attributes = _ObjectAttributes(
            Length=ctypes.sizeof(_ObjectAttributes),
            RootDirectory=_HANDLE(parent_handle),
            ObjectName=ctypes.pointer(name),
            Attributes=_OBJ_CASE_INSENSITIVE | _OBJ_DONT_REPARSE,
            SecurityDescriptor=None,
            SecurityQualityOfService=None,
        )
        io_status = _IoStatusBlock()
        opened = _HANDLE()
        desired_access = _FILE_READ_ATTRIBUTES | _SYNCHRONIZE
        create_options = _FILE_SYNCHRONOUS_IO_NONALERT | _FILE_OPEN_REPARSE_POINT
        if directory:
            desired_access |= _FILE_TRAVERSE
            create_options |= _FILE_DIRECTORY_FILE
        else:
            desired_access |= _FILE_READ_DATA
            create_options |= _FILE_NON_DIRECTORY_FILE
        status = self._nt_create_file(
            ctypes.byref(opened),
            desired_access,
            ctypes.byref(attributes),
            ctypes.byref(io_status),
            None,
            0,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
            _FILE_OPEN,
            create_options,
            None,
            0,
        )
        if status < 0:
            error_code = self._rtl_status_to_error(status)
            raise _WindowsNativeError(int(error_code))
        handle = _handle_value(opened)
        if handle is None:
            raise _WindowsNativeError(_ERROR_FILE_NOT_FOUND)
        return handle

    def identity(self, handle: int) -> _FileIdentity:
        """Query all handle metadata required for stable identity comparison."""
        file_id = _FileIdInfo()
        basic = _FileBasicInfo()
        standard = _FileStandardInfo()
        self._query(handle, _FILE_ID_INFO_CLASS, file_id)
        self._query(handle, _FILE_BASIC_INFO_CLASS, basic)
        self._query(handle, _FILE_STANDARD_INFO_CLASS, standard)
        return _FileIdentity(
            volume_serial=int(file_id.VolumeSerialNumber),
            file_id=bytes(file_id.FileId.Identifier),
            size=int(standard.EndOfFile),
            creation_time=int(basic.CreationTime),
            last_write_time=int(basic.LastWriteTime),
            change_time=int(basic.ChangeTime),
            attributes=int(basic.FileAttributes),
        )

    def attributes(self, handle: int) -> _FileAttributeTagInfo:
        """Return reparse and file-type attributes for one retained handle."""
        value = _FileAttributeTagInfo()
        self._query(handle, _FILE_ATTRIBUTE_TAG_INFO_CLASS, value)
        return value

    def final_path(self, handle: int) -> str:
        """Return the normalized DOS path naming one retained handle."""
        needed = self._get_final_path(_HANDLE(handle), None, 0, 0)
        if needed <= 0:
            message = "Normalized handle paths are unavailable."
            raise _WindowsCapabilityError(message)
        buffer = ctypes.create_unicode_buffer(needed + 1)
        written = self._get_final_path(
            _HANDLE(handle),
            buffer,
            len(buffer),
            0,
        )
        if written <= 0 or written >= len(buffer):
            message = "Normalized handle paths are unavailable."
            raise _WindowsCapabilityError(message)
        return buffer.value

    def filesystem_name(self, handle: int) -> str:
        """Return the filesystem owning one retained worktree handle."""
        volume_name = ctypes.create_unicode_buffer(261)
        filesystem_name = ctypes.create_unicode_buffer(32)
        serial = _DWORD()
        max_component = _DWORD()
        flags = _DWORD()
        ok = self._get_volume_information(
            _HANDLE(handle),
            volume_name,
            len(volume_name),
            ctypes.byref(serial),
            ctypes.byref(max_component),
            ctypes.byref(flags),
            filesystem_name,
            len(filesystem_name),
        )
        if not ok:
            message = "Filesystem capability query failed."
            raise _WindowsCapabilityError(message)
        return filesystem_name.value.upper()

    def is_remote(self, handle: int) -> bool:
        """Return whether Windows identifies the retained handle as remote."""
        value = _FileRemoteProtocolInfo()
        ok = self._get_file_information(
            _HANDLE(handle),
            _FILE_REMOTE_PROTOCOL_INFO_CLASS,
            ctypes.byref(value),
            ctypes.sizeof(value),
        )
        if ok:
            return bool(value.Protocol)
        error_code = self._get_last_error()
        if error_code in {
            _ERROR_INVALID_FUNCTION,
            _ERROR_NOT_SUPPORTED,
            _ERROR_INVALID_PARAMETER,
        }:
            return False
        message = "Remote filesystem capability query failed."
        raise _WindowsCapabilityError(message)

    def read(self, handle: int, byte_limit: int) -> bytes:
        """Read a bounded number of bytes from one synchronous file handle."""
        content = bytearray()
        while len(content) < byte_limit:
            requested = min(64 * 1024, byte_limit - len(content))
            buffer = ctypes.create_string_buffer(requested)
            observed = _DWORD()
            ok = self._read_file(
                _HANDLE(handle),
                buffer,
                requested,
                ctypes.byref(observed),
                None,
            )
            if not ok:
                raise _WindowsNativeError(self._get_last_error())
            count = int(observed.value)
            if count == 0:
                break
            content.extend(buffer.raw[:count])
        return bytes(content)

    def close(self, handle: int, *, native: bool) -> None:
        """Close a root Win32 handle or a relative NT native handle."""
        if native:
            status = self._nt_close(_HANDLE(handle))
            if status < 0:
                raise _WindowsNativeError(int(self._rtl_status_to_error(status)))
            return
        if not self._close_handle(_HANDLE(handle)):
            raise _WindowsNativeError(self._get_last_error())

    def _query(self, handle: int, info_class: int, value: ctypes.Structure) -> None:
        ok = self._get_file_information(
            _HANDLE(handle),
            info_class,
            ctypes.byref(value),
            ctypes.sizeof(value),
        )
        if not ok:
            message = "Required file identity query failed."
            raise _WindowsCapabilityError(message)


@dataclass
class _OwnedHandle:
    api: _WindowsApi
    value: int
    native: bool
    _closed: bool = False

    def close(self) -> None:
        if not self._closed:
            self.api.close(self.value, native=self.native)
            self._closed = True


@dataclass
class _OpenedEvidenceLeaf:
    leaf: _OwnedHandle
    parent_handle: int
    owned_parent: _OwnedHandle | None
    identity: _FileIdentity
    final_path: str

    def close(self) -> None:
        """Close every handle retained for one evidence read."""
        self.leaf.close()
        if self.owned_parent is not None:
            self.owned_parent.close()


@dataclass
class _WindowsSourceBinding:
    state: Literal["open", "missing", "unreadable"]
    sentinel: _OwnedHandle | None = None
    identity: _FileIdentity | None = None
    final_path: str | None = None

    def close(self) -> None:
        """Close the logical-source sentinel when one was retained."""
        if self.sentinel is not None:
            self.sentinel.close()


@dataclass
class _WindowsEvidenceWorktree:
    reader: WindowsRepositoryEvidenceReader
    worktree: Path
    root: _OwnedHandle
    root_identity: _FileIdentity
    root_final_path: str

    def __enter__(self) -> _WindowsEvidenceWorktree:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_value, traceback
        try:
            if exc_type is None:
                self.reader._verify_root_unchanged(
                    self.worktree,
                    self.root_identity,
                    self.root_final_path,
                )
        finally:
            self.root.close()

    def read(
        self,
        resolved_path: str,
        source_path: str,
        warnings: list[VisionEvidenceWarning],
        byte_limit: int,
        binding: RepositoryEvidenceBinding,
    ) -> bytes | None:
        if not isinstance(binding, _WindowsSourceBinding):
            message = "Windows evidence read received an incompatible binding."
            raise RepositoryEvidenceCapabilityError(message)
        return self.reader._read_relative(
            root_handle=self.root.value,
            root_final_path=self.root_final_path,
            resolved_path=resolved_path,
            source_path=source_path,
            warnings=warnings,
            byte_limit=byte_limit,
            binding=binding,
        )

    def bind(
        self,
        source_path: str,
        warnings: list[VisionEvidenceWarning],
    ) -> RepositoryEvidenceBinding:
        """Bind a logical source before its compatible target is resolved."""
        return self.reader._bind_source(
            root_final_path=self.root_final_path,
            source_path=source_path,
            warnings=warnings,
        )


class WindowsRepositoryEvidenceReader:
    """Read evidence through retained Windows directory handles."""

    def __init__(self, api: _WindowsApi | None = None) -> None:
        """Accept an injected native API table only for bounded tests."""
        self._api_override = api
        self._loaded_api: _WindowsApi | None = None

    def capability(self, worktree: Path) -> RepositoryEvidenceCapability:
        """Probe native APIs and filesystem identity through a root handle."""
        try:
            api = self._api()
            root = _OwnedHandle(api=api, value=api.open_root(worktree), native=False)
            try:
                identity, final_path = self._validate_root(api, root.value)
                self._probe_relative_root(
                    api,
                    root.value,
                    identity,
                    final_path,
                )
            finally:
                root.close()
        except (_WindowsCapabilityError, _WindowsNativeError, OSError):
            return RepositoryEvidenceCapability(
                available=False,
                code="REPOSITORY_EVIDENCE_CAPABILITY_UNAVAILABLE",
                message=(
                    "Repository evidence collection is unavailable on this "
                    "Windows platform or filesystem."
                ),
            )
        return RepositoryEvidenceCapability(available=True)

    def open(self, worktree: Path) -> _WindowsEvidenceWorktree:
        """Open and retain one validated Windows worktree root."""
        api = self._api()
        try:
            root = _OwnedHandle(api=api, value=api.open_root(worktree), native=False)
        except _WindowsNativeError as exc:
            raise RepositoryEvidenceChangedError(_WORKTREE_CHANGED_BEFORE_READ) from exc
        try:
            identity, final_path = self._validate_root(api, root.value)
            self._probe_relative_root(api, root.value, identity, final_path)
        except (_WindowsCapabilityError, _WindowsNativeError) as exc:
            root.close()
            raise RepositoryEvidenceCapabilityError(str(exc)) from exc
        return _WindowsEvidenceWorktree(
            reader=self,
            worktree=worktree,
            root=root,
            root_identity=identity,
            root_final_path=final_path,
        )

    def _api(self) -> _WindowsApi:
        if self._api_override is not None:
            return self._api_override
        if self._loaded_api is None:
            self._loaded_api = _WindowsApi.load()
        return self._loaded_api

    def _validate_root(
        self, api: _WindowsApi, handle: int
    ) -> tuple[_FileIdentity, str]:
        attributes = api.attributes(handle)
        if not attributes.FileAttributes & _FILE_ATTRIBUTE_DIRECTORY:
            message = "Repository worktree is not a directory."
            raise _WindowsCapabilityError(message)
        if attributes.FileAttributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            message = "Resolved worktree root is a reparse point."
            raise _WindowsCapabilityError(message)
        filesystem_name = api.filesystem_name(handle)
        if filesystem_name not in _SUPPORTED_FILESYSTEMS:
            message = "Repository filesystem is unsupported."
            raise _WindowsCapabilityError(message)
        identity = api.identity(handle)
        final_path = api.final_path(handle)
        if api.is_remote(handle) or _is_unc_path(final_path):
            message = "Remote repository worktrees are unsupported."
            raise _WindowsCapabilityError(message)
        return identity, final_path

    def _probe_relative_root(
        self,
        api: _WindowsApi,
        root_handle: int,
        identity: _FileIdentity,
        final_path: str,
    ) -> None:
        relative_root = _OwnedHandle(
            api=api,
            value=api.open_relative(root_handle, ".", directory=True),
            native=True,
        )
        try:
            self._validate_component(api, relative_root.value, directory=True)
            relative_identity = api.identity(relative_root.value)
            relative_final_path = api.final_path(relative_root.value)
        finally:
            relative_root.close()
        if not _same_file_object(identity, relative_identity) or (
            _normalized_windows_path(final_path)
            != _normalized_windows_path(relative_final_path)
        ):
            message = "Directory-relative root probe changed identity."
            raise _WindowsCapabilityError(message)

    def _open_relative(
        self,
        parent_handle: int,
        component: str,
        *,
        directory: bool,
    ) -> int:
        return self._api().open_relative(
            parent_handle,
            component,
            directory=directory,
        )

    def _read_handle(self, handle: int, byte_limit: int) -> bytes:
        return self._api().read(handle, byte_limit)

    def _bind_source(
        self,
        *,
        root_final_path: str,
        source_path: str,
        warnings: list[VisionEvidenceWarning],
    ) -> _WindowsSourceBinding:
        relative = PurePosixPath(source_path)
        parts = relative.parts
        if (
            relative.is_absolute()
            or not parts
            or any(part in {"", ".", ".."} for part in parts)
        ):
            warnings.append(
                _warning(
                    code="EVIDENCE_UNREADABLE",
                    source=source_path,
                    message="Approved evidence path is not repository-relative.",
                )
            )
            return _WindowsSourceBinding(state="unreadable")
        api = self._api()
        sentinel: _OwnedHandle | None = None
        try:
            sentinel = _OwnedHandle(
                api=api,
                value=api.open_source_sentinel(ntpath.join(root_final_path, *parts)),
                native=False,
            )
            self._validate_component(api, sentinel.value, directory=False)
            identity = api.identity(sentinel.value)
            final_path = api.final_path(sentinel.value)
        except _WindowsNativeError as exc:
            if sentinel is not None:
                sentinel.close()
            if exc.error_code in {_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND}:
                return _WindowsSourceBinding(state="missing")
            warnings.append(
                _warning(
                    code="EVIDENCE_UNREADABLE",
                    source=source_path,
                    message="Approved evidence file could not be bound.",
                )
            )
            return _WindowsSourceBinding(state="unreadable")
        except _WindowsCapabilityError as exc:
            if sentinel is not None:
                sentinel.close()
            raise RepositoryEvidenceCapabilityError(str(exc)) from exc
        return _WindowsSourceBinding(
            state="open",
            sentinel=sentinel,
            identity=identity,
            final_path=final_path,
        )

    def _read_relative(  # noqa: PLR0913
        self,
        *,
        root_handle: int,
        root_final_path: str,
        resolved_path: str,
        source_path: str,
        warnings: list[VisionEvidenceWarning],
        byte_limit: int,
        binding: _WindowsSourceBinding,
    ) -> bytes | None:
        relative = PurePosixPath(resolved_path)
        parts = relative.parts
        if (
            relative.is_absolute()
            or not parts
            or any(part in {"", ".", ".."} for part in parts)
        ):
            warnings.append(
                _warning(
                    code="EVIDENCE_UNREADABLE",
                    source=source_path,
                    message="Approved evidence path is not repository-relative.",
                )
            )
            return None
        if binding.state == "unreadable":
            return None
        api = self._api()
        try:
            opened = self._open_approved_leaf(
                api=api,
                root_handle=root_handle,
                root_final_path=root_final_path,
                parts=parts,
                binding=binding,
            )
        except _WindowsNativeError as exc:
            if exc.error_code in {_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND}:
                raise RepositoryEvidenceChangedError(_CHANGED_DURING_READ) from exc
            warnings.append(
                _warning(
                    code="EVIDENCE_UNREADABLE",
                    source=source_path,
                    message="Approved evidence file could not be opened.",
                )
            )
            return None
        except _WindowsCapabilityError as exc:
            raise RepositoryEvidenceCapabilityError(str(exc)) from exc
        try:
            try:
                content = self._read_handle(opened.leaf.value, byte_limit)
            except _WindowsNativeError:
                warnings.append(
                    _warning(
                        code="EVIDENCE_UNREADABLE",
                        source=source_path,
                        message="Approved evidence file could not be read.",
                    )
                )
                return None
            try:
                self._verify_leaf_after_read(
                    api=api,
                    opened=opened,
                    root_final_path=root_final_path,
                    parts=parts,
                )
            except _WindowsNativeError as exc:
                raise RepositoryEvidenceChangedError(_CHANGED_DURING_READ) from exc
            except _WindowsCapabilityError as exc:
                raise RepositoryEvidenceCapabilityError(str(exc)) from exc
            return content
        finally:
            opened.close()

    def _open_approved_leaf(
        self,
        *,
        api: _WindowsApi,
        root_handle: int,
        root_final_path: str,
        parts: tuple[str, ...],
        binding: _WindowsSourceBinding,
    ) -> _OpenedEvidenceLeaf:
        if (
            binding.state == "missing"
            or binding.identity is None
            or binding.final_path is None
        ):
            raise RepositoryEvidenceChangedError(_CHANGED_DURING_READ)
        owned_parent: _OwnedHandle | None = None
        leaf: _OwnedHandle | None = None
        try:
            parent_handle = root_handle
            for component in parts[:-1]:
                next_parent = _OwnedHandle(
                    api=api,
                    value=self._open_relative(
                        parent_handle,
                        component,
                        directory=True,
                    ),
                    native=True,
                )
                if owned_parent is not None:
                    owned_parent.close()
                owned_parent = next_parent
                parent_handle = next_parent.value
                self._validate_component(api, parent_handle, directory=True)
            leaf = _OwnedHandle(
                api=api,
                value=self._open_relative(
                    parent_handle,
                    parts[-1],
                    directory=False,
                ),
                native=True,
            )
            self._validate_component(api, leaf.value, directory=False)
            identity = api.identity(leaf.value)
            final_path = api.final_path(leaf.value)
            self._require_expected_final_path(root_final_path, parts, final_path)
            _require_same_leaf(
                binding.identity,
                identity,
                binding.final_path,
                final_path,
            )
            return _OpenedEvidenceLeaf(
                leaf=leaf,
                parent_handle=parent_handle,
                owned_parent=owned_parent,
                identity=identity,
                final_path=final_path,
            )
        except BaseException:
            if leaf is not None:
                leaf.close()
            if owned_parent is not None:
                owned_parent.close()
            raise

    def _verify_leaf_after_read(
        self,
        *,
        api: _WindowsApi,
        opened: _OpenedEvidenceLeaf,
        root_final_path: str,
        parts: tuple[str, ...],
    ) -> None:
        after = api.identity(opened.leaf.value)
        current = _OwnedHandle(
            api=api,
            value=self._open_relative(
                opened.parent_handle,
                parts[-1],
                directory=False,
            ),
            native=True,
        )
        try:
            self._validate_component(api, current.value, directory=False)
            current_identity = api.identity(current.value)
            current_final_path = api.final_path(current.value)
            self._require_expected_final_path(
                root_final_path,
                parts,
                current_final_path,
            )
        finally:
            current.close()
        _require_same_leaf(
            opened.identity,
            after,
            opened.final_path,
            opened.final_path,
        )
        _require_same_leaf(
            opened.identity,
            current_identity,
            opened.final_path,
            current_final_path,
        )

    @staticmethod
    def _validate_component(api: _WindowsApi, handle: int, *, directory: bool) -> None:
        attributes = api.attributes(handle)
        if attributes.FileAttributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise _WindowsNativeError(_ERROR_PATH_NOT_FOUND)
        is_directory = bool(attributes.FileAttributes & _FILE_ATTRIBUTE_DIRECTORY)
        if is_directory != directory:
            raise _WindowsNativeError(_ERROR_PATH_NOT_FOUND)

    @staticmethod
    def _require_expected_final_path(
        root_path: str,
        parts: tuple[str, ...],
        leaf_path: str,
    ) -> None:
        expected = _normalized_windows_path(ntpath.join(root_path, *parts))
        if _normalized_windows_path(leaf_path) != expected:
            raise _WindowsNativeError(_ERROR_PATH_NOT_FOUND)

    def _verify_root_unchanged(
        self,
        worktree: Path,
        expected_identity: _FileIdentity,
        expected_final_path: str,
    ) -> None:
        api = self._api()
        try:
            current = _OwnedHandle(
                api=api,
                value=api.open_root(worktree),
                native=False,
            )
            try:
                identity, final_path = self._validate_root(api, current.value)
            finally:
                current.close()
        except (_WindowsCapabilityError, _WindowsNativeError) as exc:
            raise RepositoryEvidenceChangedError(
                _WORKTREE_CHANGED_DURING_COLLECTION
            ) from exc
        if not _same_file_object(
            identity, expected_identity
        ) or _normalized_windows_path(final_path) != _normalized_windows_path(
            expected_final_path
        ):
            raise RepositoryEvidenceChangedError(_WORKTREE_CHANGED_DURING_COLLECTION)


def _function(library: object, name: str) -> _NativeFunction:
    value = getattr(library, name, None)
    if value is None or not callable(value):
        message = f"Required Windows API {name} is unavailable."
        raise _WindowsCapabilityError(message)
    return cast("_NativeFunction", value)


def _handle_value(handle: object) -> int | None:
    value = getattr(handle, "value", handle)
    return value if isinstance(value, int) else None


def _normalized_windows_path(value: str) -> str:
    path = value
    if path.startswith("\\\\?\\UNC\\"):
        path = "\\\\" + path[8:]
    elif path.startswith("\\\\?\\"):
        path = path[4:]
    return ntpath.normcase(ntpath.normpath(path))


def _is_unc_path(value: str) -> bool:
    return _normalized_windows_path(value).startswith("\\\\")


def _same_file_object(left: _FileIdentity, right: _FileIdentity) -> bool:
    return (
        left.volume_serial == right.volume_serial
        and left.file_id == right.file_id
        and left.creation_time == right.creation_time
        and left.attributes == right.attributes
    )


def _require_same_leaf(
    expected_identity: _FileIdentity,
    observed_identity: _FileIdentity,
    expected_path: str,
    observed_path: str,
) -> None:
    if expected_identity != observed_identity or _normalized_windows_path(
        expected_path
    ) != _normalized_windows_path(observed_path):
        raise RepositoryEvidenceChangedError(_CHANGED_DURING_READ)


def _warning(*, code: str, source: str, message: str) -> VisionEvidenceWarning:
    return VisionEvidenceWarning(code=code, source=source, message=message)


__all__ = ["WindowsRepositoryEvidenceReader"]
