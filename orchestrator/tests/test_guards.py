"""The guards in conftest.py must actually block network and docker."""

import socket
import subprocess

import pytest

from conftest import REAL_DOCKER


def test_network_is_blocked():
    with pytest.raises(AssertionError, match="network"):
        socket.create_connection(("192.0.2.1", 443), timeout=1)


@pytest.mark.skipif(REAL_DOCKER is None, reason="docker not installed here")
def test_docker_is_blocked():
    with pytest.raises(AssertionError, match="docker"):
        subprocess.run(["docker", "version"], capture_output=True)
