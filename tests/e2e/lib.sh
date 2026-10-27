# Shared helpers for the e2e tests. Source this file; do not run it.
#
# The tests use the docker prefix "reviewbot-e2e" (REVIEWBOT_DOCKER_PREFIX):
# images reviewbot-e2e-proxy, reviewbot-e2e-reviewer and reviewbot-e2e-fetcher,
# built by deploy/build-images.sh from the same Dockerfiles as the deployment,
# and networks reviewbot-e2e_internal and reviewbot-e2e_default. They never
# build, start, change or remove anything with the deployment's prefix
# "reviewbot". The deployment's images, networks and proxy containers must be
# the same before and after (prod_proxy_record before the first build or proxy
# start, prod_proxy_verify at the end).
#
# The proxy is started and removed by the orchestrator's own code
# (reviewbot.dockerrun.prepare, start_proxy and stop_proxy), run with the
# orchestrator's virtual environment, so the tests run the same proxy as a
# review does.
#
# The real Claude token is held in the shell variable REAL_TOKEN, which is
# never exported. It is passed only to the python process that starts the
# proxy, and to python scanners, through their environment (never on a
# command line, where other processes could read it).

set -euo pipefail

E2E_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$E2E_DIR/../.." && pwd)"
PREFIX="reviewbot-e2e"
PROD_PREFIX="reviewbot"
if [ "$PREFIX" = "$PROD_PREFIX" ]; then
  echo "lib.sh: the e2e prefix must not be the deployment's prefix $PROD_PREFIX" >&2
  exit 2
fi
PY="$ROOT/orchestrator/.venv/bin/python"
# The stand-in token reviewer/entrypoint.sh exports, for the leak scans; must
# equal it (orchestrator/tests/test_containers.py compares them).
DUMMY_TOKEN="sk-ant-oat01-dummy"
REVIEWER_IMAGE="$PREFIX-reviewer"
FETCHER_IMAGE="$PREFIX-fetcher"
PROXY_IMAGE="$PREFIX-proxy"
INTERNAL_NET="${PREFIX}_internal"
DEFAULT_NET="${PREFIX}_default"
# The deployment's names, which no e2e test may build, start, change or remove.
PROD_IMAGES="$PROD_PREFIX-proxy $PROD_PREFIX-reviewer $PROD_PREFIX-fetcher"
PROD_NETS="${PROD_PREFIX}_internal ${PROD_PREFIX}_default"
# Where the deployment keeps its token (KEY=VALUE lines).
DEPLOY_ENV="$HOME/.config/reviewbot/env"

# Per-run output directory. Holds every log the tests write, and is scanned
# for the real token at the end.
OUT="${REVIEWBOT_E2E_OUT:-$E2E_DIR/out/$(date -u +%Y%m%dT%H%M%SZ)-$$}"
mkdir -p "$OUT"

FAILS=0
PASSES=0

log() { printf '%s\n' "$*" | tee -a "$OUT/summary.log" >&2; }
pass() { PASSES=$((PASSES + 1)); log "PASS: $*"; }
fail() { FAILS=$((FAILS + 1)); log "FAIL: $*"; }

finish() {
  log "result: $PASSES passed, $FAILS failed (logs in $OUT)"
  if [ "$FAILS" -ne 0 ] || [ "$PASSES" -eq 0 ]; then
    exit 1
  fi
  exit 0
}

load_real_token() {
  if [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
    REAL_TOKEN="$CLAUDE_CODE_OAUTH_TOKEN"
    log "real token: from CLAUDE_CODE_OAUTH_TOKEN"
  elif [ -f "$DEPLOY_ENV" ]; then
    # Sourced in a subshell, so nothing else in the file reaches this shell.
    REAL_TOKEN="$(set -a; . "$DEPLOY_ENV" >/dev/null 2>&1; printf '%s' "${CLAUDE_CODE_OAUTH_TOKEN:-}")" || REAL_TOKEN=""
    log "real token: from CLAUDE_CODE_OAUTH_TOKEN in $DEPLOY_ENV"
  else
    log "real token: CLAUDE_CODE_OAUTH_TOKEN is not set and $DEPLOY_ENV does not exist"
    exit 2
  fi
  if [ "${#REAL_TOKEN}" -lt 20 ]; then
    log "real token is missing or too short"
    exit 2
  fi
  # The scanner below would otherwise compare against an empty string.
  case "$REAL_TOKEN" in
    *"$DUMMY_TOKEN"*) log "real token equals the dummy token"; exit 2 ;;
  esac
}

build_images() {
  prod_record_once
  log "building images $PROXY_IMAGE $REVIEWER_IMAGE $FETCHER_IMAGE"
  "$ROOT/deploy/build-images.sh" "$PREFIX" >"$OUT/build.log" 2>&1 || {
    log "deploy/build-images.sh $PREFIX failed; see $OUT/build.log"
    exit 2
  }
}

# prod_proxy_read -> sets PROD_IDS, PROD_STATES and PROD_TOKENS for every
# container whose name starts with "reviewbot-proxy-" (the deployment's
# proxies), all empty when there is none, PROD_IMAGE_IDS with the image ID
# of each of PROD_IMAGES ("none" when absent), and PROD_NET_IDS with the ID
# and Internal value of each of PROD_NETS ("none" when absent). PROD_TOKENS
# holds token values; it is never exported or printed. A docker failure
# stops the run (exit 2): an unknown state cannot be compared.
prod_proxy_read() {
  local id st envs line tok image image_id net net_id
  PROD_IMAGE_IDS=""
  for image in $PROD_IMAGES; do
    image_id="$(docker image ls -q --no-trunc "$image:latest")" || {
      log "docker image ls $image failed; cannot check the deployment's images"
      exit 2
    }
    PROD_IMAGE_IDS="$PROD_IMAGE_IDS $image=${image_id:-none}"
  done
  PROD_NET_IDS=""
  for net in $PROD_NETS; do
    net_id="$(docker network ls -q --no-trunc --filter "name=^${net}\$")" || {
      log "docker network ls $net failed; cannot check the deployment's networks"
      exit 2
    }
    if [ -n "$net_id" ]; then
      net_id="$net_id,internal=$(docker network inspect --format '{{.Internal}}' "$net_id")" || {
        log "docker network inspect $net failed"
        exit 2
      }
    fi
    PROD_NET_IDS="$PROD_NET_IDS $net=${net_id:-none}"
  done
  PROD_IDS="$(docker ps -a --no-trunc --filter "name=^$PROD_PREFIX-proxy-" --format '{{.ID}}')" || {
    log "docker ps failed; cannot check the deployment's proxy containers"
    exit 2
  }
  PROD_STATES="" PROD_TOKENS=""
  for id in $PROD_IDS; do
    st="$(docker inspect --format '{{.State.Status}}' "$id")" || { log "docker inspect $id failed"; exit 2; }
    envs="$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$id")" || {
      log "docker inspect env of $id failed"
      exit 2
    }
    tok=""
    while IFS= read -r line; do
      case "$line" in CLAUDE_CODE_OAUTH_TOKEN=*) tok=${line#CLAUDE_CODE_OAUTH_TOKEN=} ;; esac
    done <<< "$envs"
    PROD_STATES="$PROD_STATES $st"
    PROD_TOKENS="$PROD_TOKENS
$tok"
  done
}

PROD_RECORDED=0
# prod_proxy_record: remember the deployment's proxies, images and networks
# before the e2e images are built or the e2e proxy starts.
prod_proxy_record() {
  prod_proxy_read
  PROD_IDS_BEFORE="$PROD_IDS" PROD_STATES_BEFORE="$PROD_STATES" PROD_TOKENS_BEFORE="$PROD_TOKENS"
  PROD_IMAGE_IDS_BEFORE="$PROD_IMAGE_IDS" PROD_NET_IDS_BEFORE="$PROD_NET_IDS"
  PROD_RECORDED=1
  if [ -z "$PROD_IDS" ]; then
    log "deployment proxy containers ($PROD_PREFIX-proxy-*): none"
  else
    log "deployment proxy containers ($PROD_PREFIX-proxy-*): ${PROD_IDS:0:12}, state:$PROD_STATES"
  fi
  log "deployment images:$PROD_IMAGE_IDS"
  log "deployment networks:$PROD_NET_IDS"
}

# prod_record_once: prod_proxy_record, unless this script already recorded.
prod_record_once() {
  [ "$PROD_RECORDED" = 1 ] || prod_proxy_record
}

# prod_proxy_verify: the deployment's proxies, images and networks must be
# exactly as recorded. A deployed orchestrator that starts or removes a proxy
# for a review during the run also changes the containers; the message then
# shows which.
prod_proxy_verify() {
  [ "$PROD_RECORDED" = 1 ] || { fail "the deployment was not recorded before the test"; return 0; }
  prod_proxy_read
  if [ "$PROD_IDS" = "$PROD_IDS_BEFORE" ] && [ "$PROD_STATES" = "$PROD_STATES_BEFORE" ] \
     && [ "$PROD_TOKENS" = "$PROD_TOKENS_BEFORE" ]; then
    pass "deployment proxy containers unchanged: ${PROD_IDS:-none}${PROD_IDS:+, state:$PROD_STATES, token unchanged}"
  else
    local tok_same=no
    [ "$PROD_TOKENS" = "$PROD_TOKENS_BEFORE" ] && tok_same=yes
    fail "deployment proxy containers changed: ids '${PROD_IDS_BEFORE}' -> '${PROD_IDS}', state '${PROD_STATES_BEFORE}' -> '${PROD_STATES}', token unchanged: $tok_same"
  fi
  if [ "$PROD_IMAGE_IDS" = "$PROD_IMAGE_IDS_BEFORE" ]; then
    pass "deployment images unchanged:$PROD_IMAGE_IDS"
  else
    fail "deployment images changed:$PROD_IMAGE_IDS_BEFORE ->$PROD_IMAGE_IDS"
  fi
  if [ "$PROD_NET_IDS" = "$PROD_NET_IDS_BEFORE" ]; then
    pass "deployment networks unchanged:$PROD_NET_IDS"
  else
    fail "deployment networks changed:$PROD_NET_IDS_BEFORE ->$PROD_NET_IDS"
  fi
}

# orchestrator_py CODE [ARG...]: run CODE with the orchestrator's python and
# REVIEWBOT_DOCKER_PREFIX=$PREFIX. CODE gets the DockerNames as "names" and
# refuses to run for the deployment's prefix.
orchestrator_py() {
  local code="$1"
  shift
  REVIEWBOT_DOCKER_PREFIX="$PREFIX" "$PY" -I -c '
import logging, os, sys
from reviewbot import config, dockerrun
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
names = config.docker_names(os.environ)
if names.prefix == "'"$PROD_PREFIX"'":
    sys.exit("refusing to run with the deployment prefix")
'"$code" "$@"
}

# proxy_up: create the e2e networks if missing, remove e2e proxies left by an
# earlier run, and start the e2e proxy (the orchestrator's dockerrun.prepare
# and start_proxy). REVIEWBOT_E2E_UPSTREAM, when set, is the proxy's upstream.
# Sets PROXY_NAME.
proxy_up() {
  prod_record_once
  # Only this python process receives the real token, through its environment.
  PROXY_NAME="$(CLAUDE_CODE_OAUTH_TOKEN="$REAL_TOKEN" orchestrator_py '
dockerrun.prepare(names)
print(dockerrun.start_proxy(names, os.environ["CLAUDE_CODE_OAUTH_TOKEN"], sys.argv[1] or None))
' "${REVIEWBOT_E2E_UPSTREAM:-}" 2>>"$OUT/proxy.log")" || {
    log "starting the e2e proxy failed; see $OUT/proxy.log"
    exit 2
  }
  log "proxy $PROXY_NAME is up (started by reviewbot.dockerrun.start_proxy)"
}

# proxy_down: remove the e2e proxy (dockerrun.stop_proxy, which copies the
# proxy's 403 lines into the orchestrator log first), check that it is gone,
# then check that the deployment is unchanged.
proxy_down() {
  local left
  orchestrator_py 'dockerrun.stop_proxy(sys.argv[1])' "$PROXY_NAME" 2>>"$OUT/proxy.log" \
    || fail "dockerrun.stop_proxy $PROXY_NAME failed; see $OUT/proxy.log"
  left="$(docker ps -a -q --filter "name=^$PROXY_NAME\$")" || left="(docker ps failed)"
  [ -z "$left" ] && pass "proxy $PROXY_NAME removed" || fail "proxy $PROXY_NAME still present: $left"
  prod_proxy_verify
}

# probe_http NETWORK URL -> prints the HTTP status code, or "ERR:<reason>"
# when no HTTP response came back. Runs in a throwaway container on NETWORK.
probe_http() {
  docker run --rm --network "$1" --entrypoint python3 "$PROXY_IMAGE" -I -c '
import sys, urllib.request, urllib.error
url = sys.argv[1]
try:
    r = urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=8)
    print(r.status)
except urllib.error.HTTPError as e:
    print(e.code)
except Exception as e:
    print("ERR:" + type(e).__name__ + ":" + str(getattr(e, "reason", e))[:80].replace(" ", "_"))
' "$2" 2>>"$OUT/probe-stderr.log"
}

# probe_tcp NETWORK HOST PORT -> prints OPEN or ERR:<reason>
probe_tcp() {
  docker run --rm --network "$1" --entrypoint python3 "$PROXY_IMAGE" -I -c '
import socket, sys
try:
    s = socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=8)
    s.close()
    print("OPEN")
except Exception as e:
    print("ERR:" + type(e).__name__)
' "$2" "$3" 2>>"$OUT/probe-stderr.log"
}

# make_work FIXTURE_DIR WORK_DIR
# Builds a /work directory as the fetcher would: a git repository is made
# from FIXTURE_DIR/base and FIXTURE_DIR/head, then repo/ (head, without .git),
# diff.patch, files.txt and pr.json (with the real commit ids filled in).
make_work() {
  local fx="$1" work="$2" g
  g="$(mktemp -d)"
  mkdir -p "$work"
  git -c init.defaultBranch=main init -q "$g"
  cp -a "$fx/base/." "$g/"
  git -C "$g" add -A
  git -C "$g" -c user.name=fixture -c user.email=fixture@example.invalid commit -q -m base
  local base_sha head_sha
  base_sha="$(git -C "$g" rev-parse HEAD)"
  find "$g" -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
  cp -a "$fx/head/." "$g/"
  git -C "$g" add -A
  git -C "$g" -c user.name=fixture -c user.email=fixture@example.invalid commit -q -m head
  head_sha="$(git -C "$g" rev-parse HEAD)"
  git -C "$g" -c core.quotepath=false diff --no-color --no-ext-diff -M "$base_sha" "$head_sha" >"$work/diff.patch"
  # the same form the fetcher writes: raw paths, one per line
  git -C "$g" diff --no-ext-diff -M --name-only -z "$base_sha" "$head_sha" | tr '\0' '\n' >"$work/files.txt"
  mkdir -p "$work/repo"
  cp -a "$fx/head/." "$work/repo/"
  BASE_SHA="$base_sha" HEAD_SHA="$head_sha" python3 -I -c '
import json, os, sys
d = json.load(open(sys.argv[1]))
d["base_sha"] = os.environ["BASE_SHA"]
d["head_sha"] = os.environ["HEAD_SHA"]
open(sys.argv[2], "w").write(json.dumps(d, indent=2) + "\n")
' "$fx/pr.json" "$work/pr.json"
  rm -rf "$g"
  chmod -R a+rX "$work"
}

# run_reviewer WORK_DIR STDOUT_FILE STDERR_FILE [extra docker args...] [-- entrypoint override args]
# Uses the docker flags from the contract and, like the orchestrator, sets
# only REVIEWBOT_MODEL and REVIEWBOT_MAX_TURNS; the image sets the rest of the
# environment. Returns the container exit code.
# The image's numeric USER (uid or uid:gid), which the orchestrator reads too.
reviewer_user() {
  docker image inspect "$REVIEWER_IMAGE" --format '{{.Config.User}}'
}

run_reviewer() {
  local work="$1" out="$2" err="$3"
  shift 3
  local user uid gid
  user="$(reviewer_user)"
  uid="${user%%:*}"
  gid="${user#*:}"
  local rc=0
  timeout --kill-after=30 1200 docker run --rm --network "$INTERNAL_NET" --read-only \
    --tmpfs /tmp --tmpfs "/home/reviewer:uid=$uid,gid=$gid,mode=0700" \
    --cap-drop ALL --security-opt no-new-privileges --pids-limit 256 --memory 4g --cpus 2 \
    -v "$work:/work:ro" \
    -e REVIEWBOT_MODEL="${REVIEWBOT_MODEL:-opus}" -e REVIEWBOT_MAX_TURNS="${REVIEWBOT_MAX_TURNS:-40}" \
    "$@" >"$out" 2>"$err" || rc=$?
  return "$rc"
}

# A copy of the image's entrypoint with "--output-format json" replaced by
# "--output-format stream-json --verbose", so the stream (including the
# init message listing the tools) can be inspected. Every other flag is the
# entrypoint's own. Fails if the substitution did not happen.
STREAM_ENTRYPOINT_ARGS=(--entrypoint /bin/bash "$REVIEWER_IMAGE" -c '
set -euo pipefail
src=/opt/reviewbot/entrypoint.sh
grep -q -- "--output-format json " "$src" || { echo "stream wrapper: pattern not found" >&2; exit 90; }
sed "s/--output-format json /--output-format stream-json --verbose /" "$src" > /tmp/ep.sh
grep -q -- "--output-format stream-json --verbose " /tmp/ep.sh || { echo "stream wrapper: substitution failed" >&2; exit 91; }
exec /bin/bash /tmp/ep.sh
')

# scan_for_secrets FILE... -> exits non-zero and prints which secret kind was
# found and where. The token values are read from the environment.
scan_for_secrets() {
  REAL_TOKEN="$REAL_TOKEN" DUMMY_TOKEN="$DUMMY_TOKEN" python3 -I -c '
import os, sys
real = os.environ["REAL_TOKEN"].encode()
dummy = os.environ["DUMMY_TOKEN"].encode()
bad = 0
for path in sys.argv[1:]:
    with open(path, "rb") as f:
        data = f.read()
    for name, needle in (("real token", real), ("dummy token", dummy), ("sk-ant-", b"sk-ant-")):
        if needle in data:
            print("%s: contains %s" % (path, name))
            bad += 1
sys.exit(1 if bad else 0)
' "$@"
}

# scan_tree_for_real_token DIR -> non-zero if any file under DIR holds the
# real token.
scan_tree_for_real_token() {
  REAL_TOKEN="$REAL_TOKEN" python3 -I -c '
import os, sys
real = os.environ["REAL_TOKEN"].encode()
hits = []
n = 0
for root, dirs, files in os.walk(sys.argv[1]):
    for fn in files:
        p = os.path.join(root, fn)
        if os.path.islink(p):
            continue
        n += 1
        with open(p, "rb") as f:
            if real in f.read():
                hits.append(p)
print("scanned %d files" % n)
for h in hits:
    print("real token found in " + h)
sys.exit(1 if hits else 0)
' "$1"
}
