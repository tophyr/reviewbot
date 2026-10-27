#!/bin/bash
# e2e happy-path review of a small genuine pull request
# (tests/e2e/fixtures/happy). The fixture's head adds cart_total() in
# shop/cart.py whose loop starts at index 1, so the first cart line is never
# counted (shop/cart.py line 16 in the head version).
#
# Runs the image's entrypoint unchanged, through the proxy, and asserts:
#   - exit 0 and a claude result object on stdout
#   - structured_output validates against orchestrator/src/reviewbot/schema.json
#   - a bug or security comment on shop/cart.py within 2 lines of line 16
#   - every comment path is listed in files.txt
#   - no real token, dummy token or "sk-ant-" in stdout
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

load_real_token
if [ "${REVIEWBOT_E2E_SKIP_BUILD:-0}" != 1 ]; then build_images; fi

if python3 -I "$E2E_DIR/check_output.py" "$ROOT/orchestrator/src/reviewbot/schema.json" --self-test >"$OUT/check_output-selftest.txt"; then
  pass "control: schema checker $(cat "$OUT/check_output-selftest.txt")"
else
  fail "schema checker self-test: $(cat "$OUT/check_output-selftest.txt")"
fi
# The planted bug must be where the assertion expects it. grep exits 1 when
# the line is gone; under set -e that must reach fail(), not abort the script.
matches="$(grep -n -F 'for i in range(1, len(items)):' "$E2E_DIR/fixtures/happy/head/shop/cart.py")" || matches="(not found)"
line="$(printf '%s\n' "$matches" | cut -d: -f1 | tr '\n' ' ')"
line="${line% }"
[ "$line" = 16 ] || { fail "fixture changed: planted bug is at line '$line', expected 16"; finish; }

proxy_up

WORK="$OUT/happy-work"
make_work "$E2E_DIR/fixtures/happy" "$WORK"
log "happy /work prepared at $WORK: $(tr '\n' ' ' < "$WORK/files.txt")"

rc=0
run_reviewer "$WORK" "$OUT/happy-stdout.json" "$OUT/happy-stderr.log" "$REVIEWER_IMAGE" || rc=$?
log "review exit code: $rc, stdout bytes: $(wc -c < "$OUT/happy-stdout.json")"
[ "$rc" -eq 0 ] && pass "review exited 0" || fail "review exited $rc (stderr: $(head -c 300 "$OUT/happy-stderr.log"))"

if python3 -I "$E2E_DIR/check_output.py" "$ROOT/orchestrator/src/reviewbot/schema.json" "$OUT/happy-stdout.json" \
     --expect-finding shop/cart.py:16:2 >"$OUT/happy-check.txt" 2>&1; then
  pass "structured_output valid and planted bug found"
else
  fail "output check failed"
fi
cat "$OUT/happy-check.txt" | tee -a "$OUT/summary.log" >&2

if python3 -I -c '
import json, sys
d = json.load(open(sys.argv[1]))
files = set(open(sys.argv[2]).read().split("\n")) - {""}
bad = [c["path"] for c in d["structured_output"]["comments"] if c["path"] not in files]
print("comment paths not in files.txt: %s" % bad)
sys.exit(1 if bad else 0)
' "$OUT/happy-stdout.json" "$WORK/files.txt" >"$OUT/happy-paths.txt" 2>&1; then
  pass "all comment paths are in files.txt"
else
  fail "$(cat "$OUT/happy-paths.txt")"
fi

if scan_for_secrets "$OUT/happy-stdout.json" >"$OUT/happy-scan.txt"; then
  pass "stdout has no real token, dummy token or sk-ant-"
else
  fail "stdout: $(tr '\n' ' ' < "$OUT/happy-scan.txt")"
fi

proxy_down
if scan_tree_for_real_token "$OUT" >"$OUT/token-scan.txt"; then
  pass "real token absent from all test logs ($(cat "$OUT/token-scan.txt"))"
else
  fail "real token found in test logs: $(cat "$OUT/token-scan.txt")"
fi
finish
