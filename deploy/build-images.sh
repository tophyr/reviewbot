#!/usr/bin/env bash
# Builds the three reviewbot images from the Dockerfiles in this checkout,
# tagged PREFIX-proxy, PREFIX-reviewer and PREFIX-fetcher.
#
# deploy/install.sh and the e2e suite run this; it is not meant to be run by
# hand. Run with the deployment's prefix, it replaces the deployed images
# without the matching build id, and the orchestrator exits at its next
# review and refuses to start until deploy/install.sh runs again.
#
#   deploy/build-images.sh PREFIX [BUILD_ID]
#
# PREFIX follows the rule for REVIEWBOT_DOCKER_PREFIX: ^[a-z][a-z0-9-]{0,31}$.
# With BUILD_ID (letters, digits, '.', '_' and '-'), each image gets the label
# reviewbot.build=BUILD_ID, which the orchestrator compares with the id that
# deploy/install.sh wrote into its virtualenv. Without it the images carry no
# such label.
# The reviewer build also gets the build context "schema", the package
# directory that holds schema.json (the reviewer image does not keep its own
# copy).
# docker build's output goes to stderr. stdout gets one line per image,
# "<tag> <image ID>", where the ID is the digest of the image manifest for the
# docker server's platform. Under the containerd image store the top-level
# image ID changes on every build, even a fully cached one, because the build
# attestation is new each time.
set -euo pipefail

die() { echo "build-images.sh: $*" >&2; exit 1; }

[ "$#" -ge 1 ] && [ "$#" -le 2 ] || { echo "usage: build-images.sh PREFIX [BUILD_ID]" >&2; exit 2; }
prefix=$1
build_id=${2:-}
[[ "$prefix" =~ ^[a-z][a-z0-9-]{0,31}$ ]] \
  || { echo "build-images.sh: prefix '$prefix' does not match ^[a-z][a-z0-9-]{0,31}\$" >&2; exit 2; }
if [ "$#" -eq 2 ]; then
  [[ "$build_id" =~ ^[A-Za-z0-9._-]+$ ]] \
    || { echo "build-images.sh: build id '$build_id' does not match ^[A-Za-z0-9._-]+\$" >&2; exit 2; }
fi

# The repository is found from this script's own location, so it works from
# any directory and through a symlink.
SCRIPT=$(readlink -f "${BASH_SOURCE[0]}") || die "cannot resolve the path of this script"
REPO_DIR=$(cd -P "$(dirname "$SCRIPT")/.." && pwd -P) || die "cannot find the repository directory"
COMPONENTS="proxy reviewer fetcher"

for c in $COMPONENTS; do
  extra=()
  if [ -n "$build_id" ]; then
    extra+=(--label "reviewbot.build=$build_id")
  fi
  if [ "$c" = reviewer ]; then
    extra+=(--build-context "schema=$REPO_DIR/orchestrator/src/reviewbot")
  fi
  docker build -t "$prefix-$c" "${extra[@]}" "$REPO_DIR/$c" >&2 || die "docker build of $prefix-$c failed"
done

platform=$(docker version --format '{{.Server.Os}}/{{.Server.Arch}}') || die "docker version failed"
for c in $COMPONENTS; do
  id=$(docker image inspect --platform "$platform" --format '{{.Id}}' "$prefix-$c") \
    || die "image $prefix-$c for $platform not found after the build"
  echo "$prefix-$c $id"
done
