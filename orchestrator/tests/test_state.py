import json

import pytest

from reviewbot.state import HOUR, State, StateLock, StateLockedError

T0 = 1_800_000_000.0


def test_forget_closed_removes_only_listed_repositories_closed_entries(tmp_path):
    s = State(tmp_path / "state.json")
    for key in ("Owner/Repo#1", "Owner/Repo#2", "Other/Repo#3"):
        s.record(key, "a" * 40, "posted", now=T0)
    # Owner/Repo was listed (in another case) with only #1 open; Other/Repo
    # could not be listed.
    assert s.forget_closed({"Owner/Repo#1"}, ["owner/repo"]) == ["Owner/Repo#2"]
    assert set(s.data["prs"]) == {"Owner/Repo#1", "Other/Repo#3"}


def test_cap_counts_runs_in_the_last_hour(tmp_path):
    s = State(tmp_path / "state.json")
    for i in range(3):
        assert s.can_start_review(3, now=T0 + i)
        s.note_review_started(now=T0 + i)
    assert not s.can_start_review(3, now=T0 + 10)
    assert s.runs_in_last_hour(now=T0 + 10) == 3
    # the first run ages out exactly one hour after it started
    assert s.seconds_until_slot(3, now=T0 + 10) == pytest.approx(HOUR - 10)
    assert not s.can_start_review(3, now=T0 + HOUR - 0.5)
    assert s.can_start_review(3, now=T0 + HOUR)
    assert s.runs_in_last_hour(now=T0 + HOUR + 2) == 0


def test_cap_zero_blocks_everything(tmp_path):
    s = State(tmp_path / "state.json")
    assert not s.can_start_review(0, now=T0)
    assert s.seconds_until_slot(0, now=T0) == float("inf")


def test_seconds_until_slot_with_more_runs_than_cap(tmp_path):
    s = State(tmp_path / "state.json")
    for t in (0, 100, 200, 300):
        s.note_review_started(now=T0 + t)
    # cap lowered to 2 after four runs: the count must fall to 1, so the runs
    # at 0, 100 and 200 must expire; the last of those expires at T0+200+HOUR
    assert s.seconds_until_slot(2, now=T0 + 400) == pytest.approx(200 + HOUR - 400)
    assert not s.can_start_review(2, now=T0 + 200 + HOUR - 1)
    assert s.can_start_review(2, now=T0 + 200 + HOUR)


def test_should_skip(tmp_path):
    s = State(tmp_path / "state.json")
    k = "o/r#1"
    assert s.should_skip(k, "a" * 40, dry_run=False, max_attempts=3) is None
    s.record(k, "a" * 40, "posted", now=T0)
    assert s.should_skip(k, "a" * 40, dry_run=False, max_attempts=3)
    assert s.should_skip(k, "b" * 40, dry_run=False, max_attempts=3) is None
    s.record(k, "b" * 40, "dry_run", now=T0)
    assert s.should_skip(k, "b" * 40, dry_run=True, max_attempts=3)
    assert s.should_skip(k, "b" * 40, dry_run=False, max_attempts=3) is None
    s.record(k, "c" * 40, "failed", now=T0)
    s.record(k, "c" * 40, "failed", now=T0)
    assert s.entry(k)["attempts"] == 2
    assert s.should_skip(k, "c" * 40, dry_run=False, max_attempts=3) is None
    s.record(k, "c" * 40, "failed", now=T0)
    assert s.should_skip(k, "c" * 40, dry_run=False, max_attempts=3)
    s.record(k, "d" * 40, "refused", now=T0)
    assert s.entry(k)["attempts"] == 1
    assert s.should_skip(k, "d" * 40, dry_run=False, max_attempts=3)


# Only consecutive failed records at the same head are attempts.

def test_dry_run_records_do_not_count_as_attempts(tmp_path):
    s = State(tmp_path / "state.json")
    k, head = "o/r#1", "a" * 40
    for _ in range(3):
        s.record(k, head, "dry_run", now=T0)
    s.record(k, head, "failed", now=T0)
    assert s.entry(k)["attempts"] == 1
    assert s.should_skip(k, head, dry_run=False, max_attempts=3) is None


def test_consecutive_failures_count(tmp_path):
    s = State(tmp_path / "state.json")
    k, head = "o/r#1", "a" * 40
    s.record(k, head, "failed", now=T0)
    s.record(k, head, "failed", now=T0)
    assert s.entry(k)["attempts"] == 2


def test_posted_record_does_not_count_as_an_attempt(tmp_path):
    # reviewbot review can run again on a posted head
    s = State(tmp_path / "state.json")
    k, head = "o/r#1", "a" * 40
    s.record(k, head, "posted", now=T0)
    s.record(k, head, "failed", now=T0)
    assert s.entry(k)["attempts"] == 1


def test_round_trip(tmp_path):
    p = tmp_path / "sub" / "state.json"
    s = State(p)
    s.record("o/r#2", "e" * 40, "posted", "review 5", now=T0)
    s.note_review_started(now=T0)
    s.save()
    raw = json.loads(p.read_text())
    assert raw["prs"]["o/r#2"]["head_sha"] == "e" * 40
    assert raw["prs"]["o/r#2"]["at"] == T0
    loaded = State.load(p)
    assert loaded.entry("o/r#2")["status"] == "posted"
    assert loaded.runs_in_last_hour(now=T0 + 1) == 1
    assert [x.name for x in p.parent.iterdir()] == ["state.json"]  # no temp files left


def test_load_rejects_unknown_format(tmp_path):
    p = tmp_path / "state.json"
    p.write_text('{"version": 2}')
    with pytest.raises(ValueError):
        State.load(p)


def test_lock_is_exclusive(tmp_path):
    with StateLock(tmp_path):
        with pytest.raises(StateLockedError):
            with StateLock(tmp_path):
                pass
    with StateLock(tmp_path):
        pass
