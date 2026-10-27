"""Test guards: no network, no docker.

Any attempt to open a non-unix socket, or to start the real docker binary,
fails the test that made it.
"""

import os
import shutil
import socket
import subprocess

import pytest

from reviewbot import dockerrun

REAL_DOCKER = shutil.which("docker")


@pytest.fixture(autouse=True)
def no_network_no_docker(monkeypatch):
    real_connect = socket.socket.connect

    def guarded_connect(self, address):
        if self.family == socket.AF_UNIX:
            return real_connect(self, address)
        raise AssertionError(f"test tried to open a network connection to {address!r}")

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)

    real_init = subprocess.Popen.__init__

    def guarded_init(self, args, *a, **kw):
        argv0 = args[0] if isinstance(args, (list, tuple)) else str(args).split()[0]
        env = kw.get("env") or os.environ
        resolved = shutil.which(argv0, path=env.get("PATH"))
        if REAL_DOCKER and resolved and os.path.realpath(resolved) == os.path.realpath(REAL_DOCKER):
            raise AssertionError("test tried to run the real docker binary")
        return real_init(self, args, *a, **kw)

    monkeypatch.setattr(subprocess.Popen, "__init__", guarded_init)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    # the stop request is process-wide; no test may see another test's
    stop = getattr(dockerrun, "STOP", None)
    if stop is not None:
        stop.clear()
    yield
    if stop is not None:
        stop.clear()


FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def fixture_text(name: str) -> str:
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture(scope="session")
def app_key_path(tmp_path_factory):
    """A PEM file holding a fresh RSA private key, for REVIEWBOT_APP_PRIVATE_KEY_PATH.
    AppAuth signs its JWTs with it offline."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = tmp_path_factory.mktemp("app-key") / "app.pem"
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                       serialization.PrivateFormat.TraditionalOpenSSL,
                                       serialization.NoEncryption()))
    return path
