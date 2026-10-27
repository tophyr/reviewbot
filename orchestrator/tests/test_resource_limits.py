"""Bounds on what one fetch can write.

- The fetcher works in a /scratch tmpfs of MAX_FETCH_MB with
  MAX_FETCH_MB * 256 inodes and
  gets --memory of that size plus FETCH_MEMORY_HEADROOM_MB, because tmpfs
  pages are charged to the container's memory cgroup.
- A fetch does not start while the filesystem of the work directory has less
  than MIN_FREE_MB free: an environment failure, so the pass ends
  and nothing is recorded.

The fake docker binary (fakedocker.py) records every call, so the tests can
assert what the fetcher was started with, or that it was not started.
"""

import json
import os

import pytest
from fakedocker import install
from test_pipeline import REAL, FakeAPI, FakeAuth, gh_pr, make_config, structured

from reviewbot import fetch, pipeline
from reviewbot.state import State

GOOD = json.dumps({"type": "result", "is_error": False, "structured_output": structured()})
KEY = "Owner/Repo#7"


def setup(tmp_path, **env):
    cfg = make_config(tmp_path, **env)
    api = FakeAPI([gh_pr()])
    state = State(cfg.state_dir / "state.json")
    return pipeline.Context(cfg, FakeAuth(api), state, REAL), api, state


def option(argv, flag):
    return [argv[i + 1] for i, a in enumerate(argv) if a == flag]


@pytest.mark.parametrize("mb", [1, 1024, 5000])
def test_fetcher_argv_has_a_sized_scratch_tmpfs_and_memory_above_it(tmp_path, mb):
    argv = fetch.build_argv("reviewbot-fetcher", "n", tmp_path, mb)
    assert option(argv, "--tmpfs") == [f"/scratch:rw,size={mb}m,nr_inodes={mb * 256}",
                                       "/log:rw,size=16m"]
    assert option(argv, "--mount") == []
    assert option(argv, "--memory") == [f"{mb + fetch.FETCH_MEMORY_HEADROOM_MB}m"]
    assert fetch.FETCH_MEMORY_HEADROOM_MB >= 512
    assert argv.index("--tmpfs") < argv.index("reviewbot-fetcher")


def test_configured_size_reaches_the_fetcher(tmp_path, monkeypatch):
    fd = install(tmp_path, monkeypatch, review_stdout=GOOD)
    monkeypatch.setattr(fetch, "MAX_FETCH_MB", 300)
    ctx, api, state = setup(tmp_path)
    assert [o.status for _, o in pipeline.one_pass(ctx).reviews] == ["posted"]
    (run,) = [c for c in fd.calls() if c[0] == "run" and "reviewbot-fetch-" in " ".join(c)]
    assert option(run, "--tmpfs") == ["/scratch:rw,size=300m,nr_inodes=76800",
                                      "/log:rw,size=16m"]
    assert option(run, "--memory") == ["1324m"]


def test_low_free_space_ends_the_pass_unrecorded(tmp_path, monkeypatch):
    fd = install(tmp_path, monkeypatch, review_stdout=GOOD)
    st = os.statvfs(tmp_path)
    free_mb = st.f_bavail * st.f_frsize // (1024 * 1024)
    monkeypatch.setattr(pipeline, "MIN_FREE_MB", free_mb + 1024 * 1024)
    ctx, api, state = setup(tmp_path)
    for _ in range(pipeline.MAX_ATTEMPTS + 1):
        results = pipeline.one_pass(ctx).reviews
        assert [(k, o.status) for k, o in results] == [(KEY, "environment")]
        assert "MB free on the filesystem" in results[0][1].detail
    assert fd.runs("fetch") == [] and fd.runs("review") == []
    assert state.data["prs"] == {} and state.runs_in_last_hour() == 0
    assert list(ctx.config.work_dir.iterdir()) == []
    assert api.posts == []


def test_enough_free_space_fetches(tmp_path, monkeypatch):
    fd = install(tmp_path, monkeypatch, review_stdout=GOOD)
    monkeypatch.setattr(pipeline, "MIN_FREE_MB", 2)
    ctx, api, state = setup(tmp_path)
    assert [o.status for _, o in pipeline.one_pass(ctx).reviews] == ["posted"]
    assert len(fd.runs("fetch")) == 1


def test_statvfs_failure_is_an_environment_failure(tmp_path, monkeypatch):
    fd = install(tmp_path, monkeypatch, review_stdout=GOOD)

    def broken(path):
        raise OSError(5, "Input/output error")
    monkeypatch.setattr(pipeline.os, "statvfs", broken)
    ctx, api, state = setup(tmp_path)
    assert [o.status for _, o in pipeline.one_pass(ctx).reviews] == ["environment"]
    assert fd.runs("fetch") == []
    assert state.data["prs"] == {}
