# reviewbot design

reviewbot reviews pull requests on GitHub repositories using a Claude Max
subscription. It runs on the owner's machine under rootless Docker. Nothing
in this system uses an Anthropic API key.

This document describes the components and the interfaces between them.
Setting up and running reviewbot is in `README.md`; the test suites are in
`TESTING.md`.

## Docker names

Every docker name derives from `REVIEWBOT_DOCKER_PREFIX`, written `<prefix>`
below. Its default is `reviewbot`, which gives the names `reviewbot-proxy`,
`reviewbot_internal` and so on that appear in the other documents. It must
match `^[a-z][a-z0-9-]{0,31}$`; the orchestrator exits 2 otherwise. The
derived names are:

- images: `<prefix>-proxy`, `<prefix>-reviewer`, `<prefix>-fetcher`
- networks: `<prefix>_internal`, created with `--internal` so that it has no
  route out, and `<prefix>_default`, an ordinary bridge network
- containers: `<prefix>-proxy-<id>`, `<prefix>-review-<id>` and
  `<prefix>-fetch-<id>`, where `<id>` is 12 random hex digits. The repository
  and pull request number appear in the orchestrator's log line for a
  container, not in its name.

The prefix exists so that a second instance can run beside the deployment
with its own images, networks and containers; the end-to-end suite runs under
the prefix `reviewbot-e2e`.

## Components

| Component | Runs where | Network | Secrets it holds |
|---|---|---|---|
| orchestrator | host (python venv) | normal egress | GitHub App private key; the real Claude token, which it passes to each proxy and looks for in review output |
| fetcher container | `docker run`, one per review | normal egress, not restricted by network policy; it holds no credential except its GitHub token | one short-lived GitHub installation token, read-only, passed in env |
| review container | `docker run`, one per review | `<prefix>_internal` only (an internal network, no route out) | a dummy `CLAUDE_CODE_OAUTH_TOKEN`. Never the real token. No GitHub credential. |
| proxy container | `docker run`, one per review, started by the orchestrator after the fetch succeeds and removed when the review ends | attached to both `<prefix>_internal` and `<prefix>_default` | the real Claude OAuth token from `claude setup-token` |

## Images

The user each image runs as, and other facts about the images that the
orchestrator relies on:

| Image | Runs as | Notes |
|---|---|---|
| `<prefix>-reviewer` | uid/gid 10001 (`USER 10001:10001`) | Use `--tmpfs /home/reviewer:uid=10001,gid=10001,mode=0700`. Claude Code is at `/usr/local/bin/claude`, outside the home directory, so the tmpfs does not hide it. |
| `<prefix>-fetcher` | uid 0 inside the container | Under rootless Docker this is the unprivileged host user, which is what lets it write the work directory. `/work` must be empty when it starts. It makes everything it writes world-readable. |
| `<prefix>-proxy` | uid 10002 | Listens on 8080. Handles SIGTERM, so `docker stop` is immediate. |

## Filesystem contract: the work volume

The orchestrator creates one scratch directory per review, `WORK`, and mounts
it into the fetcher read-write and into the review container read-only at
`/work`.

The fetcher starts with an empty `/work` and never writes `pr.json`. The
orchestrator writes `pr.json` after the fetcher has exited, so it is the last
file to appear. It creates the file with `O_EXCL`: if the fetcher left an
entry named `pr.json`, the write fails and the review is recorded as failed.
The fetcher never sees the title, body or labels, and the review container
reads them only from this file.

Layout after the fetcher has succeeded and the orchestrator has written
`pr.json`:

```
/work/
  pr.json          metadata, schema below; written by the orchestrator (mode 0644), not the fetcher
  diff.patch       unified diff from the merge base of base_sha and head_sha to head_sha (-M, no color)
  repo/            git checkout of the PR head commit, no submodules, no hooks
  files.txt        newline-separated list of paths changed in the PR, relative to repo/
```

`pr.json`:

```json
{
  "repo": "owner/name",
  "number": 123,
  "title": "...",
  "body": "...",
  "author": "login",
  "base_ref": "main",
  "base_sha": "...",
  "head_ref": "feature",
  "head_sha": "...",
  "head_repo": "forkowner/name",
  "html_url": "https://github.com/owner/name/pull/123",
  "labels": ["..."]
}
```

The review container must treat every byte under `/work` as untrusted data,
including `pr.json` and `diff.patch`.

## Fetcher container contract

Image: `<prefix>-fetcher`. Entrypoint takes no arguments.

The fetcher never calls the GitHub API. Its only network operations are `git`
over HTTPS to `github.com`:

- `git fetch` of the base commit and of `refs/pull/<number>/head`, which also
  covers pull requests from forks;
- a second fetch of the exact `head_sha`, when that ref has moved since the
  orchestrator read the pull request;
- `--deepen` and `--unshallow` fetches, only when no merge base is found at
  the initial depth;
- two `git ls-remote` probes, run only after a fetch has failed, to tell an
  unreachable git endpoint from a missing repository. The first probes the
  base repository. If it fails, the second probes the public repository
  `git/git`, with `GITHUB_TOKEN` unset.

The image adds only `git`, `jq` and CA certificates to Alpine, and the
entrypoint contains no other network call. The reason is that the fetcher
handles hostile checkouts. It holds one short-lived token with clone rights
and has no code path that could use a stronger one. The GitHub API is used
only by the orchestrator, to list pull requests, mint installation tokens
and post reviews.

For the same reason `PR_JSON` carries exactly `repo`, `number`, `base_sha`
and `head_sha`. `repo` and `number` locate the pull request and its ref.
`head_sha` pins the review to the commit the orchestrator recorded, so the
fetcher notices a push made after the orchestrator read the pull request and
fetches the exact commit. `base_sha` fixes the merge base for the diff.
Deriving the shas from a pull request URL would require the fetcher to query
the API, which the rule forbids.

Inputs via env:

- `GITHUB_TOKEN`: installation token
- `PR_JSON`: a JSON object with only the `repo`, `number`, `base_sha` and `head_sha` of `pr.json`.
  The rest of `pr.json` (title, body and so on) is not passed: it can exceed
  the 128 KiB Linux allows for one environment string.

Mounts: `/work` read-write (the work volume above), and `/scratch`, a tmpfs
of N = 10240 megabytes (`fetch.MAX_FETCH_MB`) with N * 256 inodes
(`--tmpfs /scratch:rw,size=<N>m,nr_inodes=<N*256>`). The container's
`--memory` is that size plus 1024 MB, because tmpfs pages count against the
memory limit; a fetch that needs more than the tmpfs then fails with ENOSPC
rather than an OOM kill. `/log`, a tmpfs of 16 megabytes
(`fetch.LOG_TMPFS_MB`), holds git's error output, so that git's message
survives a full `/scratch`. The fetcher exits non-zero if `/scratch` or `/log`
is not a tmpfs mount.

Behaviour: clone `repo` at `base_sha` with depth sufficient to diff, fetch
`refs/pull/<number>/head`, check out `head_sha`, write `diff.patch` and
`files.txt`. The git directory, the checkout and both files are made in
`/scratch`; only after every check has passed are `repo/`, `diff.patch` and
`files.txt` copied to `/work`, so `/work` never receives more than the
tmpfs holds, plus a directory block for each directory. A fetch that runs
out of space or inodes in `/scratch` fails. The fetcher does not write
`pr.json`; after the fetcher succeeds, the orchestrator writes the full
`pr.json` into `/work` (refusing if a file of that name already exists).
Git must run with
`-c core.hooksPath=/dev/null -c core.fsmonitor=false -c protocol.allow=never -c protocol.https.allow=always`
and `--no-recurse-submodules`. Remove `/work/repo/.git` after checkout so the
review container cannot see refs or config. Exit non-zero with a message on
stderr on any failure. When a git fetch fails, run `git ls-remote <url> HEAD`
for the base repository and then, without the token, for the public
repository `https://github.com/git/git.git`; if both fail, github.com's git
endpoint is unreachable and the exit status is 75, which the orchestrator
treats as an environment failure (nothing recorded, the pass ends). When a
git command fails because `/scratch` ran out of space or inodes (git's error
output says "No space left on device" or "Out of diskspace"), the exit status
is 74, which the orchestrator records as a failure with the detail "the
repository does not fit in the fetcher's N MiB scratch space". Any other
failure exits with a non-zero status other than 74 and 75. The token must
never be written to disk, including into `.git/config`; the fetcher gives
git a credential helper with `-c` that reads the token from the environment,
and deletes `.git`.

## Review container contract

Image: `<prefix>-reviewer`. Built from a pinned Claude Code native release.
Runs as a non-root user. Entrypoint takes no arguments. The image holds, under
`/opt/reviewbot/`, `schema.json` and the two parts of the system prompt:

- `rules.md`, fixed: the inputs, that everything under `/work` is untrusted
  data, how to cite a comment's path and line, the 30-comment limit, and what
  never to include.
- `review.md`, the default review instructions: what to look for, how to write
  comments, what each severity means and how to choose the verdict.

The entrypoint writes `rules.md` followed by the review instructions to
`/tmp/reviewbot-prompt.md`. The review instructions are `REVIEWBOT_PROMPT`
when it is set, and `review.md` otherwise. The orchestrator sets
`REVIEWBOT_PROMPT` to the contents of `REVIEWBOT_PROMPT_FILE`, read for each
review, when that setting is given. The instructions travel in an environment
variable rather than a file under `/work`, because everything under `/work`
is untrusted, and rather than a bind mount, which under rootless Docker the
reviewer's uid could read only if the file and its directories were readable
by others.

Environment: the entrypoint exports `CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-dummy`,
a stand-in that the proxy replaces with the real token, overriding any value
passed to the container. The image's `ENV` sets the other constants:

- `ANTHROPIC_BASE_URL=http://proxy:8080`
- `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, `DISABLE_TELEMETRY=1`,
  `DISABLE_ERROR_REPORTING=1`, `DISABLE_AUTOUPDATER=1`, `DISABLE_UPDATES=1`,
  `ENABLE_CLAUDEAI_MCP_SERVERS=false`, `CLAUDE_CODE_DISABLE_ARTIFACT=1`
- defaults `REVIEWBOT_MODEL=opus` and `REVIEWBOT_MAX_TURNS=40`

The orchestrator passes only `REVIEWBOT_MODEL`, `REVIEWBOT_MAX_TURNS` and,
when `REVIEWBOT_PROMPT_FILE` is set, `REVIEWBOT_PROMPT`, by name (`-e NAME`;
docker leaves out a name that is unset). It keeps one copy of the dummy token (`review.DUMMY_TOKEN`),
which the end-to-end leak tests look for, and a unit test asserts that it
equals the value the entrypoint exports and the one in `tests/e2e/lib.sh`.

The entrypoint runs, from `/work` as cwd:

```
claude -p --restricted --tools Read,Grep,Glob \
  --permission-prompts none --permission-mode dontAsk \
  --setting-sources "" --strict-mcp-config --disable-slash-commands \
  --no-session-persistence --max-turns "$REVIEWBOT_MAX_TURNS" --model "$REVIEWBOT_MODEL" \
  --output-format json --json-schema "$(cat /opt/reviewbot/schema.json)" \
  --append-system-prompt-file /tmp/reviewbot-prompt.md \
  "Review the pull request described in /work/pr.json. The diff is /work/diff.patch and the checkout is /work/repo."
```

stdout: exactly the JSON that `claude -p --output-format json` prints. The
orchestrator reads `.structured_output`. stderr: logs. Exit code: 0 on a
completed run, non-zero otherwise. The orchestrator treats missing
`structured_output` as a failed review and posts nothing.

Docker flags the orchestrator must use for this container:
`--rm --log-driver none --network <prefix>_internal --read-only --tmpfs /tmp --tmpfs /home/reviewer:uid=<uid>,gid=<gid>,mode=0700 --cap-drop ALL --security-opt no-new-privileges --pids-limit 256 --memory 4g --cpus 2 -v WORK:/work:ro`
plus a wall clock timeout (default 20 minutes) after which it is killed.

## Structured output schema (`schema.json`)

The file is `orchestrator/src/reviewbot/schema.json`, the package's data file
that the validator reads. `deploy/build-images.sh` passes its directory to
the reviewer build as the build context `schema`, which is where the image's
copy comes from.

```json
{
  "type": "object",
  "required": ["summary", "verdict", "comments"],
  "properties": {
    "summary": {"type": "string", "maxLength": 4000},
    "verdict": {"type": "string", "enum": ["approve", "comment", "request_changes"]},
    "comments": {
      "type": "array", "maxItems": 30,
      "items": {
        "type": "object",
        "required": ["path", "line", "severity", "body"],
        "properties": {
          "path": {"type": "string"},
          "line": {"type": "integer", "minimum": 1},
          "severity": {"type": "string", "enum": ["bug", "security", "suggestion", "nit"]},
          "body": {"type": "string", "maxLength": 1500}
        }
      }
    }
  }
}
```

`line` refers to a line number in the new (head) version of `path`.

## Proxy container contract

Image: `<prefix>-proxy`. Listens on `:8080`. Env: `CLAUDE_CODE_OAUTH_TOKEN`
(the real one, from `claude setup-token`).

Lifetime: the orchestrator starts one proxy container per review, after the
fetcher has succeeded and `pr.json` is written, and only for a pull request
that changes files. It removes the container in the same `finally` that
removes the reviewer. No proxy runs between reviews.

How it is started:

- `docker run -d --rm --name <prefix>-proxy-<id> --network
  <prefix>_internal --network-alias proxy --read-only --cap-drop ALL
  --security-opt no-new-privileges --pids-limit 128 --memory 256m -e
  CLAUDE_CODE_OAUTH_TOKEN <prefix>-proxy`. The review container reaches it as
  `http://proxy:8080`. The proxy keeps docker's default log driver, because
  the orchestrator reads its log.
- The token is passed by name (`-e CLAUDE_CODE_OAUTH_TOKEN` with no value),
  so docker takes it from the `docker` client's environment and it appears
  on no command line. The proxy's environment holds that variable only;
  `REVIEWBOT_UPSTREAM` (default `https://api.anthropic.com`) is set only by
  the end-to-end suite, to point the proxy at a stub.
- `docker network connect <prefix>_default`, the network the proxy uses to
  reach `api.anthropic.com`.
- Readiness: every 0.5 seconds the orchestrator runs `docker exec` in the
  container to open a TCP connection to `127.0.0.1:8080`, for at most 15
  seconds. The reviewer is not started before the probe succeeds.
- A start that fails or a proxy that is not ready in time is removed and is
  an environment failure: the pass ends and nothing is recorded against the
  pull request.

How it ends: after the reviewer has ended, the orchestrator reads the proxy's
log (`docker logs --tail 200`), writes every line that contains ` 403 ` to
its own log at warning level, and removes the container. The proxy also stops
itself after `MAX_LIFETIME_SECONDS` (1800 seconds), and `--rm` then removes
the container, so a proxy left by an orchestrator that was killed does not
outlive that. A review holds its proxy for at most 15 + 1200 + 5 x 10 = 1265
seconds (readiness wait, review timeout, cleanup steps), so a request still
running at 1800 seconds belongs to no live review.

Behaviour: the proxy is a reverse proxy: it accepts plain HTTP requests and
forwards them to `https://api.anthropic.com` with the incoming
`Authorization` and `X-Api-Key` headers removed and `Authorization: Bearer
<real token>` set. It streams response bodies (SSE) without buffering. It
forwards only `POST /v1/messages` and `POST /v1/messages/count_tokens`; every
other method and path returns 403 and is logged by path only. The
orchestrator copies those log lines into its own log before it removes the
container. The proxy never logs request or response bodies or any header
value.

## Orchestrator contract

Python 3.12, standard library plus `PyJWT[crypto]` and `requests` only.

The orchestrator's configuration is environment variables, read by
`reviewbot.service` from `~/.config/reviewbot/env` (not committed; mode
0600, because it holds the Claude token):

- `CLAUDE_CODE_OAUTH_TOKEN`: the real Claude token. The orchestrator passes
  it by name to each proxy container and uses it for output validation; it
  exits 2 at startup when the variable is unset or empty.
- `REVIEWBOT_DOCKER_PREFIX` (default `reviewbot`): see "Docker names".
- `REVIEWBOT_APP_ID`, `REVIEWBOT_APP_PRIVATE_KEY_PATH`: the GitHub App; both required
- `REVIEWBOT_POLL_SECONDS` (default 180), `REVIEWBOT_MAX_REVIEWS_PER_HOUR` (default 6)
- `REVIEWBOT_STATE_DIR` (default `./state`), `REVIEWBOT_WORK_DIR` (default `./work`)
- `REVIEWBOT_DRY_RUN`: `1` prints the review instead of posting

Startup (`run`, `run --once` and `review`), in this order: the orchestrator takes
the lock on the state directory, loads the state, and exits 2 if
`CLAUDE_CODE_OAUTH_TOKEN` is empty. It then prepares docker:

- a docker daemon that does not answer exits 1, so that systemd restarts the
  service;
- a missing image exits 2 with a message that says to run
  `deploy/install.sh`;
- the build id check, after the three images are known to exist. The file
  `reviewbot-build-id` in `sys.prefix` (the virtualenv) holds the id that
  `deploy/install.sh` generated for its run, and `deploy/build-images.sh`
  gave every image the label `reviewbot.build` with the same value. For each
  image the orchestrator runs
  `docker image inspect --format '{{index .Config.Labels "reviewbot.build"}}'`;
  a label that differs from the id, or is empty, exits 2 with a message that
  names the image, its label and the expected id and says to run
  `deploy/install.sh`, and so does an empty id file. When the file is absent
  (a development virtualenv, or the end-to-end suite) the orchestrator logs
  one line that it is not checking and continues. The same check runs
  again before the fetcher of every review (see "Loop" below);
- a missing network is created, and a failed create exits 1;
- `<prefix>_internal` that is not an internal network exits 2, and so does
  `<prefix>_default` that is internal;
- every container named `<prefix>-proxy-<id>`, `<prefix>-review-<id>` or
  `<prefix>-fetch-<id>` is removed, with one warning each. Containers with
  any other name are left alone.

Stale work directories are removed after that, so that no left-over
container is still writing into one. A second orchestrator on the same state
directory exits with the lock error before it touches docker.

Before each review starts (before its work directory is made) the
orchestrator repeats the build id check. A failure is not a pull request
failure: nothing is recorded for the pull request, and the process exits 2
as it would at startup. This catches images retagged under a running
orchestrator, for example by running `deploy/build-images.sh` by hand.

Loop: list every repository of every installation of the GitHub App, list
open non-draft PRs, skip PRs with the label `no-reviewbot` or a bot author,
skip PRs whose `head_sha` equals the last reviewed sha in state, respect the
hourly cap, then for each: run fetcher, write `pr.json`, start the proxy
(only for a pull request that changes files), run reviewer, remove the proxy,
validate output, post, record `head_sha` in state. State is a JSON file keyed
by `owner/name#number`.

Posting: `POST /repos/{owner}/{repo}/pulls/{number}/reviews` with
`event: COMMENT` (never `APPROVE` or `REQUEST_CHANGES`; the verdict goes in
the body text). Inline comments use `path`, `line`, `side: RIGHT`,
`commit_id: head_sha`. A comment whose `path` is not in `files.txt` or whose
line is not an added or context line in `diff.patch` is moved into the body
text instead of dropped.

Output validation before posting, in order; any failure means nothing is posted
and the failure is logged:

1. `structured_output` present and valid against `schema.json`
2. no field contains the real Claude token, which the orchestrator reads
   from `CLAUDE_CODE_OAUTH_TOKEN`
3. no field contains a GitHub token the orchestrator is using for this
   review
4. total body length under 60000 characters

### Environment failures and pull request failures

A failure that any pull request would meet in the same way is an environment
failure. It is not recorded, so it cannot use up the three attempts allowed
for a head commit; the pass ends and the pull request is reviewed on a later
pass. A failure that the pull request's content could cause, or after which
GitHub may already have created the review, is recorded as `failed`.

Environment failures (nothing recorded, the pass ends):

- The `docker` command cannot be run, or `docker run` exits 125 (Docker could
  not create or start the container), or a container fails and `docker
  version` then gets no answer from the daemon.
- The work directory cannot be created, `pr.json` cannot be written for a
  reason other than an existing entry, or the work directory's filesystem has
  less than 40960 MB free.
- The reviewer image cannot be inspected for its uid and gid.
- The proxy container cannot be started or does not accept connections within
  15 seconds.
- A GitHub installation token request gets no response, 401, 403, 429 or a 5xx.
- The Anthropic API answered the reviewer 401, 403, 404, 429 or 500 to 599,
  read from the top-level `is_error`, `terminal_reason` (`api_error`) and
  `api_error_status` fields of Claude Code's result, never from the model's
  text. 404 means the model in `REVIEWBOT_MODEL` is unknown.
- The fetcher exits 75 without timing out.
- A stop request.

Recorded as `failed` against the pull request:

- The Anthropic API answered 400 (the API refused the request, which a large
  pull request can cause) or any other status not listed above, the run ended
  at `max_turns`, or stdout is not JSON.
- A fetcher or reviewer timeout.
- Fetcher exit 74: the repository does not fit in `/scratch`.
- Fetcher exit 1 and every other non-zero status except 74 and 75, including
  126 and 127.
- A container that fails while the daemon answers.
- A rejected fetcher output.
- A GitHub token request that gets 404 or 422 (the repository is not in the
  installation).
- A review POST that fails: every 4xx, including a rate limit and a 403 for a
  missing permission, every 5xx, and a lost response. A POST whose response
  was lost may have created the review, and recording it limits repeats.
- A step 4 refusal (total body too long).

A reviewer run that ends in an environment failure, and a proxy that fails to
start or become ready, still count against the hourly cap.

When a head commit will not be reviewed again, the orchestrator posts a
notice on the pull request, as a review with event `COMMENT` and a body only:

- after the third `failed` record for that commit, quoting the last failure's
  detail. Only failures whose detail the orchestrator wrote are quoted: a
  container's exit status, a rejected fetcher output, a token request's
  status, a schema error or the body length. An unexpected exception or a
  failed review POST gets no notice.
- after a `refused` record, saying only that the output failed a safety
  check, so the notice does not tell a prompt injection which check it met.

No notice is posted in dry run (it is logged), and a notice that cannot be
posted is logged and does not change the outcome.
