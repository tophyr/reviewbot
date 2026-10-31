# reviewbot

## What reviewbot is

reviewbot reviews pull requests on GitHub repositories that its owner
maintains. The review model is Claude, reached with a Claude Max subscription
token created by `claude setup-token`. No Anthropic API key is used anywhere.
Reviews are posted as a GitHub App, as comments only: a review never approves
or blocks a pull request.

reviewbot polls GitHub from the owner's machine and runs everything under
rootless Docker there. No GitHub Actions workflow, webhook or other file is
needed in any repository being reviewed.

## How a review runs

There are four components. Docker names below (`reviewbot-proxy`,
`reviewbot_internal` and so on) are the ones the default value of
`REVIEWBOT_DOCKER_PREFIX`, `reviewbot`, produces; "Configuration" describes
the setting.

- The orchestrator is a Python program (`orchestrator/`) that runs on the host
  as a systemd user service. It holds the GitHub App private key. It holds
  the Claude token too, which it passes by name to the proxy container and
  otherwise uses only to check that a review does not contain it.
- The fetcher container (`reviewbot-fetcher`) is started once per review. It
  clones the repository, checks out the pull request head, and writes the
  files the reviewer reads into a scratch directory. It holds one short-lived,
  read-only GitHub token.
- The review container (`reviewbot-reviewer`) is started once per review on
  the Docker network `reviewbot_internal`, which has no route out. It holds a
  dummy token and no GitHub credential.
- The proxy container (`reviewbot-proxy`) is started once per review, after
  the fetch, and removed when the review ends. It is on `reviewbot_internal`
  and on a normal bridge network (`reviewbot_default`), and it is the only
  component that sends the real Claude token anywhere. No proxy exists
  between reviews.

The steps of one review:

1. The orchestrator lists open, non-draft pull requests on the repositories the
   app is installed on. It skips pull requests with the skip label, pull
   requests by bots, pull requests whose head commit was already reviewed, and
   anything over the hourly cap.
2. It creates an empty scratch directory and starts the fetcher container
   with a read-only installation token for that one repository.
3. The fetcher writes `diff.patch`, `files.txt` and `repo/` (the checkout,
   without `.git`) into the scratch directory, and the orchestrator then
   writes `pr.json` there.
4. The orchestrator starts the proxy container and waits until it accepts
   connections (at most 15 seconds), then starts the review container with
   the scratch directory mounted read-only at `/work`. Claude Code runs with
   its API traffic sent to the proxy, and returns a JSON review (summary,
   verdict, inline comments).
5. The proxy replaces the dummy credential with the real token and forwards
   the request to `api.anthropic.com`. When the review container ends, the
   orchestrator removes the proxy.
6. The orchestrator checks the JSON against the schema and refuses it if it
   contains a token.
7. The orchestrator posts the review to GitHub as the app, with the review
   event `COMMENT`, and records the head commit in its state file.

## Security model

The contents of a pull request are untrusted, and an attacker who controls one
can write anything into `pr.json`, `diff.patch` and the checkout. The controls
below limit what that text can do.

- The review container has the tools Read, Grep and Glob (plus the
  `StructuredOutput` tool that returns the result). It has no Bash tool.
- The file tools are confined to `/work` (`claude --restricted`), including
  through symlinks. The fetcher also writes symlinks as plain files.
- The review container holds a dummy token (`sk-ant-oat01-dummy`) and no GitHub
  credential. Its only network is the internal network
  (`reviewbot_internal`), which has no route to the internet. The only
  reachable host is the proxy.
- The proxy removes the incoming `Authorization` and `X-Api-Key` headers,
  adds the real token, and forwards only `POST /v1/messages` and
  `POST /v1/messages/count_tokens` to `api.anthropic.com`. Every other
  method and path gets 403. It does not log request bodies or header values.
- Before posting, the orchestrator refuses any output that contains the real
  Claude token or a GitHub token it is using. The review container is given
  none of these, so this check catches a failure of that isolation. A
  refused review is not posted.
- Reviews are posted as the GitHub App, and the app token the orchestrator
  uses can act only on repositories where the app is installed.

Residual risks:

- The contents of each reviewed pull request (diff and checkout) are sent to
  Anthropic.
- A hostile pull request can make the review text misleading, for example by
  hiding a real problem or inventing one. A reader has to treat the review as
  advice.
- Every review consumes Claude Max subscription quota. The hourly cap limits
  how fast it is used.

## Setup

Requirements: Linux, rootless Docker running as the systemd user unit
`docker.service`, Python 3.12 or newer, and the `claude` command.

1. Create a GitHub App at https://github.com/settings/apps/new with
   repository permissions Pull requests (read and write), Contents (read) and
   Metadata (read), and the webhook inactive. Under "Where can this GitHub
   App be installed?" choose "Only on this account": reviewbot reviews every
   installation of the app it can see, on the operator's subscription. To
   review an organization's repositories, create the app under that
   organization. Note the App ID, generate a private key, and save the
   `.pem` file with mode 0600. Install the app, selecting the repositories
   to review.
2. Clone this repository and run the remaining steps from the checkout.
   Install:

   ```
   deploy/install.sh
   ```

   The installer builds the three images with `deploy/build-images.sh`,
   creates a virtualenv in `~/.local/share/reviewbot` and installs a copy of
   the orchestrator package into it, installs and enables the user unit
   `reviewbot.service` without starting it, and creates
   `~/.config/reviewbot/env` from `deploy/env.example` if it does not exist.
   It generates a build id for the run and records it as the label
   `reviewbot.build` on each image and in the file `reviewbot-build-id` in
   the virtualenv; the orchestrator compares the two at startup. The
   installed package does not read the checkout, so the checkout can be
   edited or moved without affecting the running service.
3. Edit `~/.config/reviewbot/env`: set `CLAUDE_CODE_OAUTH_TOKEN` (the token
   from `claude setup-token`), `REVIEWBOT_APP_ID` and
   `REVIEWBOT_APP_PRIVATE_KEY_PATH`. The file holds the Claude token, so it
   must stay mode 0600 (the installer creates it that way).

   ```
   $EDITOR ~/.config/reviewbot/env
   ```

4. Dry run. The env file ships with `REVIEWBOT_DRY_RUN=1`, so the review is
   printed, not posted:

   ```
   cd ~/.local/share/reviewbot && set -a && . ~/.config/reviewbot/env && set +a
   venv/bin/reviewbot review owner/name#123
   ```

5. Set `REVIEWBOT_DRY_RUN=0` in `~/.config/reviewbot/env` and start the
   service:

   ```
   systemctl --user start reviewbot.service
   loginctl enable-linger "$USER"    # optional: keep running while logged out
   ```

## Configuration

`~/.config/reviewbot/env` is read by `reviewbot.service`. Every setting the
orchestrator reads, with its default:

| Variable | Meaning | Default |
|---|---|---|
| `REVIEWBOT_APP_ID` | GitHub App ID; required | none |
| `REVIEWBOT_APP_PRIVATE_KEY_PATH` | path to the app private key; required | none |
| `REVIEWBOT_POLL_SECONDS` | seconds between polling passes | 180 |
| `REVIEWBOT_MAX_REVIEWS_PER_HOUR` | cap on reviewer runs in a rolling hour | 6 |
| `REVIEWBOT_DRY_RUN` | `1` prints the review instead of posting it | `1` in the shipped env file |
| `REVIEWBOT_MODEL` | model for the review container | `opus` |
| `REVIEWBOT_GITHUB_API_URL` | GitHub REST API base URL; the tests point it at a local fake | `https://api.github.com` |
| `REVIEWBOT_MAX_TURNS` | Claude Code `--max-turns` for a review, 1 to 9999 | 40 |
| `REVIEWBOT_PROMPT_FILE` | a file of review instructions that replaces the reviewer image's `reviewer/review.md` ("Customizing the review"); read for each review, at most 64 KiB of UTF-8 | unset |
| `REVIEWBOT_LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING` or `ERROR` (any case) | `INFO` |
| `REVIEWBOT_KEEP_WORK` | `1` keeps each review's scratch directory instead of deleting it | `0` |
| `REVIEWBOT_STATE_DIR` | directory of the state file and its lock; `reviewbot.service` sets `~/.local/share/reviewbot/state` | `./state` |
| `REVIEWBOT_WORK_DIR` | directory for the per-review scratch directories (no `:` or `,`); `reviewbot.service` sets `~/.local/share/reviewbot/work` | `./work` |
| `CLAUDE_CODE_OAUTH_TOKEN` | the real Claude token from `claude setup-token`; the orchestrator passes it by name to each proxy container and uses it to check that a review does not contain it; it exits 2 when the variable is unset or empty | none |
| `REVIEWBOT_DOCKER_PREFIX` | prefix of every docker name: the images `<prefix>-proxy`, `<prefix>-reviewer` and `<prefix>-fetcher`, the networks `<prefix>_internal` and `<prefix>_default`, and the container names (which start with `<prefix>-`); must match `^[a-z][a-z0-9-]{0,31}$`, otherwise (an empty value included) the orchestrator exits 2; a different prefix lets a second instance run beside the deployment | `reviewbot` |
| `REVIEWBOT_REVIEWER_UID`, `REVIEWBOT_REVIEWER_GID` | owner of the review container's home tmpfs; taken from the image when unset; never 0 | from the image |

`REVIEWBOT_DRY_RUN` is `0` when unset; the env file that `install.sh` creates
sets it to `1`.

The orchestrator reviews every repository of every installation of the
GitHub App. To add or remove a repository, change the repository selection
of the installation on GitHub.

Every container is named `<prefix>-proxy-<id>`, `<prefix>-review-<id>` or
`<prefix>-fetch-<id>`, with 12 random hex digits as `<id>`.

At startup (`run`, `run --once` and `review`) the orchestrator takes the lock on
the state directory and exits 2 if `CLAUDE_CODE_OAUTH_TOKEN` is empty. It
then prepares docker. A daemon that does not answer exits 1, so that systemd
restarts the service. A missing image exits 2 with a message to run
`deploy/install.sh`. When the virtualenv holds the file `reviewbot-build-id`,
an image whose `reviewbot.build` label differs from it, or has none, exits 2
with the same message, at startup and again before each review (so images
retagged under a running orchestrator stop it at its next review, with the
pull request unrecorded); a virtualenv without the file (a development
environment) skips this check and logs that once, at startup. A missing network is
created. `<prefix>_internal` that is not an internal network exits 2, and so
does `<prefix>_default` that is internal. Every container of the three name forms above under the prefix is
removed, with one warning each. Stale work directories are removed after
that, so that no left-over container is still writing into one. A second
orchestrator on the same state directory exits with the lock error before it
touches docker.

## Customizing the review

The reviewer's system prompt has two parts. `reviewer/rules.md` is fixed: it
covers the inputs, that pull request content is untrusted, how comments cite
lines, and what never to include. `reviewer/review.md` holds the review
instructions: what to look for, what each severity means and how to choose
the verdict.

The image holds a copy of `reviewer/review.md` made when it was built, so
editing that file in the checkout changes nothing until `deploy/install.sh`
rebuilds the image. To change the review instructions without rebuilding,
copy `reviewer/review.md`, edit the copy, and set `REVIEWBOT_PROMPT_FILE` to
its path in `~/.config/reviewbot/env`; the copy then replaces the image's. It
can live in any git repository you like. The orchestrator reads the copy at
the start of every review, so edits to it apply from the next review without
a restart. Only the setting itself
needs one: systemd reads `~/.config/reviewbot/env` when the service starts, so
after adding or changing `REVIEWBOT_PROMPT_FILE`, run
`systemctl --user restart reviewbot.service`.

## Day to day

- Logs: `journalctl --user -u reviewbot -f`. Each line carries its level as
  the journal priority, so `journalctl --user -u reviewbot -p warning` shows
  only warnings and errors. The fetcher, review and proxy
  containers exist only during a review and have no log driver, so their
  output is not kept, except the proxy's: before it removes the proxy, the
  orchestrator copies the proxy's log lines with status 403 (requests the
  review container made that the proxy does not forward) into its own log as
  warnings. After every reviewer run, successful or not, a `reviewer usage`
  line gives the number of turns, the input tokens (split into cache reads,
  cache writes and uncached), the output tokens, the cost at list price as
  Claude Code reports it, and the duration.
- State: `~/.local/share/reviewbot/state/state.json` records, for each
  `owner/name#number` (the repository name as GitHub reports it), the last
  reviewed head commit and its status (`posted`, `refused`, `skipped`,
  `dry_run`, `failed`), plus the start times used for the hourly cap. The first
  three are final for that head commit, `dry_run` is final only while dry run
  is on (so turning it off reviews the pull request for real), and `failed` is
  retried. A pull request's entry is removed on the first pass that lists
  its repository without it, so the file holds only open pull requests.
  Scratch directories are in `~/.local/share/reviewbot/work` and are deleted
  after each review unless `REVIEWBOT_KEEP_WORK=1`.
- Excluding a pull request: add the label `no-reviewbot` to it. Draft pull
  requests and pull requests by bots are always skipped.
- Re-reviewing: a new push to the pull request changes the head commit and
  triggers a new review. To review the same commit again, close the pull
  request, wait for one pass (`REVIEWBOT_POLL_SECONDS`, 180 by default, plus
  the pass itself) and reopen it. To review a skipped pull request, stop the
  service (it holds a lock on the state directory), source the env file and
  run `venv/bin/reviewbot review owner/name#123` as in setup step 4.
- Hourly cap: `REVIEWBOT_MAX_REVIEWS_PER_HOUR` limits reviewer runs in any
  rolling hour. Failed and refused runs count, because they also use quota.
  A pull request that failed is retried up to 3 times for the same head
  commit. After the third failure, or when the output is refused, reviewbot
  posts a short notice on the pull request saying it could not review that
  commit.
- Environment failures do not count against a pull request. A review that
  fails for a reason any pull request would meet is not recorded, and the
  pass ends there: Docker cannot start a container or stops answering, the
  work directory's filesystem is short of space, GitHub does not answer or
  refuses or rate-limits a token request, github.com's git endpoint is
  unreachable, the proxy container does not start or does not accept
  connections within 15 seconds, or the Anthropic API refuses the token,
  limits requests, does not know the model or fails. The pull request is
  reviewed on a later pass once the environment works. The full list is in
  `DESIGN.md`, "Environment failures and pull request failures".
- Exit status 2 is a configuration error (a bad setting, a missing image or
  token, or an image built by a different `deploy/install.sh` run than the
  installed package). `reviewbot.service` does not restart on it, so the service stays down
  until the setting is fixed and it is started again. Any other failure exit,
  such as a Docker daemon that does not answer, is restarted after 60 seconds.
  `reviewbot run --once` exits 1 when the pass raised, could not list pull
  requests, or any review failed or hit an environment failure.
- Starting: `systemctl --user start reviewbot.service`.
- Stopping: `systemctl --user stop reviewbot.service`. A stop ordinarily
  takes a few seconds and at most about 51 seconds even when Docker hangs,
  inside the 60 seconds systemd allows; a stop that arrives during a slow
  GitHub request can take longer and end in SIGKILL. A fetcher, proxy or
  review container that is running is removed, its scratch directory is
  removed, and that review is not recorded, so the pull request is reviewed
  again after the next start.
  Scratch directories and containers left behind by an orchestrator that was
  killed are removed when it starts (the scratch directories unless
  `REVIEWBOT_KEEP_WORK=1`). A proxy that no orchestrator removes stops itself
  after 30 minutes.
- Restarting: `systemctl --user restart reviewbot.service`.
- Disabling: `systemctl --user disable reviewbot.service` stops it from
  starting at login.
- Moving the checkout: nothing breaks, because the installed package does not
  read it. Run `deploy/install.sh` from the new location for the next update.

## Updating reviewbot

```
git pull
deploy/install.sh
```

`deploy/install.sh` stops `reviewbot.service` if it is running, rebuilds the
three images, reinstalls the orchestrator package into the virtualenv,
refreshes the unit file and starts the service again (it checks that the
service stays active for a few seconds). The service is down for the whole
update, so it never runs new images with the old package or the reverse. If
the script fails after stopping the service, it says so and leaves the
service stopped. A service that was not running stays stopped.
`deploy/build-images.sh` alone is not a deployment step: it neither updates
the installed package nor restarts the service, and the orchestrator,
which checks the images' build label before every review, exits 2 at its next
review (or at its next start).

## Tests

`TESTING.md` describes every test suite: the orchestrator and proxy unit
tests (no Docker), the shell script lint, the end-to-end suite (Docker and a
Claude token; it spends subscription quota and can run on a host where
reviewbot is deployed), and the deploy test of `deploy/install.sh`.

## Updating Claude Code

The reviewer image installs a pinned Claude Code release. The pin is the
build arguments `CLAUDE_VERSION` and `CLAUDE_SHA256` in `reviewer/Dockerfile`
(the `linux-x64` checksum from
`https://downloads.claude.ai/claude-code-releases/<version>/manifest.json`),
or the same names passed with `--build-arg`. The build verifies the
manifest's GPG signature against the release key fingerprint
`31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE` (build argument
`RELEASE_FINGERPRINT`), checks that the manifest lists the pinned checksum, and
checks the binary against it. Any mismatch fails the build.

The review command uses two flags that Claude Code 2.1.310 accepts but does not
list in `--help`: `--max-turns` and `--append-system-prompt-file`. A later
version could remove them without notice. After changing the pinned version,
check that both are still accepted (an unknown flag fails with `error: unknown
option`, and `--append-system-prompt-file /nonexistent` fails with `Append
system prompt file not found`), then run `tests/e2e/run_all.sh` and
`tests/e2e/run_all.sh upstream_errors`. The second measures what the
binary prints when the API answers 401, 403, 404, 429, 500, 529 and 400, and
writes it to `reviewer_api_errors.json` in its output directory, in the
format of `orchestrator/tests/fixtures/reviewer_api_errors.json`. If the
output's shape changed, copy that file over the fixture and run the
orchestrator unit tests; `orchestrator/tests/test_environment.py` runs the
pipeline on the fixture and is what checks the classification of API errors
between pin changes. The e2e run uses the prefix `reviewbot-e2e`, so it builds
and tests its own images (`reviewbot-e2e-*`) and does not update the deployed
images, so the running service keeps the version it was built with. After
the e2e suite passes, run `deploy/install.sh`, which rebuilds
`reviewbot-proxy`, `reviewbot-reviewer` and `reviewbot-fetcher` from the
same Dockerfiles and pin, and stops and starts a running service around the update.

## Known limitations

- A pull request that reliably makes the Anthropic API answer 401, 403, 404,
  429 or 5xx is never recorded and ends every pass. No mechanism is known by
  which content causes those statuses, and a per-pull-request counter would
  record pull requests again during a real outage.
- The status Claude Code reports when a Max subscription's usage limit is
  reached is not known. If it is not one of those statuses, the run is
  recorded against the pull request.
- A repository rename or transfer changes the state key of every open pull
  request, so each is reviewed and posted once more at an unchanged head.
- A 403 or 429 on the review POST (a missing permission such as "Resource not
  accessible by integration", or a rate limit) counts as an attempt against the
  pull request.
- A lone surrogate in model output (`\ud800` in the JSON) makes dry-run
  printing fail with `UnicodeEncodeError`, because the payload is printed
  with `ensure_ascii=False` to UTF-8 stdout.
- GitHub Enterprise Server is not supported, because the fetcher clones from
  github.com.
- Repositories larger than `fetch.MAX_FETCH_MB` (10240 MiB of scratch space)
  fail in the fetcher. After the third attempt, the pull request gets a
  notice saying the repository does not fit.
- Output that holds one of the orchestrator's secrets in any form other than
  the exact string (split by whitespace, across fields, or encoded) is not
  refused.
- When the base repository cannot be listed, the fetcher decides that
  github.com's git endpoint is unreachable by listing `github.com/git/git`.
  If that repository were removed, such a fetch would exit 75 and end every
  pass.
- A stop that arrives during a GitHub request waits for it (30 second
  timeout, GET requests retried twice), so it can exceed the 60 seconds
  systemd allows and end in SIGKILL. No container is running then, and the
  startup cleanup removes the work directory.
- Two orchestrators with different state directories but the same work
  directory remove each other's work directories at startup. The deployment
  runs one orchestrator per user.
