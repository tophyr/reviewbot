#!/usr/bin/env bash
# Run deploy/install.sh end to end inside a disposable container that boots
# systemd, with a real user manager for a non-root test user, and assert the
# resulting state. Nothing is installed or enabled on the host.
#
# Needs docker (rootless works; the container uses --privileged, which under
# rootless docker is confined to the user namespace). Pulls ubuntu:24.04
# packages and PyPI wheels over the network.
#
# The container has no Docker. Its PATH holds a stand-in `docker` that appends
# its arguments to ~/docker-argv.log and exits 0, and exits 1 for the Nth
# `build` when DOCKER_STUB_FAIL_BUILD=N. Before each build it logs the state of
# reviewbot.service as "service-at-build <state>". It also answers the queries the
# orchestrator makes at startup: `image inspect` with the reviewbot.build
# label prints the label of the last logged `build` of that image, and
# `network inspect` prints true for a network named *_internal and false
# otherwise. A placeholder user unit docker.service (a oneshot that runs
# /bin/true) lets Requires=docker.service resolve. Neither is evidence that
# Docker works; the stand-in shows only which commands install.sh (through
# deploy/build-images.sh) runs, and what label a real build would have been
# given.
#
# Checks:
#   1. install.sh reaches "install.sh: done" and exits 0
#   2. independently: reviewbot.service enabled and not active, FragmentPath,
#      ExecStart, the single EnvironmentFile, Requires/After docker.service,
#      restart and stop settings, the env file mode 600 and holding
#      CLAUDE_CODE_OAUTH_TOKEN, and no src symlink
#   3. install.sh ran exactly three `docker build -t reviewbot-<name> <dir>`,
#      for proxy, reviewer and fetcher, with the contexts in the checkout, all
#      three with --label reviewbot.build=<id>, the reviewer and fetcher also
#      with --build-context package=<checkout>/orchestrator/src/reviewbot; the id has
#      the form <UTC timestamp>-<8 hex digits> and is the one line, mode 644,
#      of <venv>/reviewbot-build-id
#   4. a second run is idempotent, keeps the existing env file, builds the
#      three images again with a new id, and does not start the service
#   5. the venv imports reviewbot from its own site-packages, not the
#      checkout; a source edit, a new module and a stale build/ directory in
#      the checkout reach the venv only after install.sh runs again, and a
#      module deleted from the checkout is gone from the venv after that
#   6. an unconfigured orchestrator exits 2 and is not restarted; a
#      configured stand-in sees the token from the env file in its environment
#   7. a broken unit file makes install.sh fail before "done"
#   8. a mismatched XDG_CONFIG_HOME makes install.sh fail
#   9. a stale src symlink is removed
#  10. a real directory at the src path is refused before any build and left
#      untouched
#  11. a masked docker.service makes install.sh fail
#  12. a build that fails (the stand-in exits 1 on the second build) makes
#      install.sh fail with a message naming build-images.sh, before "done",
#      with no unit and no venv installed
#  13. REVIEWBOT_NO_SYSTEMCTL=1 installs the unit without enabling
#  14. install.sh stops a running reviewbot.service before it builds anything
#      (the stand-in records the service state at each build) and starts it
#      again (new MainPID), and the started orchestrator (the real one, run
#      against the stand-in) reaches its poll loop
#  15. the running orchestrator exits 2 when an image carries another build id
#  16. install.sh does not start a stopped service, and fails, without the
#      "stopped for this update" message, when the service it started is not
#      active afterwards
#  17. install.sh failing after it stopped the service (the stand-in fails the
#      first build) prints that the service was stopped and not started again,
#      and the service is inactive
set -euo pipefail

REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
IMAGE=reviewbot-installtest:local
NAME=reviewbot-installtest-$$
BUILD_DIR=$(mktemp -d)
trap 'docker rm -f "$NAME" >/dev/null 2>&1 || true; rm -rf "$BUILD_DIR"' EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "PASS: $*"; }

cat > "$BUILD_DIR/docker-stub" <<'STUB'
#!/bin/sh
log=${DOCKER_STUB_LOG:-$HOME/docker-argv.log}
echo "$*" >> "$log"
if [ "${1:-}" = build ]; then
  echo "service-at-build $(systemctl --user is-active reviewbot.service)" >> "$log"
fi
if [ "${1:-}" = build ] && [ -n "${DOCKER_STUB_FAIL_BUILD:-}" ]; then
  n=$(grep -c '^build ' "$log") || n=0
  if [ "$n" = "$DOCKER_STUB_FAIL_BUILD" ]; then
    echo "docker stub: failing build number $n" >&2
    exit 1
  fi
fi
if [ "${1:-} ${2:-}" = "image inspect" ]; then
  case "$*" in
    *reviewbot.build*)
      for image; do :; done
      line=$(grep "^build -t $image " "$log" | tail -n 1) || line=""
      case "$line" in
        *"--label reviewbot.build="*) rest=${line#*--label reviewbot.build=}; echo "${rest%% *}" ;;
        *) echo ;;
      esac ;;
    *) echo sha256:stub ;;
  esac
fi
if [ "${1:-} ${2:-}" = "network inspect" ]; then
  for network; do :; done
  case "$network" in *_internal) echo true ;; *) echo false ;; esac
fi
exit 0
STUB

cat > "$BUILD_DIR/Dockerfile" <<'DOCKERFILE'
FROM ubuntu:24.04
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      systemd systemd-sysv dbus dbus-user-session python3 python3-venv ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN userdel -r ubuntu 2>/dev/null; useradd -m -u 1000 -s /bin/bash tester \
 && useradd -m -u 1001 -s /bin/bash tester2
RUN printf '%s\n' '[Unit]' 'Description=placeholder for rootless docker (this test container has no Docker)' \
      '[Service]' 'Type=oneshot' 'RemainAfterExit=yes' 'ExecStart=/bin/true' \
      > /etc/systemd/user/docker.service
COPY docker-stub /usr/local/bin/docker
RUN chmod 0755 /usr/local/bin/docker
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
DOCKERFILE
docker build -q -t "$IMAGE" "$BUILD_DIR" > /dev/null

docker run -d -t --name "$NAME" --privileged --cgroupns=private \
  --tmpfs /run --tmpfs /run/lock -v "$REPO_DIR:/src:ro" "$IMAGE" > /dev/null

state=""
for _ in $(seq 1 60); do
  state=$(docker exec "$NAME" systemctl is-system-running 2>&1) || true
  case "$state" in running|degraded) break ;; esac
  sleep 1
done
case "$state" in running|degraded) ;; *) fail "systemd in container did not boot: $state" ;; esac

docker exec "$NAME" loginctl enable-linger tester
active=""
for _ in $(seq 1 30); do
  active=$(docker exec "$NAME" systemctl is-active user@1000.service 2>&1) || true
  [ "$active" = active ] && break
  sleep 1
done
[ "$active" = active ] || fail "user manager for tester not active: $active"

as_user() {
  local user=$1 uid=$2; shift 2
  docker exec -u "$user" -e XDG_RUNTIME_DIR="/run/user/$uid" \
    -e DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" \
    -w "/home/$user" "$NAME" bash -c "$*"
}
copy_src='mkdir -p rb && (cd /src && tar --exclude=./orchestrator/.venv --exclude="*.egg-info" --exclude=./orchestrator/build --exclude=__pycache__ -cf - ./orchestrator ./deploy ./proxy ./reviewer ./fetcher) | tar -xf - -C rb'

# Poll a unit's state for up to 15 seconds while it is in one of the given
# transient states; print the state it ends in.
settle() {
  local user=$1 uid=$2 unit=$3 transient=" $4 " st=""
  for _ in $(seq 1 15); do
    st=$(as_user "$user" "$uid" "systemctl --user is-active $unit") || true
    case "$transient" in *" $st "*) sleep 1 ;; *) break ;; esac
  done
  echo "$st"
}

# build_lines USER UID: the stand-in's log lines for `docker build`, sorted
build_lines() {
  local lines
  lines=$(as_user "$1" "$2" "grep '^build ' docker-argv.log | sort") || lines=""
  printf '%s\n' "$lines"
}
# expect_builds USER UID HOME ID LABEL: the log holds exactly the three builds,
# each labelled with ID, and the reviewer's and fetcher's with the package
# build context
expect_builds() {
  local want got
  want=$(printf '%s\n' \
    "build -t reviewbot-fetcher --label reviewbot.build=$4 --build-context package=$3/rb/orchestrator/src/reviewbot $3/rb/fetcher" \
    "build -t reviewbot-proxy --label reviewbot.build=$4 $3/rb/proxy" \
    "build -t reviewbot-reviewer --label reviewbot.build=$4 --build-context package=$3/rb/orchestrator/src/reviewbot $3/rb/reviewer") \
    || want="(printf failed)"
  got=$(build_lines "$1" "$2") || got="(build_lines failed)"
  [ "$got" = "$want" ] || { echo "got:"; echo "$got"; echo "want:"; echo "$want"; fail "$5: docker build invocations differ"; }
}
# installed_build_id USER UID HOME: print the build id in the venv, after
# checking that the file is that id on one line with no other bytes, mode 644
installed_build_id() {
  local file="$3/.local/share/reviewbot/venv/reviewbot-build-id" id raw mode
  id=$(as_user "$1" "$2" "cat $file") || fail "cannot read $file"
  [[ "$id" =~ ^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$ ]] || fail "$file holds '$id', not <UTC timestamp>-<8 hex digits>"
  raw=$(as_user "$1" "$2" "cat -A $file") || fail "cannot read $file"
  [ "$raw" = "$id\$" ] || fail "$file is not exactly one line: '$raw'"
  mode=$(as_user "$1" "$2" "stat -c %a $file") || fail "cannot stat $file"
  [ "$mode" = 644 ] || fail "$file has mode $mode, expected 644"
  printf '%s\n' "$id"
}

as_user tester 1000 "$copy_src"

# 1
as_user tester 1000 'rm -f docker-argv.log'
out=$(as_user tester 1000 'rb/deploy/install.sh 2>&1') || { echo "$out"; fail "install.sh exited non-zero"; }
last=$(printf '%s\n' "$out" | tail -n 1) || last="(tail failed)"
[ "$last" = "install.sh: done" ] || { echo "$out"; fail "install.sh did not reach the end"; }
pass "install.sh ran to the end"

# 2
H=/home/tester
SRC=$H/.local/share/reviewbot/src
V=$H/.local/share/reviewbot/venv
unit=reviewbot.service
enabled=$(as_user tester 1000 "systemctl --user is-enabled $unit") || true
[ "$enabled" = enabled ] || fail "$unit is-enabled: $enabled"
is_active=$(as_user tester 1000 "systemctl --user is-active $unit") || true
[ "$is_active" = inactive ] || fail "$unit should not be started by install.sh, is-active: $is_active"
frag=$(as_user tester 1000 "systemctl --user show -p FragmentPath --value $unit") || frag="(systemctl failed)"
[ "$frag" = "$H/.config/systemd/user/$unit" ] || fail "$unit FragmentPath: $frag"
wanted=$(as_user tester 1000 "readlink .config/systemd/user/default.target.wants/$unit") || true
[ "$wanted" = "$H/.config/systemd/user/$unit" ] || fail "$unit not linked from default.target.wants: '$wanted'"
as_user tester 1000 'test ! -e .local/share/reviewbot/src && test ! -L .local/share/reviewbot/src' \
  || fail "$SRC exists after install"
execs=$(as_user tester 1000 "systemctl --user show -p ExecStart --value $unit") || execs="(systemctl failed)"
case "$execs" in *"argv[]=$H/.local/share/reviewbot/venv/bin/reviewbot run ;"*) ;; *) fail "ExecStart: $execs" ;; esac
envf=$(as_user tester 1000 "systemctl --user show -p EnvironmentFiles --value $unit") || envf="(systemctl failed)"
[ "$envf" = "$H/.config/reviewbot/env (ignore_errors=no)" ] || fail "EnvironmentFiles: '$envf', expected only $H/.config/reviewbot/env"
deps=$(as_user tester 1000 "systemctl --user show -p Requires -p After -p Restart -p RestartUSec -p RestartPreventExitStatus -p TimeoutStopUSec -p NoNewPrivileges -p UMask -p WorkingDirectory $unit") \
  || deps="(systemctl failed)"
for want in "Restart=on-failure" "RestartUSec=1min" "RestartPreventExitStatus=2" "TimeoutStopUSec=1min" \
            "NoNewPrivileges=yes" "UMask=0077" "WorkingDirectory=$H/.local/share/reviewbot"; do
  printf '%s\n' "$deps" | grep -qxF "$want" || { echo "$deps"; fail "$unit lacks $want"; }
done
for prop_name in Requires After; do
  line=$(printf '%s\n' "$deps" | grep "^$prop_name=") || line="(no $prop_name line)"
  case " ${line#*=} " in *" docker.service "*) ;; *) fail "$unit $prop_name does not include docker.service: $line" ;; esac
done
mode=$(as_user tester 1000 'stat -c %a .config/reviewbot/env') || mode="(stat failed: env file missing?)"
[ "$mode" = 600 ] || fail "env file mode $mode"
as_user tester 1000 'grep -qx "CLAUDE_CODE_OAUTH_TOKEN=" .config/reviewbot/env' || fail "env file lacks the line CLAUDE_CODE_OAUTH_TOKEN="
pass "reviewbot.service enabled and not active; unit settings and env file correct; no src symlink"

# 3: the stand-in also records the other docker commands build-images.sh
# runs to print the image IDs; only the builds are compared
id1=$(installed_build_id tester 1000 "$H") || exit 1
expect_builds tester 1000 "$H" "$id1" "3"
pass "install.sh ran exactly three docker builds, each labelled with the id in the venv's reviewbot-build-id ($id1); the reviewer build has the schema context"

# 4
as_user tester 1000 'echo "# local edit" >> .config/reviewbot/env; rm -f docker-argv.log'
out=$(as_user tester 1000 'rb/deploy/install.sh 2>&1') || { echo "$out"; fail "second run failed"; }
[ "$(printf '%s\n' "$out" | tail -n 1)" = "install.sh: done" ] || fail "second run did not reach the end"
kept=$(as_user tester 1000 'tail -n 1 .config/reviewbot/env') || kept="(tail failed: env file missing?)"
[ "$kept" = "# local edit" ] || fail "second run replaced the env file"
id2=$(installed_build_id tester 1000 "$H") || exit 1
[ "$id2" != "$id1" ] || fail "4: the second run reused the build id $id1"
expect_builds tester 1000 "$H" "$id2" "4"
is_active=$(as_user tester 1000 "systemctl --user is-active $unit") || true
[ "$is_active" = inactive ] || fail "4: $unit was started by the second run: $is_active"
pass "second run keeps the env file, builds the three images again with a new id ($id2) and does not start the service"

# 5: the venv holds a copy of the package
pkg=$(as_user tester 1000 "cd / && $V/bin/python -I -c 'import reviewbot; print(reviewbot.__file__)'") \
  || pkg="(import of reviewbot failed)"
case "$pkg" in "$V/lib/"*"/site-packages/reviewbot/__init__.py") ;; *) fail "venv imports reviewbot from '$pkg', expected its own site-packages" ;; esac
site=$(dirname "$pkg") || fail "5: dirname failed"
marker="installtest-$$-$RANDOM"
as_user tester 1000 "printf '%s\n' 'INSTALLTEST_MARKER = \"$marker\"' >> rb/orchestrator/src/reviewbot/__init__.py \
  && printf '%s\n' 'X = 1' > rb/orchestrator/src/reviewbot/zz_new.py \
  && mkdir -p rb/orchestrator/build/lib/reviewbot && printf '%s\n' 'STALE = 1' > rb/orchestrator/build/lib/reviewbot/zz_stale.py" \
  || fail "5: could not edit the checkout"
has=$(as_user tester 1000 "cd / && $V/bin/python -I -c 'import reviewbot; print(hasattr(reviewbot, \"INSTALLTEST_MARKER\"))'") \
  || has="(python failed)"
[ "$has" = False ] || fail "5: an edit in the checkout is visible to the venv without installing: $has"
as_user tester 1000 "test ! -e $site/zz_new.py" || fail "5: a new module in the checkout is in the venv without installing"
out=$(as_user tester 1000 'rb/deploy/install.sh 2>&1') || { echo "$out"; fail "5: install.sh failed after an edit"; }
seen=$(as_user tester 1000 "cd / && $V/bin/python -I -c 'import reviewbot; print(reviewbot.INSTALLTEST_MARKER)'") \
  || seen="(the venv did not see the edit)"
[ "$seen" = "$marker" ] || fail "5: the edit is not in the venv after install.sh: got '$seen'"
as_user tester 1000 "cd / && $V/bin/python -I -c 'import reviewbot.zz_new'" || fail "5: the new module is not in the venv after install.sh"
[ -z "$(as_user tester 1000 "find $V/lib -name zz_stale.py")" ] || fail "5: a file of a stale build/ directory was packaged"
as_user tester 1000 'test ! -e rb/orchestrator/build' || fail "5: install.sh left orchestrator/build in the checkout"
as_user tester 1000 'sed -i "/^INSTALLTEST_MARKER/d" rb/orchestrator/src/reviewbot/__init__.py && rm rb/orchestrator/src/reviewbot/zz_new.py' \
  || fail "5: could not restore the checkout"
out=$(as_user tester 1000 'rb/deploy/install.sh 2>&1') || { echo "$out"; fail "5: install.sh failed after the restore"; }
[ -z "$(as_user tester 1000 "find $V/lib -name 'zz_new*'")" ] || fail "5: a module deleted from the checkout is still in the venv"
has=$(as_user tester 1000 "cd / && $V/bin/python -I -c 'import reviewbot; print(hasattr(reviewbot, \"INSTALLTEST_MARKER\"))'") \
  || has="(python failed)"
[ "$has" = False ] || fail "5: the removed edit is still in the venv: $has"
as_user tester 1000 'rm -f docker-argv.log'
pass "the venv imports reviewbot from its own site-packages; an edit, a new module and a stale build/ directory reach it only through install.sh, and a deleted module is removed"

# 6
as_user tester 1000 'sed -i "s|^CLAUDE_CODE_OAUTH_TOKEN=.*|CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-not-a-real-token|" .config/reviewbot/env'
as_user tester 1000 'grep -qx "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-not-a-real-token" .config/reviewbot/env' \
  || fail "6: could not put a token into the env file"
as_user tester 1000 'systemctl --user start reviewbot.service'
st=$(settle tester 1000 reviewbot.service "active activating deactivating") || st="(settle failed)"
[ "$st" = failed ] || fail "expected the unconfigured service to fail, got $st"
status=$(as_user tester 1000 'systemctl --user show -p ExecMainStatus --value reviewbot.service') || status="(systemctl failed)"
restarts=$(as_user tester 1000 'systemctl --user show -p NRestarts --value reviewbot.service') || restarts="(systemctl failed)"
[ "$status" = 2 ] || fail "ExecMainStatus $status, expected 2"
[ "$restarts" = 0 ] || fail "NRestarts $restarts, expected 0"
journal=$(as_user tester 1000 'journalctl --user -u reviewbot.service --no-pager -o cat') || journal="(journalctl failed)"
case "$journal" in *"reviewbot needs REVIEWBOT_APP_ID"*) ;; *) echo "$journal"; fail "config error not in journal" ;; esac
as_user tester 1000 'systemctl --user reset-failed reviewbot.service'
D=.config/systemd/user
as_user tester 1000 "mkdir -p $D/reviewbot.service.d && printf '%s\n' '[Service]' 'ExecStart=' 'ExecStart=/bin/sleep infinity' > $D/reviewbot.service.d/test-sleep.conf && systemctl --user daemon-reload"
as_user tester 1000 'systemctl --user start reviewbot.service'
st=$(settle tester 1000 reviewbot.service "activating") || st="(settle failed)"
[ "$st" = active ] || fail "reviewbot.service with sleep drop-in is $st, expected active"
pid1=$(as_user tester 1000 'systemctl --user show -p MainPID --value reviewbot.service') || pid1="(systemctl failed)"
as_user tester 1000 "tr '\\0' '\\n' < /proc/$pid1/environ | grep -qx 'CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-not-a-real-token'" \
  || fail "reviewbot.service's environment lacks CLAUDE_CODE_OAUTH_TOKEN from the env file"
as_user tester 1000 "systemctl --user stop reviewbot.service; rm -rf $D/reviewbot.service.d; systemctl --user daemon-reload; systemctl --user reset-failed"
execs=$(as_user tester 1000 'systemctl --user show -p ExecStart --value reviewbot.service') || execs="(systemctl failed)"
case "$execs" in *"argv[]=$V/bin/reviewbot run ;"*) ;; *) fail "drop-in not removed, ExecStart: $execs" ;; esac
pass "unconfigured service exits 2 without restarting; the env file's token reaches the service's environment"

# 7
as_user tester 1000 'rm -rf rb-bad && cp -r rb rb-bad && sed -i "s|^ExecStart=.*|ExecStart=relative/path run|" rb-bad/deploy/reviewbot.service'
if out=$(as_user tester 1000 'rb-bad/deploy/install.sh 2>&1'); then
  echo "$out"; fail "install.sh accepted a broken unit"
fi
case "$out" in *"install.sh: done"*) fail "broken unit run printed done" ;; esac
case "$out" in *"systemd-analyze verify failed"*) ;; *) echo "$out"; fail "unexpected failure for broken unit" ;; esac
out=$(as_user tester 1000 'rb/deploy/install.sh 2>&1') || { echo "$out"; fail "restoring the good unit failed"; }
pass "broken unit is rejected"

# 8
if out=$(as_user tester 1000 'XDG_CONFIG_HOME=/tmp/elsewhere rb/deploy/install.sh 2>&1'); then
  fail "install.sh accepted a mismatched XDG_CONFIG_HOME"
fi
case "$out" in *"the unit expects"*) ;; *) echo "$out"; fail "unexpected failure for XDG_CONFIG_HOME" ;; esac
pass "mismatched XDG_CONFIG_HOME is rejected"

# 9
as_user tester 1000 'ln -sfn /home/tester/no-such-checkout .local/share/reviewbot/src'
out=$(as_user tester 1000 'rb/deploy/install.sh 2>&1') || { echo "$out"; fail "install.sh failed with a stale symlink"; }
[ "$(printf '%s\n' "$out" | tail -n 1)" = "install.sh: done" ] || { echo "$out"; fail "stale-symlink run did not reach the end"; }
case "$out" in *"removing the symlink $SRC (was -> /home/tester/no-such-checkout)"*) ;; *) echo "$out"; fail "stale symlink removal not reported" ;; esac
as_user tester 1000 'test ! -e .local/share/reviewbot/src && test ! -L .local/share/reviewbot/src' \
  || fail "stale symlink still present after install.sh"
pass "stale src symlink is removed"

# 10
as_user tester 1000 'mkdir .local/share/reviewbot/src && echo keep > .local/share/reviewbot/src/marker && rm -f docker-argv.log'
if out=$(as_user tester 1000 'rb/deploy/install.sh 2>&1'); then
  echo "$out"; fail "install.sh replaced a real directory"
fi
case "$out" in *"install.sh: done"*) fail "real-directory run printed done" ;; esac
case "$out" in *"exists and is not a symlink; refusing to remove it"*) ;; *) echo "$out"; fail "unexpected failure for a real directory" ;; esac
as_user tester 1000 'test -d .local/share/reviewbot/src && test ! -L .local/share/reviewbot/src && test "$(cat .local/share/reviewbot/src/marker)" = keep' \
  || fail "the real directory was modified"
as_user tester 1000 'test ! -e docker-argv.log' || fail "the refusal came after a docker command ran"
out=$(as_user tester 1000 'rm -rf .local/share/reviewbot/src && rb/deploy/install.sh 2>&1') || { echo "$out"; fail "install.sh after removing the directory failed"; }
pass "real directory at the src path is refused before any build and left untouched"

# 11
as_user tester 1000 'systemctl --user mask docker.service'
if out=$(as_user tester 1000 'rb/deploy/install.sh 2>&1'); then
  echo "$out"; fail "install.sh accepted a masked docker.service"
fi
case "$out" in *"the user unit docker.service is 'masked'"*) ;; *) echo "$out"; fail "unexpected failure for masked docker.service" ;; esac
as_user tester 1000 'systemctl --user unmask docker.service'
pass "masked docker.service is rejected"

# 12: the stand-in fails the second build; tester2 has nothing installed yet
as_user tester2 1001 "$copy_src"
as_user tester2 1001 'rm -f docker-argv.log'
rc=0
out=$(as_user tester2 1001 'DOCKER_STUB_FAIL_BUILD=2 REVIEWBOT_NO_SYSTEMCTL=1 rb/deploy/install.sh 2>&1') || rc=$?
[ "$rc" != 0 ] || { echo "$out"; fail "12: install.sh succeeded although the second docker build failed"; }
case "$out" in *"install.sh: done"*) echo "$out"; fail "12: install.sh printed done after a failed build" ;; esac
case "$out" in *"docker stub: failing build number 2"*) ;; *) echo "$out"; fail "12: the stand-in did not fail the second build" ;; esac
case "$out" in *"install.sh: deploy/build-images.sh reviewbot failed"*) ;; *) echo "$out"; fail "12: install.sh did not say that build-images.sh failed" ;; esac
n=$(as_user tester2 1001 "grep -c '^build ' docker-argv.log") || n="(count failed)"
[ "$n" = 2 ] || fail "12: $n docker builds ran, expected 2 (the third must not run after the second fails)"
as_user tester2 1001 'test ! -e .config/systemd/user/reviewbot.service' || fail "12: a unit was installed although the build failed"
as_user tester2 1001 'test ! -e .local/share/reviewbot/venv' || fail "12: a venv was created although the build failed"
pass "a failed docker build makes install.sh fail with a message naming build-images.sh and installs no unit and no package"

# 13
as_user tester2 1001 'rm -f docker-argv.log'
out=$(as_user tester2 1001 'REVIEWBOT_NO_SYSTEMCTL=1 rb/deploy/install.sh 2>&1') || { echo "$out"; fail "no-systemctl run failed"; }
[ "$(printf '%s\n' "$out" | tail -n 1)" = "install.sh: done" ] || fail "no-systemctl run did not reach the end"
as_user tester2 1001 "test -f .config/systemd/user/$unit" || fail "$unit file missing for tester2"
if as_user tester2 1001 "test -e .config/systemd/user/default.target.wants/$unit"; then
  fail "no-systemctl run enabled $unit"
fi
as_user tester2 1001 'test ! -e .local/share/reviewbot/src && test ! -L .local/share/reviewbot/src' || fail "13: src exists"
id13=$(installed_build_id tester2 1001 /home/tester2) || exit 1
expect_builds tester2 1001 /home/tester2 "$id13" "13"
pass "REVIEWBOT_NO_SYSTEMCTL=1 installs the package and the unit without enabling"

# The remaining checks run the real orchestrator under the user manager of
# tester, against the stand-in docker, with an App key generated here. GitHub
# is an address that refuses connections, so the orchestrator starts, fails
# its poll and waits.
as_user tester 1000 '.local/share/reviewbot/venv/bin/python -I -c "
from cryptography.hazmat.primitives import serialization as s
from cryptography.hazmat.primitives.asymmetric import rsa
key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
open(\"app.pem\", \"wb\").write(key.private_bytes(s.Encoding.PEM, s.PrivateFormat.TraditionalOpenSSL, s.NoEncryption()))
" && chmod 600 app.pem && grep -q "BEGIN RSA PRIVATE KEY" app.pem' \
  || fail "could not generate the App private key"
as_user tester 1000 "sed -i -e 's|^REVIEWBOT_APP_ID=.*|REVIEWBOT_APP_ID=1|' -e 's|^REVIEWBOT_APP_PRIVATE_KEY_PATH=.*|REVIEWBOT_APP_PRIVATE_KEY_PATH=$H/app.pem|' .config/reviewbot/env \
  && printf '%s\n' 'REVIEWBOT_GITHUB_API_URL=http://127.0.0.1:9' >> .config/reviewbot/env \
  && grep -qx 'REVIEWBOT_APP_ID=1' .config/reviewbot/env \
  && grep -qx 'REVIEWBOT_APP_PRIVATE_KEY_PATH=$H/app.pem' .config/reviewbot/env" \
  || fail "could not configure the env file for the real orchestrator"
as_user tester 1000 'rm -f docker-argv.log; systemctl --user reset-failed'
out=$(as_user tester 1000 'rb/deploy/install.sh 2>&1') || { echo "$out"; fail "install.sh failed before check 14"; }
id14a=$(installed_build_id tester 1000 "$H") || exit 1

# 14
as_user tester 1000 'systemctl --user start reviewbot.service'
st=$(settle tester 1000 reviewbot.service "activating") || st="(settle failed)"
[ "$st" = active ] || { as_user tester 1000 'journalctl --user -u reviewbot.service --no-pager -o cat' || true; fail "14: the orchestrator is $st, expected active"; }
sleep 3
st=$(as_user tester 1000 'systemctl --user is-active reviewbot.service') || st="(not active: $st)"
[ "$st" = active ] || { as_user tester 1000 'journalctl --user -u reviewbot.service --no-pager -o cat' || true; fail "14: the orchestrator is $st after 3 seconds"; }
pid_a=$(as_user tester 1000 'systemctl --user show -p MainPID --value reviewbot.service') || pid_a="(systemctl failed)"
stops_before=$(as_user tester 1000 "journalctl --user -u reviewbot.service --no-pager -o cat | grep -c '^Stopped reviewbot.service'") || stops_before=0
as_user tester 1000 'rm -f docker-argv.log'
out=$(as_user tester 1000 'rb/deploy/install.sh 2>&1') || { echo "$out"; fail "14: install.sh failed while the service was running"; }
[ "$(printf '%s\n' "$out" | tail -n 1)" = "install.sh: done" ] || { echo "$out"; fail "14: install.sh did not reach the end"; }
case "$out" in *"reviewbot.service is running; stopping it for the update"*) ;; *) echo "$out"; fail "14: install.sh did not report the stop" ;; esac
case "$out" in *"stopped for this update and has not been started again"*) echo "$out"; fail "14: the stopped-service message appeared on success" ;; esac
at_build=$(as_user tester 1000 "grep '^service-at-build ' docker-argv.log") || at_build="(no service-at-build lines)"
[ "$at_build" = "$(printf 'service-at-build inactive\nservice-at-build inactive\nservice-at-build inactive')" ] \
  || { echo "$at_build"; fail "14: the service was not inactive at each of the three builds"; }
stops_after=$(as_user tester 1000 "journalctl --user -u reviewbot.service --no-pager -o cat | grep -c '^Stopped reviewbot.service'") || stops_after=0
[ "$stops_after" = "$((stops_before + 1))" ] || fail "14: the journal shows $stops_before stops before and $stops_after after install.sh, expected one more"
id14=$(installed_build_id tester 1000 "$H") || exit 1
[ "$id14" != "$id14a" ] || fail "14: the build id did not change"
expect_builds tester 1000 "$H" "$id14" "14"
pid_b=$(as_user tester 1000 'systemctl --user show -p MainPID --value reviewbot.service') || pid_b="(systemctl failed)"
st=$(as_user tester 1000 'systemctl --user is-active reviewbot.service') || st="(not active: $st)"
[ "$st" = active ] || fail "14: the service is $st after install.sh"
[ "$pid_b" != "$pid_a" ] && [ "$pid_b" != 0 ] || fail "14: MainPID $pid_a before and $pid_b after install.sh; the service was not started again"
# The poll loop starts after the identity lookup, which retries against the
# refused address for a few seconds.
polls=0
for _ in $(seq 1 40); do
  journal=$(as_user tester 1000 'journalctl --user -u reviewbot.service --no-pager -o cat') || journal="(journalctl failed)"
  polls=$(printf '%s\n' "$journal" | grep -c 'polling every') || polls=0
  [ "$polls" -ge 2 ] && break
  sleep 1
done
[ "$polls" = 2 ] || { echo "$journal"; fail "14: expected the orchestrator to reach its poll loop twice (before and after the restart), saw $polls"; }
case "$journal" in *"reviewbot.build label"*) echo "$journal"; fail "14: a build id error in the journal" ;; esac
pass "install.sh stops a running service before the builds and starts it again; the new orchestrator accepts the new build's images"

# 15: an image from another build. The stand-in answers the label query with
# the last build line it logged for the image.
as_user tester 1000 'systemctl --user stop reviewbot.service'
as_user tester 1000 'echo "build -t reviewbot-proxy --label reviewbot.build=19990101T000000Z-deadbeef /elsewhere" >> docker-argv.log'
as_user tester 1000 'systemctl --user start reviewbot.service'
st=$(settle tester 1000 reviewbot.service "active activating deactivating") || st="(settle failed)"
[ "$st" = failed ] || fail "15: expected the orchestrator to fail on an image from another build, got $st"
status=$(as_user tester 1000 'systemctl --user show -p ExecMainStatus --value reviewbot.service') || status="(systemctl failed)"
restarts=$(as_user tester 1000 'systemctl --user show -p NRestarts --value reviewbot.service') || restarts="(systemctl failed)"
[ "$status" = 2 ] || fail "15: ExecMainStatus $status, expected 2"
[ "$restarts" = 0 ] || fail "15: NRestarts $restarts, expected 0"
journal=$(as_user tester 1000 'journalctl --user -u reviewbot.service --no-pager -o cat') || journal="(journalctl failed)"
case "$journal" in *"docker image reviewbot-proxy has reviewbot.build label '19990101T000000Z-deadbeef', but this orchestrator was installed as build $id14; run deploy/install.sh"*) ;; *) echo "$journal"; fail "15: build id error not in journal" ;; esac
as_user tester 1000 'systemctl --user reset-failed reviewbot.service'
pass "the orchestrator exits 2 without restarting when an image has another build id"

# 16: a stopped service stays stopped; a restart that does not come up is an error
as_user tester 1000 'rm -f docker-argv.log'
out=$(as_user tester 1000 'rb/deploy/install.sh 2>&1') || { echo "$out"; fail "16: install.sh failed with the service stopped"; }
st=$(as_user tester 1000 'systemctl --user is-active reviewbot.service') || st="${st:-unknown}"
[ "$st" = inactive ] || fail "16: install.sh left the stopped service $st"
case "$out" in *"next steps"*) ;; *) echo "$out"; fail "16: no next-steps message for a stopped service" ;; esac
case "$out" in *"stopping it for the update"*) echo "$out"; fail "16: install.sh stopped a service that was not running" ;; esac
case "$out" in *"starting reviewbot.service again"*) echo "$out"; fail "16: install.sh started a stopped service" ;; esac
as_user tester 1000 'systemctl --user start reviewbot.service'
st=$(settle tester 1000 reviewbot.service "activating") || st="(settle failed)"
[ "$st" = active ] || fail "16: the orchestrator is $st, expected active"
as_user tester 1000 'sed -i "/^REVIEWBOT_APP_ID=/d" .config/reviewbot/env'
rc=0
out=$(as_user tester 1000 'rb/deploy/install.sh 2>&1') || rc=$?
[ "$rc" != 0 ] || { echo "$out"; fail "16: install.sh succeeded although the restarted service failed"; }
case "$out" in *"install.sh: done"*) echo "$out"; fail "16: install.sh printed done after a failed restart" ;; esac
case "$out" in *"reviewbot.service is 'failed' after the start"*) ;; *) echo "$out"; fail "16: install.sh did not report the failed start" ;; esac
case "$out" in *"has not been started again"*) echo "$out"; fail "16: the stopped-service message appeared after a start" ;; esac
as_user tester 1000 'systemctl --user reset-failed reviewbot.service'
pass "install.sh leaves a stopped service stopped, and fails when the service it started does not stay active"

# 17: a failure after the stop leaves the service stopped, and says so
as_user tester 1000 "printf '%s\n' 'REVIEWBOT_APP_ID=1' >> .config/reviewbot/env"
as_user tester 1000 'systemctl --user start reviewbot.service'
st=$(settle tester 1000 reviewbot.service "activating") || st="(settle failed)"
[ "$st" = active ] || fail "17: the orchestrator is $st, expected active"
as_user tester 1000 'rm -f docker-argv.log'
rc=0
out=$(as_user tester 1000 'DOCKER_STUB_FAIL_BUILD=1 rb/deploy/install.sh 2>&1') || rc=$?
[ "$rc" != 0 ] || { echo "$out"; fail "17: install.sh succeeded although the first build failed"; }
case "$out" in *"install.sh: done"*) echo "$out"; fail "17: install.sh printed done after a failed build" ;; esac
case "$out" in *"install.sh: reviewbot.service was stopped for this update and has not been started again"*) ;; *) echo "$out"; fail "17: the stopped-service message is missing" ;; esac
st=$(as_user tester 1000 'systemctl --user is-active reviewbot.service') || st="${st:-unknown}"
[ "$st" = inactive ] || fail "17: the service is $st after the failed update, expected inactive"
pass "a failure after the stop leaves the service inactive and install.sh says it was stopped and not started again"

echo "ALL PASSED"
