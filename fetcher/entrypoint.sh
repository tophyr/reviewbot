#!/bin/sh
# reviewbot fetcher. Takes no arguments.
#
# Inputs (env):
#   PR_JSON       a JSON object with repo, number, base_sha and head_sha from
#                 pr.json (see DESIGN.md); any other key is ignored
#   GITHUB_TOKEN  optional; installation token. Unset or empty means
#                 anonymous access, which works for public repositories.
#
# Output, in /work (mounted read-write by the orchestrator):
#   diff.patch, files.txt, repo/ (head checkout without .git)
#   pr.json is not written here; the orchestrator writes it afterwards.
#
# Scratch: /scratch must be a tmpfs mount, which the orchestrator gives a
# size limit (fetch.MAX_FETCH_MB) and an inode limit (256 per MiB). The
# git directory, the checkout, diff.patch and files.txt are all made there,
# and copied to /work only after every check has passed. A pull request that
# needs more than either limit fails with ENOSPC inside the tmpfs, and neither
# the host's /work nor the container's writable layer receives more than that.
# A tmpfs is used because rootless docker does not enforce --storage-opt.
# Running out of either limit exits 74, so that the orchestrator can report
# that the repository does not fit.
#
# Log: /log must be a small tmpfs mount of its own. It holds git's stderr,
# so that git's error message survives a full /scratch.
#
# The token is never written to disk and never placed on a command line. git
# asks for credentials through a credential helper defined with -c; the helper
# is a shell snippet that prints $GITHUB_TOKEN from the environment it
# inherits, so the token value appears only in that process's output pipe to
# git. The git directory lives outside /work and is discarded with the
# container.
set -eu
umask 022

WORK=/work
SCRATCH=/scratch
LOG=/log
GIT_DIR_PATH="$SCRATCH/git"
TREE="$SCRATCH/repo"

die() {
  echo "reviewbot-fetcher: $*" >&2
  exit 1
}

# git's stderr from fetch, checkout and diff goes to a file in the log tmpfs,
# one file per command, and only its last 64 KiB are printed. A pull request
# can make git print without bound (checkout prints one error line per file
# it cannot create, for example a name longer than 255 bytes, and exits 0),
# and the orchestrator reads this container's stderr. A command that fails
# for lack of space or inodes in /scratch exits 74. The log is the only
# reliable sign, because git removes a partial pack when a fetch fails. git
# reports it as "No space left on device" when a fetch runs out and as "Out
# of diskspace" when a checkout does.
GIT_LOG="$LOG/git-stderr.log"
git_logged() {
  git_rc=0
  git_ "$@" 2> "$GIT_LOG" || git_rc=$?
  if [ -s "$GIT_LOG" ]; then
    tail -c 65536 "$GIT_LOG" >&2 || echo "reviewbot-fetcher: cannot read $GIT_LOG" >&2
  fi
  if [ "$git_rc" -ne 0 ] && grep -qE "No space left on device|Out of diskspace" "$GIT_LOG"; then
    echo "reviewbot-fetcher: the repository does not fit in $SCRATCH (its size or inode limit)" >&2
    exit 74
  fi
  return "$git_rc"
}

[ "$#" -eq 0 ] || die "the entrypoint takes no arguments"
[ -n "${PR_JSON:-}" ] || die "PR_JSON is not set"
[ -d "$WORK" ] || die "$WORK does not exist"
# The size limit is the tmpfs mount's; without the mount there is none.
awk -v d="$SCRATCH" '$2 == d && $3 == "tmpfs" { found = 1 } END { exit !found }' /proc/self/mounts \
  || die "$SCRATCH is not a tmpfs mount; the orchestrator mounts one with the fetch size limit"
awk -v d="$LOG" '$2 == d && $3 == "tmpfs" { found = 1 } END { exit !found }' /proc/self/mounts \
  || die "$LOG is not a tmpfs mount; the orchestrator mounts one for git's error output"

# Parse and validate pr.json. The values end up in URLs and git arguments,
# so each must match a strict pattern.
printf '%s' "$PR_JSON" | jq -e 'type == "object"' >/dev/null || die "PR_JSON is not a JSON object"
field() {
  printf '%s' "$PR_JSON" | jq -r --arg k "$1" '.[$k] | if type == "string" or type == "number" then tostring else "" end'
}
REPO=$(field repo) || die "cannot read repo from PR_JSON"
NUMBER=$(field number) || die "cannot read number from PR_JSON"
BASE_SHA=$(field base_sha) || die "cannot read base_sha from PR_JSON"
HEAD_SHA=$(field head_sha) || die "cannot read head_sha from PR_JSON"

printf '%s' "$REPO" | grep -Eqx '[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+' || die "repo is not owner/name"
case "$REPO" in *..*) die "repo contains '..'" ;; esac
printf '%s' "$NUMBER" | grep -Eqx '[1-9][0-9]{0,9}' || die "number is not a positive integer"
printf '%s' "$BASE_SHA" | grep -Eqx '[0-9a-f]{40}|[0-9a-f]{64}' || die "base_sha is not a full commit id"
printf '%s' "$HEAD_SHA" | grep -Eqx '[0-9a-f]{40}|[0-9a-f]{64}' || die "head_sha is not a full commit id"

# Refuse to run on a non-empty work directory, so stale files from another
# review can never be mixed in.
listing=$(ls -A "$WORK") || die "cannot list $WORK"
[ -z "$listing" ] || die "$WORK is not empty"
# Test writability by writing; [ -w ] is not reliable for uid 0.
: > "$WORK/.write-probe" || die "$WORK is not writable"
rm -f "$WORK/.write-probe"

URL="https://github.com/$REPO.git"

# Options applied to every git invocation. core.symlinks=false checks out
# symlinks as small text files holding the link target, so nothing in
# /work/repo can point outside it. "credential.helper=" clears any helper;
# with a token, a helper that prints it from the environment is added after
# that. core.maxTreeDepth=256 makes git refuse a tree nested more than 256
# levels deep (in fetch, checkout and diff), because the orchestrator removes
# work directories with shutil.rmtree, which recurses once per directory level
# and fails near 1,000 levels. git has no low-speed limit by default, and
# GitHub sends nothing while it packs, so http.lowSpeedLimit and
# http.lowSpeedTime abort only a transfer silent for five minutes. The extra
# options are prepended to this function's own arguments, so there is one git
# command line. When git_timeout is set, that command line runs under
# "timeout $git_timeout".
git_() {
  set -- --git-dir="$GIT_DIR_PATH" --work-tree="$TREE" "$@"
  if [ -n "${GITHUB_TOKEN:-}" ]; then
    set -- \
      -c 'credential.helper=!f() { test "$1" = get || exit 0; echo username=x-access-token; echo "password=$GITHUB_TOKEN"; }; f' \
      -c credential.useHttpPath=false \
      "$@"
  fi
  set -- git \
    -c core.hooksPath=/dev/null -c core.fsmonitor=false \
    -c protocol.allow=never -c protocol.https.allow=always \
    -c core.symlinks=false -c submodule.recurse=false \
    -c core.maxTreeDepth=256 \
    -c http.lowSpeedLimit=1 -c http.lowSpeedTime=300 \
    -c credential.helper= \
    "$@"
  if [ -n "${git_timeout:-}" ]; then
    set -- timeout "$git_timeout" "$@"
  fi
  "$@"
}

# A failed git fetch is either this pull request's failure (exit 1, recorded
# against it) or github.com's git endpoint being unreachable (exit 75,
# EX_TEMPFAIL; the orchestrator records nothing and ends the pass). The
# endpoint counts as unreachable only when git ls-remote fails both for the
# base repository and for a known public repository, fetched without the
# token, so a repository that is missing or not accessible still gives exit 1.
# Each probe runs under "timeout 60" because git has no connect timeout of its
# own.
PROBE_URL="https://github.com/git/git.git"
PROBE_TIMEOUT=60
fetch_failed() {
  fetch_msg=$1
  if (git_timeout=$PROBE_TIMEOUT; git_ ls-remote "$URL" HEAD > /dev/null); then
    die "$fetch_msg"
  fi
  if (unset GITHUB_TOKEN; git_timeout=$PROBE_TIMEOUT; git_ ls-remote "$PROBE_URL" HEAD > /dev/null); then
    die "$fetch_msg"
  fi
  echo "reviewbot-fetcher: $fetch_msg; github.com's git endpoint is unreachable (git ls-remote of $URL and of $PROBE_URL failed)" >&2
  exit 75
}

rm -rf "$GIT_DIR_PATH" "$TREE"
mkdir -p "$TREE"
git init -q --bare "$GIT_DIR_PATH" >/dev/null || die "git init failed"
# A bare init sets core.bare=true; this repository has a work tree.
git --git-dir="$GIT_DIR_PATH" config core.bare false

# Fetch the base commit and the pull request head. GitHub serves any
# reachable commit by id, and refs/pull/<n>/head also covers forks.
echo "reviewbot-fetcher: fetching $REPO#$NUMBER" >&2
git_logged fetch -q --no-tags --no-recurse-submodules --depth=50 "$URL" \
  "$BASE_SHA" "+refs/pull/$NUMBER/head:refs/remotes/pr/head" \
  || fetch_failed "fetch of base and refs/pull/$NUMBER/head failed"

if ! git_ cat-file -e "$HEAD_SHA^{commit}"; then
  # The pull request moved after the orchestrator read it. Fetch the exact
  # head the orchestrator asked for.
  echo "reviewbot-fetcher: refs/pull/$NUMBER/head is not $HEAD_SHA, fetching that commit" >&2
  git_logged fetch -q --no-tags --no-recurse-submodules --depth=50 "$URL" "$HEAD_SHA" \
    || fetch_failed "fetch of head_sha failed"
fi
git_ cat-file -e "$BASE_SHA^{commit}" || die "base_sha missing after fetch"
git_ cat-file -e "$HEAD_SHA^{commit}" || die "head_sha missing after fetch"

# diff.patch is base...head (changes on head since the merge base): the diff
# GitHub shows and positions review comments against, so the orchestrator's line
# map matches the lines GitHub accepts. Deepen the shallow history until a
# merge base is found.
merge_base=""
for deepen in 0 200 2000 unshallow; do
  case "$deepen" in
    0) ;;
    unshallow)
      # git refuses --unshallow on a complete repository.
      if [ "$(git_ rev-parse --is-shallow-repository)" = false ]; then
        continue
      fi
      git_logged fetch -q --no-tags --no-recurse-submodules --unshallow "$URL" \
        "$BASE_SHA" "$HEAD_SHA" || fetch_failed "unshallow fetch failed" ;;
    *)
      git_logged fetch -q --no-tags --no-recurse-submodules --deepen="$deepen" "$URL" \
        "$BASE_SHA" "$HEAD_SHA" || fetch_failed "deepen fetch failed" ;;
  esac
  if merge_base=$(git_ merge-base "$BASE_SHA" "$HEAD_SHA"); then
    break
  fi
  merge_base=""
done
[ -n "$merge_base" ] || die "no merge base between base_sha and head_sha"
if [ "$merge_base" != "$BASE_SHA" ]; then
  echo "reviewbot-fetcher: base_sha is not an ancestor of head; diffing from merge base $merge_base" >&2
fi

git_logged checkout -q --no-recurse-submodules --detach "$HEAD_SHA" || die "checkout failed"
# git checkout exits 0 when it could not create some files, after printing an
# error line for each. Such a checkout is incomplete and must not reach the
# reviewer.
if grep -q -E '^(error|fatal):' "$GIT_LOG"; then
  die "checkout reported errors (see above); refusing an incomplete checkout"
fi

git_logged -c core.quotepath=false diff --no-color --no-ext-diff --no-textconv -M \
  "$merge_base" "$HEAD_SHA" > "$SCRATCH/diff.patch" || die "git diff failed"

# files.txt: one path per line. A path containing a newline cannot be
# represented, so refuse rather than write a misleading list.
git_logged diff --no-ext-diff --no-textconv -M --name-only -z \
  "$merge_base" "$HEAD_SHA" > "$GIT_DIR_PATH/files.z" || die "git diff --name-only failed"
nul_count=$(tr -cd '\0' < "$GIT_DIR_PATH/files.z" | wc -c) || die "cannot count the paths in files.z"
nl_count=$(tr -cd '\n' < "$GIT_DIR_PATH/files.z" | wc -c) || die "cannot count the newlines in files.z"
[ "$nl_count" -eq 0 ] || die "a changed path contains a newline"
tr '\0' '\n' < "$GIT_DIR_PATH/files.z" > "$SCRATCH/files.txt" || die "cannot write files.txt"
[ "$(wc -l < "$SCRATCH/files.txt")" -eq "$nul_count" ] || die "files.txt line count mismatch"

# Belt and braces: nothing git-related may remain in the checkout.
rm -rf "$GIT_DIR_PATH" "$TREE/.git"
[ ! -e "$TREE/.git" ] || die "$TREE/.git still exists"

# Every check has passed: copy the result from the scratch tmpfs to /work.
cp -R "$TREE" "$WORK/repo" || die "copying repo/ to $WORK failed"
cp "$SCRATCH/diff.patch" "$SCRATCH/files.txt" "$WORK/" || die "copying diff.patch and files.txt to $WORK failed"
[ ! -e "$WORK/repo/.git" ] || die "$WORK/repo/.git still exists"

# The review container runs as a different uid; under rootless docker that
# uid is neither the owner nor in the group of these files. chmod -R does not
# follow symlinks, and core.symlinks=false means the checkout has none.
chmod -R a+rX "$WORK" || die "chmod failed"

echo "reviewbot-fetcher: ok, $(wc -l < "$WORK/files.txt") changed paths, head $HEAD_SHA" >&2
