import json
import os
import re
import subprocess
import sys

import pytest

from reviewbot import dockerrun, fetch, review
from reviewbot.config import DockerNames

SECRET = "ghs_" + "s" * 36


def test_reviewer_argv_matches_contract(tmp_path):
    argv = review.build_argv("reviewbot-reviewer", "n1", tmp_path, 10001, 10001,
                             "x_internal")
    flags = " ".join(argv[argv.index("n1") + 1:argv.index("-e")])
    assert flags == (
        "--log-driver none --network x_internal --read-only --tmpfs /tmp "
        "--tmpfs /home/reviewer:uid=10001,gid=10001,mode=0700 --cap-drop ALL "
        "--security-opt no-new-privileges --pids-limit 256 --memory 4g --cpus 2 "
        f"-v {tmp_path}:/work:ro"
    )
    assert argv[:4] == ["docker", "run", "--rm", "--name"]
    assert argv[-1] == "reviewbot-reviewer"


def test_reviewer_env_passed_by_name_only(tmp_path):
    argv = review.build_argv("img", "n1", tmp_path, 1, 1, "net")
    names = [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]
    assert names == ["REVIEWBOT_MODEL", "REVIEWBOT_MAX_TURNS", "REVIEWBOT_PROMPT"]
    assert review.reviewer_env("opus", 40, None) == {"REVIEWBOT_MODEL": "opus",
                                                     "REVIEWBOT_MAX_TURNS": "40"}


def test_reviewer_env_reads_the_prompt_file_for_every_review(tmp_path):
    prompt = tmp_path / "review.md"
    prompt.write_text("Focus on error handling.\n")
    assert review.reviewer_env("opus", 40, prompt)["REVIEWBOT_PROMPT"] == \
        "Focus on error handling.\n"
    prompt.write_text("Focus on naming.\n")
    assert review.reviewer_env("opus", 40, prompt)["REVIEWBOT_PROMPT"] == "Focus on naming.\n"


def test_prompt_file_that_vanished_is_an_environment_failure(tmp_path):
    with pytest.raises(dockerrun.EnvironmentFailure, match="REVIEWBOT_PROMPT_FILE"):
        review.reviewer_env("opus", 40, tmp_path / "missing.md")


REPO = os.path.join(os.path.dirname(__file__), "..", "..")


def dockerfile_env(text):
    """{name: value} from every ENV instruction (KEY=VALUE form) of a Dockerfile."""
    joined = re.sub(r"\\\n", " ", text)
    env = {}
    for line in joined.splitlines():
        if line.startswith("ENV "):
            env.update(item.split("=", 1) for item in line[4:].split())
    return env


def test_proxy_lifetime_exceeds_the_longest_review():
    # The proxy stops itself after MAX_LIFETIME_SECONDS; a live review keeps
    # it at most for the readiness wait, the reviewer's timeout and the stop
    # path's cleanup steps.
    # Compiled from the source each time, never from a cached .pyc.
    import types
    path = os.path.join(REPO, "proxy", "proxy.py")
    proxy = types.ModuleType("reviewbot_proxy_lifetime")
    with open(path, encoding="utf-8") as fh:
        exec(compile(fh.read(), path, "exec"), proxy.__dict__)
    longest = (dockerrun.PROXY_READY_TIMEOUT + review.REVIEW_TIMEOUT
               + 5 * dockerrun.CLEANUP_TIMEOUT)
    assert proxy.MAX_LIFETIME_SECONDS > longest


@pytest.mark.parametrize("given, missing", [
    ({}, "REVIEWBOT_MODEL"),
    ({"REVIEWBOT_MODEL": "opus"}, "REVIEWBOT_MAX_TURNS"),
    ({"REVIEWBOT_MAX_TURNS": "500"}, "REVIEWBOT_MODEL"),
])
def test_reviewer_entrypoint_refuses_to_run_without_the_orchestrators_settings(given, missing):
    script = os.path.join(REPO, "reviewer", "entrypoint.sh")
    r = subprocess.run(["bash", script], env={"PATH": os.environ["PATH"], **given},
                       capture_output=True, text=True, timeout=10)
    assert r.returncode == 64
    assert r.stderr == f"reviewbot-reviewer: {missing} is not set; the orchestrator passes it\n"


def test_dockerfile_env_parser_reads_continued_env_lines():
    text = "FROM x\nENV A=1 \\\n    B=two\nRUN true\nENV C=3\n"
    assert dockerfile_env(text) == {"A": "1", "B": "two", "C": "3"}


def test_reviewer_image_sets_the_constants_the_host_relies_on():
    with open(os.path.join(REPO, "reviewer", "Dockerfile"), encoding="utf-8") as fh:
        env = dockerfile_env(fh.read())
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env  # the entrypoint sets it
    # the orchestrator passes these, with config.py's defaults
    assert "REVIEWBOT_MODEL" not in env and "REVIEWBOT_MAX_TURNS" not in env
    with open(os.path.join(REPO, "reviewer", "entrypoint.sh"), encoding="utf-8") as fh:
        assert f"\nexport CLAUDE_CODE_OAUTH_TOKEN={review.DUMMY_TOKEN}\n" in fh.read()
    assert env["ANTHROPIC_BASE_URL"] == f"http://{dockerrun.PROXY_ALIAS}:8080"
    assert {k: env.get(k) for k in (
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "DISABLE_TELEMETRY", "DISABLE_ERROR_REPORTING",
        "DISABLE_AUTOUPDATER", "ENABLE_CLAUDEAI_MCP_SERVERS", "CLAUDE_CODE_DISABLE_ARTIFACT")} == {
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_TELEMETRY": "1",
        "DISABLE_ERROR_REPORTING": "1", "DISABLE_AUTOUPDATER": "1",
        "ENABLE_CLAUDEAI_MCP_SERVERS": "false", "CLAUDE_CODE_DISABLE_ARTIFACT": "1"}
    # the e2e leak scans look for the same stand-in
    with open(os.path.join(REPO, "tests", "e2e", "lib.sh"), encoding="utf-8") as fh:
        assert f'\nDUMMY_TOKEN="{review.DUMMY_TOKEN}"\n' in fh.read()


def test_fetcher_argv_has_no_secret_values(tmp_path):
    argv = fetch.build_argv("reviewbot-fetcher", "n2", tmp_path, 1024)
    assert f"{tmp_path}:/work" in argv
    names = [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]
    assert names == ["GITHUB_TOKEN", "PR_JSON"]
    assert not any(SECRET in a for a in argv)


def test_every_docker_run_has_no_log_driver(tmp_path):
    # The daemon keeps no copy of the output on the host's disk.
    argvs = [
        fetch.build_argv("reviewbot-fetcher", "n", tmp_path, 1024),
        review.build_argv("reviewbot-reviewer", "n", tmp_path, 10001, 10001, "net"),
    ]
    for argv in argvs:
        assert argv[:2] == ["docker", "run"]
        i = argv.index("--log-driver")
        assert argv[i + 1] == "none"
        assert i < len(argv) - 1  # before the image, the last argument
    # The proxy keeps its log (one line per request), which stop_proxy reads.
    assert "--log-driver" not in dockerrun.proxy_argv(DockerNames("reviewbot"), "n")


def test_client_env_drops_orchestrator_secrets(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-REAL" + "x" * 40)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_" + "p" * 36)
    monkeypatch.setenv("REVIEWBOT_APP_PRIVATE_KEY_PATH", "/k.pem")
    monkeypatch.setenv("HOME", "/home/x")
    env = dockerrun.client_env(review.reviewer_env("opus", 40, None))
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert "GITHUB_TOKEN" not in env
    assert "REVIEWBOT_APP_PRIVATE_KEY_PATH" not in env
    assert env["HOME"] == "/home/x"


def fake_docker(tmp_path, script):
    """Put a fake `docker` first on PATH that logs its argv and env."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / "docker"
    exe.write_text(f"#!{sys.executable}\n" + script)
    exe.chmod(0o755)
    return bindir


LOGGER = """
import json, os, sys, time
log = os.environ.get("FAKE_DOCKER_LOG", "/dev/null")
with open(log, "a") as fh:
    fh.write(json.dumps({"argv": sys.argv[1:], "env": dict(os.environ)}) + "\\n")
if sys.argv[1] == "kill":
    # like the daemon: killing the container ends the attached `docker run`
    import signal
    pidfile = os.path.join(os.path.dirname(log), "pid-" + sys.argv[2])
    os.kill(int(open(pidfile).read()), signal.SIGKILL)
    sys.exit(0)
if sys.argv[1] == "run":
    name = sys.argv[sys.argv.index("--name") + 1]
    with open(os.path.join(os.path.dirname(log), "pid-" + name), "w") as fh:
        fh.write(str(os.getpid()))
    if os.environ.get("FAKE_SLEEP"):
        time.sleep(float(os.environ["FAKE_SLEEP"]))
    print("out"); print("err", file=sys.stderr); sys.exit(3)
if sys.argv[1] == "image":
    print(os.environ.get("FAKE_INSPECT", "{}"))
"""


def test_run_passes_secret_through_env_not_argv(tmp_path, monkeypatch):
    bindir = fake_docker(tmp_path, LOGGER)
    log = tmp_path / "log"
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    # FAKE_DOCKER_LOG must reach the fake binary; it is not in the client
    # allowlist, so pass it as a container variable for this test.
    r = dockerrun.run(["docker", "run", "--name", "n", "-e", "GITHUB_TOKEN", "img"],
                      {"GITHUB_TOKEN": SECRET, "FAKE_DOCKER_LOG": str(log)}, 10, "n")
    assert (r.returncode, r.stdout, r.stderr, r.timed_out) == (3, "out\n", "err\n", False)
    call = json.loads(log.read_text().splitlines()[0])
    assert SECRET not in " ".join(call["argv"])
    assert call["env"]["GITHUB_TOKEN"] == SECRET


def test_run_timeout_kills_container(tmp_path, monkeypatch):
    bindir = fake_docker(tmp_path, LOGGER)
    log = tmp_path / "log"
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(log))  # for the kill/rm calls
    monkeypatch.setattr(dockerrun, "DOCKER_CLIENT_ENV",
                        dockerrun.DOCKER_CLIENT_ENV + ("FAKE_DOCKER_LOG",))
    r = dockerrun.run(["docker", "run", "--name", "slow", "img"],
                      {"FAKE_SLEEP": "30", "FAKE_DOCKER_LOG": str(log)}, 1, "slow")
    assert r.timed_out and r.duration < 10
    assert r.returncode == -9
    calls = [json.loads(x)["argv"] for x in log.read_text().splitlines()]
    assert ["kill", "slow"] in calls
    assert ["rm", "-f", "slow"] in calls


@pytest.mark.parametrize("cfg,expected", [
    ({"User": "10001:10001"}, (10001, 10001)),
    ({"User": "10002"}, (10002, 10002)),
])
def test_resolve_uid_gid(tmp_path, monkeypatch, cfg, expected):
    bindir = fake_docker(tmp_path, LOGGER)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setattr(dockerrun, "DOCKER_CLIENT_ENV", dockerrun.DOCKER_CLIENT_ENV + ("FAKE_INSPECT",))
    monkeypatch.setenv("FAKE_INSPECT", json.dumps(cfg))
    assert review.resolve_uid_gid("img") == expected


# An unset or named user is refused too: the uid is needed as a number.
@pytest.mark.parametrize("user", ["", "root", "0", "0:0", "root:root", "10001:0", "reviewer"])
def test_resolve_uid_gid_refuses_root_or_non_numeric(tmp_path, monkeypatch, user):
    bindir = fake_docker(tmp_path, LOGGER)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setattr(dockerrun, "DOCKER_CLIENT_ENV", dockerrun.DOCKER_CLIENT_ENV + ("FAKE_INSPECT",))
    monkeypatch.setenv("FAKE_INSPECT", json.dumps({"User": user}))
    with pytest.raises(review.ReviewerImageError):
        review.resolve_uid_gid("img")


def test_resolve_uid_gid_override_skips_docker():
    assert review.resolve_uid_gid("img", 5, None) == (5, 5)


@pytest.mark.parametrize("uid,gid", [(0, None), (0, 5), (5, 0)])
def test_resolve_uid_gid_override_refuses_root(uid, gid):
    with pytest.raises(review.ReviewerImageError, match="root"):
        review.resolve_uid_gid("img", uid, gid)


# ---- fetcher output checks


def good_work(tmp_path):
    w = tmp_path / "work"
    (w / "repo").mkdir(parents=True)
    (w / "diff.patch").write_text("")
    (w / "files.txt").write_text("café.txt\nsrc/a.py\n", "utf-8")
    return w


def test_read_fetch_output_reads_the_files_list(tmp_path):
    fw = fetch.read_fetch_output(good_work(tmp_path))
    assert fw.files == {"café.txt", "src/a.py"}


def test_read_fetch_output_refuses_symlinked_outputs(tmp_path):
    w = good_work(tmp_path)
    secret = tmp_path / "secret"
    secret.write_text("host secret")
    (w / "diff.patch").unlink()
    (w / "diff.patch").symlink_to(secret)
    with pytest.raises(fetch.FetchError):
        fetch.read_fetch_output(w)


# A FIFO in place of a fetcher output must be refused, not waited on. Each
# check runs in a child process with a timeout, so a regression fails the test
# instead of hanging the suite.
FIFO_CHECK = r"""
import json, os, sys
from pathlib import Path
from reviewbot import fetch
w, name, what = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
os.unlink(w / name)
os.mkfifo(w / name)
if what == "read_regular":
    try:
        fetch._read_regular(w / name, 100)
    except fetch.FetchError as exc:
        print("refused:", exc)
elif what == "read_fetch_output":
    try:
        fetch.read_fetch_output(w)
    except fetch.FetchError as exc:
        print("refused:", exc)
"""


def run_fifo_check(w, name, what):
    src = os.path.join(os.path.dirname(fetch.__file__), "..")
    r = subprocess.run([sys.executable, "-c", FIFO_CHECK, str(w), name, what],
                       env={**os.environ, "PYTHONPATH": os.path.abspath(src)},
                       capture_output=True, text=True, timeout=10)
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_read_regular_refuses_a_fifo_without_waiting(tmp_path):
    out = run_fifo_check(good_work(tmp_path), "diff.patch", "read_regular")
    assert out == "refused: diff.patch is not a regular file\n"


@pytest.mark.parametrize("name", ["diff.patch", "files.txt"])
def test_read_fetch_output_refuses_a_fifo_without_waiting(tmp_path, name):
    out = run_fifo_check(good_work(tmp_path), name, "read_fetch_output")
    assert out.startswith("refused:") and "not a regular file" in out


# The two git commands fetcher/entrypoint.sh runs to write diff.patch and
# files.txt (files.txt is the -z output with each NUL turned into a newline).
FETCHER_DIFF = ["-c", "core.quotepath=false", "diff", "--no-color", "--no-ext-diff",
                "--no-textconv", "-M"]
FETCHER_NAMES = ["diff", "--no-ext-diff", "--no-textconv", "-M", "--name-only", "-z"]
UNUSUAL_PATHS = ['say "hi".txt', '"quoted"', "with space.txt", 'both "q" and space.txt',
                 "café.txt"]


def test_fetcher_commands_are_the_ones_tested():
    here = os.path.dirname(__file__)
    with open(os.path.join(here, "..", "..", "fetcher", "entrypoint.sh"), encoding="utf-8") as fh:
        script = fh.read()
    assert "git_logged -c core.quotepath=false diff --no-color --no-ext-diff --no-textconv -M" in script
    assert "git_logged diff --no-ext-diff --no-textconv -M --name-only -z" in script
    assert "tr '\\0' '\\n' < \"$GIT_DIR_PATH/files.z\" > \"$SCRATCH/files.txt\"" in script


def test_unusual_paths_from_real_git_output(tmp_path):
    from reviewbot.diffmap import can_place, parse_diff

    g = tmp_path / "g"
    g.mkdir()

    def git(*args):
        return subprocess.run(["git", "-C", str(g), "-c", "user.name=t", "-c", "user.email=t@t",
                               *args], check=True, capture_output=True).stdout

    git("init", "-q")
    (g / "keep.txt").write_text("keep\n")
    git("add", "-A")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD").decode().strip()
    for p in UNUSUAL_PATHS:
        (g / p).write_text("one\ntwo\n", "utf-8")
    git("add", "-A")
    git("commit", "-qm", "head")
    head = git("rev-parse", "HEAD").decode().strip()

    w = tmp_path / "work"
    (w / "repo").mkdir(parents=True)
    (w / "diff.patch").write_bytes(git(*FETCHER_DIFF, base, head))
    (w / "files.txt").write_bytes(git(*FETCHER_NAMES, base, head).replace(b"\0", b"\n"))

    fw = fetch.read_fetch_output(w)
    assert fw.files == set(UNUSUAL_PATHS)
    dmap = parse_diff(fw.diff_text)
    assert set(dmap) == set(UNUSUAL_PATHS)
    for p in UNUSUAL_PATHS:
        assert dmap[p] == {1, 2}
        assert can_place(dmap, fw.files, p, 2)
