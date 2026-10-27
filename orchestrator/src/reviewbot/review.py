"""Run the review container (DESIGN.md, "Review container contract")."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from . import dockerrun
from .config import ConfigError, read_prompt_file

log = logging.getLogger(__name__)

REVIEW_TIMEOUT = 1200  # seconds
# The stand-in token the reviewer image sets. The e2e leak tests look for it
# in the reviewer's output as a sign that the model read its environment.
# Must equal the CLAUDE_CODE_OAUTH_TOKEN that reviewer/entrypoint.sh exports
# and DUMMY_TOKEN in tests/e2e/lib.sh (test_containers.py compares them).
DUMMY_TOKEN = "sk-ant-oat01-dummy"
# The reviewer's environment that varies between runs; reviewer/Dockerfile
# sets every constant one. REVIEWBOT_PROMPT is passed only when
# REVIEWBOT_PROMPT_FILE is set (docker leaves out a -e NAME that is unset).
REVIEWER_ENV_NAMES = ("REVIEWBOT_MODEL", "REVIEWBOT_MAX_TURNS", "REVIEWBOT_PROMPT")


class ReviewerImageError(dockerrun.EnvironmentFailure):
    """The reviewer image, not the pull request: every review would fail the
    same way."""


# Anthropic API statuses that say nothing about the pull request (5xx is checked
# separately). 400 is absent because a large pull request can cause it; 404
# means the configured model is unknown, since the request path is fixed.
API_ENVIRONMENT_STATUSES = {401: "authentication", 403: "permission", 404: "model not found",
                            429: "rate limit"}


def api_environment_error(stdout: str) -> int | None:
    """The API status of a reviewer run that failed because of the Anthropic
    API or the token rather than the pull request, else None.

    Reads only the top-level fields Claude Code writes (is_error,
    terminal_reason, api_error_status); the model's own text is in
    "result" and "structured_output" and is not looked at.
    """
    try:
        doc = json.loads(stdout)
    except ValueError:
        return None
    if isinstance(doc, list):  # the array form; see validate.extract_structured
        results = [m for m in doc if isinstance(m, dict) and m.get("type") == "result"]
        doc = results[-1] if results else None
    if not isinstance(doc, dict) or doc.get("is_error") is not True:
        return None
    if doc.get("terminal_reason") != "api_error":
        return None
    status = doc.get("api_error_status")
    if not isinstance(status, int) or isinstance(status, bool):
        return None
    if status in API_ENVIRONMENT_STATUSES or 500 <= status <= 599:
        return status
    return None


def describe_api_status(status: int) -> str:
    return API_ENVIRONMENT_STATUSES.get(status, "server error")


def reviewer_env(model: str, max_turns: int, prompt_file: Path | None) -> dict[str, str]:
    """The reviewer's variable environment. REVIEWBOT_PROMPT is set only when
    prompt_file is; it is read now, so an edit applies to the next review. A
    file that can no longer be read is an EnvironmentFailure."""
    env = {
        "REVIEWBOT_MODEL": model,
        "REVIEWBOT_MAX_TURNS": str(max_turns),
    }
    if prompt_file is not None:
        try:
            env["REVIEWBOT_PROMPT"] = read_prompt_file(prompt_file)
        except ConfigError as exc:
            raise dockerrun.EnvironmentFailure(str(exc)) from exc
    assert set(env) <= set(REVIEWER_ENV_NAMES)
    return env


def build_argv(image: str, name: str, work: Path, uid: int, gid: int,
               network: str) -> list[str]:
    return [
        "docker", "run", "--rm", "--name", name,
        # no copy of the output in the daemon's log files on the host's disk;
        # dockerrun reads it from the attached streams
        "--log-driver", "none",
        "--network", network,
        "--read-only",
        "--tmpfs", "/tmp",
        "--tmpfs", f"/home/reviewer:uid={uid},gid={gid},mode=0700",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--pids-limit", "256",
        "--memory", "4g",
        "--cpus", "2",
        "-v", f"{work}:/work:ro",
        *dockerrun.env_flags(REVIEWER_ENV_NAMES),
        image,
    ]


def _parse_user(user: str) -> tuple[int | None, int | None]:
    m = re.fullmatch(r"(\d+)(?::(\d+))?", user.strip())
    if not m:
        return None, None
    uid = int(m.group(1))
    return uid, int(m.group(2)) if m.group(2) else None


def resolve_uid_gid(image: str, uid_override: int | None = None,
                    gid_override: int | None = None) -> tuple[int, int]:
    """uid/gid for the /home/reviewer tmpfs.

    REVIEWBOT_REVIEWER_UID/GID when set, otherwise the image's USER, which
    must be numeric. Refuses uid or gid 0, because the contract requires a
    non-root reviewer.
    """
    if uid_override is not None:
        uid = uid_override
        gid = gid_override if gid_override is not None else uid_override
        if uid == 0 or gid == 0:
            raise ReviewerImageError(f"reviewer uid/gid override {uid}:{gid} is root")
        return uid, gid
    try:
        r = dockerrun.docker(["image", "inspect", "--format", "{{json .Config}}", image])
    except dockerrun.EnvironmentFailure as exc:
        raise ReviewerImageError(f"docker image inspect {image}: {exc}") from exc
    if r.returncode != 0:
        raise ReviewerImageError(f"docker image inspect {image}: {r.stderr.strip()}")
    user = (json.loads(r.stdout).get("User") or "").strip()
    uid, gid = _parse_user(user)
    if uid is None:
        raise ReviewerImageError(f"image {image} user {user!r} is not a numeric uid[:gid]")
    if gid is None:
        gid = uid
    if uid == 0 or gid == 0:
        raise ReviewerImageError(f"image {image} runs as root (User={user!r})")
    return uid, gid


def run_reviewer(names, work: Path, repo: str, number: int, *, uid: int, gid: int,
                 env: dict[str, str], timeout: float) -> dockerrun.RunResult:
    """Run names.reviewer_image on names.internal_network.

    Raises EnvironmentFailure when the Anthropic API answered with a status
    that says nothing about the pull request (api_environment_error), and
    PullRequestFailure on any other failure, after logging the reviewer's
    stderr tail.
    """
    name = dockerrun.container_name(names.prefix, "review")
    argv = build_argv(names.reviewer_image, name, work, uid, gid, names.internal_network)
    log.info("reviewing %s#%d in %s (timeout %ss)", repo, number, name, timeout)
    result = dockerrun.run(argv, env, timeout, name)
    log.info("reviewer exited %s after %.1fs%s, %d bytes on stdout", result.returncode,
             result.duration, " (timed out)" if result.timed_out else "",
             len(result.stdout))
    log.debug("reviewer stderr:\n%s", result.stderr)
    status = None if result.timed_out else api_environment_error(result.stdout)
    if status is not None:
        raise dockerrun.EnvironmentFailure(
            f"Anthropic API error {status} ({describe_api_status(status)})")
    if result.ok:
        return result
    log.error("reviewer stderr tail:\n%s", dockerrun.tail(result.stderr))
    raise dockerrun.PullRequestFailure(
        f"reviewer exit {result.returncode}{' (timeout)' if result.timed_out else ''}")
