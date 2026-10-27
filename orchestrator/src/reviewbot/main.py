"""reviewbot command line: run, review."""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import signal
import sys
import time

from . import discovery, dockerrun, pipeline, validate
from .config import ConfigError, load_config
from .github_auth import GitHubAPI, GitHubError, make_auth
from .logutil import register_secret, setup_logging
from .state import State, StateLock, StateLockedError

log = logging.getLogger("reviewbot")


def _context(config, auth, state):
    real = validate.read_real_token()
    if not real:
        raise ConfigError("CLAUDE_CODE_OAUTH_TOKEN is not set or empty; reviewbot.service reads "
                          "it from ~/.config/reviewbot/env, for the proxy and the leak checks")
    register_secret(real)
    log.info("real Claude token loaded for the proxy and the leak checks")
    return pipeline.Context(config=config, auth=auth, state=state, real_token=real)


def _log_identity(auth) -> None:
    """Log who reviewbot acts as on GitHub. Only named fields are logged. A
    failure is logged and startup continues, as the poll loop does when
    GitHub is unreachable."""
    try:
        info = auth.app_info()
        installs = [f"{(i.get('account') or {}).get('login')} "
                    f"({i.get('repository_selection')})" for i in auth.list_installations()]
        log.info("GitHub App %s (id %s); installations: %s", info.get("slug"),
                 info.get("id"), ", ".join(installs) or "none")
    except GitHubError as exc:
        log.warning("could not read the GitHub identity: %s", exc)


@contextlib.contextmanager
def _session(config, auth):
    """Startup for every command: SIGTERM and SIGINT handlers, the state
    lock, the Claude token, the docker checks, stale work directory removal
    and a log line naming the GitHub identity. Yields the pipeline.Context.

    The signal sets dockerrun.STOP. A container that is running is killed
    within about a second (dockerrun.run), its work directory is removed,
    and the interrupted review is not recorded, so the pull request is
    reviewed again after a restart. No further review starts.

    The handler only sets the flag. It does not log and takes no lock: the
    interrupted code may hold the logging or redaction locks, and waiting
    for them in the handler would never return.
    """
    stop = dockerrun.STOP
    stop.clear()

    def handle(signum, _frame):
        stop.set()

    previous = {sig: signal.signal(sig, handle) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        with StateLock(config.state_dir):
            state = State.load(config.state_dir / "state.json")
            ctx = _context(config, auth, state)
            # Stale containers go first: one may still be writing into a work directory.
            dockerrun.prepare(config.docker)
            pipeline.remove_stale_work_dirs(config)
            _log_identity(auth)
            yield ctx
            if stop.is_set():
                log.info("stop requested by SIGTERM or SIGINT; no further review starts (a "
                         "container that was running was killed and its review is not recorded)")
                log.info("stopped")
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def cmd_review(ctx, ref: str) -> int:
    auth = ctx.auth
    repo, number = discovery.parse_pr_ref(ref)
    cand = discovery.make_candidate(
        discovery.get_pr(auth.api, auth.token_for_repo(repo), repo, number))
    why = pipeline.skip_reason(ctx, cand)
    if why:
        log.warning("%s would be skipped by the poll loop (%s); reviewing because it was "
                    "requested explicitly", cand.key, why)
    outcome = pipeline.run_and_record(ctx, cand.pr_json)
    return 0 if outcome.status in ("posted", "dry_run", "skipped") else 1


def _sleep_unless_stopped(stop, seconds: float) -> None:
    """Sleep for seconds, in steps of at most one second, until stop is set."""
    deadline = time.monotonic() + seconds
    while not stop.is_set():
        left = deadline - time.monotonic()
        if left <= 0:
            return
        time.sleep(min(1.0, left))


def cmd_run(ctx, *, once: bool = False) -> int:
    """Poll until SIGTERM or SIGINT, or for a single pass when once is set.

    With once, the exit status is 1 when the pass raised, could not list the
    pull requests of a repository or installation, or a review in it ended
    "failed" or "environment", and 0 otherwise. A dockerrun.SetupError from a
    review (an image retagged since startup) is not caught here. Signal
    handling is described in _session.
    """
    config = ctx.config
    stop = dockerrun.STOP
    if not once:
        log.info("polling every %ds (dry run: %s)", config.poll_seconds, config.dry_run)
    failed = False
    while not stop.is_set():
        try:
            done = pipeline.one_pass(ctx)
            failed = bool(done.listing_failures) or any(
                o.status in ("failed", "environment") for _, o in done.reviews)
        except ConfigError as exc:
            log.error("pass failed: %s", exc)
            return 2
        except dockerrun.SetupError:
            raise  # main logs it and exits 2
        except GitHubError as exc:
            log.error("pass failed: %s", exc)
            failed = True
        except Exception:
            log.exception("pass failed")
            failed = True
        if once:
            break
        _sleep_unless_stopped(stop, config.poll_seconds)
    return 1 if once and failed else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="reviewbot", description=__doc__)
    p.add_argument("--log-level", default=os.environ.get("REVIEWBOT_LOG_LEVEL", "INFO"),
                   type=str.upper, choices=("DEBUG", "INFO", "WARNING", "ERROR"),
                   help="default from REVIEWBOT_LOG_LEVEL or INFO")
    sub = p.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="poll until SIGTERM or SIGINT")
    run.add_argument("--once", action="store_true", help="run one polling pass and exit")
    r = sub.add_parser("review", help="review one pull request: owner/name#N")
    r.add_argument("ref")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        # argparse does not check a default from REVIEWBOT_LOG_LEVEL against
        # choices; setup_logging raises ValueError for an unknown level.
        setup_logging(args.log_level)
        config = load_config()
        auth = make_auth(config, GitHubAPI(config.api_url))
        with _session(config, auth) as ctx:
            match args.command:
                case "review":
                    return cmd_review(ctx, args.ref)
                case "run":
                    return cmd_run(ctx, once=args.once)
                case _:
                    raise AssertionError(f"no dispatch for command {args.command!r}")
    # Exit 2 tells systemd not to restart (RestartPreventExitStatus=2); exit 1
    # is for failures a restart may clear, such as Docker or GitHub being down.
    except (ConfigError, StateLockedError, ValueError, dockerrun.SetupError) as exc:
        log.error("%s", exc)
        return 2
    except GitHubError as exc:
        # The review command's requests, such as 404 for a repository that no
        # installation of the App covers. The message names the request.
        log.error("%s", exc)
        return 1
    except Exception:
        # Without this the interpreter would print the traceback itself,
        # outside the redacting log handler.
        log.exception("exiting")
        return 1


if __name__ == "__main__":
    sys.exit(main())
