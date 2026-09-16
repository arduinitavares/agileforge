"""Run one checkout command while holding the shared Linux workspace fence."""

from __future__ import annotations

import argparse
import signal
import subprocess  # nosec B404
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cli.dev_server import PosixProcessGroup, UIChild, stop_ui
from utils.runtime_fence import runtime_fence

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence


def _interrupt(_number: int, _frame: object) -> None:
    raise KeyboardInterrupt


@contextmanager
def _termination_interrupt() -> Iterator[None]:
    previous = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, _interrupt)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def main(
    arguments: Sequence[str] | None = None,
    *,
    workspace_root: Path = Path("/workspace"),
) -> int:
    """Hold the checkout fence for the complete child command lifetime."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    parsed = parser.parse_args(arguments)
    command = parsed.command
    while command[:1] == ["--"]:
        command = command[1:]
    if not command:
        message = "a command is required"
        raise ValueError(message)
    with runtime_fence(workspace_root), _termination_interrupt():
        process = subprocess.Popen(  # noqa: S603 # nosec B603
            command, start_new_session=True
        )
        child = UIChild(process=PosixProcessGroup(process), port=0)
        try:
            return process.wait()
        except KeyboardInterrupt:
            return 130
        finally:
            stop_ui(child)


if __name__ == "__main__":
    raise SystemExit(main())
