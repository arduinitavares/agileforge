"""Tests for the default offline pytest policy."""

from __future__ import annotations

import socket

import pytest
from pytest_socket import SocketBlockedError


def test_default_suite_blocks_external_network() -> None:
    """Require external TCP connections to be blocked by default."""
    with pytest.raises(SocketBlockedError):
        socket.create_connection(("example.com", 443), timeout=0.01)
