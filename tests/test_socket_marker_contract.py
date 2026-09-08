"""Collection contract for tests that fully enable sockets."""

import shutil
from pathlib import Path

import pytest

pytest_plugins = ("pytester",)

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_PROJECT_CONFTEST = _PROJECT_ROOT / "tests" / "conftest.py"


def test_lone_enable_socket_marker_fails_during_collection(
    pytester: pytest.Pytester,
) -> None:
    """Reject enable_socket before its test body can execute."""
    shutil.copyfile(_PROJECT_CONFTEST, pytester.path / "conftest.py")
    pytester.makepyfile(
        test_lone_enable_socket="""
        import pytest

        @pytest.mark.enable_socket
        def test_lone_enable_socket():
            raise AssertionError("test body executed")
        """,
    )

    result = pytester.runpytest_subprocess(
        "--disable-socket",
        "--allow-unix-socket",
        "-q",
    )

    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(
        ["*ERROR: enable_socket requires integration: test_lone_enable_socket.py::*"],
    )
    assert "test body executed" not in result.stdout.str()


def test_enable_socket_with_integration_marker_is_valid(
    pytester: pytest.Pytester,
) -> None:
    """Accept full socket access when the test is explicitly an integration test."""
    shutil.copyfile(_PROJECT_CONFTEST, pytester.path / "conftest.py")
    pytester.makeini(
        """
        [pytest]
        markers =
            integration: marks tests that call external services
        """,
    )
    pytester.makepyfile(
        test_integration_enable_socket="""
        import pytest

        @pytest.mark.integration
        @pytest.mark.enable_socket
        def test_integration_enable_socket():
            pass
        """,
    )

    result = pytester.runpytest_subprocess(
        "--disable-socket",
        "--allow-unix-socket",
        "-q",
    )

    result.assert_outcomes(passed=1)
