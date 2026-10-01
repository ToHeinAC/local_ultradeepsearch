import socket
from pathlib import Path

import pytest


def test_network_is_blocked() -> None:
    with pytest.raises(RuntimeError, match="offline"):
        socket.create_connection(("127.0.0.1", 9), timeout=1)


LIVE_AND_OFFLINE = """
import socket

import pytest


@pytest.mark.live
def test_live_may_connect():
    socket.create_connection(("127.0.0.1", {port}), timeout=1).close()


def test_unmarked_is_blocked():
    with pytest.raises(RuntimeError, match="offline"):
        socket.create_connection(("127.0.0.1", {port}), timeout=1)
"""


def test_live_marker_lifts_the_block(pytester: pytest.Pytester) -> None:
    """Runs in a subprocess so this suite's own socket patch cannot leak into it."""
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(2)
        port = server.getsockname()[1]
        pytester.makeini("[pytest]\nmarkers =\n    live: needs real services\n")
        pytester.makeconftest((Path(__file__).parent / "conftest.py").read_text())
        pytester.makepyfile(LIVE_AND_OFFLINE.format(port=port))
        result = pytester.runpytest_subprocess("-m", "live or not live")
    result.assert_outcomes(passed=2)
