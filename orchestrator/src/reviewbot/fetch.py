"""Run the fetcher container (DESIGN.md, "Fetcher container contract")."""

from __future__ import annotations

import json
import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from . import dockerrun

log = logging.getLogger(__name__)

MAX_DIFF_BYTES = 50 * 1024 * 1024
MAX_FILES_BYTES = 10 * 1024 * 1024

# The only pr.json fields the fetcher receives. Linux refuses an environment
# string over 128 KiB (MAX_ARG_STRLEN) and json.dumps writes each non-ASCII
# character as 6 bytes, so a pull request body would break the docker start.
# The orchestrator writes the full pr.json itself after the fetcher succeeds.
FETCHER_FIELDS = ("repo", "number", "base_sha", "head_sha")
# Seconds; above the fetcher's worst case of about 420: a transfer that stalls
# for 300 plus two 60-second endpoint probes.
FETCH_TIMEOUT = 600
# Size of the fetcher's /scratch tmpfs in MiB. Rootless docker does not enforce
# --storage-opt; a sized tmpfs is enforced.
MAX_FETCH_MB = 10240
# Memory on top of the scratch tmpfs. tmpfs pages count against --memory, so
# the headroom makes a full tmpfs fail with ENOSPC rather than an OOM kill.
FETCH_MEMORY_HEADROOM_MB = 1024
# One inode per 4 KiB block. Directories and empty files use no tmpfs pages,
# so the size does not bound them, but each directory takes a block on the
# host after the copy.
FETCH_INODES_PER_MB = 256
# github.com's git endpoint is unreachable, which is not the pull request's
# fault.
FETCHER_EXIT_GIT_UNREACHABLE = 75
# The repository does not fit in the /scratch size or inode limit.
FETCHER_EXIT_TOO_LARGE = 74
# Size of the /log tmpfs, which holds git's stderr apart from /scratch so
# that the message survives a full /scratch.
LOG_TMPFS_MB = 16


class FetchError(dockerrun.PullRequestFailure):
    """The fetcher's output is unusable."""


def build_argv(image: str, name: str, work: Path, max_fetch_mb: int) -> list[str]:
    """docker run command line for the fetcher.

    The contract fixes the image, the env inputs, the read-write /work mount
    and the /scratch tmpfs of max_fetch_mb (MAX_FETCH_MB) with
    max_fetch_mb * FETCH_INODES_PER_MB inodes, in which the fetcher does all
    its work before copying the result to /work. The
    remaining flags are hardening chosen by the orchestrator.
    """
    return [
        "docker", "run", "--rm", "--name", name,
        # no copy of the output in the daemon's log files on the host's disk;
        # dockerrun reads it from the attached streams
        "--log-driver", "none",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--pids-limit", "512",
        "--memory", f"{max_fetch_mb + FETCH_MEMORY_HEADROOM_MB}m",
        "--cpus", "2",
        # --tmpfs passes size and nr_inodes to the tmpfs mount as written
        "--tmpfs", f"/scratch:rw,size={max_fetch_mb}m,nr_inodes={max_fetch_mb * FETCH_INODES_PER_MB}",
        "--tmpfs", f"/log:rw,size={LOG_TMPFS_MB}m",
        *dockerrun.env_flags(["GITHUB_TOKEN", "PR_JSON"]),
        "-v", f"{work}:/work",
        image,
    ]


def run_fetcher(names, work: Path, pr_json: dict, token: str,
                timeout: float, max_fetch_mb: int) -> dockerrun.RunResult:
    """Run names.fetcher_image with repo, number, base_sha and head_sha in PR_JSON.

    Raises EnvironmentFailure when github.com's git endpoint is unreachable
    (FETCHER_EXIT_GIT_UNREACHABLE), and PullRequestFailure on any other
    failure, after logging the fetcher's stderr tail.
    """
    fetch_input = {k: pr_json[k] for k in FETCHER_FIELDS}
    name = dockerrun.container_name(names.prefix, "fetch")
    argv = build_argv(names.fetcher_image, name, work, max_fetch_mb)
    env = {"GITHUB_TOKEN": token, "PR_JSON": json.dumps(fetch_input)}
    log.info("fetching %s#%d at %s in %s", pr_json["repo"], pr_json["number"],
             pr_json["head_sha"][:12], name)
    result = dockerrun.run(argv, env, timeout, name)
    log.info("fetcher exited %s after %.1fs%s", result.returncode, result.duration,
             " (timed out)" if result.timed_out else "")
    log.debug("fetcher stderr:\n%s", result.stderr)
    if result.ok:
        return result
    log.error("fetcher stderr tail:\n%s", dockerrun.tail(result.stderr))
    if not result.timed_out and result.returncode == FETCHER_EXIT_GIT_UNREACHABLE:
        raise dockerrun.EnvironmentFailure("github.com's git endpoint is unreachable "
                                           f"(fetcher exit {result.returncode})")
    if not result.timed_out and result.returncode == FETCHER_EXIT_TOO_LARGE:
        raise dockerrun.PullRequestFailure(
            f"the repository does not fit in the fetcher's {max_fetch_mb} MiB scratch space")
    raise dockerrun.PullRequestFailure(
        f"fetch exit {result.returncode}{' (timeout)' if result.timed_out else ''}")


def _read_regular(path: Path, limit: int) -> str:
    """Read a regular file without following symlinks, with a size cap.

    O_NONBLOCK: open() of a FIFO left in place of the file would wait for a
    writer forever; the S_ISREG check below refuses it instead."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise FetchError(f"{path.name} is not a regular file")
        if st.st_size > limit:
            raise FetchError(f"{path.name} is {st.st_size} bytes, limit {limit}")
        with os.fdopen(fd, "rb", closefd=False) as fh:
            data = fh.read(limit + 1)
    finally:
        os.close(fd)
    return data.decode("utf-8", errors="replace")


def write_pr_json(work: Path, pr_json: dict) -> None:
    """Write the full pr.json into WORK, mode 0644 so that the reviewer's
    uid can read it (the service runs with UMask=0077).

    Called after the fetcher succeeds. The file must
    not exist yet: the fetcher does not write it. Raises FetchError if it
    does exist, and EnvironmentFailure if it cannot be written.
    """
    data = (json.dumps(pr_json) + "\n").encode("utf-8")
    try:
        # "x" opens with O_CREAT | O_EXCL, which also refuses a symlink
        # planted in its place, even one whose target does not exist.
        with open(work / "pr.json", "xb") as fh:
            os.fchmod(fh.fileno(), 0o644)
            fh.write(data)
    except FileExistsError as exc:
        raise FetchError("pr.json already exists in the work directory; "
                         "the fetcher must not write it") from exc
    except OSError as exc:
        raise dockerrun.EnvironmentFailure(f"cannot write pr.json in {work}: {exc}") from exc


@dataclass
class FetchedWork:
    diff_text: str
    files: set[str]


def read_fetch_output(work: Path) -> FetchedWork:
    """Read diff.patch and files.txt from the fetcher's output. Both are
    read with _read_regular: the fetcher handles hostile repository content,
    and this runs in the orchestrator on the host."""
    try:
        diff_text = _read_regular(work / "diff.patch", MAX_DIFF_BYTES)
        files_text = _read_regular(work / "files.txt", MAX_FILES_BYTES)
    except OSError as exc:
        raise FetchError(f"fetcher output unreadable: {exc}") from exc
    # One raw path per line: the fetcher writes git diff --name-only -z with
    # each NUL turned into a newline, and fails on a path that contains a
    # newline. The lines are not in git's quoted form and are used as they are.
    files = {line for line in files_text.split("\n") if line}
    return FetchedWork(diff_text, files)
