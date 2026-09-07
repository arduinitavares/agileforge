"""Real Windows handles and explicit fault injection for launcher secrets."""

from __future__ import annotations

import ctypes
import os
import stat
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from cli import dev_main, dev_secrets
from tests.windows.test_vision_evidence_windows import _junction

if sys.platform == "win32":
    import msvcrt

if TYPE_CHECKING:
    from typing import TextIO

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="requires native Windows file handles"
)
_ERROR_INVALID_HANDLE = 6


@pytest.fixture
def selected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a real file without consulting real credentials."""
    monkeypatch.delenv("OPEN_ROUTER_API_KEY", raising=False)
    path = tmp_path / "provider.env"
    path.write_text("OPEN_ROUTER_API_KEY=dummy-windows-sentinel\n", encoding="utf-8")
    return path


def test_windows_secrets_regular_file_uses_native_handle(selected: Path) -> None:
    """Windows must load a regular file even though O_NOFOLLOW is unavailable."""
    assert sys.platform == "win32"
    assert not hasattr(os, "O_NOFOLLOW")
    assert dev_main._provider_environment(selected) == {
        "OPEN_ROUTER_API_KEY": "dummy-windows-sentinel"
    }
    selected.unlink()


def test_windows_secrets_rejects_leaf_junction_but_trusts_parents(
    selected: Path,
) -> None:
    """Leaf reparses are unsafe; parent junction traversal is the stated contract."""
    link = selected.parent / "parent-junction"
    _junction(link, selected.parent)
    try:
        with pytest.raises(dev_main.DeveloperCommandError, match="regular file"):
            dev_main._provider_environment(link)
        assert dev_main._provider_environment(link / selected.name) == {
            "OPEN_ROUTER_API_KEY": "dummy-windows-sentinel"
        }
    finally:
        link.rmdir()


@pytest.mark.parametrize(
    "spelling",
    [
        "NUL",
        "CON",
        r"\\.\NUL",
        r"\\?\C:\NUL",
        r"\\server\pipe\name",
        r"\\server\PiPe\name",
        r"\\server\mailslot\name",
        r"\\server\IPC$\name",
        r"\\server\iPc$\name",
    ],
)
def test_windows_secrets_rejects_device_names_before_open(
    spelling: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A special namespace must not reach native device opening."""

    def unexpected_api() -> dev_secrets._WindowsApi:
        message = "device path reached Windows file API"
        raise AssertionError(message)

    monkeypatch.setattr(dev_secrets._WindowsApi, "load", unexpected_api)
    with pytest.raises(dev_main.DeveloperCommandError, match="regular file"):
        dev_main._provider_environment(Path(spelling))


def test_windows_secrets_rejects_alternate_data_stream(selected: Path) -> None:
    """An existing file's named stream is not an accepted credential source."""
    stream_path = Path(f"{selected}:credentials")
    stream_path.write_text("OPEN_ROUTER_API_KEY=dummy-ads-sentinel", encoding="utf-8")
    with pytest.raises(dev_main.DeveloperCommandError, match="regular file"):
        dev_main._provider_environment(stream_path)


@pytest.mark.parametrize("stage", ["validate", "read"])
def test_windows_retained_file_denies_replacement_and_writes(
    selected: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    """Neither validation nor parsing can be redirected to a replacement object."""
    alternate = selected.with_name("replacement.env")
    alternate.write_text("OPEN_ROUTER_API_KEY=dummy-replacement", encoding="utf-8")
    observed: list[str] = []

    def attack() -> None:
        observed.append(stage)
        with pytest.raises(PermissionError):
            alternate.replace(selected)
        with pytest.raises(PermissionError):
            selected.write_text("dummy-write-attack", encoding="utf-8")

    real_validate = dev_secrets._validate_descriptor

    def validate(descriptor: int) -> None:
        attack()
        real_validate(descriptor)

    real_parse = dev_main.dotenv_values

    def parse(
        *, stream: TextIO, verbose: bool, interpolate: bool
    ) -> dict[str, str | None]:
        attack()
        return real_parse(stream=stream, verbose=verbose, interpolate=interpolate)

    if stage == "validate":
        monkeypatch.setattr(dev_secrets, "_validate_descriptor", validate)
    else:
        monkeypatch.setattr(dev_main, "dotenv_values", parse)
    assert dev_main._provider_environment(selected) == {
        "OPEN_ROUTER_API_KEY": "dummy-windows-sentinel"
    }
    assert observed == [stage]
    alternate.replace(selected)  # Replacement is possible again after close.


def test_windows_replacement_before_open_validates_the_replacement(
    selected: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-open swap to a junction cannot exploit a pathname precheck."""
    api = dev_secrets._WindowsApi.load()

    def swap_and_open(*args: object) -> int | None:
        selected.unlink()
        _junction(selected, selected.parent)
        return api.create_file(*args)

    instrumented = replace(
        api, create_file=cast("dev_secrets._NativeFunction", swap_and_open)
    )
    monkeypatch.setattr(dev_secrets._WindowsApi, "load", lambda: instrumented)
    try:
        with pytest.raises(dev_main.DeveloperCommandError, match="regular file"):
            dev_main._provider_environment(selected)
    finally:
        selected.rmdir()


@pytest.mark.parametrize("stage", ["type", "transfer", "metadata", "stream", "success"])
def test_windows_handle_ownership_on_all_exit_paths(
    selected: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    """Close native handles before transfer and descriptors/streams after transfer."""
    api = dev_secrets._WindowsApi.load()
    opened: list[int] = []
    native_closes: list[int] = []

    def track_open(*args: object) -> int | None:
        handle = api.create_file(*args)
        assert isinstance(handle, int)
        opened.append(handle)
        return handle

    def track_close(handle: int) -> int | None:
        native_closes.append(handle)
        return api.close_handle(handle)

    def fail(*_args: object, **_kwargs: object) -> int:
        message = "dummy-native-error-sentinel"
        raise OSError(message)

    instrumented = replace(
        api,
        create_file=cast("dev_secrets._NativeFunction", track_open),
        close_handle=cast("dev_secrets._NativeFunction", track_close),
        get_file_type=(
            cast("dev_secrets._NativeFunction", lambda _handle: 0)
            if stage == "type"
            else api.get_file_type
        ),
    )
    with monkeypatch.context() as patch:
        patch.setattr(dev_secrets._WindowsApi, "load", lambda: instrumented)
        if stage == "transfer":
            patch.setattr(msvcrt, "open_osfhandle", fail)
        elif stage == "metadata":
            patch.setattr(os, "fstat", fail)
        elif stage == "stream":
            patch.setattr(os, "fdopen", fail)
        if stage == "success":
            assert dev_main._provider_environment(selected) == {
                "OPEN_ROUTER_API_KEY": "dummy-windows-sentinel"
            }
        else:
            with pytest.raises(dev_main.DeveloperCommandError) as caught:
                dev_main._provider_environment(selected)
            assert "dummy-native-error-sentinel" not in str(caught.value)
    assert len(opened) == 1
    assert native_closes == (opened if stage in {"type", "transfer"} else [])
    assert api.get_file_type(opened[0]) == 0
    get_last_error = getattr(ctypes, "get_last_error", None)
    assert callable(get_last_error)
    assert get_last_error() == _ERROR_INVALID_HANDLE
    selected.unlink()


def test_windows_reparse_attribute_rejected_even_for_regular_mode(
    selected: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Injected non-directory reparse metadata must fail before parsing bytes."""
    monkeypatch.setattr(
        os,
        "fstat",
        lambda _fd: SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0x400),
    )
    with pytest.raises(dev_main.DeveloperCommandError, match="reparse"):
        dev_main._provider_environment(selected)
    selected.unlink()


def test_windows_missing_native_api_fails_closed(
    selected: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unavailable safety capability must not misdiagnose a regular file."""
    monkeypatch.delattr(ctypes, "WinDLL")
    with pytest.raises(dev_main.DeveloperCommandError, match="unavailable"):
        dev_main._provider_environment(selected)
