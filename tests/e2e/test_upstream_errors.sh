#!/bin/bash
# e2e: what the review container prints when the Anthropic API answers with
# an error, and how the orchestrator classifies it.
#
# The e2e proxy forwards to tests/e2e/stub_upstream.py (a container on the
# e2e internal network) instead of api.anthropic.com, so no real token is
# used and no quota is spent. For each status in REVIEWBOT_E2E_STATUSES
# (default 401 403 404 429 500 529 400) the stub answers every request with that
# status and the Anthropic error body, and the image's entrypoint runs
# unchanged on the happy fixture. Logged for each status: the exit code, the
# time taken, the number of requests the stub received, and the result's
# is_error, subtype, terminal_reason and api_error_status. Asserted:
#   - stdout is one claude result object with is_error true
#   - reviewbot.review.api_environment_error (the orchestrator's classifier)
#     returns the status for 401, 403, 404, 429 and 5xx, so the pipeline treats
#     the run as an environment failure, and None for 400, which stays a
#     failure of the pull request
# 429, 500 and 529 take about three minutes each, because Claude Code
# retries them.
#
# When to run it: after changing the pinned Claude Code version, by name
# (tests/e2e/run_all.sh upstream_errors); run_all.sh does not run it by
# default. Its purpose is to check whether the new binary's output for these
# statuses changed shape, and if it did, to refresh
# orchestrator/tests/fixtures/reviewer_api_errors.json. It writes the
# measured stdout of every status, in that file's format, to
# $OUT/reviewer_api_errors.json, so refreshing the fixture is a copy. Day to
# day, the classifier is guarded by the unit tests in
# orchestrator/tests/test_environment.py, which run the pipeline on that
# fixture.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

if [ "${REVIEWBOT_E2E_SKIP_BUILD:-0}" != 1 ]; then build_images; fi
read -r -a STATUSES <<< "${REVIEWBOT_E2E_STATUSES:-401 403 404 429 500 529 400}"

# The proxy needs a token to start; it only ever reaches the stub.
REAL_TOKEN="sk-ant-oat01-e2e-stub-upstream-placeholder-0000000000"
export REVIEWBOT_E2E_UPSTREAM="http://stub-upstream:9000"
STUB="reviewbot-e2e-stub-upstream"
stub_down() { docker rm -f "$STUB" >/dev/null 2>&1 || true; }

proxy_up
WORK="$OUT/upstream-work"
make_work "$E2E_DIR/fixtures/happy" "$WORK"

for status in "${STATUSES[@]}"; do
  stub_down
  docker run -d --name "$STUB" --network "$INTERNAL_NET" --network-alias stub-upstream \
    -v "$E2E_DIR/stub_upstream.py:/stub.py:ro" --entrypoint python3 "$PROXY_IMAGE" \
    -I /stub.py 9000 "$status" >/dev/null 2>>"$OUT/probe-stderr.log" \
    || { fail "status $status: the stub upstream did not start"; continue; }
  ready=0
  for _ in $(seq 1 20); do
    code="$(probe_http "$INTERNAL_NET" "http://stub-upstream:9000/")"
    if [ "$code" = "$status" ]; then ready=1; break; fi
    sleep 0.5
  done
  [ "$ready" = 1 ] || { fail "status $status: the stub upstream did not answer"; continue; }

  rc=0
  t0=$(date +%s)
  run_reviewer "$WORK" "$OUT/upstream-$status.json" "$OUT/upstream-$status.stderr" "$REVIEWER_IMAGE" || rc=$?
  t1=$(date +%s)
  printf '%s\n' "$rc" >"$OUT/upstream-$status.exit"
  docker logs "$STUB" >"$OUT/upstream-$status.stub.log" 2>&1 || true
  stub_down
  requests="$(grep -c '^POST /v1/messages ' "$OUT/upstream-$status.stub.log")" || requests="${requests:-(grep failed)}"

  if summary="$(python3 -I - "$ROOT/orchestrator/src" "$OUT/upstream-$status.json" "$status" <<'PY'
import json, sys
sys.path.insert(0, sys.argv[1])
from reviewbot.review import api_environment_error
text = open(sys.argv[2], encoding="utf-8").read()
status = int(sys.argv[3])
try:
    d = json.loads(text)
except ValueError:
    print("stdout is not JSON")
    sys.exit(1)
got = api_environment_error(text)
fields = " ".join("%s=%s" % (k, json.dumps(d.get(k))) for k in
                  ("is_error", "subtype", "terminal_reason", "api_error_status"))
want = None if status == 400 else status
print("%s; classified as %s" % (fields, "environment" if got is not None else "pull request failure"))
ok = d.get("type") == "result" and d.get("is_error") is True and got == want
sys.exit(0 if ok else 1)
PY
)"; then
    pass "status $status: exit $rc, $((t1 - t0)) s, $requests requests to the stub; $summary"
  else
    fail "status $status: exit $rc, $((t1 - t0)) s, $requests requests to the stub; $summary"
  fi
done

# The measured stdout of every status that ran, in the format of
# orchestrator/tests/fixtures/reviewer_api_errors.json (stdout without its
# final newline), so that refreshing the fixture is a copy.
cc_version="$(docker image inspect "$REVIEWER_IMAGE" --format '{{index .Config.Labels "reviewbot.claude_code_version"}}')" \
  || cc_version="(unknown version)"
# BEGIN fixture writer
if python3 -I - "$OUT" "$cc_version" "$(date -u +%Y-%m-%d)" "${STATUSES[@]}" >"$OUT/reviewer_api_errors.json" <<'PY'
import json, os, sys
out, version, day, statuses = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4:]
stdout, exits = {}, {}
for s in statuses:
    path = os.path.join(out, "upstream-%s.json" % s)
    if not os.path.exists(path):
        continue  # the stub did not start or answer for this status
    with open(path, encoding="utf-8") as f:
        stdout[s] = f.read().rstrip("\n")
    with open(os.path.join(out, "upstream-%s.exit" % s)) as f:
        exits[s] = f.read().strip()
if not stdout:
    sys.exit("no status produced any stdout")
keys = sorted(stdout, key=int)
codes = ", ".join("%s exit %s" % (s, exits[s]) for s in keys)
doc = {"_source": "stdout of the reviewer image (Claude Code %s) with "
                  "tests/e2e/stub_upstream.py answering each status (%s); recorded %s"
                  % (version, codes, day),
       "stdout": {s: stdout[s] for s in keys}}
print(json.dumps(doc, indent=2))
PY
then
  log "measured stdout for each status, in the fixture's format: $OUT/reviewer_api_errors.json (copy it to orchestrator/tests/fixtures/reviewer_api_errors.json if the shape changed)"
else
  fail "could not write $OUT/reviewer_api_errors.json"
fi
# END fixture writer

proxy_down
finish
