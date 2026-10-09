#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ "$1" == "nocompress" ]; then
    SKIP_COMPRESSION="true"
else
    SKIP_COMPRESSION="false"
fi

SROBO_NAME="PSC Robotics OS"
SROBO_VERSION="${PSC_IMAGE_VERSION:-$(git describe --tags --always --dirty 2>/dev/null || printf 'local')}"

docker run --rm --privileged \
    -v /dev:/dev \
    -v "${SCRIPT_DIR}:/build" \
    -v "${SCRIPT_DIR}/../psc-sr-robot3:/psc-sr-robot3:ro" \
    -w /build \
    mkaczanowski/packer-builder-arm:latest \
    build \
    -var "SROBO_NAME=${SROBO_NAME}" \
    -var "SROBO_VERSION=${SROBO_VERSION}" \
    -var "SKIP_COMPRESSION=${SKIP_COMPRESSION}" \
    pi.json
