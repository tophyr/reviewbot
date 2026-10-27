"""The per-review proxy, the startup checks and REVIEWBOT_DOCKER_PREFIX.

The proxy and startup tests run the real dockerrun code against the fake
docker binary in fakedocker.py.
"""

import json
import logging
import re
import threading
import time

import pytest
from fakedocker import install
from test_pipeline import REAL, FakeAPI, FakeAuth, gh_pr, make_config, structured, use_fake_auth

from reviewbot import config as config_mod
from reviewbot import discovery, dockerrun, main, pipeline
from reviewbot.config import ConfigError, DockerNames
from reviewbot.state import State

GOOD = json.dumps({"type": "result", "is_error": False, "structured_output": structured()})
NAMES = DockerNames("rbtest")
DEFAULT = config_mod.docker_names({})  # the names test_default_prefix_names pins
INTERNAL, BRIDGE = DEFAULT.internal_network, DEFAULT.default_network


@pytest.fixture
def fast_readiness(monkeypatch):
    monkeypatch.setattr(dockerrun, "PROXY_READY_TIMEOUT", 0.5)
    monkeypatch.setattr(dockerrun, "PROXY_PROBE_INTERVAL", 0.05)


def proxy_runs(fd):
    return [c for c in fd.calls() if c[0] == "run" and "-proxy-" in c[c.index("--name") + 1]]


# ---- docker run of the proxy


def test_proxy_argv(tmp_path, monkeypatch):
    fd = install(tmp_path, monkeypatch)
    name = dockerrun.start_proxy(NAMES, REAL)
    assert re.fullmatch(r"rbtest-proxy-[0-9a-f]{12}", name)
    run, = proxy_runs(fd)
    assert run == [
        "run", "-d", "--rm", "--name", name,
        "--network", "rbtest_internal", "--network-alias", "proxy",
        "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--pids-limit", "128", "--memory", "256m",
        "-e", "CLAUDE_CODE_OAUTH_TOKEN",
        "rbtest-proxy",
    ]
    assert REAL not in json.dumps(fd.calls())
    connect = [c for c in fd.calls() if c[:2] == ["network", "connect"]]
    assert connect == [["network", "connect", "rbtest_default", name]]
    execs = [c for c in fd.calls() if c[0] == "exec"]
    assert execs == [["exec", name, *dockerrun.PROXY_PROBE]]
    assert dockerrun.PROXY_PROBE[-1] == \
        'import socket; socket.create_connection(("127.0.0.1", 8080), 1).close()'
    assert fd.removed() == []  # still running for the review


def test_proxy_upstream_only_when_given(tmp_path, monkeypatch):
    fd = install(tmp_path, monkeypatch)
    dockerrun.start_proxy(NAMES, REAL, upstream="http://stub-upstream:9000")
    dockerrun.start_proxy(NAMES, REAL)
    with_upstream, without = proxy_runs(fd)
    i = with_upstream.index("REVIEWBOT_UPSTREAM=http://stub-upstream:9000")
    assert with_upstream[i - 1] == "-e" and with_upstream[-1] == "rbtest-proxy"
    assert not any("REVIEWBOT_UPSTREAM" in a for a in without)


def test_proxy_token_reaches_only_the_proxy_docker_run(tmp_path, monkeypatch):
    # The orchestrator holds the real token in its own environment; the
    # docker client gets it only for the proxy's docker run.
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", REAL)
    fd = install(tmp_path, monkeypatch, review_stdout=GOOD)
    cfg = make_config(tmp_path)
    ctx = pipeline.Context(cfg, FakeAuth(FakeAPI([gh_pr()])), State(cfg.state_dir / "s.json"), REAL)
    out = pipeline.run_and_record(ctx, discovery.build_pr_json(gh_pr()))
    assert out.status == "posted", out.detail
    with_token = [args for args, env in fd.envs() if env.get("CLAUDE_CODE_OAUTH_TOKEN") == REAL]
    assert with_token == proxy_runs(fd) and len(with_token) == 1
    review_env, = [env for args, env in fd.envs() if args[0] == "run" and "-review-" in " ".join(args)]
    # the reviewer's token is the image's own stand-in (reviewer/Dockerfile)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in review_env


def test_proxy_ready_after_failed_probes(tmp_path, monkeypatch, fast_readiness):
    monkeypatch.setattr(dockerrun, "PROXY_READY_TIMEOUT", 5.0)
    fd = install(tmp_path, monkeypatch, probe="after:3")
    name = dockerrun.start_proxy(NAMES, REAL)
    assert len([c for c in fd.calls() if c[0] == "exec"]) == 4
    assert fd.removed() == []
    dockerrun.stop_proxy(name)
    assert fd.removed() == [name]


def call_within(seconds, fn, *args):
    """fn(*args) on another thread; fail the test if it has not returned
    within seconds, instead of hanging. Returns the exception fn raised."""
    result = {}

    def target():
        try:
            fn(*args)
        except BaseException as exc:
            result["exc"] = exc

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        # End the readiness loop before the fixtures restore PATH, so the
        # thread never reaches the real docker binary.
        dockerrun.STOP.set()
        t.join(5)
        pytest.fail(f"{fn.__name__} did not return within {seconds}s")
    return result.get("exc")


def test_proxy_not_ready_in_time_is_removed(tmp_path, monkeypatch, fast_readiness):
    fd = install(tmp_path, monkeypatch, probe="exit:1")
    exc = call_within(10, dockerrun.start_proxy, NAMES, REAL)
    assert isinstance(exc, dockerrun.EnvironmentFailure), exc
    assert "did not accept connections" in str(exc)
    run, = proxy_runs(fd)
    assert fd.removed() == [run[run.index("--name") + 1]]
    assert len([c for c in fd.calls() if c[0] == "exec"]) >= 2


@pytest.mark.parametrize("behaviour,match", [
    (dict(proxy="exit:125"), "exited 125"),
    (dict(proxy="exit:1"), "exited 1"),
    (dict(connect="exit:1"), "network connect"),
])
def test_proxy_start_failure_is_an_environment_failure(tmp_path, monkeypatch, behaviour, match):
    fd = install(tmp_path, monkeypatch, **behaviour)
    with pytest.raises(dockerrun.EnvironmentFailure, match=match):
        dockerrun.start_proxy(NAMES, REAL)
    run, = proxy_runs(fd)
    assert fd.removed() == [run[run.index("--name") + 1]]


def test_stop_during_readiness_removes_the_proxy(tmp_path, monkeypatch):
    monkeypatch.setattr(dockerrun, "PROXY_READY_TIMEOUT", 30.0)
    fd = install(tmp_path, monkeypatch, probe="exit:1")

    def stopper():
        deadline = time.monotonic() + 10
        while not (fd.bindir / "execs").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        dockerrun.STOP.set()

    threading.Thread(target=stopper, daemon=True).start()
    start = time.monotonic()
    with pytest.raises(dockerrun.Stopped):
        dockerrun.start_proxy(NAMES, REAL)
    assert time.monotonic() - start < 5
    run, = proxy_runs(fd)
    assert fd.removed() == [run[run.index("--name") + 1]]


PROXY_LOG = ["reviewbot-proxy: listening on :8080, upstream https://api.anthropic.com",
             "POST /v1/messages 200 5012ms",
             "GET /v1/models 403 0ms denied",
             "CONNECT example.com:443 403 0ms denied"]


def test_stop_proxy_logs_403_lines_then_removes(tmp_path, monkeypatch, caplog):
    fd = install(tmp_path, monkeypatch, proxy_log=PROXY_LOG)
    with caplog.at_level(logging.DEBUG, logger="reviewbot.dockerrun"):
        dockerrun.stop_proxy("rbtest-proxy-0123456789ab")
    assert fd.calls() == [["logs", "--tail", "200", "rbtest-proxy-0123456789ab"],
                          ["rm", "-f", "rbtest-proxy-0123456789ab"]]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == [
        "proxy rbtest-proxy-0123456789ab answered 403: GET /v1/models 403 0ms denied",
        "proxy rbtest-proxy-0123456789ab answered 403: CONNECT example.com:443 403 0ms denied"]
    debug = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
    assert "proxy rbtest-proxy-0123456789ab: 4 log lines" in debug


def test_stop_proxy_without_a_log_still_removes(tmp_path, monkeypatch, caplog):
    fd = install(tmp_path, monkeypatch, proxy_log="exit:1")
    dockerrun.stop_proxy("rbtest-proxy-0123456789ab")  # does not raise
    assert fd.removed() == ["rbtest-proxy-0123456789ab"]
    assert "docker logs rbtest-proxy-0123456789ab" in caplog.text


def test_proxy_denials_of_a_review_are_logged(tmp_path, monkeypatch, caplog):
    fd, out = run_review(tmp_path, monkeypatch, review_stdout=GOOD, proxy_log=PROXY_LOG)
    assert out.status == "posted", out.detail
    proxy, = fd.runs("proxy")
    assert f"proxy {proxy} answered 403: GET /v1/models 403 0ms denied" in caplog.text
    order = [c[0] for c in fd.calls() if c[0] in ("logs", "rm")]
    assert order == ["logs", "rm"]


def test_no_proxy_starts_once_stop_is_set(tmp_path, monkeypatch):
    fd = install(tmp_path, monkeypatch)
    dockerrun.STOP.set()
    with pytest.raises(dockerrun.Stopped):
        dockerrun.start_proxy(NAMES, REAL)
    assert fd.calls() == []


# ---- the proxy in the pipeline


def run_review(tmp_path, monkeypatch, **behaviour):
    fd = install(tmp_path, monkeypatch, **behaviour)
    cfg = make_config(tmp_path)
    ctx = pipeline.Context(cfg, FakeAuth(FakeAPI([gh_pr()])), State(cfg.state_dir / "s.json"), REAL)
    out = pipeline.run_and_record(ctx, discovery.build_pr_json(gh_pr()))
    return fd, out


@pytest.mark.parametrize("behaviour,status", [
    (dict(review_stdout=GOOD), "posted"),
    (dict(review_stdout="not json", review_rc=1), "failed"),
    (dict(review="exit:125"), "environment"),
], ids=["reviewer-ok", "reviewer-failed", "reviewer-docker-error"])
def test_proxy_is_removed_after_the_reviewer(tmp_path, monkeypatch, behaviour, status):
    fd, out = run_review(tmp_path, monkeypatch, **behaviour)
    assert out.status == status, out.detail
    kinds = []
    for c in fd.calls():
        if c[0] == "run":
            kinds.append("run " + c[c.index("--name") + 1].split("-")[1])
        elif c[:2] == ["rm", "-f"]:
            kinds.append("rm " + c[-1].split("-")[1])
    assert kinds == ["run fetch", "run proxy", "run review", "rm proxy"]
    proxy, = fd.runs("proxy")
    assert fd.removed() == [proxy]


def test_proxy_failure_ends_the_review_unrecorded_without_a_reviewer(tmp_path, monkeypatch,
                                                                     fast_readiness):
    fd, out = run_review(tmp_path, monkeypatch, probe="exit:1")
    assert out.status == "environment" and "did not accept connections" in out.detail
    assert fd.runs("review") == []
    assert fd.removed() == fd.runs("proxy")


# ---- startup checks


@pytest.fixture
def cli_env(monkeypatch, tmp_path, app_key_path):
    import os
    for k in list(os.environ):
        if k.startswith("REVIEWBOT_"):
            monkeypatch.delenv(k)
    monkeypatch.setenv("REVIEWBOT_APP_ID", "1")
    monkeypatch.setenv("REVIEWBOT_APP_PRIVATE_KEY_PATH", str(app_key_path))
    monkeypatch.setenv("REVIEWBOT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("REVIEWBOT_WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", REAL)

    def one_pass(ctx):
        return pipeline.PassResult([], [])
    monkeypatch.setattr(pipeline, "one_pass", one_pass)
    monkeypatch.setattr(main, "_log_identity", lambda auth: None)  # no network in tests
    use_fake_auth(monkeypatch)  # the review command asks GitHub for a token
    return monkeypatch


@pytest.mark.parametrize("command", [["run", "--once"], ["run"], ["review", "o/r#1"]])
def test_missing_image_exits_2(cli_env, tmp_path, capsys, command):
    fd = install(tmp_path, cli_env, missing_images=["reviewbot-fetcher"])
    assert main.main(command) == 2
    err = capsys.readouterr().err
    assert "docker image reviewbot-fetcher not found" in err and "run deploy/install.sh" in err
    assert not any(c[0] == "run" for c in fd.calls())


BUILD_ID = "20261027T071500Z-3fa9c1d2"
OTHER_ID = "20261026T010203Z-0badc0de"
LABEL_FORMAT = '{{index .Config.Labels "reviewbot.build"}}'


def set_venv(monkeypatch, tmp_path, build_id=BUILD_ID):
    """Point sys.prefix at a directory with the build id file deploy/install.sh
    writes (no file when build_id is None)."""
    import sys
    prefix = tmp_path / "venv"
    prefix.mkdir()
    if build_id is not None:
        (prefix / "reviewbot-build-id").write_text(build_id)
    monkeypatch.setattr(sys, "prefix", str(prefix))


def label_inspects(fd):
    return [c for c in fd.calls() if c[:2] == ["image", "inspect"] and "--format" in c
            and c[c.index("--format") + 1] == LABEL_FORMAT]


@pytest.mark.parametrize("labels", [{}, {"reviewbot-proxy": OTHER_ID}])
def test_no_build_id_file_skips_the_build_check(cli_env, tmp_path, capsys, labels):
    set_venv(cli_env, tmp_path, build_id=None)
    fd = install(tmp_path, cli_env, labels=labels)
    assert main.main(["run", "--once"]) == 0
    assert label_inspects(fd) == []
    assert "not checking the image build labels" in capsys.readouterr().err


def test_skipped_build_check_is_logged_by_startup_only(tmp_path, monkeypatch, caplog):
    set_venv(monkeypatch, tmp_path, build_id=None)
    fd = install(tmp_path, monkeypatch)
    with caplog.at_level(logging.INFO, logger="reviewbot.dockerrun"):
        dockerrun.check_image_builds(NAMES)
        dockerrun.check_image_builds(NAMES)
    assert caplog.records == [] and label_inspects(fd) == []
    with caplog.at_level(logging.INFO, logger="reviewbot.dockerrun"):
        dockerrun.prepare(NAMES)
    assert [r.getMessage().startswith("no reviewbot-build-id") for r in caplog.records].count(True) == 1


def test_matching_build_labels_pass(cli_env, tmp_path, capsys):
    set_venv(cli_env, tmp_path, build_id=BUILD_ID + "\n")
    fd = install(tmp_path, cli_env, labels={n: BUILD_ID for n in DEFAULT.images})
    assert main.main(["run", "--once"]) == 0
    assert label_inspects(fd) == [["image", "inspect", "--format", LABEL_FORMAT, n]
                                  for n in DEFAULT.images]
    assert "not checking" not in capsys.readouterr().err


@pytest.mark.parametrize("image", DEFAULT.images)
@pytest.mark.parametrize("command", [["run", "--once"], ["run"], ["review", "o/r#1"]])
def test_mismatched_build_label_exits_2(cli_env, tmp_path, capsys, image, command):
    set_venv(cli_env, tmp_path)
    labels = {n: BUILD_ID for n in DEFAULT.images}
    labels[image] = OTHER_ID
    fd = install(tmp_path, cli_env, labels=labels)
    assert main.main(command) == 2
    err = capsys.readouterr().err
    assert f"docker image {image} has reviewbot.build label '{OTHER_ID}'" in err
    assert f"installed as build {BUILD_ID}" in err and "run deploy/install.sh" in err
    assert not any(c[0] in ("run", "ps") or c[:2] == ["network", "create"] for c in fd.calls())


def test_image_without_a_build_label_exits_2(cli_env, tmp_path, capsys):
    set_venv(cli_env, tmp_path)
    fd = install(tmp_path, cli_env,
                 labels={n: BUILD_ID for n in DEFAULT.images if n != "reviewbot-fetcher"})
    assert main.main(["run", "--once"]) == 2
    err = capsys.readouterr().err
    assert "docker image reviewbot-fetcher has reviewbot.build label ''" in err
    assert f"installed as build {BUILD_ID}" in err
    assert not any(c[0] == "ps" for c in fd.calls())


def test_empty_build_id_file_exits_2(cli_env, tmp_path, capsys):
    # An empty id must not match images that have no label.
    set_venv(cli_env, tmp_path, build_id="\n")
    fd = install(tmp_path, cli_env)
    assert main.main(["run", "--once"]) == 2
    assert "reviewbot-build-id is empty; run deploy/install.sh" in capsys.readouterr().err
    assert label_inspects(fd) == []


def test_missing_image_is_reported_before_the_build_check(cli_env, tmp_path, capsys):
    set_venv(cli_env, tmp_path)
    fd = install(tmp_path, cli_env, missing_images=["reviewbot-fetcher"],
                 labels={n: OTHER_ID for n in DEFAULT.images})
    assert main.main(["run", "--once"]) == 2
    assert "docker image reviewbot-fetcher not found" in capsys.readouterr().err
    assert label_inspects(fd) == []


def retag(fd, **labels):
    """Change what the fake docker answers for the build label, as running
    build-images.sh by hand would."""
    path = fd.bindir / "config.json"
    cfg = json.loads(path.read_text())
    cfg["labels"] = labels
    path.write_text(json.dumps(cfg))


def review_in_the_pass(cli_env, tmp_path, fd, relabel=None):
    """Make the pass review one pull request; relabel (a dict) is applied to the
    fake docker after startup and before the review."""
    seen = {}

    def one_pass(ctx):
        if relabel is not None:
            retag(fd, **relabel)
        out = pipeline.run_and_record(ctx, discovery.build_pr_json(gh_pr()))
        seen["status"] = out.status
        return pipeline.PassResult([("Owner/Repo#7", out)], [])
    cli_env.setattr(pipeline, "one_pass", one_pass)
    return seen


def test_image_retagged_after_startup_exits_2_and_records_nothing(cli_env, tmp_path, capsys):
    set_venv(cli_env, tmp_path)
    good = {n: BUILD_ID for n in DEFAULT.images}
    fd = install(tmp_path, cli_env, labels=good, review_stdout=GOOD)
    seen = review_in_the_pass(cli_env, tmp_path, fd, relabel=dict(good, **{"reviewbot-reviewer": OTHER_ID}))
    assert main.main(["run", "--once"]) == 2
    err = capsys.readouterr().err
    assert f"docker image reviewbot-reviewer has reviewbot.build label '{OTHER_ID}'" in err
    assert "status" not in seen  # run_and_record raised
    assert fd.runs("fetch") == [] and fd.runs("proxy") == [] and fd.runs("review") == []
    state = tmp_path / "state" / "state.json"
    assert not state.exists() or "Owner/Repo#7" not in state.read_text()


def test_image_retagged_after_startup_exits_2_for_review_command(cli_env, tmp_path, capsys):
    set_venv(cli_env, tmp_path)
    good = {n: BUILD_ID for n in DEFAULT.images}
    fd = install(tmp_path, cli_env, labels=good, review_stdout=GOOD)
    cli_env.setattr(main.discovery, "get_pr", lambda *a, **k: gh_pr())
    cli_env.setattr(main.pipeline, "skip_reason", lambda ctx, cand: None)
    original = dockerrun.check_image_builds
    calls = []

    def retag_after_startup(names):
        calls.append(1)
        if len(calls) == 2:  # the first call is the startup check
            retag(fd)
        original(names)
    cli_env.setattr(dockerrun, "check_image_builds", retag_after_startup)
    assert main.main(["review", "o/r#7"]) == 2
    assert len(calls) == 2 and fd.runs("fetch") == []
    state = tmp_path / "state" / "state.json"
    assert not state.exists() or "o/r#7" not in state.read_text()


def test_build_labels_are_checked_before_every_review(tmp_path, monkeypatch):
    set_venv(monkeypatch, tmp_path)
    fd = install(tmp_path, monkeypatch, review_stdout=GOOD,
                 labels={n: BUILD_ID for n in DEFAULT.images})
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", REAL)
    cfg = make_config(tmp_path)
    ctx = pipeline.Context(cfg, FakeAuth(FakeAPI([gh_pr()])), State(cfg.state_dir / "s.json"), REAL)
    out = pipeline.run_and_record(ctx, discovery.build_pr_json(gh_pr()))
    assert out.status == "posted", out.detail
    calls = fd.calls()
    first_run = next(i for i, c in enumerate(calls) if c[0] == "run")
    before = [c for c in calls[:first_run] if c in label_inspects(fd)]
    assert before == [["image", "inspect", "--format", LABEL_FORMAT, n] for n in DEFAULT.images]
    pipeline.run_and_record(ctx, discovery.build_pr_json(gh_pr(number=8)))
    assert len(label_inspects(fd)) == 6


def test_missing_networks_are_created(cli_env, tmp_path):
    fd = install(tmp_path, cli_env)
    assert main.main(["run", "--once"]) == 0
    creates = [c for c in fd.calls() if c[:2] == ["network", "create"]]
    assert creates == [["network", "create", "--internal", INTERNAL],
                       ["network", "create", BRIDGE]]


def test_existing_networks_are_kept(cli_env, tmp_path):
    fd = install(tmp_path, cli_env,
                 networks={INTERNAL: "true", BRIDGE: "false"})
    assert main.main(["run", "--once"]) == 0
    assert not any(c[:2] == ["network", "create"] for c in fd.calls())


def test_default_network_that_is_internal_exits_2(cli_env, tmp_path, capsys):
    fd = install(tmp_path, cli_env, networks={INTERNAL: "true", BRIDGE: "true"})
    assert main.main(["run", "--once"]) == 2
    assert f"docker network {BRIDGE} exists but is internal" in capsys.readouterr().err
    assert not any(c[0] in ("run", "ps") for c in fd.calls())


def test_internal_network_that_is_not_internal_exits_2(cli_env, tmp_path, capsys):
    fd = install(tmp_path, cli_env,
                 networks={INTERNAL: "false", BRIDGE: "false"})
    assert main.main(["run", "--once"]) == 2
    assert f"docker network {INTERNAL} exists but is not internal" in capsys.readouterr().err
    assert not any(c[:2] == ["network", "create"] for c in fd.calls())


def test_network_create_failure_exits_1(cli_env, tmp_path, capsys):
    install(tmp_path, cli_env, network_create="exit:1")
    assert main.main(["run", "--once"]) == 1
    assert f"docker network create {INTERNAL}" in capsys.readouterr().err


def test_unreachable_daemon_at_startup_exits_1(cli_env, tmp_path, capsys):
    # Not exit 2: the daemon may come up later, and systemd restarts on 1.
    fd = install(tmp_path, cli_env, version=1)
    assert main.main(["run", "--once"]) == 1
    assert "docker daemon does not answer" in capsys.readouterr().err
    assert [c[0] for c in fd.calls()] == ["version"]


STALE_MATCHING = ["reviewbot-proxy-0123456789ab", "reviewbot-review-abcdef012345",
                  "reviewbot-fetch-00ff00ff00ff"]
STALE_OTHERS = [
    "reviewbot-proxy-1",                         # an earlier deployment's proxy
    "reviewbot-fetch-Owner-Repo-7-deadbeef",     # an earlier name form
    "reviewbot-e2e-proxy-0123456789ab",          # another prefix
    "reviewbot-proxy-proxy-0123456789ab",        # prefix "reviewbot-proxy"
    "reviewbot-proxy-0123456789abc",             # 13 hex digits
    "reviewbot-proxy-0123456789AB",              # not lower-case hex
    "reviewbot-other-0123456789ab",              # not a kind this module starts
]


def test_stale_containers_are_removed_at_startup(cli_env, tmp_path, capsys):
    # The fake lists every name whatever the filter, so the names that are
    # not <prefix>-<kind>-<12 hex> must survive the Python-side check as well.
    fd = install(tmp_path, cli_env, stale=STALE_MATCHING + STALE_OTHERS)
    assert main.main(["run", "--once"]) == 0
    ps, = [c for c in fd.calls() if c[0] == "ps"]
    assert ps == ["ps", "-a", "--filter", "name=^reviewbot-(?:proxy|review|fetch)-[0-9a-f]{12}$",
                  "--format", "{{.ID}} {{.Names}}"]
    assert fd.removed() == ["id0", "id1", "id2"]  # by id
    err = capsys.readouterr().err
    for name in STALE_MATCHING:
        assert (f"WARNING reviewbot.dockerrun: removing container {name} "
                "left by an earlier run") in err
    assert err.count("removing container") == 3


def test_stale_containers_are_removed_before_work_dirs(cli_env, tmp_path):
    fd = install(tmp_path, cli_env, stale=["reviewbot-fetch-0123456789ab"])
    removed_when_swept = []
    real_sweep = pipeline.remove_stale_work_dirs

    def sweep(config):
        removed_when_swept.append(fd.removed())
        return real_sweep(config)
    cli_env.setattr(pipeline, "remove_stale_work_dirs", sweep)
    assert main.main(["run", "--once"]) == 0
    assert removed_when_swept == [["id0"]]


@pytest.mark.parametrize("kind", dockerrun.CONTAINER_KINDS)
def test_every_container_name_is_one_the_sweep_matches(kind):
    pattern = r"rbtest-(?:proxy|review|fetch)-[0-9a-f]{12}"
    names = {dockerrun.container_name("rbtest", kind) for _ in range(20)}
    assert len(names) == 20 and all(re.fullmatch(pattern, n) for n in names)


@pytest.mark.parametrize("command", [["run", "--once"], ["run"], ["review", "o/r#1"]])
def test_held_state_lock_exits_2_before_any_docker_call(cli_env, tmp_path, capsys, command):
    # A second orchestrator on the same state directory must not sweep the
    # first one's proxy.
    from reviewbot.state import StateLock
    fd = install(tmp_path, cli_env, stale=["reviewbot-proxy-0123456789ab"])
    with StateLock(tmp_path / "state"):
        assert main.main(command) == 2
    assert fd.calls() == []
    assert "lock" in capsys.readouterr().err


def test_startup_checks_use_the_prefix(cli_env, tmp_path):
    cli_env.setenv("REVIEWBOT_DOCKER_PREFIX", "rbtest")
    fd = install(tmp_path, cli_env, stale=["rbtest-review-0123456789ab"])
    assert main.main(["run", "--once"]) == 0
    inspected = [c[-1] for c in fd.calls() if c[:2] == ["image", "inspect"]]
    assert inspected == ["rbtest-proxy", "rbtest-reviewer", "rbtest-fetcher"]
    created = [c[-1] for c in fd.calls() if c[:2] == ["network", "create"]]
    assert created == ["rbtest_internal", "rbtest_default"]
    ps, = [c for c in fd.calls() if c[0] == "ps"]
    assert "name=^rbtest-(?:proxy|review|fetch)-[0-9a-f]{12}$" in ps
    assert fd.removed() == ["id0"]
    assert not any("reviewbot" in a for c in fd.calls() for a in c)


# ---- REVIEWBOT_DOCKER_PREFIX


def test_default_prefix_names():
    names = config_mod.docker_names({})
    assert names.prefix == "reviewbot"
    assert names.images == ("reviewbot-proxy", "reviewbot-reviewer", "reviewbot-fetcher")
    assert (names.internal_network, names.default_network) == ("reviewbot_internal",
                                                               "reviewbot_default")


@pytest.mark.parametrize("value", ["a", "reviewbot-e2e", "a" + "-" * 31])
def test_prefix_accepted(value):
    assert config_mod.docker_names({"REVIEWBOT_DOCKER_PREFIX": value}).prefix == value


@pytest.mark.parametrize("value", ["", "Reviewbot", "1abc", "-a", "a_b", "a.b", "a" * 33,
                                   "reviewbot\n", " reviewbot"])
def test_prefix_refused(value):
    with pytest.raises(ConfigError, match="REVIEWBOT_DOCKER_PREFIX"):
        config_mod.docker_names({"REVIEWBOT_DOCKER_PREFIX": value})


def test_bad_prefix_exits_2_with_one_line(cli_env, tmp_path, capsys):
    fd = install(tmp_path, cli_env)
    cli_env.setenv("REVIEWBOT_DOCKER_PREFIX", "Bad_Prefix")
    assert main.main(["run", "--once"]) == 2
    err = capsys.readouterr().err
    assert len(err.strip().splitlines()) == 1 and "REVIEWBOT_DOCKER_PREFIX" in err
    assert fd.calls() == []


def test_container_names_use_the_prefix(tmp_path, monkeypatch):
    fd = install(tmp_path, monkeypatch, review_stdout=GOOD)
    cfg = make_config(tmp_path, REVIEWBOT_DOCKER_PREFIX="rbtest")
    ctx = pipeline.Context(cfg, FakeAuth(FakeAPI([gh_pr()])), State(cfg.state_dir / "s.json"), REAL)
    assert pipeline.run_and_record(ctx, discovery.build_pr_json(gh_pr())).status \
        == "posted"
    runs = [c for c in fd.calls() if c[0] == "run"]
    names = [c[c.index("--name") + 1] for c in runs]
    assert [n.split("-")[:2] for n in names] == [["rbtest", "fetch"], ["rbtest", "proxy"],
                                                 ["rbtest", "review"]]
    images = [c[-1] for c in runs]
    assert images == ["rbtest-fetcher", "rbtest-proxy", "rbtest-reviewer"]
    review_run = runs[2]
    assert review_run[review_run.index("--network") + 1] == "rbtest_internal"
