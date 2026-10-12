#!/usr/bin/env bash
# Run inside a Bench directory. Shared by local setup and image builds.
set -euo pipefail
# shellcheck source=deploy/suite.env
source "$(dirname "$0")/../deploy/suite.env"
[[ $SUITE_COMMIT =~ ^[a-f0-9]{40}$ ]]
if [[ -d apps/suite ]]; then
  actual=$(git -C apps/suite rev-parse HEAD)
  if [[ $actual != "$SUITE_COMMIT" ]]; then
    echo "Suite checkout differs from the pinned commit; refusing to overwrite it." >&2
    exit 1
  fi
else
  git init apps/suite
  git -C apps/suite remote add origin "$SUITE_REPOSITORY"
  git -C apps/suite fetch --depth 1 origin "$SUITE_COMMIT"
  git -C apps/suite checkout --detach FETCH_HEAD
fi
patch_dir=$(realpath "$(dirname "$0")/../deploy")
for app in frappe suite; do
  patch_file="$patch_dir/$app-mandant-access.patch"
  if ! git -C "apps/$app" apply --reverse --check "$patch_file" 2>/dev/null; then
    git -C "apps/$app" apply --check "$patch_file"
    git -C "apps/$app" apply "$patch_file"
  fi
done
git -C apps/suite submodule update --init --recursive --depth 1
grep -qxF suite sites/apps.txt || printf '\nsuite\n' >> sites/apps.txt
bench pip install -e apps/suite
yarn --cwd apps/suite install --frozen-lockfile
bench build --app suite --production
