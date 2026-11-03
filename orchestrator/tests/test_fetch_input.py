"""What the fetcher receives, and pr.json in the work directory.

The fetcher gets only repo, number, base_sha and head_sha in PR_JSON. The
orchestrator writes the full pr.json into WORK after the fetcher succeeds,
because a pull request body can exceed the 128 KiB Linux allows for one
environment string.

These tests run the real pipeline, fetch and docker-run code against the
fake docker binary in fakedocker.py.
"""

import json
import os
import stat

import pytest
from fakedocker import install
from test_pipeline import REAL, FakeAPI, FakeAuth, gh_pr, make_config, structured

from reviewbot import discovery, dockerrun, fetch, pipeline
from reviewbot.diffmap import number_diff
from reviewbot.state import State

GOOD = json.dumps({"type": "result", "is_error": False, "structured_output": structured()})
MAX_ARG_STRLEN = 128 * 1024  # Linux limit on one environment string
SMALL = 512  # every environment value docker receives must be shorter


def context(tmp_path, pr, **env):
    cfg = make_config(tmp_path, **env)
    state = State(cfg.state_dir / "state.json")
    return pipeline.Context(cfg, FakeAuth(FakeAPI([pr])), state, REAL), state


def test_large_body_reaches_work_and_not_the_fetcher_environment(tmp_path, monkeypatch):
    fd = install(tmp_path, monkeypatch, review_stdout=GOOD)
    # a short PATH, so every value in the docker client environment is small
    monkeypatch.setenv("PATH", f"{fd.bindir}:/usr/bin:/bin")
    pr = gh_pr()
    pr["body"] = "中" * 25000 + "\U0001f600" * 1000 + ' "quoted" \\ line\n' * 100
    doc = discovery.build_pr_json(pr)
    assert len(json.dumps(doc).encode()) > MAX_ARG_STRLEN
    ctx, state = context(tmp_path, pr, REVIEWBOT_KEEP_WORK="1")
    old_umask = os.umask(0o077)  # reviewbot.service runs with UMask=0077
    try:
        out = pipeline.run_and_record(ctx, doc)
    finally:
        os.umask(old_umask)
    assert out.status == "posted", out.detail

    calls = fd.envs()
    fetches = [env for args, env in calls if args[0] == "run" and
               args[args.index("--name") + 1].startswith("reviewbot-fetch-")]
    assert len(fetches) == 1
    assert json.loads(fetches[0]["PR_JSON"]) == {k: doc[k] for k in
                                                  ("repo", "number", "base_sha", "head_sha")}
    for args, env in calls:
        long_values = {k: len(v.encode()) for k, v in env.items() if len(v.encode()) >= SMALL}
        assert long_values == {}, (args[:2], long_values)

    [work] = list(ctx.config.work_dir.iterdir())
    written = work / "pr.json"
    assert json.loads(written.read_text("utf-8")) == doc  # the body intact
    assert stat.S_IMODE(os.lstat(written).st_mode) == 0o644


VALID = discovery.build_pr_json(gh_pr())
def test_pr_json_written_by_the_fetcher_is_refused(tmp_path):
    work = tmp_path / "w"
    work.mkdir()
    (work / "pr.json").write_text("{}")
    with pytest.raises(fetch.FetchError, match="already exists"):
        fetch.write_json(work, "pr.json", VALID)


def test_pr_json_is_not_written_through_a_symlink(tmp_path):
    work = tmp_path / "w"
    work.mkdir()
    target = tmp_path / "target"
    (work / "pr.json").symlink_to(target)
    with pytest.raises(fetch.FetchError):
        fetch.write_json(work, "pr.json", VALID)
    assert not target.exists()


def test_pr_json_that_cannot_be_written_is_an_environment_failure(tmp_path):
    # Not the pull request's fault: a missing work directory here, a full
    # disk or a permission problem in practice.
    with pytest.raises(dockerrun.EnvironmentFailure, match="cannot write pr.json"):
        fetch.write_json(tmp_path / "missing", "pr.json", VALID)


def one_char_line_diff(lines: int) -> str:
    """A new file of `lines` one-character lines: each 2-byte "+" line gains
    its line number in the numbered diff."""
    return ("diff --git a/f b/f\nnew file mode 100644\n--- /dev/null\n"
            f"+++ b/f\n@@ -0,0 +1,{lines} @@\n" + "+\n" * lines)


def test_numbered_diff_over_the_cap_is_too_large_and_removed(tmp_path):
    diff = one_char_line_diff(20000)
    numbered = "".join(number_diff(diff))
    assert len(numbered) > 3 * len(diff)
    with pytest.raises(fetch.TooLarge, match="numbered_diff.txt would be over"):
        fetch.write_text(tmp_path, "numbered_diff.txt", number_diff(diff), 2 * len(diff))
    assert not os.path.lexists(tmp_path / "numbered_diff.txt")
    # exactly at the cap is written whole
    fetch.write_text(tmp_path, "numbered_diff.txt", number_diff(diff), len(numbered))
    assert (tmp_path / "numbered_diff.txt").read_text("utf-8") == numbered


def test_capped_write_stops_reading_at_the_cap(tmp_path):
    def chunks():
        yield from ["x" * 10] * 3
        raise AssertionError("read past the cap")
    with pytest.raises(fetch.TooLarge):
        fetch.write_text(tmp_path, "out.txt", chunks(), 25)


def test_fetcher_does_not_write_the_orchestrators_files():
    here = os.path.dirname(__file__)
    with open(os.path.join(here, "..", "..", "fetcher", "entrypoint.sh"), encoding="utf-8") as fh:
        script = fh.read()
    assert '"$WORK/pr.json"' not in script
    assert "$WORK/previous_review.json" not in script


def test_previous_review_written_by_the_fetcher_is_refused(tmp_path):
    work = tmp_path / "w"
    work.mkdir()
    (work / "previous_review.json").write_text("{}")
    with pytest.raises(fetch.FetchError, match="previous_review.json already exists"):
        fetch.write_json(work, "previous_review.json", {"head_sha": "a" * 40})


def test_fetcher_gets_prev_sha_of_the_last_posted_review(tmp_path, monkeypatch):
    fd = install(tmp_path, monkeypatch, review_stdout=GOOD)
    pr = gh_pr()
    doc = discovery.build_pr_json(pr)
    ctx, state = context(tmp_path, pr)
    earlier = "e" * 40
    state.record(discovery.state_key(doc), earlier, "posted", review=structured())
    assert pipeline.run_and_record(ctx, doc).status == "posted"
    [fetch_env] = [env for args, env in fd.envs() if args[0] == "run" and
                   args[args.index("--name") + 1].startswith("reviewbot-fetch-")]
    assert json.loads(fetch_env["PR_JSON"]) == {
        **{k: doc[k] for k in ("repo", "number", "base_sha", "head_sha")}, "prev_sha": earlier}
