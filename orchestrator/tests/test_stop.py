"""Stopping: SIGTERM during a review, between reviews, and leftovers at startup.

reviewbot.service has TimeoutStopSec=60, after which systemd sends SIGKILL.
The stop must finish well inside that, kill the running container by name,
remove the work directory and record nothing, so the pull request is
reviewed again after a restart.
"""

import http.server
import json
import logging
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from fakedocker import install
from test_pipeline import FakeAPI, FakeAuth, gh_pr, make_config, run_command, structured

from reviewbot import discovery, dockerrun, main, pipeline
from reviewbot.state import State

GOOD = json.dumps({"type": "result", "is_error": False, "structured_output": structured()})
REAL = "sk-ant-oat01-" + "r" * 40


def wait_for(cond, timeout=30.0):
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        time.sleep(0.02)


def handlers():
    return signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)


def test_sigterm_during_review_kills_the_container_and_records_nothing(tmp_path, monkeypatch):
    fd = install(tmp_path, monkeypatch, review="sleep")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", REAL)
    cfg = make_config(tmp_path)
    before = handlers()
    sent = {}

    def stopper():
        wait_for(fd.running)
        sent["name"] = fd.running()[0]
        sent["t"] = time.monotonic()
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=stopper, daemon=True).start()
    assert run_command(cfg, FakeAuth(FakeAPI([gh_pr()]))) == 0
    elapsed = time.monotonic() - sent["t"]
    assert elapsed < 10, elapsed
    assert sent["name"].startswith("reviewbot-review-")
    assert ["kill", sent["name"]] in fd.calls()
    assert ["rm", "-f", sent["name"]] in fd.calls()
    # the review's proxy is removed after the reviewer
    proxy, = fd.runs("proxy")
    assert fd.removed()[-1] == proxy
    assert list(cfg.work_dir.iterdir()) == []
    state = State.load(cfg.state_dir / "state.json")
    assert state.entry("Owner/Repo#7") is None  # reviewed again after a restart
    assert handlers() == before


def test_container_that_exits_on_the_forwarded_sigterm_is_not_recorded(tmp_path, monkeypatch):
    # systemd also sends SIGTERM to the docker client, which forwards it to
    # the container, so the container can end before the poll sees the stop.
    # A long poll interval makes that ordering certain here.
    fd = install(tmp_path, monkeypatch, review="sleep")
    monkeypatch.setattr(dockerrun, "POLL_SECONDS", 30.0)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", REAL)
    cfg = make_config(tmp_path)

    def stopper():
        wait_for(fd.running)
        name = fd.running()[0]
        os.kill(os.getpid(), signal.SIGTERM)
        wait_for(dockerrun.STOP.is_set)
        os.kill(int((fd.bindir / f"pid-{name}").read_text()), signal.SIGTERM)

    threading.Thread(target=stopper, daemon=True).start()
    start = time.monotonic()
    assert run_command(cfg, FakeAuth(FakeAPI([gh_pr()]))) == 0
    assert time.monotonic() - start < 10
    assert not any(c[0] == "kill" for c in fd.calls())  # it had already ended
    assert list(cfg.work_dir.iterdir()) == []
    assert State.load(cfg.state_dir / "state.json").entry("Owner/Repo#7") is None


def test_stop_path_is_bounded_when_docker_hangs(tmp_path, monkeypatch):
    # docker kill and docker rm hang (daemon stuck). Every cleanup command
    # has CLEANUP_TIMEOUT, so the stop takes at most about
    # POLL_SECONDS + 5 * CLEANUP_TIMEOUT (three steps for the reviewer, two
    # for the proxy: docker logs and docker rm). Production values give
    # 51 s, inside TimeoutStopSec=60; this test scales CLEANUP_TIMEOUT down.
    assert dockerrun.POLL_SECONDS + 5 * dockerrun.CLEANUP_TIMEOUT < 60
    fd = install(tmp_path, monkeypatch, review="sleep", kill_hangs=True)
    monkeypatch.setattr(dockerrun, "CLEANUP_TIMEOUT", 1.0)
    cfg = make_config(tmp_path)
    sent = {}

    def stopper():
        wait_for(fd.running)
        # docker kill hangs, so the docker client keeps running until the
        # SIGKILL fallback after CLEANUP_TIMEOUT
        name = fd.running()[0]
        sent["pid"] = int((fd.bindir / f"pid-{name}").read_text())
        sent["t"] = time.monotonic()
        dockerrun.STOP.set()

    threading.Thread(target=stopper, daemon=True).start()
    ctx = pipeline.Context(cfg, FakeAuth(FakeAPI([gh_pr()])), State(cfg.state_dir / "s.json"), REAL)
    out = pipeline.run_and_record(ctx, discovery.build_pr_json(gh_pr()))
    elapsed = time.monotonic() - sent["t"]
    assert out.status == "stopped"
    assert elapsed < dockerrun.POLL_SECONDS + 5 * 1.0 + 2, elapsed
    with pytest.raises(ProcessLookupError):
        os.kill(sent["pid"], 0)  # the docker client was killed


def test_stop_is_checked_between_pull_requests(tmp_path, monkeypatch):
    fd = install(tmp_path, monkeypatch, review_stdout=GOOD)
    cfg = make_config(tmp_path)
    api = FakeAPI([gh_pr(1, head="a" * 40), gh_pr(2, head="b" * 40)])
    real_request = api.request

    def request(*args, **kw):  # the stop arrives while the first review is posted
        resp = real_request(*args, **kw)
        dockerrun.STOP.set()
        return resp
    api.request = request
    state = State(cfg.state_dir / "state.json")
    results = pipeline.one_pass(pipeline.Context(cfg, FakeAuth(api), state, REAL)).reviews
    assert [(k, o.status) for k, o in results] == [("Owner/Repo#1", "posted")]
    assert len(fd.runs("fetch")) == 1
    assert state.entry("Owner/Repo#2") is None


@pytest.mark.parametrize("command", [["run", "--once"], ["review", "o/r#1"]])
def test_stale_work_dirs_are_removed_at_startup(tmp_path, monkeypatch, app_key_path, command):
    for k in list(os.environ):
        if k.startswith("REVIEWBOT_"):
            monkeypatch.delenv(k)
    work = tmp_path / "work"
    monkeypatch.setenv("REVIEWBOT_APP_ID", "1")
    monkeypatch.setenv("REVIEWBOT_APP_PRIVATE_KEY_PATH", str(app_key_path))
    monkeypatch.setenv("REVIEWBOT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("REVIEWBOT_WORK_DIR", str(work))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", REAL)
    install(tmp_path, monkeypatch)  # the sweep runs after the docker checks
    monkeypatch.setattr(main, "_log_identity", lambda auth: None)
    monkeypatch.setattr(main, "cmd_run", lambda ctx, **_: 0)
    monkeypatch.setattr(main, "cmd_review", lambda ctx, ref: 0)
    stale = pipeline.make_work_dir(work, "o/r", 1, "a" * 40)  # left by a killed run
    (stale / "repo").mkdir()
    (stale / "repo" / "f.txt").write_text("x")
    other = work / "notes"
    other.mkdir()
    lookalike = work / "x__1__aaaaaaaaaaaa__deadbeef"  # a file, not a work directory
    lookalike.write_text("keep")
    assert main.main(command) == 0
    assert not stale.exists()
    assert other.is_dir() and lookalike.is_file()


def test_stale_work_dirs_are_kept_with_keep_work(tmp_path):
    cfg = make_config(tmp_path, REVIEWBOT_KEEP_WORK="1")
    stale = pipeline.make_work_dir(cfg.work_dir, "o/r", 1, "a" * 40)
    assert pipeline.remove_stale_work_dirs(cfg) == 0
    assert stale.exists()


def test_stale_dir_that_cannot_be_removed_does_not_stop_cmd_run(tmp_path, monkeypatch, caplog):
    if os.geteuid() == 0:
        return  # root removes the locked directory regardless of its mode
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", REAL)
    install(tmp_path, monkeypatch)
    cfg = make_config(tmp_path)
    stale = pipeline.make_work_dir(cfg.work_dir, "o/r", 1, "a" * 40)
    locked = stale / "locked"  # a directory whose file cannot be removed
    locked.mkdir()
    (locked / "f.txt").write_text("x")
    locked.chmod(stat.S_IRUSR | stat.S_IXUSR)
    removable = pipeline.make_work_dir(cfg.work_dir, "o/r", 2, "b" * 40)
    passes = []

    def one_pass(ctx):
        passes.append(ctx)
        dockerrun.STOP.set()  # one pass, then stop
        return pipeline.PassResult([], [])
    monkeypatch.setattr(pipeline, "one_pass", one_pass)
    try:
        with caplog.at_level(logging.WARNING, logger="reviewbot.pipeline"):
            rc = run_command(cfg, FakeAuth(FakeAPI([gh_pr()])))
        assert rc == 0
        assert len(passes) == 1
        assert f"could not remove work directory {stale}" in caplog.text
        assert stale.exists() and not removable.exists()
        assert pipeline.remove_stale_work_dirs(cfg) == 0  # counts only removed dirs
    finally:
        locked.chmod(0o755)
    assert State.load(cfg.state_dir / "state.json").data["prs"] == {}


# End to end: the real `reviewbot run` process, a fake GitHub on localhost,
# the fake docker, and SIGTERM sent the way systemd sends it (to the service
# process and to the docker client it started).

INSTALLATION_TOKEN = "ghs_" + "i" * 36
FETCH_TOKEN = "ghs_" + "f" * 36


class FakeGitHub(http.server.BaseHTTPRequestHandler):
    """GitHub with one App installation (id 1) holding Owner/Repo. The App's
    endpoints accept only a JWT that verifies with server.public_key, and the
    installation's endpoints only the installation token minted here."""

    def _bearer(self):
        return (self.headers.get("Authorization") or "").removeprefix("Bearer ")

    def _jwt_ok(self):
        try:
            jwt.decode(self._bearer(), self.server.public_key, algorithms=["RS256"],
                       issuer="1", options={"require": ["iat", "exp", "iss"]})
            return True
        except jwt.InvalidTokenError:
            return False

    def _send_json(self, status, doc):
        body = json.dumps(doc).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _refuse(self, path):
        self.server.refused.append(path)
        self.send_error(401)

    def do_GET(self):
        path = self.path.split("?")[0]
        self.server.requests.append(("GET", path))
        if path in ("/app/installations", "/repos/Owner/Repo/installation"):
            if not self._jwt_ok():
                return self._refuse(path)
            self._send_json(200, [{"id": 1, "account": {"login": "Owner"}}]
                            if path == "/app/installations" else {"id": 1})
        elif path in ("/installation/repositories", "/repos/Owner/Repo/pulls",
                      f"/repos/Owner/Repo/pulls/{gh_pr()['number']}"):
            if self._bearer() != INSTALLATION_TOKEN:
                return self._refuse(path)
            if path == "/installation/repositories":
                self._send_json(200, {"repositories": [{"full_name": "Owner/Repo"}]})
            else:
                self._send_json(200, [gh_pr()] if path.endswith("/pulls") else gh_pr())
        else:
            self.send_error(404)

    def do_POST(self):
        self.server.requests.append(("POST", self.path))
        if self.path == "/app/installations/1/access_tokens":
            if not self._jwt_ok():
                return self._refuse(self.path)
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            # A body restricts the token to one repository: the fetcher's token.
            scoped = bool(body) and json.loads(body) is not None
            expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
            return self._send_json(201, {"token": FETCH_TOKEN if scoped else INSTALLATION_TOKEN,
                                         "expires_at": expires})
        self.server.posts.append(self.path)
        self.send_error(500)

    def log_message(self, *args):
        pass


# review exits 1 because the review it was asked for did not happen.
@pytest.mark.parametrize("command, rc", [(["run"], 0), (["run", "--once"], 0),
                                         (["review", f"Owner/Repo#{gh_pr()['number']}"], 1)])
def test_reviewbot_run_stops_within_the_systemd_timeout(tmp_path, monkeypatch, app_key_path,
                                                       command, rc):
    fd = install(tmp_path, monkeypatch, review="sleep")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeGitHub)
    server.posts, server.requests, server.refused = [], [], []
    server.public_key = serialization.load_pem_private_key(
        app_key_path.read_bytes(), password=None).public_key()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    work = tmp_path / "work"
    stale = pipeline.make_work_dir(work, "Owner/Repo", 3, "c" * 40)
    env = {
        "PATH": os.environ["PATH"], "HOME": str(tmp_path),
        "REVIEWBOT_APP_ID": "1", "REVIEWBOT_APP_PRIVATE_KEY_PATH": str(app_key_path),
        "REVIEWBOT_REVIEWER_UID": "10001",
        "REVIEWBOT_STATE_DIR": str(tmp_path / "state"), "REVIEWBOT_WORK_DIR": str(work),
        "REVIEWBOT_GITHUB_API_URL": f"http://127.0.0.1:{server.server_address[1]}",
        "CLAUDE_CODE_OAUTH_TOKEN": REAL,
    }
    proc = subprocess.Popen([sys.executable, "-m", "reviewbot.main", *command], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        wait_for(lambda: fd.running() or proc.poll() is not None)
        assert proc.poll() is None, proc.communicate()
        name = fd.running()[0]
        client = int((fd.bindir / f"pid-{name}").read_text())
        t0 = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        os.kill(client, signal.SIGTERM)
        out, err = proc.communicate(timeout=60)
        elapsed = time.monotonic() - t0
    finally:
        if proc.poll() is None:
            proc.kill()
        server.shutdown()
    assert proc.returncode == rc, err
    assert elapsed < 10, elapsed
    state = json.loads((tmp_path / "state" / "state.json").read_text())
    assert state["prs"] == {}, err  # not recorded: reviewed again after a restart
    assert not stale.exists()
    assert list(work.iterdir()) == []
    assert server.posts == []
    assert server.refused == [] and ("POST", "/app/installations/1/access_tokens") in server.requests
    assert ["rm", "-f", name] in fd.calls()
    proxy, = fd.runs("proxy")
    assert fd.removed()[-1] == proxy
    assert "stop requested by SIGTERM or SIGINT" in err and "stopped" in err
    assert err.count("stop requested by SIGTERM or SIGINT") == 1  # logged once


def test_signal_handler_makes_no_logging_or_threading_call(tmp_path, monkeypatch):
    # The handler can interrupt the main thread while it holds a logging,
    # redaction or threading.Event lock. Any call that takes such a lock
    # from the handler waits for the interrupted code, which never resumes.
    # So the handler body may only set the flag. A profile hook records
    # every Python call made while the handler runs.
    import logging as logging_mod
    import threading as threading_mod

    from reviewbot import logutil

    installed = {}
    real_signal = signal.signal

    def capture(sig, handler):
        installed[sig] = handler
        return real_signal(sig, handler)
    monkeypatch.setattr(main.signal, "signal", capture)

    watched = {logging_mod.__file__, threading_mod.__file__, logutil.__file__}
    seen = []

    def profile(frame, event, arg):
        if event == "call" and frame.f_code.co_filename in watched:
            seen.append(f"{frame.f_code.co_filename}:{frame.f_code.co_name}")
        elif event == "c_call" and getattr(arg, "__name__", "") in ("acquire", "__enter__"):
            seen.append(f"builtin {arg.__name__}")

    def one_pass(ctx):
        handler = installed[signal.SIGTERM]
        sys.setprofile(profile)
        try:
            handler(signal.SIGTERM, None)
        finally:
            sys.setprofile(None)
        return pipeline.PassResult([], [])
    monkeypatch.setattr(pipeline, "one_pass", one_pass)
    monkeypatch.setattr(main.validate, "read_real_token", lambda: REAL)
    install(tmp_path, monkeypatch)

    cfg = make_config(tmp_path)
    assert run_command(cfg, FakeAuth(FakeAPI([gh_pr()]))) == 0
    assert dockerrun.STOP.is_set()
    # sys.setprofile(None) itself is the only call the hook may see
    assert [s for s in seen if "setprofile" not in s] == []
