#!/usr/bin/env bash
set -euo pipefail
if [[ $# -ne 1 ]]; then
  echo "Usage: bash scripts/build-production-image.sh LOCAL_IMAGE_TAG" >&2
  exit 1
fi
revision=$(git rev-parse HEAD)
source_date_epoch=$(git show -s --format=%ct HEAD)
docker buildx build --load --platform "${PLATFORM:-linux/amd64}" \
  --build-arg "REVISION=$revision" --build-arg "SOURCE_DATE_EPOCH=$source_date_epoch" \
  --tag "$1" --file deploy/Dockerfile .
