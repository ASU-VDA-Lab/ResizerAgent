#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# build_image.sh - build the patched ORFS Docker image from the bundled source.
#
# Requires: Docker + an internet connection (the build pulls the ORFS dev base
# image from Docker Hub and downloads a few CMake FetchContent dependencies
# such as fmt/mimalloc/cpptrace/catch2 - this is stock upstream ORFS behavior).
#
# No git is needed: the bundled openroad-flow-scripts/ already contains the
# patched OpenROAD source and every vendored file, so we call `docker build`
# directly instead of ORFS's DockerHelper.sh (which relies on `git describe`).
#
# Usage:
#   ./build_image.sh                # -> orfs_ra:latest   (matches run.py default)
#   ./build_image.sh myimg mytag    # -> myimg:mytag
# ---------------------------------------------------------------------------
set -euo pipefail

IMG="${1:-orfs_ra}"
TAG="${2:-latest}"
THREADS="$(nproc)"
DEV_BASE="openroad/flow-ubuntu22.04-dev:latest"
OR_VERSION="26Q1-735-patched"

HERE="$(cd "$(dirname "$0")" && pwd)"
ORFS="$HERE/openroad-flow-scripts"

if [ ! -f "$ORFS/docker/Dockerfile.builder" ]; then
    echo "ERROR: $ORFS/docker/Dockerfile.builder not found." >&2
    echo "Run this script from inside the extracted ResizerAgent folder." >&2
    exit 1
fi

echo "[build_image] Building ${IMG}:${TAG} from local source (${THREADS} threads)"
echo "[build_image] Base image: ${DEV_BASE} (pulled if not present)"

cd "$ORFS"
DOCKER_BUILDKIT=1 docker build \
    --file docker/Dockerfile.builder \
    --build-arg "fromImage=${DEV_BASE}" \
    --build-arg "numThreads=${THREADS}" \
    --build-arg "openroadVersion=${OR_VERSION}" \
    --tag "${IMG}:${TAG}" \
    .

echo "[build_image] Done -> ${IMG}:${TAG}"
echo "[build_image] Run the flow with:  python3 run.py --design aes --agent claude --pdk asap7 --run-stage all"
[ "${IMG}:${TAG}" = "orfs_ra:latest" ] || \
    echo "[build_image] NOTE: pass --docker-image ${IMG}:${TAG} to run.py (non-default image name)."
