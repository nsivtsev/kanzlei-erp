#!/usr/bin/env bash
# Provision local S3 without enabling it or moving any existing application files.
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 scripts/minio_setup.py "$@"
