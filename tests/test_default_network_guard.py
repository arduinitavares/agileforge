"""Tests for the default offline pytest policy."""

from __future__ import annotations

import socket

import pytest
from pytest_socket import SocketBlockedError


def test_default_suite_blocks_external_network() -> None:
    """Require external TCP connections to be blocked by default."""
    with pytest.raises(SocketBlockedError):
        socket.create_connection(("example.com", 443), timeout=0.01)


@pytest.mark.allow_hosts(["127.0.0.1"])
def test_local_allow_hosts_does_not_require_integration() -> None:
    """Allow explicitly marked loopback TCP without enabling external sockets."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        host, port = listener.getsockname()
        with socket.create_connection((str(host), int(port)), timeout=1):
            connection, _address = listener.accept()
            connection.close()
