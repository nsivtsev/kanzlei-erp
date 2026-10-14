#!/usr/bin/env bash
set -euo pipefail
if [[ $# -ne 1 ]]; then
  echo "Usage: bash scripts/build-production-image.sh LOCAL_IMAGE_TAG" >&2
  exit 1
fi
revision=$(git rev-parse HEAD)
[[ $revision =~ ^[a-f0-9]{40}$ ]]
docker buildx build --load --platform "${PLATFORM:-linux/amd64}" \
  --build-arg "REVISION=$revision" --build-arg "SOURCE_DATE_EPOCH=0" \
  --tag "$1" --file deploy/Dockerfile .
