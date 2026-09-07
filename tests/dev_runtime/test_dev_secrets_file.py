"""Credential-file contracts using only disposable sentinel files."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from cli import dev_main, dev_secrets

if TYPE_CHECKING:
    from pathlib import Path
    from typing import TextIO


@pytest.fixture(autouse=True)
def no_invoking_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never read a real invoking credential in these tests."""
    monkeypatch.delenv("OPEN_ROUTER_API_KEY", raising=False)


def test_regular_file_loads_on_the_actual_platform(tmp_path: Path) -> None:
    """Missing POSIX flags must not disable native Windows regular-file reads."""
    selected = tmp_path / "provider.env"
    selected.write_text("OPEN_ROUTER_API_KEY=dummy-native-sentinel\n", encoding="utf-8")
    assert dev_main._provider_environment(selected) == {
        "OPEN_ROUTER_API_KEY": "dummy-native-sentinel"
    }


@pytest.mark.parametrize("parent", [None, "", "dummy-parent-sentinel"])
def test_allowlist_interpolation_and_environment_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, parent: str | None
) -> None:
    """Never expand dotenv variables or import non-provider runtime controls."""
    selected = tmp_path / "provider.env"
    selected.write_text(
        "BASE=dummy-expansion-sentinel\n"
        "OPEN_ROUTER_API_KEY=${BASE}\n"
        "MODEL_CONFIG_PATH=forbidden-model\n"
        "AGILEFORGE_DB_URL=forbidden-database\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("BASE", "dummy-parent-expansion")
    if parent is not None:
        monkeypatch.setenv("OPEN_ROUTER_API_KEY", parent)
    assert dev_main._provider_environment(selected) == {
        "OPEN_ROUTER_API_KEY": "${BASE}" if parent is None else parent
    }


def test_invalid_utf8_has_a_content_free_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Decode failures cannot include a credential-bearing source fragment."""
    selected = tmp_path / "provider.env"
    sentinel = b"dummy-invalid-input-sentinel"
    selected.write_bytes(b"OPEN_ROUTER_API_KEY=" + sentinel + b"\xff")
    with pytest.raises(dev_main.DeveloperCommandError, match="UTF-8") as caught:
        dev_main._provider_environment(selected)
    assert sentinel.decode() not in str(caught.value)
    assert sentinel.decode() not in caplog.text
    assert caught.value.__suppress_context__
    selected.unlink()  # Also proves no Windows handle remains open.


def test_malformed_dotenv_diagnostics_do_not_expose_content(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Preserve tolerant malformed-line parsing without source-value diagnostics."""
    selected = tmp_path / "provider.env"
    selected.write_text(
        'OPEN_ROUTER_API_KEY="dummy-malformed-sentinel\n', encoding="utf-8"
    )
    assert dev_main._provider_environment(selected) == {}
    assert "dummy-malformed-sentinel" not in caplog.text
    selected.unlink()


@pytest.mark.parametrize("flag", [None, 0])
def test_missing_posix_no_follow_capability_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: int | None
) -> None:
    """Unsupported POSIX opening cannot become an unchecked fallback."""
    monkeypatch.setattr(os, "O_NOFOLLOW", flag, raising=False)
    with pytest.raises(dev_secrets.SecretsFileError, match="unavailable"):
        dev_secrets._open_posix_descriptor(tmp_path / "provider.env")


@pytest.mark.parametrize("failure", [False, True])
def test_parser_retains_noninheritable_descriptor_and_closes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, failure: bool
) -> None:
    """The parser owns exactly the validated descriptor until success or failure."""
    selected = tmp_path / "provider.env"
    selected.write_text("OPEN_ROUTER_API_KEY=dummy-owned-sentinel\n", encoding="utf-8")
    descriptors: list[int] = []

    def parse(*, stream: TextIO, verbose: bool, interpolate: bool) -> dict[str, str]:
        assert not verbose
        assert not interpolate
        descriptor = stream.fileno()
        descriptors.append(descriptor)
        assert not os.get_inheritable(descriptor)
        if failure:
            message = "dummy-owned-sentinel"
            raise OSError(message)
        assert "dummy-owned-sentinel" in stream.read()
        return {"OPEN_ROUTER_API_KEY": "dummy-owned-sentinel"}

    monkeypatch.setattr(dev_main, "dotenv_values", parse)
    if failure:
        with pytest.raises(dev_main.DeveloperCommandError) as caught:
            dev_main._provider_environment(selected)
        assert "dummy-owned-sentinel" not in str(caught.value)
    else:
        assert dev_main._provider_environment(selected) == {
            "OPEN_ROUTER_API_KEY": "dummy-owned-sentinel"
        }
    assert len(descriptors) == 1
    with pytest.raises(OSError, match=r"\[(?:Errno 9|WinError 6)\]"):
        os.fstat(descriptors[0])
    selected.unlink()
