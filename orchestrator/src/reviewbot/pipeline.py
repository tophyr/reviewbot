"""Review one pull request from fetch to post, and one polling pass."""

from __future__ import annotations

import logging
import os
import re
import secrets as pysecrets
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
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
    # posted: the structured review, kept in state as the last posted review.
    review: dict | None = None


# Outcomes that are not recorded against the pull request.
NOT_RECORDED = ("environment", "stopped", "capped")
# The level of the one log line each review's outcome gets; any other status
# is logged at INFO.
OUTCOME_LEVELS = {"failed": logging.ERROR, "refused": logging.ERROR,
                  "environment": logging.ERROR, "stopped": logging.WARNING}

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
    _slug: str | None = field(default=None, repr=False)

    @property
    def api(self):
        return self.auth.api

    def reviewer_uid_gid(self) -> tuple[int, int]:
        if self._uid_gid is None:
            self._uid_gid = review.resolve_uid_gid(
                self.config.docker.reviewer_image, self.config.reviewer_uid,
                self.config.reviewer_gid)
        return self._uid_gid

    def app_slug(self) -> str:
        """The GitHub App's slug, which review request comments mention.
        Raises GitHubError when it cannot be read or is not a slug, since it
        is written into review bodies inside a code span."""
        if self._slug is None:
            slug = self.auth.app_info().get("slug")
            if not isinstance(slug, str) or not SLUG_RE.fullmatch(slug):
                raise GitHubError(None, f"GET /app returned an unexpected slug {slug!r:.80}")
            self._slug = slug
        return self._slug


# A GitHub App slug: lowercase letters, digits and hyphens.
SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]*")

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
    try:
        slug = ctx.app_slug()  # read before the reviewer runs, for the review footer
    except GitHubError as exc:
        raise dockerrun.EnvironmentFailure(f"cannot read the app slug: {exc}") from exc
    prev = ctx.state.last_posted(discovery.state_key(pr_json))
    if prev and prev["head_sha"] == head_sha:
        prev = None  # a second review of the same head (the review command) starts afresh

    # fetch
    fetch.run_fetcher(cfg.docker, work, pr_json, fetch_token,
                      fetch.FETCH_TIMEOUT, fetch.MAX_FETCH_MB,
                      prev_sha=prev["head_sha"] if prev else None)
    # The fetcher gets only repo, number and the shas; the full pr.json and
    # the previous review are written here.
    if prev:
        fetch.write_json(work, "previous_review.json",
                         {"head_sha": prev["head_sha"], **prev["review"]})
    fetch.write_json(work, "pr.json", pr_json)
    fetched = fetch.read_fetch_output(work)
    if not fetched.files:
        log.info("%s#%d changes no files at %s; not running the reviewer", repo, number,
                 head_sha[:12])
        return Outcome("skipped", "no changed files")
    dmap = parse_diff(fetched.diff_text)
    if not prev:
        since = "no earlier review"
    elif fetched.has_interdiff:
        since = f"interdiff from the review of {prev['head_sha'][:12]}"
    else:
        since = f"earlier review of {prev['head_sha'][:12]}, no interdiff"
    log.info("%s#%d: %d changed files, %d with commentable lines, %s", repo, number,
             len(fetched.files), sum(1 for p, s in dmap.items() if s and p in fetched.files),
             since)

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
        return post.build_payload(structured, head_sha, dmap, fetched.files, slug=slug,
                                  relocate_all=relocate_all)

    result = validate.validate_review(rres.stdout, secrets, build_payload=build)
    if not result.ok:
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
    return Outcome("posted", f"review {resp.get('id')}", review=result.structured)


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
    log.log(OUTCOME_LEVELS.get(outcome.status, logging.INFO), "%s at %s: %s %s", key,
            pr_json["head_sha"][:12], outcome.status, outcome.detail)
    return outcome


def _run_and_record(ctx: Context, key: str, pr_json: dict) -> Outcome:
    cap = ctx.config.max_reviews_per_hour
    if not ctx.state.can_start_review(cap):
        return Outcome("capped", f"hourly cap of {cap} reached; next slot in "
                                 f"{ctx.state.seconds_until_slot(cap):.0f}s")
    try:
        outcome = review_pr(ctx, pr_json)
    except dockerrun.Stopped as exc:
        return Outcome("stopped", str(exc))
    except dockerrun.SetupError:
        raise
    except dockerrun.EnvironmentFailure as exc:
        return Outcome("environment", str(exc))
    except dockerrun.PullRequestFailure as exc:
        outcome = Outcome("failed", str(exc), public=True)
    except Exception as exc:
        log.exception("review of %s failed", key)
        outcome = Outcome("failed", f"{type(exc).__name__}: {exc}")
    ctx.state.record(key, pr_json["head_sha"], outcome.status, outcome.detail,
                     review=outcome.review)
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


# How far back the first comment poll of a repository reads, in seconds.
FIRST_POLL_WINDOW = 600
# How long after a newer comment an older one may still become visible in the
# API, in seconds. Every read goes back this far.
COMMENT_OVERLAP = 120
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _parse_time(text: str) -> datetime:
    return datetime.fromisoformat(text)


def take_review_requests(ctx: Context, listing: discovery.Listing) -> list[str]:
    """Read new comments in the repositories that have open pull requests and
    mark each pull request that a comment asks to review (see
    discovery.request_pattern). Returns the repositories whose comments could
    not be read; their polling position is kept, so the next pass reads the
    comments again.

    Each read lists the comments updated in the last COMMENT_OVERLAP seconds
    before the newest updated_at seen, so a comment that appears late is
    still read. A comment counts only if it was created in that window
    (otherwise it is an edit of an older comment) and its id is not in the
    position's seen set."""
    state, failures = ctx.state, []
    open_prs: dict[str, dict[int, discovery.Candidate]] = {}
    for cand in listing.candidates:
        open_prs.setdefault(cand.repo, {})[cand.number] = cand
    state.forget_comment_positions(set(open_prs), listing.listed)
    if not open_prs:
        return failures
    try:
        slug = ctx.app_slug()
    except GitHubError as exc:
        log.error("cannot read the app slug, so review requests are not read: %s", exc)
        return [f"app slug: {exc}"]
    pattern, own_login = discovery.request_pattern(slug), f"{slug}[bot]"
    first_since = datetime.fromtimestamp(time.time() - FIRST_POLL_WINDOW, timezone.utc)
    overlap = timedelta(seconds=COMMENT_OVERLAP)
    for repo, prs in open_prs.items():
        since, seen = state.comment_position(repo)
        try:
            since_t = _parse_time(since) if since else first_since
            read_from = since_t - overlap
            comments = discovery.list_comments(ctx.api, ctx.auth.token_for_repo(repo), repo,
                                               read_from.strftime(TIME_FORMAT))
            times = {c["id"]: (_parse_time(c["created_at"]), _parse_time(c["updated_at"]))
                     for c in comments}
        except Exception as exc:  # one broken repo must not stop the pass
            log.error("listing comments for %s failed: %s", repo, exc)
            failures.append(f"{repo} comments: {exc}")
            continue
        for c in comments:
            if c["id"] in seen or times[c["id"]][0] < read_from:
                continue  # read on an earlier pass, or an edit of an older comment
            login = (c.get("user") or {}).get("login", "")
            if login == own_login or not pattern.match(c.get("body") or ""):
                continue
            cand = prs.get(int(c["issue_url"].rsplit("/", 1)[1]))
            if cand:  # otherwise an issue or a closed pull request
                _take_request(ctx, cand, c["id"], login)
        since_t = max([since_t, *(updated for _, updated in times.values())])
        # The comments the next read lists again.
        seen = {cid for cid, (_, updated) in times.items() if updated >= since_t - overlap}
        state.set_comment_position(repo, since_t.strftime(TIME_FORMAT), seen)
    state.save()
    return failures


def _take_request(ctx: Context, cand: discovery.Candidate, comment_id: int, login: str) -> None:
    """Act on a review request: mark the pull request, or log why not."""
    if cand.skip:
        log.info("%s: review request by %s ignored: %s", cand.key, login, cand.skip)
        return
    if ctx.state.entry(cand.key) is None:
        log.info("%s: review request by %s: not reviewed yet, so its first review is "
                 "pending anyway", cand.key, login)
        return
    done = ctx.state.head_done(cand.key, cand.head_sha, dry_run=ctx.config.dry_run,
                               max_attempts=MAX_ATTEMPTS)
    if done:
        log.info("%s: review request by %s ignored: %s", cand.key, login, done)
        return
    ctx.state.request_review(cand.key, comment_id, login)
    log.info("%s: review requested by %s (comment %d)", cand.key, login, comment_id)


@dataclass
class PassResult:
    reviews: list[tuple[str, Outcome]]
    # Repositories or installations whose pull requests or comments could not
    # be listed.
    listing_failures: list[str]


def one_pass(ctx: Context) -> PassResult:
    results: list[tuple[str, Outcome]] = []
    listing = discovery.candidates(ctx.auth)
    log.info("pass: %d open pull requests", len(listing.candidates))
    gone = ctx.state.forget_closed({c.key for c in listing.candidates}, listing.listed)
    if gone:
        ctx.state.save()
        log.info("removed the state of %d closed pull requests: %s", len(gone), ", ".join(gone))
    comment_failures = take_review_requests(ctx, listing)
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
    return PassResult(results, listing.failures + comment_failures)
