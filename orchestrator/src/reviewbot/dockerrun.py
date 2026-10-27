"""Run docker containers: the fetcher and reviewer with a wall-clock
timeout, and the proxy that holds the real Claude token for one review.

Secrets are never put on the command line. Each variable a container needs
is passed as "-e NAME" (no value), and the value is placed in the
environment of the docker client process, which forwards it. That child
environment is built from an explicit allowlist plus the variables of the
one container being started, so variables the orchestrator itself holds
cannot reach a container by accident. CLAUDE_CODE_OAUTH_TOKEN, the real
token, is added only for the docker run that starts the proxy.

Stopping: the orchestrator's SIGTERM and SIGINT handler sets STOP, a
StopFlag. run() checks it every POLL_SECONDS while a container runs; when
it is set, the container is killed by name (docker kill, then docker rm -f)
and run() raises Stopped. No container is started once STOP is set. Each cleanup
step has a CLEANUP_TIMEOUT. A stop during a review kills the reviewer (three
steps) and then reads the proxy's log and removes the proxy (two steps), so
the whole stop path takes at most about POLL_SECONDS + 5 * CLEANUP_TIMEOUT
(51 seconds), inside the 60 seconds reviewbot.service allows
(TimeoutStopSec=60).

The proxy: start_proxy() starts one proxy container for a review, attached
to the internal network under the alias "proxy" and to the default network
for its upstream traffic, and waits until it accepts connections;
stop_proxy() logs the requests the proxy answered with 403 (a request the
reviewer should not have made) and removes it. The readiness probe runs
inside the container with docker exec, because under rootless docker the
host cannot reach container addresses on any network.

Environment failures: run() raises EnvironmentFailure, instead of returning
a result, when the docker binary cannot be run, when docker run exits 125
(docker itself could not create or start the container: a missing image or
network, a bad flag, a daemon error), and when the container failed and the
docker daemon does not answer afterwards (docker 29.7.2 exits 1, not 125,
when it cannot reach the daemon). These failures are not caused by the pull
request under review, so the pipeline does not count them against it.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


class StopFlag:
    """A stop request that a signal handler can set safely.

    set() and clear() assign a bool and is_set() reads it. None of them
    takes a lock. threading.Event.set takes a non-reentrant lock that the
    main thread holds briefly inside Event.wait, so setting an Event from a
    signal handler that interrupts that window waits forever.
    """

    def __init__(self) -> None:
        self._set = False

    def set(self) -> None:
        self._set = True

    def clear(self) -> None:
        self._set = False

    def is_set(self) -> bool:
        return self._set


STOP = StopFlag()
POLL_SECONDS = 1.0
CLEANUP_TIMEOUT = 10.0  # also the limit for each short docker command
DOCKER_RUN_ERROR = 125  # docker run's own failure, before the container command ran


class EnvironmentFailure(Exception):
    """docker or the review environment is broken; not the pull request's fault.
    The pull request is not recorded and is reviewed again later."""


class PullRequestFailure(Exception):
    """The review of this pull request failed. It is recorded as "failed" and
    retried up to pipeline.MAX_ATTEMPTS times. The message is the detail kept
    in the state file."""


class Stopped(Exception):
    """A stop was requested; the container was killed or never started."""


class SetupError(Exception):
    """Docker is not set up for this prefix (an image is missing or comes
    from another build, or the internal network is not internal); restarting
    does not fix it."""


PROXY_ALIAS = "proxy"  # the host of ANTHROPIC_BASE_URL in reviewer/Dockerfile
PROXY_READY_TIMEOUT = 15.0  # seconds from docker run until the proxy must accept connections
PROXY_PROBE_INTERVAL = 0.5
PROXY_PROBE = ["python3", "-c",
               'import socket; socket.create_connection(("127.0.0.1", 8080), 1).close()']
CONTAINER_ID_HEX = 12  # hex digits after "<prefix>-<kind>-" in every container name
CONTAINER_KINDS = ("proxy", "review", "fetch")
PROXY_LOG_LINES = 200  # the proxy writes one line per request
# deploy/install.sh writes the build id to this file in the venv and builds
# every image with it as the value of the label BUILD_LABEL.
BUILD_ID_FILE = "reviewbot-build-id"
BUILD_LABEL = "reviewbot.build"


# Variables the docker CLI itself needs to find the (rootless) daemon.
DOCKER_CLIENT_ENV = ("PATH", "HOME", "USER", "LANG", "LC_ALL", "XDG_RUNTIME_DIR",
                     "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG",
                     "DOCKER_CERT_PATH", "DOCKER_TLS_VERIFY")


@dataclass
class RunResult:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool
    duration: float

    @property
    def ok(self) -> bool:
        return not self.timed_out and self.returncode == 0


def container_name(prefix: str, kind: str) -> str:
    """<prefix>-<kind>-<12 hex digits>, the form remove_stale_containers matches."""
    return f"{prefix}-{kind}-{secrets.token_hex(CONTAINER_ID_HEX // 2)}"


def client_env(container_env: dict[str, str]) -> dict[str, str]:
    env = {k: os.environ[k] for k in DOCKER_CLIENT_ENV if k in os.environ}
    env.update(container_env)
    return env


def env_flags(names) -> list[str]:
    out: list[str] = []
    for name in names:
        out += ["-e", name]
    return out


def run(argv: list[str], container_env: dict[str, str], timeout: float,
        name: str) -> RunResult:
    """Run `docker run ...` (argv must include --name <name>).

    Raises Stopped if STOP is set before or while the container runs, and
    EnvironmentFailure as described in the module docstring.
    """
    if "--name" not in argv or name not in argv:
        raise ValueError("argv must name the container so it can be killed on timeout")
    if STOP.is_set():
        raise Stopped(f"stop requested; not starting {name}")
    start = time.monotonic()
    try:
        proc = subprocess.Popen(argv, env=client_env(container_env), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        raise EnvironmentFailure(f"cannot run {argv[0]}: {exc.strerror}") from exc
    deadline = start + timeout
    timed_out = False
    while True:
        try:
            # Waiting in short steps lets a stop request or the deadline
            # interrupt the wait; communicate() keeps what it has read so far.
            out, err = proc.communicate(
                timeout=max(0.0, min(POLL_SECONDS, deadline - time.monotonic())))
            break
        except subprocess.TimeoutExpired:
            if STOP.is_set():
                log.warning("stop requested; killing container %s", name)
                _kill(proc, name)
                raise Stopped(f"container {name} killed on stop request") from None
            if time.monotonic() >= deadline:
                timed_out = True
                log.warning("container %s exceeded %ss, killing it", name, timeout)
                out, err = _kill(proc, name)
                break
    if STOP.is_set() and not timed_out:
        # The container ended while a stop was requested. systemd sends
        # SIGTERM to every process of the service, including this docker
        # client, which forwards it to the container; the result of such a
        # run is not a review result. Remove the container if it is still
        # there.
        _quiet_docker(["rm", "-f", name])
        raise Stopped(f"container {name} ended after a stop request (exit {proc.returncode})")
    result = RunResult(
        returncode=proc.returncode,
        stdout=out.decode("utf-8", errors="replace"),
        stderr=err.decode("utf-8", errors="replace"),
        timed_out=timed_out,
        duration=time.monotonic() - start,
    )
    if result.returncode == DOCKER_RUN_ERROR:
        raise EnvironmentFailure(f"docker run for {name} exited {DOCKER_RUN_ERROR} "
                                 f"(docker could not start the container): "
                                 f"{tail(result.stderr.strip(), 500)}")
    if not result.ok and not daemon_reachable():
        raise EnvironmentFailure(f"docker run for {name} exited {result.returncode} and the "
                                 f"docker daemon does not answer: {tail(result.stderr.strip(), 500)}")
    return result


def _quiet_docker(args: list[str]) -> bool:
    """Run a docker cleanup command with a timeout; log, never raise, on
    failure. True when the command succeeded."""
    try:
        r = docker(args)
    except EnvironmentFailure as exc:
        log.warning("docker %s: %s", " ".join(args), exc)
        return False
    if r.returncode != 0:
        log.warning("docker %s: %s", " ".join(args), r.stderr.strip())
        return False
    return True


def docker(args: list[str], env: dict[str, str] | None = None,
            timeout: float | None = None) -> subprocess.CompletedProcess:
    """Run a short docker command. Raises EnvironmentFailure when docker
    cannot be run or does not finish within timeout (default CLEANUP_TIMEOUT)."""
    timeout = CLEANUP_TIMEOUT if timeout is None else timeout
    try:
        return subprocess.run(["docker", *args], env=client_env({}) if env is None else env,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              errors="replace", timeout=timeout)
    except OSError as exc:
        raise EnvironmentFailure(f"cannot run docker: {exc.strerror}") from exc
    except subprocess.TimeoutExpired as exc:
        raise EnvironmentFailure(f"docker {' '.join(args[:2])} did not finish "
                                 f"within {timeout:.0f}s") from exc


def _kill(proc: subprocess.Popen, name: str) -> tuple[bytes, bytes]:
    """docker kill and docker rm -f the container, and wait for docker run.
    Returns docker run's stdout and stderr."""
    _quiet_docker(["kill", name])
    try:
        out, err = proc.communicate(timeout=CLEANUP_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
    _quiet_docker(["rm", "-f", name])
    return out, err


def daemon_reachable() -> bool:
    """True if `docker version` gets an answer from the daemon."""
    try:
        return docker(["version", "--format", "{{.Server.Version}}"]).returncode == 0
    except EnvironmentFailure:
        return False


def image_exists(image: str) -> tuple[bool, str]:
    """(exists, docker's error text)."""
    try:
        r = docker(["image", "inspect", "--format", "{{.Id}}", image])
    except EnvironmentFailure as exc:
        return False, str(exc)
    return r.returncode == 0, r.stderr.strip()


def tail(text: str, limit: int = 2000) -> str:
    return text if len(text) <= limit else "..." + text[-limit:]


def ensure_networks(names) -> None:
    """Create the internal and the default network of names when missing.
    Raises SetupError when the internal network exists but is not internal,
    or the default network exists but is internal."""
    for net, internal in ((names.internal_network, True), (names.default_network, False)):
        want = "true" if internal else "false"
        r = docker(["network", "inspect", "--format", "{{.Internal}}", net])
        if r.returncode != 0:
            log.info("creating docker network %s%s", net, " (internal)" if internal else "")
            c = docker(["network", "create", *(["--internal"] if internal else []), net])
            if c.returncode != 0:
                raise EnvironmentFailure(f"docker network create {net}: {c.stderr.strip()}")
            r = docker(["network", "inspect", "--format", "{{.Internal}}", net])
            if r.returncode != 0:
                raise EnvironmentFailure(f"docker network inspect {net} after creating it: "
                                         f"{r.stderr.strip()}")
        if r.stdout.strip() != want:
            what = "is not internal" if internal else "is internal, so the proxy has no egress"
            raise SetupError(f"docker network {net} exists but {what} "
                             f"(Internal={r.stdout.strip()}); remove it with "
                             f"'docker network rm {net}' and start again")


def remove_stale_containers(names) -> int:
    """Remove every proxy, reviewer and fetcher container of this prefix
    (<prefix>-<kind>-<12 hex digits>, the names container_name gives): one
    that an earlier run started and could not remove, for example when it was
    killed. Returns the number removed."""
    # Exactly the names this module makes, so that prefix "a" never matches
    # the containers of a prefix "a-proxy". The prefix is [a-z0-9-] only, so
    # it needs no escaping in either Go's or Python's regular expressions.
    pattern = (f"{names.prefix}-(?:{'|'.join(CONTAINER_KINDS)})"
               f"-[0-9a-f]{{{CONTAINER_ID_HEX}}}")
    r = docker(["ps", "-a", "--filter", f"name=^{pattern}$",
                "--format", "{{.ID}} {{.Names}}"])
    if r.returncode != 0:
        raise EnvironmentFailure(f"docker ps: {r.stderr.strip()}")
    removed = 0
    for line in r.stdout.splitlines():
        cid, _, cname = line.partition(" ")
        if not re.fullmatch(pattern, cname):
            continue  # docker also matches the name with a leading "/"; check it here too
        log.warning("removing container %s left by an earlier run", cname)
        if _quiet_docker(["rm", "-f", cid]):
            removed += 1
    return removed


def installed_build_id() -> str | None:
    """The build id deploy/install.sh wrote into this virtualenv, or None when
    the file is absent. Raises SetupError when it is empty, because an empty
    id would match an image that has no label."""
    path = Path(sys.prefix) / BUILD_ID_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    build_id = text.strip()
    if not build_id:
        raise SetupError(f"{path} is empty; run deploy/install.sh")
    return build_id


def check_image_builds(names) -> None:
    """Raise SetupError when an image's build label is not the id this
    virtualenv was installed with, so that an orchestrator never runs against
    images from another build. Does nothing, silently, when the virtualenv has
    no id. Runs at startup and again before every review, because the images
    can be retagged while the orchestrator runs."""
    expected = installed_build_id()
    if expected is None:
        return
    for image in names.images:
        r = docker(["image", "inspect", "--format",
                    f'{{{{index .Config.Labels "{BUILD_LABEL}"}}}}', image])
        if r.returncode != 0:
            raise EnvironmentFailure(f"docker image inspect {image}: {r.stderr.strip()}")
        label = r.stdout.strip()
        if label != expected:
            raise SetupError(f"docker image {image} has {BUILD_LABEL} label {label!r}, but this "
                             f"orchestrator was installed as build {expected}; run "
                             f"deploy/install.sh")


def prepare(names) -> None:
    """Startup checks for one prefix: the docker daemon answers, the three
    images exist and carry this installation's build id, both networks exist
    (created when missing), and no proxy, reviewer or fetcher container is
    left from an earlier run.

    Raises SetupError for a missing image, an image from another build or a
    non-internal internal network, and EnvironmentFailure when docker itself
    fails.
    """
    if not daemon_reachable():
        raise EnvironmentFailure("docker version failed: no docker binary on PATH, or the "
                                 "docker daemon does not answer")
    for image in names.images:
        exists, err = image_exists(image)
        if not exists:
            raise SetupError(f"docker image {image} not found ({err}); run deploy/install.sh")
    if installed_build_id() is None:
        log.info("no %s in %s: not checking the image build labels (a development "
                 "virtualenv has none)", BUILD_ID_FILE, sys.prefix)
    check_image_builds(names)
    ensure_networks(names)
    remove_stale_containers(names)


def proxy_argv(names, name: str, upstream: str | None = None) -> list[str]:
    """docker run command line for the proxy. The token is passed by name;
    docker takes its value from the client's environment."""
    argv = [
        "docker", "run", "-d", "--rm", "--name", name,
        "--network", names.internal_network, "--network-alias", PROXY_ALIAS,
        "--read-only",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--pids-limit", "128",
        "--memory", "256m",
        # No --log-driver none: stop_proxy reads the proxy's log, one line per
        # request with method, path and status only.
        "-e", "CLAUDE_CODE_OAUTH_TOKEN",
    ]
    if upstream is not None:
        argv += ["-e", f"REVIEWBOT_UPSTREAM={upstream}"]
    return argv + [names.proxy_image]


def start_proxy(names, token: str, upstream: str | None = None) -> str:
    """Start a proxy container for one review and wait until it accepts
    connections. Returns its name, for stop_proxy.

    Raises EnvironmentFailure when docker fails or the proxy is not ready
    within PROXY_READY_TIMEOUT, and Stopped on a stop request; in both cases
    the container is removed first.
    """
    if not token:
        raise ValueError("start_proxy needs the Claude token")
    if STOP.is_set():
        raise Stopped("stop requested; not starting a proxy")
    name = container_name(names.prefix, "proxy")
    try:
        _start_proxy(names, name, token, upstream)
    except BaseException:
        stop_proxy(name)
        raise
    return name


def _start_proxy(names, name: str, token: str, upstream: str | None) -> None:
    start = time.monotonic()
    # The only docker command whose environment holds the real token.
    r = docker(proxy_argv(names, name, upstream)[1:],
               env=client_env({"CLAUDE_CODE_OAUTH_TOKEN": token}))
    if r.returncode != 0:
        raise EnvironmentFailure(f"docker run for {name} exited {r.returncode}: "
                                 f"{tail(r.stderr.strip(), 500)}")
    r = docker(["network", "connect", names.default_network, name])
    if r.returncode != 0:
        raise EnvironmentFailure(f"docker network connect {names.default_network} {name} "
                                 f"exited {r.returncode}: {tail(r.stderr.strip(), 500)}")
    deadline = start + PROXY_READY_TIMEOUT
    while True:
        if STOP.is_set():
            raise Stopped(f"stop requested while waiting for proxy {name}")
        left = deadline - time.monotonic()
        r = docker(["exec", name, *PROXY_PROBE], timeout=max(1.0, min(CLEANUP_TIMEOUT, left)))
        if r.returncode == 0:
            log.info("proxy %s ready after %.1fs", name, time.monotonic() - start)
            return
        if time.monotonic() + PROXY_PROBE_INTERVAL >= deadline:
            raise EnvironmentFailure(
                f"proxy {name} did not accept connections within {PROXY_READY_TIMEOUT:.0f}s; "
                f"last probe exited {r.returncode}: {tail(r.stderr.strip(), 500)}")
        time.sleep(PROXY_PROBE_INTERVAL)


def stop_proxy(name: str) -> None:
    """Log the requests the proxy answered with 403, then remove the proxy
    container. Logs, never raises."""
    _log_proxy_denials(name)
    if _quiet_docker(["rm", "-f", name]):
        log.info("removed proxy %s", name)


def _log_proxy_denials(name: str) -> None:
    try:
        r = docker(["logs", "--tail", str(PROXY_LOG_LINES), name])
    except EnvironmentFailure as exc:
        log.warning("docker logs %s: %s", name, exc)
        return
    if r.returncode != 0:
        log.warning("docker logs %s: %s", name, r.stderr.strip())
        return
    # docker logs replays the container's stdout and stderr on its own; the
    # proxy writes to stderr.
    lines = (r.stdout + r.stderr).splitlines()
    for line in lines:
        if " 403 " in line:
            log.warning("proxy %s answered 403: %s", name, line)
    log.debug("proxy %s: %d log lines", name, len(lines))
