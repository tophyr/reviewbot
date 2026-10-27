"""Review one pull request from fetch to post, and one polling pass."""

from __future__ import annotations

import logging
import os
import re
import secrets as pysecrets
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import discovery, dockerrun, fetch, post, review, validate
from .config import Config, ConfigError
from .diffmap import parse_diff
from .github_auth import GitHubError
from .state import State

log = logging.getLogger(__name__)


@dataclass
class Outcome:
    # posted, dry_run, refused, failed, skipped: recorded in state.
    # environment, stopped, capped: not recorded (see run_and_record).
    status: str
    detail: str = ""
    # The detail is written by reviewbot (no container output or exception
    # text), so a failure notice on the pull request may quote it.
    public: bool = False


# Outcomes that are not recorded against the pull request.
NOT_RECORDED = ("environment", "stopped", "capped")

# GitHub statuses on a token request that are not caused by the pull request.
TOKEN_ENVIRONMENT_STATUSES = (401, 403, 429)

MAX_ATTEMPTS = 3  # failed reviews of one head commit before it is skipped
# Free space needed before a fetch: a worst-case fetch writes 2 * MAX_FETCH_MB
# (file data and directory blocks), and about as much again stays free.
MIN_FREE_MB = 4 * fetch.MAX_FETCH_MB


@dataclass
class Context:
    config: Config
    auth: object
    state: State
    real_token: str
    _uid_gid: tuple[int, int] | None = field(default=None, repr=False)

    @property
    def api(self):
        return self.auth.api

    def reviewer_uid_gid(self) -> tuple[int, int]:
        if self._uid_gid is None:
            self._uid_gid = review.resolve_uid_gid(
                self.config.docker.reviewer_image, self.config.reviewer_uid,
                self.config.reviewer_gid)
            log.info("reviewer tmpfs uid/gid %d:%d", *self._uid_gid)
        return self._uid_gid


# Names make_work_dir gives: <repo slug>__<number>__<sha12>__<8 hex>
WORK_NAME_RE = re.compile(r".+__[0-9]+__[0-9a-f]{12}__[0-9a-f]{8}")


def make_work_dir(base: Path, repo: str, number: int, head_sha: str) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", repo.replace("/", "__"))
    work = base / f"{slug}__{number}__{head_sha[:12]}__{pysecrets.token_hex(4)}"
    work.mkdir(mode=0o755)
    os.chmod(work, 0o755)  # mkdir mode is filtered by umask
    return work


def safe_remove_work_dir(work: Path) -> bool:
    """Remove a work directory. Any exception is logged and not raised, so
    that a cleanup failure never replaces a review's outcome or stops
    startup. Returns True when the directory was removed."""
    try:
        shutil.rmtree(work)
    except Exception as exc:
        log.warning("could not remove work directory %s: %s: %s", work, type(exc).__name__, exc)
        return False
    return True


def remove_stale_work_dirs(cfg: Config) -> int:
    """Remove work directories an earlier run left behind (for example when
    it was killed). Called at startup while holding the state lock. Only
    directories named the way make_work_dir names them are removed, and
    nothing is removed when REVIEWBOT_KEEP_WORK=1."""
    if cfg.keep_work:
        return 0
    try:
        entries = list(os.scandir(cfg.work_dir))
    except FileNotFoundError:
        return 0
    except OSError as exc:
        raise ConfigError(f"REVIEWBOT_WORK_DIR {cfg.work_dir}: {exc.strerror}") from exc
    removed = 0
    for entry in entries:
        if WORK_NAME_RE.fullmatch(entry.name) and entry.is_dir(follow_symlinks=False):
            log.warning("removing work directory %s left by an earlier run", entry.path)
            if safe_remove_work_dir(Path(entry.path)):
                removed += 1
    return removed


def check_free_space(path: Path, min_free_mb: int) -> None:
    """Raise EnvironmentFailure when the filesystem holding path has less
    than min_free_mb available to this user. A full disk is not the pull
    request's fault, so the pass ends and nothing is recorded."""
    try:
        st = os.statvfs(path)
    except OSError as exc:
        raise dockerrun.EnvironmentFailure(f"cannot read free space of {path}: {exc}") from exc
    free_mb = st.f_bavail * st.f_frsize / (1024 * 1024)
    if free_mb < min_free_mb:
        raise dockerrun.EnvironmentFailure(
            f"{free_mb:.0f} MB free on the filesystem of {path}, below "
            f"{min_free_mb} MB; not fetching")


def review_pr(ctx: Context, pr_json: dict) -> Outcome:
    """Fetch, review, validate and post one PR. Counts against the hourly cap."""
    cfg = ctx.config
    repo, number, head_sha = pr_json["repo"], pr_json["number"], pr_json["head_sha"]
    # Images retagged since startup (build-images.sh run by hand) must not be
    # used; SetupError ends the process with exit 2 (see _run_and_record).
    dockerrun.check_image_builds(cfg.docker)
    try:
        work = make_work_dir(cfg.work_dir, repo, number, head_sha)
    except OSError as exc:
        raise dockerrun.EnvironmentFailure(
            f"cannot create a work directory under {cfg.work_dir}: {exc}") from exc
    try:
        check_free_space(work, MIN_FREE_MB)
        return _review_in(ctx, pr_json, work)
    finally:
        if cfg.keep_work:
            log.info("keeping work directory %s", work)
        else:
            safe_remove_work_dir(work)


def _token(request, repo: str) -> str:
    """A token from request(repo). A failure not caused by this PR (no
    response, a GitHub server error, a refused app JWT (401), a rate limit or
    suspended installation (403, 429)) raises EnvironmentFailure; any other,
    such as 404 or 422 for a repository that is not in the installation,
    raises PullRequestFailure."""
    try:
        return request(repo)
    except GitHubError as exc:
        if exc.status is None or exc.status in TOKEN_ENVIRONMENT_STATUSES or exc.status >= 500:
            raise dockerrun.EnvironmentFailure(f"GitHub token request failed: {exc}") from exc
        raise dockerrun.PullRequestFailure(f"GitHub token request failed: {exc}") from exc


def _review_in(ctx: Context, pr_json: dict, work: Path) -> Outcome:
    cfg = ctx.config
    repo, number, head_sha = pr_json["repo"], pr_json["number"], pr_json["head_sha"]
    orchestrator_token = _token(ctx.auth.token_for_repo, repo)
    fetch_token = _token(ctx.auth.fetch_token_for_repo, repo)

    # fetch
    fetch.run_fetcher(cfg.docker, work, pr_json, fetch_token,
                      fetch.FETCH_TIMEOUT, fetch.MAX_FETCH_MB)
    # The fetcher gets only repo, number and the two shas; the full pr.json
    # is written here.
    fetch.write_pr_json(work, pr_json)
    fetched = fetch.read_fetch_output(work)
    if not fetched.files:
        log.info("%s#%d changes no files at %s; not running the reviewer", repo, number,
                 head_sha[:12])
        return Outcome("skipped", "no changed files")
    dmap = parse_diff(fetched.diff_text)
    log.info("%s#%d: %d changed files, %d with commentable lines", repo, number,
             len(fetched.files), sum(1 for p, s in dmap.items() if s and p in fetched.files))

    # review (counts against the hourly cap from here on)
    ctx.state.note_review_started()
    ctx.state.save()
    uid, gid = ctx.reviewer_uid_gid()
    env = review.reviewer_env(cfg.model, cfg.max_turns, cfg.prompt_file)
    proxy = dockerrun.start_proxy(cfg.docker, ctx.real_token)
    try:
        rres = review.run_reviewer(cfg.docker, work, repo, number, uid=uid, gid=gid,
                                   env=env, timeout=review.REVIEW_TIMEOUT)
    finally:
        dockerrun.stop_proxy(proxy)

    # A fresh token for the post: the cached one from the start of the review
    # may have 5 minutes left, and the fetch and review can take 30.
    post_token = _token(ctx.auth.token_for_repo, repo)

    # validate
    secrets = validate.Secrets(real_token=ctx.real_token,
                               github_tokens=[orchestrator_token, fetch_token, post_token])

    def build(structured, relocate_all=False):
        return post.build_payload(structured, head_sha, dmap, fetched.files,
                                  relocate_all=relocate_all)

    result = validate.validate_review(rres.stdout, secrets, build_payload=build)
    if not result.ok:
        log.error("review output for %s#%d %s; nothing posted", repo, number, result)
        # A review that is only too long (step 4) holds no leak and the next
        # run may be shorter, so it is retried; a token in it is final.
        status = "failed" if result.step == 1 or result.code == "too_long" else "refused"
        return Outcome(status, str(result), public=True)
    payload = result.payload
    log.info("review for %s#%d validated: verdict %s, %d inline, %d in body", repo, number,
             result.structured["verdict"], len(payload["comments"]),
             len(result.structured["comments"]) - len(payload["comments"]))

    if cfg.dry_run:
        post.print_payload(repo, number, payload)
        return Outcome("dry_run", "printed")

    def rebuild_without_inline():
        # The same validated text, rearranged; only its length can change.
        again = build(result.structured, relocate_all=True)
        size = validate.total_body_length(again)
        if size >= validate.MAX_TOTAL_BODY:
            raise RuntimeError(f"relocated payload is {size} characters, limit "
                               f"{validate.MAX_TOTAL_BODY}")
        return again

    resp = post.post_review(ctx.api, post_token, repo, number, payload,
                            rebuild_without_inline=rebuild_without_inline)
    log.info("posted review %s on %s#%d", resp.get("id"), repo, number)
    return Outcome("posted", f"review {resp.get('id')}")


def run_and_record(ctx: Context, pr_json: dict) -> Outcome:
    """Review one PR, record the outcome in state and log it.

    Three outcomes are not recorded, so the PR keeps its previous record and
    attempt count and is reviewed again on a later pass or after a restart:
    the hourly cap was reached (nothing started), an environment failure
    (dockerrun.EnvironmentFailure) and a stop request (dockerrun.Stopped).
    A dockerrun.SetupError (an image from another build) is neither recorded
    nor caught: it ends the process with exit 2.
    """
    key = discovery.state_key(pr_json)
    outcome = _run_and_record(ctx, key, pr_json)
    log.info("%s at %s: %s %s", key, pr_json["head_sha"][:12], outcome.status, outcome.detail)
    return outcome


def _run_and_record(ctx: Context, key: str, pr_json: dict) -> Outcome:
    cap = ctx.config.max_reviews_per_hour
    if not ctx.state.can_start_review(cap):
        return Outcome("capped", f"hourly cap of {cap} reached; next slot in "
                                 f"{ctx.state.seconds_until_slot(cap):.0f}s")
    try:
        outcome = review_pr(ctx, pr_json)
    except dockerrun.Stopped as exc:
        log.warning("review of %s stopped (%s); not recorded, it is retried after restart",
                    key, exc)
        return Outcome("stopped", str(exc))
    except dockerrun.SetupError:
        raise
    except dockerrun.EnvironmentFailure as exc:
        log.error("review of %s hit an environment failure; not counted against it: %s", key, exc)
        return Outcome("environment", str(exc))
    except dockerrun.PullRequestFailure as exc:
        log.error("review of %s failed: %s", key, exc)
        outcome = Outcome("failed", str(exc), public=True)
    except Exception as exc:
        log.exception("review of %s failed", key)
        outcome = Outcome("failed", f"{type(exc).__name__}: {exc}")
    ctx.state.record(key, pr_json["head_sha"], outcome.status, outcome.detail)
    ctx.state.save()
    notice = _final_notice(ctx, key, pr_json["head_sha"], outcome)
    if notice:
        _post_notice(ctx, key, pr_json, notice)
    return outcome


def _final_notice(ctx: Context, key: str, head_sha: str, outcome: Outcome) -> str | None:
    """The text to post on a pull request whose head will not be reviewed
    again: its output was refused, or it failed MAX_ATTEMPTS times. None for
    any other outcome, and for a failure whose detail reviewbot did not write."""
    if not outcome.public:
        return None
    sha = f"`{head_sha[:12]}`"
    if outcome.status == "refused":
        # No detail: it would tell a prompt injection which check it met.
        return f"**reviewbot withheld its review of {sha}**: the output failed a safety check."
    attempts = ctx.state.entry(key)["attempts"]
    if outcome.status == "failed" and attempts >= MAX_ATTEMPTS:
        return (f"**reviewbot could not review {sha}** after {attempts} attempts. The last "
                f"attempt failed with: {post.defang(outcome.detail)}")
    return None


def _post_notice(ctx: Context, key: str, pr_json: dict, text: str) -> None:
    """Post text on the pull request. A failure is logged and does not change
    the review's outcome."""
    if ctx.config.dry_run:
        log.info("dry run: not posting on %s: %s", key, text)
        return
    try:
        token = _token(ctx.auth.token_for_repo, pr_json["repo"])
        post.post_notice(ctx.api, token, pr_json["repo"], pr_json["number"],
                         pr_json["head_sha"], text)
        log.info("posted a failure notice on %s", key)
    except Exception as exc:
        log.warning("could not post the failure notice on %s: %s", key, exc)


def skip_reason(ctx: Context, cand: discovery.Candidate) -> str | None:
    """Why the poll loop does not review this pull request now, or None."""
    return cand.skip or ctx.state.should_skip(cand.key, cand.head_sha, dry_run=ctx.config.dry_run,
                                              max_attempts=MAX_ATTEMPTS)


@dataclass
class PassResult:
    reviews: list[tuple[str, Outcome]]
    # Repositories or installations whose pull requests could not be listed.
    listing_failures: list[str]


def one_pass(ctx: Context) -> PassResult:
    results: list[tuple[str, Outcome]] = []
    listing = discovery.candidates(ctx.auth)
    log.info("pass: %d open pull requests", len(listing.candidates))
    gone = ctx.state.forget_closed({c.key for c in listing.candidates}, listing.listed)
    if gone:
        ctx.state.save()
        log.info("removed the state of %d closed pull requests: %s", len(gone), ", ".join(gone))
    for cand in listing.candidates:
        if dockerrun.STOP.is_set():
            log.info("stop requested; not starting another review")
            break
        why = skip_reason(ctx, cand)
        if why:
            log.debug("skip %s: %s", cand.key, why)
            continue
        outcome = run_and_record(ctx, cand.pr_json)
        results.append((cand.key, outcome))
        if outcome.status in NOT_RECORDED:
            break  # the next review would meet the same environment, stop or cap
    return PassResult(results, listing.failures)
