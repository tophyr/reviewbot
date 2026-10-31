"""Pipeline and discovery with the GitHub API and both containers faked."""

import json
import logging

import pytest
from conftest import fixture_text

from reviewbot import config as config_mod
from reviewbot import discovery, dockerrun, fetch, main, pipeline, review
from reviewbot.dockerrun import RunResult
from reviewbot.github_auth import GitHubError
from reviewbot.state import State

HEAD = "1" * 40
REAL = "sk-ant-oat01-" + "R" * 90
ORCH_TOKEN = "ghs_" + "o" * 36
FETCH_TOKEN = "ghs_" + "f" * 36

CONTRACT_KEYS = {"repo", "number", "title", "body", "author", "base_ref", "base_sha",
                 "head_ref", "head_sha", "head_repo", "html_url", "labels"}


def gh_pr(number=7, head=HEAD, draft=False, labels=(), user_type="User", login="alice"):
    return {
        "number": number, "state": "open", "draft": draft, "title": f"PR {number}",
        "body": None, "user": {"login": login, "type": user_type},
        "labels": [{"name": n} for n in labels],
        "base": {"ref": "main", "sha": "0" * 40, "repo": {"full_name": "Owner/Repo"}},
        "head": {"ref": "feat", "sha": head, "repo": {"full_name": "fork/Repo"}},
        "html_url": f"https://github.com/Owner/Repo/pull/{number}",
    }


class FakeAPI:
    def __init__(self, prs=(), list_path="/repos/Owner/Repo/pulls"):
        self.prs = list(prs)
        self.posts = []
        self.list_path = list_path

    def paginate(self, path, token, params=None, item_key=None):
        assert path == self.list_path
        return iter(self.prs)

    def get_json(self, path, token, params=None):
        return self.prs[0]

    def request(self, method, path, token, json=None, expected=(200,), **kw):
        assert method == "POST"
        self.posts.append((path, token, json))

        class R:
            def json(self_inner):
                return {"id": 1234}
        return R()


class FakeAuth:
    """github_auth.AppAuth without GitHub. installations is what
    list_installations returns, and repos maps an installation id to its
    repositories or to the GitHubError that listing them raises. The default
    is one installation holding Owner/Repo. Tokens are fixed strings."""

    def __init__(self, api, installations=None, repos=None):
        self.api = api
        self.installations = ([{"id": 1, "account": {"login": "Owner"}, "suspended_at": None}]
                              if installations is None else list(installations))
        self.repos = {1: [{"full_name": "Owner/Repo"}]} if repos is None else dict(repos)
        self.repo_calls = []
        self.remembered = {}

    def app_info(self):
        return {"id": 1, "slug": "reviewbot-test"}

    def list_installations(self):
        return list(self.installations)

    def installation_repos(self, inst_id):
        self.repo_calls.append(inst_id)
        repos = self.repos[inst_id]
        if isinstance(repos, Exception):
            raise repos
        return repos

    def remember_repo(self, full_name, inst_id):
        self.remembered[full_name] = inst_id

    def token_for_repo(self, repo):
        return ORCH_TOKEN

    def fetch_token_for_repo(self, repo):
        return FETCH_TOKEN


def use_fake_auth(monkeypatch, auth=None):
    """Make main.main use auth (by default a FakeAuth with no installations,
    so a pass finds no repositories) instead of an AppAuth that calls GitHub."""
    auth = auth if auth is not None else FakeAuth(FakeAPI(), installations=[])
    monkeypatch.setattr(main, "make_auth", lambda config, api: auth)
    return auth


def make_config(tmp_path, **env):
    # load_config only records the key path; these tests pass a FakeAuth.
    base = {"REVIEWBOT_APP_ID": "1", "REVIEWBOT_APP_PRIVATE_KEY_PATH": str(tmp_path / "app.pem"),
            "REVIEWBOT_STATE_DIR": str(tmp_path / "state"),
            "REVIEWBOT_WORK_DIR": str(tmp_path / "work"), "REVIEWBOT_REVIEWER_UID": "10001"}
    base.update(env)
    return config_mod.load_config(base)


def run_command(cfg, auth, **kw):
    """cmd_run inside the startup session, as main runs it."""
    with main._session(cfg, auth) as ctx:
        return main.cmd_run(ctx, **kw)


def structured(extra_summary=""):
    return {"summary": "Fine." + extra_summary, "verdict": "approve", "comments": [
        {"path": "src/calc.py", "line": 6, "severity": "bug", "body": "inline"},
        {"path": "src/calc.py", "line": 1, "severity": "nit", "body": "moved"},
    ]}


class Harness:
    def __init__(self, tmp_path, monkeypatch, *, stdout=None, review_rc=0, no_files=False,
                 **env):
        self.cfg = make_config(tmp_path, **env)
        self.api = FakeAPI([gh_pr()])
        self.state = State(self.cfg.state_dir / "state.json")
        self.ctx = pipeline.Context(self.cfg, FakeAuth(self.api), self.state, REAL)
        self.fetch_calls, self.review_calls = [], []
        stdout = stdout if stdout is not None else json.dumps(
            {"type": "result", "is_error": False, "structured_output": structured()})

        def fake_fetcher(image, work, pr_json, token, timeout, max_fetch_mb):
            self.fetch_calls.append((work, pr_json, token))
            (work / "repo").mkdir()
            (work / "diff.patch").write_text(fixture_text("small.diff"))
            (work / "files.txt").write_text("" if no_files else fixture_text("small.files.txt"))
            return RunResult(0, "", "", False, 0.1)

        def fake_reviewer(names, work, repo, number, *, uid, gid, env, timeout):
            self.review_calls.append({"work": work, "uid": uid, "gid": gid,
                                      "network": names.internal_network, "env": env,
                                      "files": sorted(p.name for p in work.iterdir())})
            if review_rc:  # what review.run_reviewer raises for a failed run
                raise dockerrun.PullRequestFailure(f"reviewer exit {review_rc}")
            return RunResult(0, stdout, "", False, 0.1)

        self.proxy_events = []

        def fake_start_proxy(names, token, upstream=None):
            self.proxy_events.append(("start", len(self.review_calls), token))
            return names.prefix + "-proxy-test"

        def fake_stop_proxy(name):
            self.proxy_events.append(("stop", len(self.review_calls), name))

        monkeypatch.setattr(fetch, "run_fetcher", fake_fetcher)
        monkeypatch.setattr(review, "run_reviewer", fake_reviewer)
        monkeypatch.setattr(dockerrun, "start_proxy", fake_start_proxy)
        monkeypatch.setattr(dockerrun, "stop_proxy", fake_stop_proxy)

    def pr_json(self):
        return discovery.build_pr_json(gh_pr())


def test_build_pr_json_matches_contract():
    doc = discovery.build_pr_json(gh_pr())
    assert set(doc) == CONTRACT_KEYS
    assert doc["repo"] == "Owner/Repo"
    assert doc["body"] == ""
    assert doc["head_repo"] == "fork/Repo"
    assert doc["labels"] == []


def test_build_pr_json_head_repo_is_null_for_a_deleted_fork():
    pr = gh_pr()
    pr["head"]["repo"] = None  # what GitHub returns once the fork is deleted
    assert discovery.build_pr_json(pr)["head_repo"] is None


def test_skip_reasons():
    assert discovery.skip_reason(gh_pr(), "no-reviewbot") is None
    assert discovery.skip_reason(gh_pr(draft=True), "no-reviewbot") == "draft"
    assert "label" in discovery.skip_reason(gh_pr(labels=["no-reviewbot"]), "no-reviewbot")
    assert discovery.skip_reason(gh_pr(user_type="Bot"), "x") == "bot author"
    assert discovery.skip_reason(gh_pr(login="renovate[bot]"), "x") == "bot author"


def test_parse_pr_ref():
    assert discovery.parse_pr_ref("a-b/c.d#12") == ("a-b/c.d", 12)
    with pytest.raises(ValueError):
        discovery.parse_pr_ref("a/b/c#1")


def test_posts_review(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    out = pipeline.run_and_record(h.ctx, h.pr_json())
    assert out.status == "posted", out.detail
    (path, token, payload), = h.api.posts
    assert path == "/repos/Owner/Repo/pulls/7/reviews"
    assert token == ORCH_TOKEN
    assert payload["event"] == "COMMENT" and payload["commit_id"] == HEAD
    assert payload["comments"] == [{"path": "src/calc.py", "line": 6, "side": "RIGHT",
                                    "body": "**bug**: inline"}]
    assert "- `src/calc.py` line 1: **nit**: moved" in payload["body"]
    assert "verdict: approve" in payload["body"]
    # the fetcher got the scoped token, the reviewer only the dummy
    assert h.fetch_calls[0][2] == FETCH_TOKEN
    rc = h.review_calls[0]
    assert rc["env"] == {"REVIEWBOT_MODEL": config_mod.DEFAULT_MODEL,
                         "REVIEWBOT_MAX_TURNS": str(config_mod.DEFAULT_MAX_TURNS)}
    assert REAL not in json.dumps(rc["env"]) and ORCH_TOKEN not in json.dumps(rc["env"])
    assert (rc["uid"], rc["network"]) == (10001, h.cfg.docker.internal_network)
    assert rc["files"] == ["diff.patch", "files.txt", "pr.json", "repo"]
    # one proxy, with the real token, started before the reviewer and removed after it
    assert h.proxy_events == [("start", 0, REAL), ("stop", 1, "reviewbot-proxy-test")]
    # bookkeeping
    assert h.state.entry("Owner/Repo#7")["status"] == "posted"
    assert h.state.runs_in_last_hour() == 1
    assert list((tmp_path / "work").iterdir()) == []


def test_dry_run_prints_and_does_not_post(tmp_path, monkeypatch, capsys):
    h = Harness(tmp_path, monkeypatch, REVIEWBOT_DRY_RUN="1")
    out = pipeline.run_and_record(h.ctx, h.pr_json())
    assert out.status == "dry_run"
    assert h.api.posts == []
    doc = json.loads(capsys.readouterr().out)
    assert doc["payload"]["event"] == "COMMENT"
    assert h.state.should_skip("Owner/Repo#7", HEAD, dry_run=True, max_attempts=3)
    assert h.state.should_skip("Owner/Repo#7", HEAD, dry_run=False, max_attempts=3) is None


def test_too_long_output_is_failed_and_retried(tmp_path, monkeypatch):
    # A review that is only too long holds no leak; it is recorded failed
    # (retried up to MAX_ATTEMPTS), not refused (final for the head).
    long = {"summary": "a" * 4000, "verdict": "comment",
            "comments": [{"path": "p" * 300, "line": 99, "severity": "nit", "body": "#1" * 750}
                         for _ in range(30)]}
    stdout = json.dumps({"type": "result", "is_error": False, "structured_output": long})
    h = Harness(tmp_path, monkeypatch, stdout=stdout)
    out = pipeline.run_and_record(h.ctx, h.pr_json())
    assert out.status == "failed" and "step 4 (too_long)" in out.detail, out.detail
    assert h.state.entry("Owner/Repo#7")["attempts"] == 1
    assert h.state.should_skip("Owner/Repo#7", HEAD, dry_run=False, max_attempts=2) is None
    out = pipeline.run_and_record(h.ctx, h.pr_json())
    assert out.status == "failed"
    assert h.state.entry("Owner/Repo#7")["attempts"] == 2
    assert h.state.should_skip("Owner/Repo#7", HEAD, dry_run=False, max_attempts=2)
    assert h.api.posts == []


def test_leaking_output_is_refused_and_not_posted(tmp_path, monkeypatch, caplog):
    leak = json.dumps({"type": "result", "is_error": False,
                       "structured_output": structured(" " + REAL)})
    h = Harness(tmp_path, monkeypatch, stdout=leak)
    out = pipeline.run_and_record(h.ctx, h.pr_json())
    assert out.status == "refused" and "step 2" in out.detail
    # only the notice is posted, and it does not hold the leak
    (path, token, payload), = h.api.posts
    assert "withheld its review" in payload["body"] and "comments" not in payload
    assert out.detail not in payload["body"]  # which check fired is not said
    assert REAL not in json.dumps(h.api.posts)
    assert REAL not in caplog.text
    assert h.state.should_skip("Owner/Repo#7", HEAD, dry_run=False, max_attempts=3)


def test_failure_notice_is_posted_only_when_the_head_is_given_up(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, review_rc=1)
    for attempt in range(1, pipeline.MAX_ATTEMPTS + 1):
        out = pipeline.run_and_record(h.ctx, h.pr_json())
        assert out.status == "failed"
        assert len(h.api.posts) == (1 if attempt == pipeline.MAX_ATTEMPTS else 0)
    (path, token, payload), = h.api.posts
    assert path == "/repos/Owner/Repo/pulls/7/reviews"
    assert payload["event"] == "COMMENT" and payload["commit_id"] == HEAD
    assert "comments" not in payload
    assert f"could not review `{HEAD[:12]}`** after {pipeline.MAX_ATTEMPTS} attempts" \
        in payload["body"]
    assert "reviewer exit 1" in payload["body"]


def test_no_failure_notice_in_dry_run(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    h = Harness(tmp_path, monkeypatch, review_rc=1, REVIEWBOT_DRY_RUN="1")
    for _ in range(pipeline.MAX_ATTEMPTS):
        pipeline.run_and_record(h.ctx, h.pr_json())
    assert h.api.posts == []
    assert "dry run: not posting on Owner/Repo#7: **reviewbot could not review" in caplog.text


def test_no_failure_notice_for_an_unexpected_exception(tmp_path, monkeypatch):
    # Its text was not written by reviewbot, so it is not quoted on the PR.
    h = Harness(tmp_path, monkeypatch)

    def broken(*a, **kw):
        raise RuntimeError("internal detail")
    monkeypatch.setattr(pipeline.post, "build_payload", broken)
    for _ in range(pipeline.MAX_ATTEMPTS):
        assert pipeline.run_and_record(h.ctx, h.pr_json()).status == "failed"
    assert h.api.posts == []


def test_failure_notice_that_cannot_be_posted_keeps_the_outcome(tmp_path, monkeypatch, caplog):
    h = Harness(tmp_path, monkeypatch, review_rc=1)

    def request(*a, **kw):
        raise GitHubError(403, "Resource not accessible by integration")
    h.api.request = request
    for _ in range(pipeline.MAX_ATTEMPTS):
        out = pipeline.run_and_record(h.ctx, h.pr_json())
    assert out.status == "failed" and out.detail == "reviewer exit 1"
    assert h.state.entry("Owner/Repo#7")["attempts"] == pipeline.MAX_ATTEMPTS
    assert "could not post the failure notice on Owner/Repo#7" in caplog.text


@pytest.mark.parametrize("kind, status, level", [
    ("good", "posted", "INFO"),
    ("leak", "refused", "ERROR"),
    ("reviewer exit", "failed", "ERROR"),
])
def test_each_outcome_is_logged_once_at_its_level(tmp_path, monkeypatch, caplog, kind, status,
                                                  level):
    caplog.set_level(logging.INFO)
    leak = json.dumps({"type": "result", "is_error": False,
                       "structured_output": structured(" " + REAL)})
    h = Harness(tmp_path, monkeypatch, stdout=leak if kind == "leak" else None,
                review_rc=1 if kind == "reviewer exit" else 0)
    assert pipeline.run_and_record(h.ctx, h.pr_json()).status == status
    prefix = f"Owner/Repo#7 at {HEAD[:12]}: "
    lines = [(r.levelname, r.getMessage()[len(prefix):].split(" ", 1)[0])
             for r in caplog.records if r.getMessage().startswith(prefix)]
    assert lines == [(level, status)]
    # and no other line repeats the outcome
    assert "posted review" not in caplog.text.replace(prefix + "posted review", "")
    assert "nothing posted" not in caplog.text and "review of Owner/Repo#7" not in caplog.text


def test_reviewer_failure_counts_against_cap(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, review_rc=1)
    out = pipeline.run_and_record(h.ctx, h.pr_json())
    assert out.status == "failed"
    assert h.api.posts == []
    assert h.state.runs_in_last_hour() == 1


def test_rejected_inline_comments_are_reposted_in_the_body(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    sent = []
    post_once = h.api.request

    def request(method, path, token, json=None, **kw):
        sent.append(json)
        if len(sent) == 1:
            raise GitHubError(422, "Unprocessable Entity")
        return post_once(method, path, token, json=json, **kw)
    h.api.request = request
    out = pipeline.run_and_record(h.ctx, h.pr_json())
    assert out.status == "posted", out.detail
    assert sent[0]["comments"] and sent[1]["comments"] == []
    assert "Comments outside the diff" in sent[1]["body"]


def test_closed_pull_request_is_forgotten_and_reviewed_again_when_reopened(tmp_path,
                                                                         monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    open_prs = list(h.api.prs)
    assert [k for k, _ in pipeline.one_pass(h.ctx).reviews] == ["Owner/Repo#7"]
    assert pipeline.one_pass(h.ctx).reviews == []  # same head, already posted
    h.api.prs = []  # closed
    pipeline.one_pass(h.ctx)
    assert h.state.entry("Owner/Repo#7") is None
    assert State.load(h.cfg.state_dir / "state.json").entry("Owner/Repo#7") is None
    h.api.prs = open_prs  # reopened at the same head
    assert [(k, o.status) for k, o in pipeline.one_pass(h.ctx).reviews] == [
        ("Owner/Repo#7", "posted")]
    assert len(h.api.posts) == 2


@pytest.mark.parametrize("failure", ["installation", "pull requests"])
def test_entries_of_a_repository_that_could_not_be_listed_are_kept(tmp_path, monkeypatch,
                                                                    failure):
    h = Harness(tmp_path, monkeypatch)
    pipeline.one_pass(h.ctx)
    assert h.state.entry("Owner/Repo#7")["status"] == "posted"
    if failure == "installation":
        h.ctx.auth = FakeAuth(h.api, repos={1: GitHubError(502, "Bad Gateway")})
    else:
        def paginate(path, token, params=None, item_key=None):
            raise GitHubError(502, "Bad Gateway")
        h.api.paginate = paginate
    assert pipeline.one_pass(h.ctx).listing_failures
    assert h.state.entry("Owner/Repo#7")["status"] == "posted"


def test_one_pass_respects_cap_and_skips(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, REVIEWBOT_MAX_REVIEWS_PER_HOUR="2")
    h.api.prs = [gh_pr(1, head="a" * 40), gh_pr(2, head="b" * 40, draft=True),
                 gh_pr(3, head="c" * 40), gh_pr(4, head="d" * 40),
                 gh_pr(5, head="e" * 40, user_type="Bot")]
    h.state.record("Owner/Repo#1", "a" * 40, "posted")
    results = pipeline.one_pass(h.ctx).reviews
    assert [k for k, _ in results] == ["Owner/Repo#3", "Owner/Repo#4"]
    assert len(h.api.posts) == 2
    # cap is now full: a second pass reviews nothing and records nothing
    h.api.prs.append(gh_pr(6, head="f" * 40))
    assert [(k, o.status) for k, o in pipeline.one_pass(h.ctx).reviews] == [
        ("Owner/Repo#6", "capped")]
    assert len(h.api.posts) == 2
    assert h.state.entry("Owner/Repo#6") is None


@pytest.mark.parametrize("draft, posted, reason", [
    (True, False, "draft"),
    (False, True, f"head {HEAD[:12]} already posted"),
])
def test_review_command_reviews_what_the_poll_loop_would_skip(tmp_path, monkeypatch, caplog,
                                                               draft, posted, reason):
    caplog.set_level(logging.INFO)
    h = Harness(tmp_path, monkeypatch)
    h.api.prs = [gh_pr(7, draft=draft)]
    if posted:
        h.state.record("Owner/Repo#7", HEAD, "posted")
    assert main.cmd_review(h.ctx, "Owner/Repo#7") == 0
    assert len(h.api.posts) == 1
    assert f"Owner/Repo#7 would be skipped by the poll loop ({reason})" in caplog.text


def test_review_command_respects_the_hourly_cap(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    h = Harness(tmp_path, monkeypatch, REVIEWBOT_MAX_REVIEWS_PER_HOUR="1")
    h.state.note_review_started()
    assert main.cmd_review(h.ctx, "Owner/Repo#7") == 1
    assert h.fetch_calls == [] and h.api.posts == []
    assert h.state.entry("Owner/Repo#7") is None
    assert "Owner/Repo#7 at 111111111111: capped hourly cap of 1 reached" in caplog.text


def test_pr_without_changed_files_is_not_reviewed(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, no_files=True)
    out = pipeline.run_and_record(h.ctx, h.pr_json())
    assert out.status == "skipped"
    assert h.review_calls == [] and h.api.posts == [] and h.proxy_events == []
    assert h.state.runs_in_last_hour() == 0
    assert h.state.should_skip("Owner/Repo#7", HEAD, dry_run=False, max_attempts=3)


def test_work_dir_is_new_empty_and_world_readable(tmp_path):
    # The fetcher refuses a non-empty /work, and the reviewer's uid needs o+rx.
    import stat
    a = pipeline.make_work_dir(tmp_path / "w", "o/r", 1, HEAD)
    b = pipeline.make_work_dir(tmp_path / "w", "o/r", 1, HEAD)
    assert a != b
    assert list(a.iterdir()) == []
    assert stat.S_IMODE(a.stat().st_mode) == 0o755


def test_suspended_or_failing_installation_does_not_stop_the_pass(caplog):
    # 1 is suspended (GitHub refuses its token), 2 fails with a server error, 3 works.
    auth = FakeAuth(FakeAPI([gh_pr()]), installations=[
        {"id": 1, "account": {"login": "gone"}, "suspended_at": "2026-10-01T00:00:00Z"},
        {"id": 2, "account": {"login": "flaky"}, "suspended_at": None},
        {"id": 3, "account": {"login": "Owner"}, "suspended_at": None},
    ], repos={
        1: GitHubError(403, "POST /app/installations/1/access_tokens: This installation has been suspended"),
        2: GitHubError(502, "POST /app/installations/2/access_tokens: Bad Gateway"),
        3: [{"full_name": "Owner/Repo"}],
    })
    listing = discovery.candidates(auth)
    cands, failures = listing.candidates, listing.failures
    assert [c.key for c in cands] == ["Owner/Repo#7"]
    assert auth.repo_calls == [2, 3]  # no token is requested for the suspended one
    # a suspended installation is expected and not a failure
    assert failures == ["installation 2 (flaky): GitHub API error 502: "
                        "POST /app/installations/2/access_tokens: Bad Gateway"]
    assert "installation 1 (gone) is suspended" in caplog.text
    assert "installation 2 (flaky) failed" in caplog.text


class RotatingAuth(FakeAuth):
    """token_for_repo returns a new token on each call, as an app's cached
    installation token does once it is refreshed. The tokens have no GitHub
    prefix, so only the literal check for the tokens in use can catch them."""

    def __init__(self, api, tokens):
        super().__init__(api)
        self.tokens = list(tokens)
        self.calls = 0

    def token_for_repo(self, repo):
        token = self.tokens[min(self.calls, len(self.tokens) - 1)]
        self.calls += 1
        return token


FIRST_TOKEN = "firstInstallationToken0123456789abcdef"
SECOND_TOKEN = "secondInstallationToken0123456789abcdef"


def test_post_uses_a_token_requested_after_the_review(tmp_path, monkeypatch):
    """The token from the start of the review may have expired by the time
    of the post."""
    h = Harness(tmp_path, monkeypatch)
    h.ctx.auth = RotatingAuth(h.api, [FIRST_TOKEN, SECOND_TOKEN])
    out = pipeline.run_and_record(h.ctx, h.pr_json())
    assert out.status == "posted", out.detail
    assert h.ctx.auth.calls == 2
    (path, token, payload), = h.api.posts
    assert token == SECOND_TOKEN


@pytest.mark.parametrize("token", [FIRST_TOKEN, SECOND_TOKEN])
def test_validation_knows_both_orchestrator_tokens(tmp_path, monkeypatch, token):
    leak = json.dumps({"type": "result", "is_error": False,
                       "structured_output": structured(" " + token)})
    h = Harness(tmp_path, monkeypatch, stdout=leak)
    h.ctx.auth = RotatingAuth(h.api, [FIRST_TOKEN, SECOND_TOKEN])
    out = pipeline.run_and_record(h.ctx, h.pr_json())
    assert out.status == "refused" and "step 3" in out.detail
    assert "GitHub token in use" in out.detail
    (path, posted_with, payload), = h.api.posts
    assert "withheld its review" in payload["body"] and "comments" not in payload
    assert token not in json.dumps(payload)
