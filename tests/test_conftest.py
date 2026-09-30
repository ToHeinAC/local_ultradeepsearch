import socket

import pytest


def test_network_is_blocked() -> None:
    with pytest.raises(RuntimeError, match="offline"):
        socket.create_connection(("127.0.0.1", 9), timeout=1)
