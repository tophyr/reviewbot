"""Environment failures are not counted against a pull request.

These tests run the real fetch, review and docker-run code against the fake
docker binary in fakedocker.py; only GitHub is faked in Python.
"""

import json
import logging

import pytest
from conftest import fixture_text
from fakedocker import install
from test_pipeline import (HEAD, REAL, FakeAPI, FakeAuth, gh_pr, make_config, run_command,
                           structured)

from reviewbot import discovery, dockerrun, fetch, pipeline
from reviewbot.github_auth import GitHubError
from reviewbot.state import State

GOOD = json.dumps({"type": "result", "is_error": False, "structured_output": structured()})
KEY = "Owner/Repo#7"


def setup(tmp_path, prs=None, auth_cls=FakeAuth, **env):
    cfg = make_config(tmp_path, **env)
    api = FakeAPI(prs if prs is not None else [gh_pr()])
    state = State(cfg.state_dir / "state.json")
    return pipeline.Context(cfg, auth_cls(api), state, REAL), api, state


def pr_json():
    return discovery.build_pr_json(gh_pr())


ENVIRONMENT_FAILURES = {
    "fetcher docker run exits 125": dict(fetch="exit:125"),
    "reviewer docker run exits 125": dict(review="exit:125"),
    "daemon stops answering during the fetch": dict(fetch="exit:1", version=1),
    "daemon stops answering during the review": dict(review="exit:1", version=1),
    "fetcher exits 75 (github.com's git endpoint unreachable)": dict(fetch="exit:75"),
    "proxy docker run exits 125": dict(proxy="exit:125"),
    "proxy cannot join the default network": dict(connect="exit:1"),
    "proxy never accepts connections": dict(probe="exit:1"),
}


@pytest.mark.parametrize("behaviour", ENVIRONMENT_FAILURES.values(), ids=ENVIRONMENT_FAILURES)
def test_environment_failure_is_not_an_attempt(tmp_path, monkeypatch, behaviour):
    monkeypatch.setattr(dockerrun, "PROXY_READY_TIMEOUT", 0.3)
    monkeypatch.setattr(dockerrun, "PROXY_PROBE_INTERVAL", 0.05)
    install(tmp_path, monkeypatch, review_stdout=GOOD, **behaviour)
    ctx, api, state = setup(tmp_path)
    for _ in range(pipeline.MAX_ATTEMPTS + 1):
        out = pipeline.run_and_record(ctx, pr_json())
        assert out.status == "environment", out.detail
    assert state.entry(KEY) is None
    assert state.should_skip(KEY, HEAD, dry_run=False, max_attempts=pipeline.MAX_ATTEMPTS) is None
    assert api.posts == []
    assert list(ctx.config.work_dir.iterdir()) == []


def test_missing_docker_binary_is_not_an_attempt(tmp_path, monkeypatch):
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    ctx, api, state = setup(tmp_path)
    out = pipeline.run_and_record(ctx, pr_json())
    assert out.status == "environment" and "cannot run docker" in out.detail
    assert state.entry(KEY) is None


def test_reviewer_image_that_cannot_be_inspected_is_not_an_attempt(tmp_path, monkeypatch):
    # Without REVIEWBOT_REVIEWER_UID the uid comes from docker image inspect.
    install(tmp_path, monkeypatch, image=1)
    ctx, api, state = setup(tmp_path, REVIEWBOT_REVIEWER_UID="")
    out = pipeline.run_and_record(ctx, pr_json())
    assert out.status == "environment" and "image inspect" in out.detail
    assert state.entry(KEY) is None


def test_unwritable_work_dir_is_not_an_attempt(tmp_path, monkeypatch):
    install(tmp_path, monkeypatch)
    locked = tmp_path / "ro"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        ctx, api, state = setup(tmp_path, REVIEWBOT_WORK_DIR=str(locked / "work"))
        out = pipeline.run_and_record(ctx, pr_json())
    finally:
        locked.chmod(0o700)
    assert out.status == "environment" and "work directory" in out.detail
    assert state.entry(KEY) is None


class TokenFailure(FakeAuth):
    status = 502

    def fetch_token_for_repo(self, repo):
        raise GitHubError(self.status, "POST /app/installations/1/access_tokens: failed")


def test_github_server_error_on_token_is_not_an_attempt(tmp_path, monkeypatch):
    install(tmp_path, monkeypatch)
    ctx, api, state = setup(tmp_path, auth_cls=TokenFailure)
    out = pipeline.run_and_record(ctx, pr_json())
    assert out.status == "environment"
    assert state.entry(KEY) is None


# The detectors must not fire on failures that belong to the pull request.

def test_pull_request_failure_is_logged_once_without_a_traceback(tmp_path, monkeypatch, caplog):
    install(tmp_path, monkeypatch, fetch="exit:1", version=0)
    ctx, api, state = setup(tmp_path)
    out = pipeline.run_and_record(ctx, pr_json())
    assert (out.status, out.detail) == ("failed", "fetch exit 1")
    # one line for the outcome, at ERROR
    lines = [r for r in caplog.records if "fetch exit 1" in r.getMessage()]
    assert [(r.levelname, r.getMessage()) for r in lines] == [
        ("ERROR", f"{KEY} at {HEAD[:12]}: failed fetch exit 1")]
    assert "fetcher stderr tail:" in caplog.text
    assert "Traceback" not in caplog.text


def test_repository_too_large_for_the_fetcher_names_the_limit(tmp_path, monkeypatch, caplog):
    install(tmp_path, monkeypatch, fetch=f"exit:{fetch.FETCHER_EXIT_TOO_LARGE}", version=0)
    ctx, api, state = setup(tmp_path)
    out = pipeline.run_and_record(ctx, pr_json())
    assert out.status == "failed"
    assert out.detail == (f"the repository does not fit in the fetcher's {fetch.MAX_FETCH_MB} "
                          "MiB scratch space")
    assert state.entry(KEY)["status"] == "failed"


def test_container_failure_with_a_working_daemon_is_an_attempt(tmp_path, monkeypatch):
    install(tmp_path, monkeypatch, fetch="exit:1", version=0)
    ctx, api, state = setup(tmp_path)
    for _ in range(2):
        assert pipeline.run_and_record(ctx, pr_json()).status == "failed"
    assert state.entry(KEY)["attempts"] == 2
    assert "failed 2 times" in state.should_skip(KEY, HEAD, dry_run=False, max_attempts=2)


@pytest.mark.parametrize("status", [404, 422])
def test_github_client_error_on_token_is_an_attempt(tmp_path, monkeypatch, caplog, status):
    install(tmp_path, monkeypatch)

    class NotFound(TokenFailure):
        pass
    NotFound.status = status
    ctx, api, state = setup(tmp_path, auth_cls=NotFound)
    out = pipeline.run_and_record(ctx, pr_json())
    assert out.status == "failed"
    assert out.detail.startswith(f"GitHub token request failed: GitHub API error {status}")
    assert state.entry(KEY)["status"] == "failed"
    assert "Traceback" not in caplog.text


@pytest.mark.parametrize("status", [401, 403, 429])
def test_github_auth_and_rate_limit_on_token_are_not_attempts(tmp_path, monkeypatch, status):
    # No reviewer runs, so the hourly cap does not stop the loop; recording
    # these would use up MAX_ATTEMPTS on every open pull request.
    fd = install(tmp_path, monkeypatch)

    class Limited(TokenFailure):
        pass
    Limited.status = status
    ctx, api, state = setup(tmp_path, prs=TWO_PRS, auth_cls=Limited)
    for _ in range(pipeline.MAX_ATTEMPTS):
        results = pipeline.one_pass(ctx).reviews
        # the pass ends at the first pull request
        assert [(k, o.status) for k, o in results] == [("Owner/Repo#1", "environment")]
        assert f"GitHub API error {status}" in results[0][1].detail
    assert state.data["prs"] == {}
    for n, head in ((1, "a" * 40), (2, "b" * 40)):
        assert state.should_skip(f"Owner/Repo#{n}", head, dry_run=False, max_attempts=3) is None
    assert fd.runs("fetch") == [] and fd.runs("review") == []
    assert api.posts == []


TWO_PRS = [gh_pr(1, head="a" * 40), gh_pr(2, head="b" * 40)]


def test_environment_failure_ends_the_pass(tmp_path, monkeypatch):
    fd = install(tmp_path, monkeypatch, fetch="exit:125")
    ctx, api, state = setup(tmp_path, prs=TWO_PRS)
    results = pipeline.one_pass(ctx).reviews
    assert [(k, o.status) for k, o in results] == [("Owner/Repo#1", "environment")]
    assert len(fd.runs("fetch")) == 1
    assert state.data["prs"] == {}


def test_unreachable_git_endpoint_ends_the_pass_unrecorded(tmp_path, monkeypatch, caplog):
    # Fetcher exit 75: git fetch and git ls-remote of the base repository and
    # of a known public repository all failed.
    fd = install(tmp_path, monkeypatch, fetch="exit:75")
    ctx, api, state = setup(tmp_path, prs=TWO_PRS)
    results = pipeline.one_pass(ctx).reviews
    assert [(k, o.status) for k, o in results] == [("Owner/Repo#1", "environment")]
    assert "github.com's git endpoint is unreachable" in results[0][1].detail
    assert "github.com's git endpoint is unreachable" in caplog.text
    assert len(fd.runs("fetch")) == 1 and fd.runs("review") == []
    assert state.data["prs"] == {}
    assert api.posts == []


def test_fetcher_exit_1_is_still_recorded_failed(tmp_path, monkeypatch):
    # Control for exit 75: any other fetcher failure belongs to the pull
    # request, and the pass goes on to the next one.
    fd = install(tmp_path, monkeypatch, fetch="exit:1", version=0)
    ctx, api, state = setup(tmp_path, prs=TWO_PRS)
    results = pipeline.one_pass(ctx).reviews
    assert [(k, o.status) for k, o in results] == [("Owner/Repo#1", "failed"),
                                                  ("Owner/Repo#2", "failed")]
    assert len(fd.runs("fetch")) == 2
    assert state.data["prs"]["Owner/Repo#1"]["attempts"] == 1


# The fixture is the stdout of the reviewer image (exit 1) against a stub upstream
# answering each status; tests/e2e/test_upstream_errors.sh regenerates it.
API_ERRORS = json.loads(fixture_text("reviewer_api_errors.json"))["stdout"]


@pytest.mark.parametrize("status", ["401", "403", "429", "500", "529"])
def test_api_auth_rate_limit_and_server_errors_are_not_attempts(tmp_path, monkeypatch, caplog,
                                                                status):
    install(tmp_path, monkeypatch, review_stdout=API_ERRORS[status], review_rc=1)
    ctx, api, state = setup(tmp_path)
    for _ in range(pipeline.MAX_ATTEMPTS + 1):
        out = pipeline.run_and_record(ctx, pr_json())
        assert out.status == "environment", out.detail
        assert f"Anthropic API error {status}" in out.detail
    assert state.entry(KEY) is None
    assert api.posts == []
    assert f"{KEY} at {HEAD[:12]}: environment Anthropic API error {status}" in caplog.text


def _assumed_404():
    """A 404 result shaped like the measured 400 row. Claude Code's output
    for a 404 was not measured; this shape is assumed until
    tests/e2e/test_upstream_errors.sh is next run."""
    doc = json.loads(API_ERRORS["400"])
    doc["api_error_status"] = 404
    doc["result"] = "API Error: 404 stub: model: not found"
    return json.dumps(doc)


def test_api_model_not_found_is_not_an_attempt(tmp_path, monkeypatch, caplog):
    install(tmp_path, monkeypatch, review_stdout=_assumed_404(), review_rc=1)
    ctx, api, state = setup(tmp_path)
    for _ in range(pipeline.MAX_ATTEMPTS + 1):
        out = pipeline.run_and_record(ctx, pr_json())
        assert out.status == "environment", out.detail
        assert "Anthropic API error 404 (model not found)" in out.detail
    assert state.entry(KEY) is None
    assert api.posts == []
    assert (f"{KEY} at {HEAD[:12]}: environment Anthropic API error 404 (model not found)"
            in caplog.text)


def test_api_error_ends_the_pass_before_the_next_pull_request(tmp_path, monkeypatch):
    fd = install(tmp_path, monkeypatch, review_stdout=API_ERRORS["429"], review_rc=1)
    ctx, api, state = setup(tmp_path, prs=[gh_pr(1, head="a" * 40), gh_pr(2, head="b" * 40)])
    results = pipeline.one_pass(ctx).reviews
    assert [o.status for _, o in results] == ["environment"]
    assert len(fd.runs("review")) == 1
    assert state.data["prs"] == {}


# The detail names how Claude Code's run ended, from its top-level fields only.
@pytest.mark.parametrize("stdout, detail", [
    # the API refused the request; a large PR can cause that
    (API_ERRORS["400"],
     "reviewer exit 1 (subtype success, terminal_reason api_error, API status 400, 1 turn)"),
    (json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True,
                 "terminal_reason": "max_turns", "num_turns": 40}),
     "reviewer exit 1 (subtype error_max_turns, terminal_reason max_turns, 40 turns)"),
    # the fields only inside model output, not at the top level
    (json.dumps({"type": "result", "is_error": True, "subtype": "success",
                 "result": '{"terminal_reason": "api_error", "api_error_status": 401}',
                 "structured_output": {"terminal_reason": "api_error",
                                       "api_error_status": 401}}),
     "reviewer exit 1 (subtype success)"),
    (json.dumps({"type": "result", "is_error": False, "terminal_reason": "api_error",
                 "api_error_status": 529}),
     "reviewer exit 1 (terminal_reason api_error, API status 529)"),
    # a value of an unexpected shape is left out
    (json.dumps({"type": "result", "is_error": True, "subtype": "@someone see #12",
                 "terminal_reason": "max_turns", "num_turns": "40"}),
     "reviewer exit 1 (terminal_reason max_turns)"),
    ("not json", "reviewer exit 1"),
], ids=["400", "max-turns", "nested-only", "not-is_error", "odd-values", "not-json"])
def test_other_reviewer_failures_are_still_attempts(tmp_path, monkeypatch, stdout, detail):
    install(tmp_path, monkeypatch, review_stdout=stdout, review_rc=1)
    ctx, api, state = setup(tmp_path)
    out = pipeline.run_and_record(ctx, pr_json())
    assert out.status == "failed", out.detail
    assert out.detail == detail
    assert state.entry(KEY)["status"] == "failed"


USAGE = {"num_turns": 5, "total_cost_usd": 0.0851574, "duration_ms": 13488,
         "usage": {"input_tokens": 6, "cache_read_input_tokens": 12887,
                   "cache_creation_input_tokens": 7622, "output_tokens": 1079}}
USAGE_LINE = ("reviewer usage for Owner/Repo#7: 5 turns, 20515 input tokens (12887 read from "
              "the cache, 7622 written to it, 6 uncached), 1079 output tokens, $0.0852 at "
              "list price, 13.5s")


@pytest.mark.parametrize("rc, status", [(0, "posted"), (1, "failed")])
def test_reviewer_usage_is_logged_for_every_run(tmp_path, monkeypatch, caplog, rc, status):
    caplog.set_level(logging.INFO)
    stdout = json.dumps({**json.loads(GOOD), **USAGE, "is_error": bool(rc)})
    install(tmp_path, monkeypatch, review_stdout=stdout, review_rc=rc)
    ctx, api, state = setup(tmp_path)
    assert pipeline.run_and_record(ctx, pr_json()).status == status
    assert USAGE_LINE in caplog.text


def test_reviewer_usage_reads_only_top_level_numbers(tmp_path, monkeypatch, caplog):
    # Counts that are not numbers are left out, and figures that appear only
    # in the model's output are not read.
    caplog.set_level(logging.INFO)
    doc = json.loads(GOOD)
    doc["structured_output"]["num_turns"] = 99
    doc.update({"num_turns": "5", "total_cost_usd": True, "duration_ms": 13488,
                "usage": {"input_tokens": 6, "cache_read_input_tokens": "12887",
                          "cache_creation_input_tokens": 7622, "output_tokens": 1079}})
    install(tmp_path, monkeypatch, review_stdout=json.dumps(doc))
    ctx, api, state = setup(tmp_path)
    pipeline.run_and_record(ctx, pr_json())
    assert "reviewer usage for Owner/Repo#7: 1079 output tokens, 13.5s" in caplog.text
    assert "99 turns" not in caplog.text


def test_once_exits_1_when_the_pass_ends_on_an_environment_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-" + "r" * 40)
    fd = install(tmp_path, monkeypatch, fetch="exit:125")
    cfg = make_config(tmp_path)
    assert run_command(cfg, FakeAuth(FakeAPI(TWO_PRS)), once=True) == 1
    assert len(fd.runs("fetch")) == 1 and fd.runs("review") == []
    assert State.load(cfg.state_dir / "state.json").data["prs"] == {}


def test_once_exits_0_when_reviews_post(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-" + "r" * 40)
    install(tmp_path, monkeypatch, review_stdout=GOOD)
    cfg = make_config(tmp_path)
    assert run_command(cfg, FakeAuth(FakeAPI(TWO_PRS)), once=True) == 0


class UnlistableAPI(FakeAPI):
    def paginate(self, path, token, params=None, item_key=None):
        raise GitHubError(401, f"GET {path}: Bad credentials")


def test_once_exits_1_when_a_repository_cannot_be_listed(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-" + "r" * 40)
    install(tmp_path, monkeypatch)
    cfg = make_config(tmp_path)
    assert run_command(cfg, FakeAuth(UnlistableAPI()), once=True) == 1
    assert "listing pull requests for Owner/Repo failed: GitHub API error 401" in caplog.text
