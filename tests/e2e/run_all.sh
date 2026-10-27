#!/bin/bash
# Runs every container-side test: proxy unit tests, checker self-tests,
# the shell lint (tests/lint_shell.py), image build, then the egress, fetcher, leak and happy-path
# e2e tests. upstream_errors runs only when named; it is for a change of the
# pinned Claude Code version (see the header of test_upstream_errors.sh).
# All logs go to one directory, which is scanned for the real token at the
# end. Exit status is non-zero if any test failed.
#
#   tests/e2e/run_all.sh                 # the default list
#   tests/e2e/run_all.sh egress leak     # a subset of the e2e tests
#   tests/e2e/run_all.sh upstream_errors # after changing the Claude Code pin
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
export REVIEWBOT_E2E_OUT="$OUT"
# The deployed proxy (project reviewbot), if any, and the deployment's images
# must be the same at the end.
prod_proxy_record

tests=("$@")
[ "${#tests[@]}" -gt 0 ] || tests=(egress fetcher leak happy)

if python3 -I "$ROOT/proxy/test_proxy.py" >"$OUT/proxy-unit.log" 2>&1; then
  pass "proxy unit tests ($(grep -E '^Ran ' "$OUT/proxy-unit.log"))"
else
  fail "proxy unit tests; see $OUT/proxy-unit.log"
fi
python3 -I "$E2E_DIR/check_output.py" "$ROOT/orchestrator/src/reviewbot/schema.json" --self-test >"$OUT/selftests.log" 2>&1 \
  && pass "check_output.py self-test" || fail "check_output.py self-test"
python3 -I "$E2E_DIR/check_stream.py" --self-test >>"$OUT/selftests.log" 2>&1 \
  && pass "check_stream.py self-test" || fail "check_stream.py self-test"
python3 -I "$ROOT/tests/lint_shell.py" --self-test >>"$OUT/selftests.log" 2>&1 \
  && pass "lint_shell.py self-test" || fail "lint_shell.py self-test"
python3 -I "$ROOT/tests/lint_shell.py" >"$OUT/lint_shell.log" 2>&1 \
  && pass "shell lint ($(tail -n 1 "$OUT/lint_shell.log"))" || fail "shell lint: $(tr '\n' ' ' < "$OUT/lint_shell.log")"

load_real_token
build_images
export REVIEWBOT_E2E_SKIP_BUILD=1

for t in "${tests[@]}"; do
  script="$E2E_DIR/test_$t.sh"
  [ -f "$script" ] || { fail "no such test: $t"; continue; }
  log "=== $t"
  if "$script"; then
    pass "test_$t.sh"
  else
    fail "test_$t.sh"
  fi
done

if scan_tree_for_real_token "$OUT" >"$OUT/token-scan-final.txt"; then
  pass "final scan: real token absent from every log in $OUT ($(cat "$OUT/token-scan-final.txt"))"
else
  fail "final scan: $(cat "$OUT/token-scan-final.txt")"
fi
prod_proxy_verify
finish
