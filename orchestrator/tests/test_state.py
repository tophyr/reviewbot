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

    def skip(head, dry_run=False):
        return s.should_skip(k, head * 40, dry_run=dry_run, max_attempts=3)

    assert skip("a") is None  # never seen
    s.record(k, "a" * 40, "posted", now=T0)
    assert "already posted" in skip("a")
    assert "only on request" in skip("b")  # a push after a review
    s.request_review(k, 5, "anyone")
    assert skip("b") is None
    assert "already posted" in skip("a")  # a request never repeats a reviewed head
    s.record(k, "b" * 40, "posted", now=T0)
    assert "only on request" in skip("c")  # the posted review used the request up
    # A review that did not finish is retried, at the same head up to
    # max_attempts and at a new head without a request.
    s.record(k, "c" * 40, "failed", now=T0)
    s.record(k, "c" * 40, "failed", now=T0)
    assert s.entry(k)["attempts"] == 2
    assert skip("c") is None and skip("d") is None
    s.record(k, "c" * 40, "failed", now=T0)
    assert "failed 3 times" in skip("c")
    assert skip("d") is None
    # skipped: the reviewer never ran, because the head changed no files
    s.record(k, "e" * 40, "skipped", now=T0)
    assert "already skipped" in skip("e")
    assert skip("f") is None
    s.record(k, "g" * 40, "dry_run", now=T0)
    assert skip("g", dry_run=True) and "only on request" in skip("h", dry_run=True)
    assert skip("g") is None and skip("h") is None  # turning dry run off reviews for real
    s.record(k, "i" * 40, "refused", now=T0)
    assert s.entry(k)["attempts"] == 1
    assert "already refused" in skip("i") and "only on request" in skip("j")


def test_request_is_kept_through_failures_only(tmp_path):
    s = State(tmp_path / "state.json")
    k = "o/r#1"
    s.record(k, "a" * 40, "posted", now=T0)
    s.request_review(k, 5, "anyone")
    s.record(k, "b" * 40, "failed", now=T0)
    assert s.entry(k)["requested"] == {"comment_id": 5, "by": "anyone"}
    s.record(k, "b" * 40, "refused", now=T0)
    assert "requested" not in s.entry(k)


def test_comment_positions(tmp_path):
    s = State(tmp_path / "state.json")
    assert s.comment_position("O/R") == (None, 0)
    for repo in ("O/R", "O/Quiet", "O/Unlisted"):
        s.set_comment_position(repo, "2026-10-31T20:00:00Z", 9)
    assert s.comment_position("o/r") == ("2026-10-31T20:00:00Z", 9)
    # O/Quiet was listed with no open pull request; O/Unlisted could not be listed
    s.forget_comment_positions({"O/R"}, ["O/R", "O/Quiet"])
    assert sorted(s.data["repos"]) == ["o/r", "o/unlisted"]


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


def test_last_posted_is_kept_until_the_next_posted_review(tmp_path):
    s = State(tmp_path / "state.json")
    k = "o/r#1"
    assert s.last_posted(k) is None
    s.record(k, "a" * 40, "posted", now=T0)  # no review given: nothing to keep
    assert s.last_posted(k) is None
    first = {"summary": "one", "verdict": "comment", "comments": []}
    s.record(k, "a" * 40, "posted", now=T0, review=first)
    for status in ("failed", "dry_run", "refused", "skipped"):
        s.record(k, "b" * 40, status, now=T0)
        assert s.last_posted(k) == {"head_sha": "a" * 40, "review": first}, status
    second = {"summary": "two", "verdict": "approve", "comments": []}
    s.record(k, "c" * 40, "posted", now=T0, review=second)
    assert s.last_posted(k) == {"head_sha": "c" * 40, "review": second}
    s.save()
    assert State.load(tmp_path / "state.json").last_posted(k) == {"head_sha": "c" * 40,
                                                                 "review": second}
    s.forget_closed(set(), ["o/r"])
    assert s.last_posted(k) is None
