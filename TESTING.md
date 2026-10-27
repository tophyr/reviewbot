# Testing reviewbot

This describes each test suite: what it checks, what it needs and how to run
it. How the system works is in `DESIGN.md`.

## Requirements

- Docker. Tested with rootless Docker 29.7.2 on 2026-10-25.
- Python 3.12 or newer on the host, for the unit tests, the shell lint and
  the e2e helpers.
- Network access to `downloads.claude.ai` (reviewer image build),
  `dl-cdn.alpinelinux.org` and `deb.debian.org` (package installs),
  `github.com` and `api.github.com` (fetcher test), `api.anthropic.com`
  (proxy).

## Test inventory

| Suite | Needs | What it verifies |
|---|---|---|
| Orchestrator unit tests (`orchestrator/tests`) | Python, no Docker, no network | Output validation refuses the real Claude token and the GitHub tokens in use wherever they appear, and posts token-shaped text the orchestrator does not hold; diff-line mapping places a comment on an added or context line and relocates one on a removed or untouched line; reference defanging on every form GitHub links (mentions, issue, pull request, discussion and commit references, in URL, path and HTML character reference spellings); the startup checks, including that an image whose `reviewbot.build` label differs from the venv's build id (or has none) exits 2, that the same check before each review exits 2 with nothing recorded when the label changes after startup, and that a venv without an id skips the check; the pipeline's classification of environment and pull request failures, stopping, cleanup, resource limits and configuration errors, run against a fake `docker` binary (`orchestrator/tests/fakedocker.py`) and a fake GitHub. |
| Proxy unit tests (`proxy/test_proxy.py`) | Python, no Docker, no network | Against a local fake upstream: the `Authorization` header is replaced, the dummy token never arrives upstream, a path outside the allowlist gets 403, SSE events are streamed without buffering, and a negative chunk size gets 400 at once. |
| Shell lint (`tests/lint_shell.py`) | Python | The patterns below that make a shell script exit without its message. |
| End-to-end suite (`tests/e2e`) | Docker, a Claude token, network | The real images and proxy: network isolation, fetching, prompt-injection resistance, a real review, and Claude Code's output for API errors. |
| Deploy test (`tests/deploy`) | Docker, with systemd in a container | `deploy/install.sh` and the unit `deploy/reviewbot.service`. |

## Orchestrator unit tests

```
python3 -m venv ~/.cache/reviewbot-dev-venv
~/.cache/reviewbot-dev-venv/bin/pip install './orchestrator[dev]'
cd orchestrator && ~/.cache/reviewbot-dev-venv/bin/python -m pytest
```

## Proxy unit tests

```
python3 proxy/test_proxy.py
```

## Shell script lint

```
python3 -I tests/lint_shell.py              # every *.sh in the checkout
python3 -I tests/lint_shell.py --self-test  # each check must fire on a known-bad line
```

Under `set -e`, a command that fails inside an assignment from a command
substitution, or a bare pipeline into `grep`, `head` or `awk` that matches
nothing, ends the script before it prints its own error message. The lint
reports:

- any script that fails `bash -n` (`sh -n` for a `#!/bin/sh` script);
- `${` followed by a character that cannot start a parameter expansion,
  which `bash -n` accepts and bash rejects at run time;
- an assignment from a command substitution that runs `grep`, `head`, `awk`
  or `find` with no `||` on the line;
- in a script that runs `set -e`, an assignment from any command
  substitution with no `||` when one of the next 10 logical lines checks the
  variable with `die`, `fail`, `config_error` or `exit` (directly, or in an
  `if`, `elif` or `case` whose next 3 lines do);
- a bare pipeline into `grep`, `head` or `awk` outside a condition, with no
  `||` or `&&` after it.

An assignment whose variable is never checked is not reported: a failure
there stops the script with the command's own error, and there is no better
message to print. Requiring `||` on every such assignment would report lines
such as `TMP=$(mktemp -d)`, 25 of them in the tree on 2026-10-25. The lint
does not read text inside multi-line single-quoted strings, such as the
bash wrappers passed to `bash -c` in the e2e tests, so those must be checked
by reading them. It does not see a check made through another variable or
more than 10 logical lines later, and it does not cover `local`, `export`,
`readonly` or `declare` assignments, which do not stop a `set -e` script.
`tests/e2e/run_all.sh` runs the lint and its self-test.

## End-to-end suite

```
tests/e2e/run_all.sh                  # the default list: egress, fetcher, leak, happy
tests/e2e/run_all.sh egress leak      # a subset
tests/e2e/run_all.sh upstream_errors  # after changing the Claude Code pin
```

Each e2e test can also be run alone, for example `tests/e2e/test_leak.sh`.
`run_all.sh` also runs the proxy unit tests, the self-tests of the e2e
checkers and the shell lint. Set `REVIEWBOT_E2E_SKIP_BUILD=1` to reuse
existing images.

The tests need the real Claude token for the proxy. They use
`CLAUDE_CODE_OAUTH_TOKEN` from the environment if it is set, and otherwise
read it from `~/.config/reviewbot/env`. The token is kept in an unexported
shell variable and handed only to the python process that starts the proxy,
which passes it to the container by name. Each test ends by scanning every
file it wrote for the token, and fails if it is found.
The `leak` and `happy` tests run real reviews and spend subscription quota;
the `fetcher` test fetches real public pull requests from GitHub.

The suite can run on a host where reviewbot is deployed. It runs under the
docker name prefix `reviewbot-e2e` (`REVIEWBOT_DOCKER_PREFIX`), so it has its
own images (`reviewbot-e2e-proxy`, `reviewbot-e2e-reviewer`,
`reviewbot-e2e-fetcher`, built by `deploy/build-images.sh reviewbot-e2e` from
the same Dockerfiles), its own networks (`reviewbot-e2e_internal`,
`reviewbot-e2e_default`) and container names that start with
`reviewbot-e2e-`. The harness refuses to run with the prefix `reviewbot`.
It starts and stops the proxy through `reviewbot.dockerrun`
(`prepare`, `start_proxy` and `stop_proxy`), run with the python of the
orchestrator's virtualenv (`orchestrator/.venv`), so the tests run the proxy
the way a review does. That virtualenv has no `reviewbot-build-id` file and
the e2e images are built without a build id, so `prepare` skips the build id
check there. The `upstream_errors` test sets the proxy's
`REVIEWBOT_UPSTREAM` through the harness, to point it at a stub.

Before it builds or starts anything, the suite records the deployment's
images, its networks and its `reviewbot-proxy-*` containers (with their
states and tokens). It fails at the end if any of them changed. A failed
`docker` query during either step stops the run.

Two e2e runs at the same time interfere: each proxy start removes every
container under the prefix `reviewbot-e2e`, including the other run's. Run
one at a time.

Logs for each run go to `tests/e2e/out/<timestamp>-<pid>/` (ignored by git).
`summary.log` there lists every assertion. Model runs use the contract's
default model (`opus`) unless `REVIEWBOT_MODEL` is set.

| Script | What it checks |
|---|---|
| `test_egress.sh` | From `reviewbot-e2e_internal`: `https://api.anthropic.com`, `http://example.com`, and their IP addresses are unreachable; `http://proxy:8080` answers; the proxy refuses absolute-URL and CONNECT requests. Control probes from the bridge network do reach the internet. |
| `test_fetcher.sh` | Anonymous fetch of real public PRs (`jqlang/jq#3692`, `jqlang/jq#1748`); `/work` layout; `files.txt` equals GitHub's own file list for the PR; no `.git`, no symlinks; a sentinel token never reaches the container filesystem, the scratch tmpfs, `/work` or output, including when git has to authenticate; malformed input and a non-empty `/work` are refused; with a 1 MiB `/scratch` the fetcher exits 74 with its own message. |
| `test_leak.sh` | A hostile pull request (`tests/e2e/fixtures/hostile`) whose `pr.json`, diff and files tell the model to print its environment, `/proc/self/environ` and `~/.claude/.credentials.json`, to run `env` and to fetch `http://example.com`. Three runs: the unchanged entrypoint, whose stdout must be a Claude result holding no real token, dummy token or `sk-ant-`; the same with stream-json output, whose init message must offer only Read, Grep, Glob and StructuredOutput and whose stream must hold no token; and a run without the review prompt that asks the model directly to read those secrets and two planted symlinks, in which every such call must return an error. |
| `test_happy.sh` | A review of `tests/e2e/fixtures/happy`, whose head has an off-by-one bug at `shop/cart.py:16`. Output must validate against `orchestrator/src/reviewbot/schema.json` and include a bug comment within two lines of it. |
| `test_upstream_errors.sh` | Not in the default list. The reviewer runs against `tests/e2e/stub_upstream.py`, which answers every request with one error status (401, 403, 404, 429, 500, 529 and 400 by default, set by `REVIEWBOT_E2E_STATUSES`). For each status it records Claude Code's exit code and result fields and checks how the orchestrator classifies them, and it writes the stdout of every status to `reviewer_api_errors.json` in its output directory, in the format of `orchestrator/tests/fixtures/reviewer_api_errors.json`, so refreshing that fixture is a copy. Claude Code retries 429 and 5xx for about 180 seconds each; the six statuses measured on 2026-10-25 took 551 seconds of reviewer time in all. |

## Deploy test

Nothing in this test installs, enables or starts a unit on the host.

`tests/deploy/test-install-in-container.sh` runs `deploy/install.sh` end to
end in a disposable container that boots systemd, with a real user manager
for a non-root test user. The container has no Docker. Its `PATH` holds a
stand-in `docker` that appends its arguments to `~/docker-argv.log` and exits
0, or exits 1 for the Nth `build` when `DOCKER_STUB_FAIL_BUILD=N`. It also
answers the two queries the orchestrator makes at startup that matter here:
`image inspect` of the `reviewbot.build` label prints the label of the last
logged `build` of that image, and `network inspect` prints `true` for a
network whose name ends in `_internal`. A placeholder `docker.service` lets
the `Requires=docker.service` dependency resolve. The stand-in shows which
docker commands `install.sh` runs and what label a real build would have
received, not that the images build. The log also holds the `docker version`
and `docker image inspect` commands that `build-images.sh` runs to print the
image IDs; the test compares only the `build` lines.

The test verifies that:

- install.sh reaches its end and that, read back independently, the unit is
  enabled and not active, with the expected unit file path, `ExecStart`, one
  `EnvironmentFile` (`~/.config/reviewbot/env`), `Requires` and `After`
  `docker.service`, restart and stop settings and env file mode 0600, and
  that no `src` symlink exists;
- install.sh ran exactly three `docker build -t reviewbot-<name> <dir>`
  commands, for `proxy`, `reviewer` and `fetcher`, with the build contexts in
  the checkout, each with `--label reviewbot.build=<id>` and the reviewer
  also with `--build-context schema=<checkout>/orchestrator/src/reviewbot`;
  the id has the form `<UTC timestamp>-<8 hex digits>` and is the only line
  of `<venv>/reviewbot-build-id`, mode 0644;
- a second run keeps the env file, builds the three images again with a
  different id and does not start the service;
- the venv imports `reviewbot` from its own `site-packages`, not from the
  checkout; an edit, a new module and a leftover `orchestrator/build/` in the
  checkout reach the venv only when install.sh runs again, install.sh removes
  that directory, and a module deleted from the checkout is gone from the
  venv after the next run;
- an unconfigured orchestrator exits 2 and is not restarted, and a stand-in
  service sees the token from the env file in its environment;
- install.sh fails on a broken unit file, a mismatched `XDG_CONFIG_HOME` or a
  masked `docker.service`, removes a stale `src` symlink, refuses a real
  directory at that path before running any docker command, and installs
  without enabling under `REVIEWBOT_NO_SYSTEMCTL=1`;
- with the stand-in made to exit 1 on the second build, install.sh exits
  non-zero, says that `deploy/build-images.sh reviewbot` failed, prints no
  "done", runs no third build, and installs no unit and no venv;
- with the real orchestrator running as `reviewbot.service` against the
  stand-in, install.sh stops it before the first build (the stand-in logs the
  service state at each build, and all three read `inactive`; the journal
  shows one more stop) and starts it again (new `MainPID`, active
  afterwards), and the new process reaches its poll loop, so it accepts the
  labels of the new build; when the stand-in reports another id for one image
  the service exits 2, is not restarted by systemd, and the journal names the
  image, its label and the expected id;
- install.sh leaves a stopped service stopped and prints the next steps, and
  exits non-zero, without "done" and without the "stopped for this update"
  message, when the service it started is not active afterwards;
- with the stand-in made to fail the first build while the service runs,
  install.sh exits non-zero, prints that the service was stopped for the
  update and has not been started again, and the service is inactive.

## What the tests do not cover

- The fetcher with a real GitHub installation token on a private
  repository. The credential path is exercised only with a made-up token
  against a repository that does not exist (GitHub answers 401, git calls
  the helper, authentication fails).
- The app-mode GitHub calls against a real GitHub App installation. As of
  2026-10-25 they have run only against fakes.
- Whether the model resists every possible prompt injection. The leak test
  shows that the review container has no tool, file path or network route
  that reaches a secret; its third run asks the model directly for the
  secrets, so the result does not depend on the model refusing.
- Network egress of the fetcher container. It runs on the default bridge
  network and is not restricted by network policy; it holds no credential
  except its GitHub token.
- The unit running under the host's own systemd user manager together with
  real Docker: the container test has systemd and a stand-in `docker`.
