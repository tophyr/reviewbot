#!/bin/bash
# e2e fetcher test against real public pull requests, with no GitHub token.
#
# For each PR: PR_JSON (repo, number, base_sha and head_sha, the fetcher's
# contract input) is built from the GitHub REST API (anonymous), the fetcher
# runs, and the test asserts:
#   - exit 0
#   - /work has exactly diff.patch, files.txt, repo/ (the orchestrator writes
#     pr.json afterwards; the fetcher must not)
#   - repo/.git does not exist; repo/ has no symlinks
#   - files.txt equals the file list GitHub reports for the PR
#     (GET /repos/{repo}/pulls/{n}/files), an independent source
#   - diff.patch is a git diff that mentions every path in files.txt
#   - every file and directory is world-readable (the reviewer runs as
#     another uid)
# Token handling, with a made-up sentinel token:
#   - on a successful fetch, the sentinel appears nowhere in the fetcher
#     container's filesystem (docker export), in /work, or in its output
#   - on a fetch that needs credentials (a repository that does not exist,
#     so GitHub answers 401), git used the credential helper (the error is an
#     authentication failure, not "terminal prompts disabled"), the exit code
#     is non-zero, and the sentinel again appears nowhere
#   - control: a container that writes the sentinel to /tmp is detected by
#     the same filesystem scan
# Input validation: a malformed repo and a non-empty /work are refused.
# Limits: with a 1 MiB /scratch the first PR does not fit, and the fetcher
# exits FETCHER_EXIT_TOO_LARGE with its own message, which needs the separate
# /log tmpfs.
#
# Needs network access to github.com and api.github.com. Uses at most 4
# anonymous API requests.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
unset GITHUB_TOKEN

# FETCHER_IMAGE is the e2e tag (reviewbot-e2e-fetcher), not the deployment's.
prod_record_once
if [ "${REVIEWBOT_E2E_SKIP_BUILD:-0}" != 1 ]; then build_images; fi

PRS="${REVIEWBOT_E2E_FETCH_PRS:-jqlang/jq#3692 jqlang/jq#1748}"
# The scratch and log tmpfs mounts the orchestrator gives the fetcher
# (fetch.build_argv); the entrypoint refuses to run without them.
SCRATCH_MOUNT=(--tmpfs "/scratch:rw,size=${MAX_FETCH_MB}m,nr_inodes=$((MAX_FETCH_MB * FETCH_INODES_PER_MB))"
               --tmpfs "/log:rw,size=${LOG_TMPFS_MB}m")
# The git directory is in that tmpfs, which docker export does not include.
# For the sentinel runs the entrypoint runs under this wrapper, which counts
# the files left in /scratch that hold the token (read from the environment,
# never from a command line) and prints "scratch sentinel files: N" to stderr.
SCRATCH_SCAN=(--entrypoint sh "$FETCHER_IMAGE" -c '/usr/local/bin/reviewbot-fetch; rc=$?
n=$(printf "%s" "$GITHUB_TOKEN" | grep -rlF -f - /scratch | wc -l)
echo "scratch sentinel files: $n" >&2
exit $rc')
SENTINEL="ghs_reviewbotSENTINEL$(date +%s)xyzzy0123456789"
# Containers this test names, so that it can export their filesystems.
CONTROL="$PREFIX-fetch-control"
SENTINEL_RUN="$PREFIX-fetch-sentinel"

# sentinel_hits PATH... -> prints how many of the files under PATH hold the
# sentinel, or "(grep error N)" when grep could not read them. grep exits 1
# for no match, which is the expected result here, and 2 for an error.
sentinel_hits() {
  local found rc=0
  found="$(grep -rlF -- "$SENTINEL" "$@")" || rc=$?
  case "$rc" in
    0) printf '%s\n' "$found" | wc -l ;;
    1) echo 0 ;;
    *) echo "(grep error $rc)" ;;
  esac
}

# pr_json REPO NUMBER OUTFILE -> writes the fetcher's PR_JSON from the API
pr_json() {
  local api="$OUT/api-$(echo "$1" | tr / _)-$2.json"
  curl -fsS -H 'Accept: application/vnd.github+json' "https://api.github.com/repos/$1/pulls/$2" -o "$api"
  python3 -I -c '
import json, sys
p = json.load(open(sys.argv[1]))
out = {
    "repo": p["base"]["repo"]["full_name"], "number": p["number"],
    "base_sha": p["base"]["sha"], "head_sha": p["head"]["sha"],
}
open(sys.argv[2], "w").write(json.dumps(out))
' "$api" "$3"
}

# api_files REPO NUMBER OUTFILE -> sorted list of changed paths per GitHub
api_files() {
  local api="$OUT/api-files-$(echo "$1" | tr / _)-$2.json"
  curl -fsS -H 'Accept: application/vnd.github+json' "https://api.github.com/repos/$1/pulls/$2/files?per_page=100" -o "$api"
  python3 -I -c '
import json, sys
print("\n".join(sorted(f["filename"] for f in json.load(open(sys.argv[1])))))
' "$api" >"$3"
}

# export_has_sentinel CONTAINER -> prints the number of files in the
# container filesystem that contain the sentinel
export_has_sentinel() {
  docker export "$1" -o "$OUT/export.tar"
  SENTINEL="$SENTINEL" python3 -I -c '
import os, sys, tarfile
s = os.environ["SENTINEL"].encode()
hits = []
with tarfile.open(sys.argv[1]) as t:
    for m in t:
        if m.isfile():
            f = t.extractfile(m)
            if f is not None and s in f.read():
                hits.append(m.name)
print(len(hits), " ".join(hits))
' "$OUT/export.tar"
  rm -f "$OUT/export.tar"
}

# Control for the filesystem scan.
docker rm -f "$CONTROL" >/dev/null 2>&1 || true
docker run --name "$CONTROL" -e GITHUB_TOKEN="$SENTINEL" --entrypoint sh "$FETCHER_IMAGE" \
  -c 'printf "%s" "$GITHUB_TOKEN" > /tmp/leaked' >>"$OUT/control.log" 2>&1
r="$(export_has_sentinel "$CONTROL")"
docker rm -f "$CONTROL" >/dev/null
case "$r" in "1 tmp/leaked") pass "control: filesystem scan finds a planted sentinel ($r)";; *) fail "control: filesystem scan missed the planted sentinel ($r)";; esac

for spec in $PRS; do
  repo="${spec%#*}" num="${spec#*#}"
  tag="$(echo "$repo-$num" | tr / _)"
  W="$OUT/fetch-$tag"
  mkdir -p "$W"
  pr_json "$repo" "$num" "$OUT/pr-$tag.json"
  api_files "$repo" "$num" "$OUT/apifiles-$tag.txt"
  PR_JSON="$(cat "$OUT/pr-$tag.json")"
  export PR_JSON

  # Anonymous fetch.
  rc=0
  timeout 600 docker run --rm --cap-drop ALL --security-opt no-new-privileges -e PR_JSON \
    "${SCRATCH_MOUNT[@]}" -v "$W:/work" "$FETCHER_IMAGE" >"$OUT/fetch-$tag.stdout" 2>"$OUT/fetch-$tag.stderr" || rc=$?
  [ "$rc" -eq 0 ] && pass "$spec: anonymous fetch exited 0 ($(tail -1 "$OUT/fetch-$tag.stderr"))" \
                  || { fail "$spec: fetch exited $rc: $(tr '\n' ' ' < "$OUT/fetch-$tag.stderr")"; continue; }

  layout="$(cd "$W" && ls -A | tr '\n' ' ')"
  # The checks below read these files; with a wrong layout they would abort
  # the script under set -e instead of reporting, so skip to the next PR.
  [ "$layout" = "diff.patch files.txt repo " ] && pass "$spec: /work layout is: $layout" \
    || { fail "$spec: /work layout is: $layout"; continue; }
  [ ! -e "$W/repo/.git" ] && pass "$spec: repo/.git absent" || fail "$spec: repo/.git present"
  # A find error (status 1, for example an unreadable directory) must be
  # reported, not abort the script through pipefail.
  nlinks="$(find "$W/repo" -type l | wc -l)" || nlinks="(find failed)"
  [ "$nlinks" = 0 ] && pass "$spec: no symlinks in repo/" || fail "$spec: $nlinks symlinks in repo/"
  sort "$W/files.txt" >"$OUT/files-sorted-$tag.txt"
  if cmp -s "$OUT/files-sorted-$tag.txt" "$OUT/apifiles-$tag.txt"; then
    pass "$spec: files.txt matches GitHub's file list ($(wc -l < "$W/files.txt") paths)"
  else
    fail "$spec: files.txt differs from GitHub's file list: $(diff "$OUT/files-sorted-$tag.txt" "$OUT/apifiles-$tag.txt" | head -5 | tr '\n' ' ')"
  fi
  first="$(head -c 11 "$W/diff.patch")" || first="(head failed)"
  missing=0
  while IFS= read -r p; do
    grep -qF -- "$p" "$W/diff.patch" || missing=$((missing + 1))
  done <"$W/files.txt"
  [ "$first" = "diff --git " ] && [ "$missing" -eq 0 ] && pass "$spec: diff.patch is a git diff covering every listed path" \
    || fail "$spec: diff.patch starts with '$first', $missing listed paths not mentioned"
  unreadable="$(find "$W" \( -type f ! -perm -o=r \) -o \( -type d ! -perm -o=rx \) | wc -l)" || unreadable="(find failed)"
  [ "$unreadable" = 0 ] && pass "$spec: everything under /work is world-readable" || fail "$spec: $unreadable entries not world-readable"
  head_tree="$(cd "$W/repo" && find . -type f | wc -l)" || head_tree="(find failed)"
  log "$spec: repo/ holds $head_tree files; diff.patch is $(wc -l < "$W/diff.patch") lines"

  # Same fetch with a sentinel token: it must not reach disk or output.
  W2="$OUT/fetch-token-$tag"
  mkdir -p "$W2"
  docker rm -f "$SENTINEL_RUN" >/dev/null 2>&1 || true
  rc=0
  GITHUB_TOKEN="$SENTINEL" timeout 600 docker run --name "$SENTINEL_RUN" --cap-drop ALL --security-opt no-new-privileges \
    -e PR_JSON -e GITHUB_TOKEN "${SCRATCH_MOUNT[@]}" -v "$W2:/work" "${SCRATCH_SCAN[@]}" >"$OUT/fetch-token-$tag.stdout" 2>"$OUT/fetch-token-$tag.stderr" || rc=$?
  r="$(export_has_sentinel "$SENTINEL_RUN")"
  docker rm -f "$SENTINEL_RUN" >/dev/null
  w2hits="$(sentinel_hits "$W2" "$OUT/fetch-token-$tag.stdout" "$OUT/fetch-token-$tag.stderr")"
  scratch="$(grep -F 'scratch sentinel files: ' "$OUT/fetch-token-$tag.stderr")" || scratch="(no scratch scan line)"
  [ "$rc" -eq 0 ] && [ "$r" = "0 " ] && [ "$w2hits" = 0 ] && [ "$scratch" = "scratch sentinel files: 0" ] \
    && pass "$spec: with a token set, fetch ok and token on disk/output: container fs=${r% } work+logs=$w2hits $scratch" \
    || fail "$spec: with a token set: exit=$rc container fs hits=$r work+log hits=$w2hits $scratch"
done

# A fetch that needs credentials: GitHub answers 401 for a missing repository.
W3="$OUT/fetch-auth"
mkdir -p "$W3"
PR_JSON='{"repo":"reviewbot-e2e-nonexistent-owner-x7/does-not-exist","number":1,"base_sha":"0123456789012345678901234567890123456789","head_sha":"0123456789012345678901234567890123456789"}'
export PR_JSON
docker rm -f "$SENTINEL_RUN" >/dev/null 2>&1 || true
rc=0
GITHUB_TOKEN="$SENTINEL" timeout 300 docker run --name "$SENTINEL_RUN" --cap-drop ALL --security-opt no-new-privileges \
  -e PR_JSON -e GITHUB_TOKEN "${SCRATCH_MOUNT[@]}" -v "$W3:/work" "${SCRATCH_SCAN[@]}" >"$OUT/fetch-auth.stdout" 2>"$OUT/fetch-auth.stderr" || rc=$?
r="$(export_has_sentinel "$SENTINEL_RUN")"
docker rm -f "$SENTINEL_RUN" >/dev/null
hits="$(sentinel_hits "$W3" "$OUT/fetch-auth.stdout" "$OUT/fetch-auth.stderr")"
scratch="$(grep -F 'scratch sentinel files: ' "$OUT/fetch-auth.stderr")" || scratch="(no scratch scan line)"
[ "$scratch" = "scratch sentinel files: 0" ] && pass "auth-required fetch: $scratch" || fail "auth-required fetch: $scratch"
stderr_text="$(tr '\n' ' ' < "$OUT/fetch-auth.stderr")"
log "auth-required fetch stderr: $stderr_text"
case "$stderr_text" in
  *"Authentication failed"*|*"Invalid username or token"*|*"Invalid username or password"*) helper=1 ;;
  *) helper=0 ;;
esac
[ "$rc" -ne 0 ] && [ "$helper" -eq 1 ] && [ "$r" = "0 " ] && [ "$hits" = 0 ] \
  && pass "auth-required fetch: exit $rc, credential helper was used, token on disk/output: none" \
  || fail "auth-required fetch: exit=$rc helper_used=$helper container fs hits=$r work+log hits=$hits"

# Same request without a token: git cannot prompt, and says so.
W4="$OUT/fetch-noauth"
mkdir -p "$W4"
rc=0
timeout 300 docker run --rm --cap-drop ALL -e PR_JSON "${SCRATCH_MOUNT[@]}" -v "$W4:/work" "$FETCHER_IMAGE" >/dev/null 2>"$OUT/fetch-noauth.stderr" || rc=$?
[ "$rc" -ne 0 ] && grep -q "terminal prompts disabled" "$OUT/fetch-noauth.stderr" \
  && pass "control: without a token the same fetch fails with 'terminal prompts disabled' (so the helper check above is meaningful)" \
  || fail "control: no-token fetch exit=$rc stderr=$(tr '\n' ' ' < "$OUT/fetch-noauth.stderr")"

# Input validation.
W5="$OUT/fetch-badrepo"
mkdir -p "$W5"
rc=0
PR_JSON='{"repo":"a/b;touch /work/pwned","number":1,"base_sha":"0123456789012345678901234567890123456789","head_sha":"0123456789012345678901234567890123456789"}' \
  docker run --rm --cap-drop ALL -e PR_JSON "${SCRATCH_MOUNT[@]}" -v "$W5:/work" "$FETCHER_IMAGE" >/dev/null 2>"$OUT/fetch-badrepo.stderr" || rc=$?
[ "$rc" -ne 0 ] && [ -z "$(ls -A "$W5")" ] && grep -q "repo is not owner/name" "$OUT/fetch-badrepo.stderr" && pass "malformed repo refused: $(cat "$OUT/fetch-badrepo.stderr")" \
  || fail "malformed repo: exit=$rc, /work has: $(ls -A "$W5" | tr '\n' ' ')"

# Limits.
W7="$OUT/fetch-toolarge"
mkdir -p "$W7"
rc=0
PR_JSON="$(cat "$OUT/pr-$(echo "${PRS%% *}" | tr '/#' '_-').json")" \
  docker run --rm --cap-drop ALL -e PR_JSON --tmpfs /scratch:rw,size=1m --tmpfs "/log:rw,size=${LOG_TMPFS_MB}m" \
  -v "$W7:/work" "$FETCHER_IMAGE" >/dev/null 2>"$OUT/fetch-toolarge.stderr" || rc=$?
[ "$rc" -eq "$FETCHER_EXIT_TOO_LARGE" ] && grep -q "does not fit in /scratch" "$OUT/fetch-toolarge.stderr" \
  && pass "a repository that does not fit in /scratch exits $FETCHER_EXIT_TOO_LARGE with its own message" \
  || fail "too large: exit=$rc stderr=$(tail -n 3 "$OUT/fetch-toolarge.stderr" | tr '\n' ' ')"
W6="$OUT/fetch-nonempty"
mkdir -p "$W6"
echo stale >"$W6/stale.txt"
rc=0
PR_JSON="$(cat "$OUT/pr-$(echo "${PRS%% *}" | tr '/#' '_-').json")" \
  docker run --rm --cap-drop ALL -e PR_JSON "${SCRATCH_MOUNT[@]}" -v "$W6:/work" "$FETCHER_IMAGE" >/dev/null 2>"$OUT/fetch-nonempty.stderr" || rc=$?
[ "$rc" -ne 0 ] && grep -q "is not empty" "$OUT/fetch-nonempty.stderr" && pass "non-empty /work refused: $(cat "$OUT/fetch-nonempty.stderr")" || fail "non-empty /work accepted"

prod_proxy_verify
finish
