#!/usr/bin/env bash
# Install or update reviewbot for the current user. Run it from a checkout
# again after every pull: it rebuilds everything from that checkout.
#   - a virtualenv at ~/.local/share/reviewbot/venv with the orchestrator
#     package installed as a copy (pip install of orchestrator/). The venv
#     does not read the checkout afterwards, so editing or moving the
#     checkout changes nothing until this script runs again.
#   - the three docker images reviewbot-proxy, reviewbot-reviewer and
#     reviewbot-fetcher, built by deploy/build-images.sh from this checkout.
#     The orchestrator starts containers from them; it builds nothing.
#   - a build id, generated once per run. It is the label reviewbot.build on
#     each image and the file reviewbot-build-id in the venv. The orchestrator
#     exits 2 at startup when an image carries a different id, so it never
#     runs against images from another build.
#   - ~/.config/reviewbot/env (mode 0600), created from deploy/env.example if
#     absent. It holds the orchestrator's settings and the Claude token.
#   - the systemd user unit reviewbot.service (the orchestrator) in
#     ~/.config/systemd/user, enabled. When it was running as this script
#     began, it is stopped before anything is replaced and started again at
#     the end, so it never runs new images with the old package or the
#     reverse. When it was not running it is not started. If the script fails
#     while the service is stopped, it says so; the service stays stopped.
#   - a symlink at ~/.local/share/reviewbot/src is removed; a real directory
#     or file at that path is refused.
#
# Every step is read back and the script exits non-zero if the result is not
# what was intended.
#
# Environment:
#   PYTHON                 interpreter for the venv (default python3, needs 3.12+)
#   REVIEWBOT_NO_SYSTEMCTL=1  install files only; skip daemon-reload, enable
#                          and the stop and start of the service (for a
#                          machine or test without a user manager)
set -euo pipefail

die() { echo "install.sh: $*" >&2; exit 1; }

REPO_DIR=$(cd -P "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd -P)
PYTHON=${PYTHON:-python3}
DATA_DIR="$HOME/.local/share/reviewbot"
VENV="$DATA_DIR/venv"
SRC_LINK="$DATA_DIR/src"
BUILD_ID_FILE="$VENV/reviewbot-build-id"
CONF_DIR="$HOME/.config/reviewbot"
ENV_FILE="$CONF_DIR/env"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_FILE="$UNIT_DIR/reviewbot.service"

# The unit names its paths with %h/.config and %h/.local/share; a different
# XDG_CONFIG_HOME would put the unit where those paths do not match.
if [ "$UNIT_DIR" != "$HOME/.config/systemd/user" ]; then
  die "XDG_CONFIG_HOME is set to ${XDG_CONFIG_HOME}; the unit expects $HOME/.config"
fi

test -f "$REPO_DIR/orchestrator/pyproject.toml" || die "orchestrator/pyproject.toml not found under $REPO_DIR"
test -f "$REPO_DIR/orchestrator/src/reviewbot/schema.json" || die "orchestrator/src/reviewbot/schema.json not found"
test -f "$REPO_DIR/deploy/reviewbot.service" || die "deploy/reviewbot.service not found"
test -f "$REPO_DIR/deploy/env.example" || die "deploy/env.example not found"
test -x "$REPO_DIR/deploy/build-images.sh" || die "deploy/build-images.sh not found or not executable"

"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' \
  || die "$PYTHON is older than 3.12"

# Whether the service runs now decides whether to stop it before the update
# and start it again at the end; this must be read before anything changes it.
was_active=0
if [ "${REVIEWBOT_NO_SYSTEMCTL:-}" != 1 ] && systemctl --user is-active --quiet reviewbot.service; then
  was_active=1
fi

stopped_for_update=0
report_stopped_service() {
  local rc=$?
  if [ "$stopped_for_update" = 1 ] && [ "$rc" -ne 0 ]; then
    echo "install.sh: reviewbot.service was stopped for this update and has not been started again; fix the cause and run install.sh again, or start it with: systemctl --user start reviewbot.service" >&2
  fi
}
trap report_stopped_service EXIT

if [ "$was_active" = 1 ]; then
  echo "install.sh: reviewbot.service is running; stopping it for the update"
  stopped_for_update=1
  systemctl --user stop reviewbot.service || die "systemctl --user stop reviewbot.service failed"
  stopped_state=$(systemctl --user is-active reviewbot.service) || stopped_state="${stopped_state:-unknown}"
  [ "$stopped_state" = inactive ] || die "reviewbot.service is '$stopped_state' after the stop, expected inactive"
fi

build_ts=$(date -u +%Y%m%dT%H%M%SZ) || die "date failed"
build_rand=$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n') || die "cannot read /dev/urandom"
BUILD_ID="$build_ts-$build_rand"
[[ "$BUILD_ID" =~ ^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$ ]] || die "generated build id '$BUILD_ID' has an unexpected form"
echo "install.sh: build id $BUILD_ID"

install -d -m 0700 "$DATA_DIR" "$DATA_DIR/state" "$DATA_DIR/work"

# Nothing reads this path. A symlink here would point at a checkout that the
# install no longer uses, so it is removed.
if [ -L "$SRC_LINK" ]; then
  echo "install.sh: removing the symlink $SRC_LINK (was -> $(readlink "$SRC_LINK"))"
  rm "$SRC_LINK"
elif [ -e "$SRC_LINK" ]; then
  die "$SRC_LINK exists and is not a symlink; refusing to remove it (move it away and run again)"
fi
if [ -e "$SRC_LINK" ] || [ -L "$SRC_LINK" ]; then
  die "$SRC_LINK still exists after removing it"
fi

echo "install.sh: building the docker images"
"$REPO_DIR/deploy/build-images.sh" reviewbot "$BUILD_ID" || die "deploy/build-images.sh reviewbot failed; no unit was installed"

echo "install.sh: installing the package into $VENV"
# setuptools packages whatever an earlier in-tree build left in build/,
# including modules since deleted, so those directories go before and after.
clean_build_dirs() { rm -rf "$REPO_DIR/orchestrator/build" "$REPO_DIR/orchestrator/src/reviewbot.egg-info"; }
clean_build_dirs
"$PYTHON" -m venv "$VENV"
"$VENV/bin/python" -m pip install --quiet --disable-pip-version-check --upgrade "$REPO_DIR/orchestrator"
clean_build_dirs

# Read back: the console script runs, the package is imported from the venv
# and not from the checkout, and every file of the checkout's package is in
# the copy. -I and cwd / keep the current directory and PYTHONPATH out of the
# import.
"$VENV/bin/reviewbot" --help > /dev/null || die "reviewbot --help failed"
pkg_file=$(cd / && "$VENV/bin/python" -I -c 'import reviewbot; print(reviewbot.__file__)') || pkg_file="(import of reviewbot failed)"
case "$pkg_file" in
  "$REPO_DIR/"*) die "reviewbot imported from $pkg_file, inside the checkout; expected a copy under $VENV" ;;
  "$VENV/"*) ;;
  *) die "reviewbot imported from $pkg_file, expected under $VENV" ;;
esac
diff -r -q --exclude=__pycache__ "$REPO_DIR/orchestrator/src/reviewbot" "$(dirname "$pkg_file")" \
  || die "the installed package differs from $REPO_DIR/orchestrator/src/reviewbot"
deps_ok=$("$VENV/bin/python" -I -c 'import jwt, requests, cryptography; print("ok")') || deps_ok=""
[ "$deps_ok" = ok ] || die "dependencies missing in venv"

# The orchestrator reads this file at sys.prefix; it must be the venv's.
prefix=$(cd / && "$VENV/bin/python" -I -c 'import sys; print(sys.prefix)') || prefix="(python failed)"
[ "$prefix" -ef "$VENV" ] || die "the venv's sys.prefix is '$prefix', expected $VENV"
printf '%s\n' "$BUILD_ID" > "$BUILD_ID_FILE.tmp"
chmod 0644 "$BUILD_ID_FILE.tmp"
mv -f "$BUILD_ID_FILE.tmp" "$BUILD_ID_FILE"
written=$(cat "$BUILD_ID_FILE") || written="(cat failed)"
[ "$written" = "$BUILD_ID" ] || die "$BUILD_ID_FILE holds '$written', expected $BUILD_ID"
id_mode=$(stat -c '%a' "$BUILD_ID_FILE") || id_mode="(stat failed)"
[ "$id_mode" = 644 ] || die "$BUILD_ID_FILE has mode $id_mode, expected 644"

echo "install.sh: installing $ENV_FILE"
install -d -m 0700 "$CONF_DIR"
if [ ! -e "$ENV_FILE" ]; then
  install -m 0600 "$REPO_DIR/deploy/env.example" "$ENV_FILE"
  echo "install.sh: created $ENV_FILE from env.example; edit it before starting"
fi
chmod 0600 "$ENV_FILE"
env_mode=$(stat -c '%a' "$ENV_FILE") || env_mode="(stat failed)"
[ "$env_mode" = 600 ] || die "$ENV_FILE has mode $env_mode, expected 600"

echo "install.sh: installing $UNIT_FILE"
install -D -m 0644 "$REPO_DIR/deploy/reviewbot.service" "$UNIT_FILE"
cmp -s "$REPO_DIR/deploy/reviewbot.service" "$UNIT_FILE" || die "installed reviewbot.service differs from source"

restarted=0
if [ "${REVIEWBOT_NO_SYSTEMCTL:-}" = 1 ]; then
  echo "install.sh: REVIEWBOT_NO_SYSTEMCTL=1, not enabling the unit"
else
  # reviewbot.service requires the rootless docker user unit.
  docker_load=$(systemctl --user show -p LoadState --value docker.service) || docker_load="(systemctl failed)"
  [ "$docker_load" = loaded ] \
    || die "the user unit docker.service is '$docker_load'; reviewbot.service needs rootless docker running as a systemd user unit"

  verify_out=$(systemd-analyze --user verify "$UNIT_FILE" 2>&1) || {
    echo "$verify_out" >&2
    die "systemd-analyze verify failed"
  }
  [ -z "$verify_out" ] || echo "$verify_out" >&2

  systemctl --user daemon-reload
  systemctl --user enable reviewbot.service

  enabled=$(systemctl --user is-enabled reviewbot.service) || true
  [ "$enabled" = enabled ] || die "reviewbot.service is '$enabled' after enable"
  fragment=$(systemctl --user show -p FragmentPath --value reviewbot.service) || fragment="(systemctl failed)"
  [ "$fragment" = "$UNIT_FILE" ] || die "user manager loads reviewbot.service from '$fragment', expected $UNIT_FILE"
  exec_start=$(systemctl --user show -p ExecStart --value reviewbot.service) || exec_start="(systemctl failed)"
  case "$exec_start" in
    *"path=$VENV/bin/reviewbot "*) ;;
    *) die "unexpected ExecStart: $exec_start" ;;
  esac
  env_files=$(systemctl --user show -p EnvironmentFiles --value reviewbot.service) || env_files="(systemctl failed)"
  [ "$env_files" = "$ENV_FILE (ignore_errors=no)" ] \
    || die "reviewbot.service EnvironmentFiles is '$env_files', expected only '$ENV_FILE (ignore_errors=no)'"

  if [ "$was_active" = 1 ]; then
    echo "install.sh: starting reviewbot.service again"
    systemctl --user start reviewbot.service || die "systemctl --user start reviewbot.service failed"
    stopped_for_update=0
    # The orchestrator checks its settings and the image build ids in its
    # first seconds and exits 2 if they are wrong, so a single is-active
    # right after the start would still read "active".
    for _ in 1 2 3 4 5; do
      sleep 1
      state=$(systemctl --user is-active reviewbot.service) || state="${state:-unknown}"
      [ "$state" = active ] \
        || die "reviewbot.service is '$state' after the start; see: journalctl --user -u reviewbot.service"
    done
    restarted=1
    echo "install.sh: reviewbot.service is enabled and running again"
  else
    echo "install.sh: reviewbot.service is enabled (not started)"
  fi
fi

if [ "$restarted" = 1 ]; then
  echo "install.sh: the new build is running; follow it with: journalctl --user -u reviewbot.service -f"
else
  cat <<MSG
install.sh: next steps
  1. edit $ENV_FILE: set CLAUDE_CODE_OAUTH_TOKEN (from "claude setup-token"),
     REVIEWBOT_APP_ID and REVIEWBOT_APP_PRIVATE_KEY_PATH; keep the file mode 0600
  2. systemctl --user start reviewbot.service
  3. journalctl --user -u reviewbot.service -f
  To keep it running while you are logged out: loginctl enable-linger $(id -un)
MSG
fi
echo "install.sh: done"
