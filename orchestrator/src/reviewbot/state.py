"""Persistent state: last reviewed head sha per PR, review requests, the
comment polling position per repository, and the hourly cap.

The file is JSON:

    {
      "version": 1,
      "prs": {
        "owner/name#12": {
          "head_sha": "...", "status": "posted", "at": 1761284000.0,
          "attempts": 1, "detail": "...",
          "last_posted": {"head_sha": "...", "review": {...}},
          "requested": {"comment_id": 123, "by": "login"}
        }
      },
      "repos": {
        "owner/name": {"since": "2026-10-31T20:19:35Z", "seen": [123, ...]}
      },
      "runs": [1761284000.0, ...]
    }

"status" is one of posted, dry_run, refused, failed, skipped (the PR
changes no files, so the reviewer was not run). A PR without an entry is
reviewed. A PR with one is reviewed again only when "requested" is set, or
when its last review did not finish (failed, skipped, or dry_run outside dry
run), and never at a head sha whose status is final for the current mode
(see State.should_skip). "requested" is a review request comment on the pull
request; it is kept through failed records and cleared by any other. An
entry is removed once its pull request is closed (State.forget_closed). "last_posted" is the most recent
review posted on the pull request: the head sha it reviewed and the
reviewer's structured output (summary, verdict, comments). Later records of
other outcomes keep it. "repos" holds, per repository with open pull
requests, the newest comment updated_at seen ("since") and the ids of the
comments that the next read lists again ("seen"), which it skips (see
pipeline.take_review_requests). "runs" holds the start time of every
reviewer run in the last hour; each one counts against
REVIEWBOT_MAX_REVIEWS_PER_HOUR whether or not it produced a review, because
each one uses model quota.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
from pathlib import Path

HOUR = 3600.0
FINAL_STATUSES = {"posted", "refused", "skipped"}


class StateLockedError(Exception):
    pass


class State:
    def __init__(self, path: Path, data: dict | None = None):
        self.path = Path(path)
        self.data = data or {"version": 1, "prs": {}, "repos": {}, "runs": []}
        self.data.setdefault("prs", {})
        self.data.setdefault("repos", {})
        self.data.setdefault("runs", [])

    @classmethod
    def load(cls, path: Path) -> "State":
        path = Path(path)
        if not path.exists():
            return cls(path)
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ValueError(f"unrecognised state file format in {path}")
        return cls(path, data)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".state-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    # per-PR records

    def entry(self, key: str) -> dict | None:
        return self.data["prs"].get(key)

    def head_done(self, key: str, head_sha: str, *, dry_run: bool,
                  max_attempts: int) -> str | None:
        """Why this head sha is not reviewed again, even on request, or None."""
        e = self.entry(key)
        if not e or e.get("head_sha") != head_sha:
            return None
        status = e.get("status")
        if status in FINAL_STATUSES:
            return f"head {head_sha[:12]} already {status}"
        if status == "dry_run" and dry_run:
            return f"head {head_sha[:12]} already reviewed in dry run"
        if status == "failed" and e.get("attempts", 0) >= max_attempts:
            return f"head {head_sha[:12]} failed {e.get('attempts')} times"
        return None

    def should_skip(self, key: str, head_sha: str, *, dry_run: bool,
                    max_attempts: int) -> str | None:
        """Reason to skip this PR at this head sha, or None to review it."""
        why = self.head_done(key, head_sha, dry_run=dry_run, max_attempts=max_attempts)
        if why:
            return why
        e = self.entry(key)
        if not e or e.get("requested"):
            return None
        status = e.get("status")
        if status in ("failed", "skipped") or (status == "dry_run" and not dry_run):
            return None
        return f"reviewed at {e.get('head_sha', '')[:12]}; reviewed again only on request"

    def request_review(self, key: str, comment_id: int, login: str) -> None:
        """Mark the PR, which must have an entry, for one more review."""
        self.data["prs"][key]["requested"] = {"comment_id": comment_id, "by": login}

    def last_posted(self, key: str) -> dict | None:
        """{"head_sha", "review"} of the most recent posted review, or None."""
        return (self.entry(key) or {}).get("last_posted")

    def record(self, key: str, head_sha: str, status: str, detail: str = "",
               now: float | None = None, review: dict | None = None) -> None:
        """review: the structured output posted for head_sha, which replaces
        last_posted; without it the previous last_posted is kept."""
        now = time.time() if now is None else now
        prev = self.entry(key) or {}
        # Attempts count consecutive failed records at the same head only, so
        # dry_run or other records before them do not use up retries.
        same_failed = prev.get("head_sha") == head_sha and prev.get("status") == "failed"
        attempts = prev.get("attempts", 0) + 1 if same_failed else 1
        entry = {
            "head_sha": head_sha,
            "status": status,
            "at": now,
            "attempts": attempts,
            "detail": detail[:500],
        }
        last = {"head_sha": head_sha, "review": review} if review is not None \
            else prev.get("last_posted")
        if last is not None:
            entry["last_posted"] = last
        if status == "failed" and prev.get("requested"):
            entry["requested"] = prev["requested"]
        self.data["prs"][key] = entry

    def forget_closed(self, open_keys: set[str], listed_repos: list[str]) -> list[str]:
        """Remove the entries of pull requests in listed_repos that are not in
        open_keys, so the file does not grow forever and a reopened pull
        request is reviewed again. Repositories that could not be listed keep
        their entries. Returns the removed keys."""
        repos = {r.lower() for r in listed_repos}
        gone = [k for k in self.data["prs"]
                if k.rpartition("#")[0].lower() in repos and k not in open_keys]
        for k in gone:
            del self.data["prs"][k]
        return gone

    # comment polling position

    def comment_position(self, repo: str) -> tuple[str | None, set[int]]:
        """("since", "seen") for repo; (None, empty) when not polled before."""
        r = self.data["repos"].get(repo.lower()) or {}
        return r.get("since"), set(r.get("seen", []))

    def set_comment_position(self, repo: str, since: str, seen: set[int]) -> None:
        self.data["repos"][repo.lower()] = {"since": since, "seen": sorted(seen)}

    def forget_comment_positions(self, open_repos: set[str], listed_repos: list[str]) -> None:
        """Remove the position of every repository in listed_repos without an
        open pull request, so that polling starts afresh when one opens.
        Repositories that could not be listed keep theirs."""
        keep = {r.lower() for r in open_repos}
        listed = {r.lower() for r in listed_repos}
        for repo in [r for r in self.data["repos"] if r in listed and r not in keep]:
            del self.data["repos"][repo]

    # hourly cap

    def _prune(self, now: float) -> list[float]:
        runs = [t for t in self.data["runs"] if now - t < HOUR]
        self.data["runs"] = runs
        return runs

    def runs_in_last_hour(self, now: float | None = None) -> int:
        return len(self._prune(time.time() if now is None else now))

    def can_start_review(self, cap: int, now: float | None = None) -> bool:
        return self.runs_in_last_hour(now) < cap

    def note_review_started(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self._prune(now)
        self.data["runs"].append(now)

    def seconds_until_slot(self, cap: int, now: float | None = None) -> float:
        now = time.time() if now is None else now
        runs = sorted(self._prune(now))
        if len(runs) < cap:
            return 0.0
        if cap <= 0:
            return float("inf")
        return runs[len(runs) - cap] + HOUR - now


class StateLock:
    """Exclusive lock so two orchestrators cannot share one state directory."""

    def __init__(self, state_dir: Path):
        self.path = Path(state_dir) / "lock"
        self._fh = None

    def __enter__(self) -> "StateLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "w")
        try:
            fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._fh.close()
            raise StateLockedError(f"another reviewbot holds {self.path}") from exc
        return self

    def __exit__(self, *exc) -> None:
        if self._fh:
            fcntl.flock(self._fh, fcntl.LOCK_UN)
            self._fh.close()
