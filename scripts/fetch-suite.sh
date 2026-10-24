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
applied=()
removed_previous=()
removed_patch_files=()
rollback_patches() {
  result=$?
  if (( result != 0 )); then
    # Frappe and Suite form one change: a conflict in either restores both.
    for (( i=${#applied[@]}-1; i>=0; i-- )); do
      app=${applied[$i]}
      git -C "apps/$app" apply --reverse "$patch_dir/$app-mandant-access.patch"
    done
    for (( i=${#removed_previous[@]}-1; i>=0; i-- )); do
      app=${removed_previous[$i]}
      git -C "apps/$app" apply "${removed_patch_files[$i]}"
    done
  fi
  exit "$result"
}
trap rollback_patches EXIT
apps=(frappe suite)
needs_apply=()
predecessors=()
# Identify both checkouts before removing any known patch. Unknown states must
# fail without touching either application or unrelated checkout changes.
for app in "${apps[@]}"; do
  patch_file="$patch_dir/$app-mandant-access.patch"
  previous=""
  if git -C "apps/$app" apply --reverse --check "$patch_file" 2>/dev/null; then
    needs_apply+=(0)
  else
    needs_apply+=(1)
    if ! git -C "apps/$app" apply --check "$patch_file" 2>/dev/null; then
      for version in v9 v8 v7 v6 v5 v4 v3 v2 v1; do
        candidate="$patch_dir/$app-mandant-access-$version.patch"
        if [[ -f $candidate ]] && git -C "apps/$app" apply --reverse --check "$candidate" 2>/dev/null; then
          previous=$candidate
          break
        fi
      done
      if [[ -z $previous ]]; then
        echo "Unknown $app patch state; refusing to overwrite checkout changes." >&2
        exit 1
      fi
    fi
  fi
  predecessors+=("$previous")
done
for (( i=0; i<${#apps[@]}; i++ )); do
  app=${apps[$i]}
  if (( needs_apply[i] == 0 )); then
    continue
  fi
  patch_file="$patch_dir/$app-mandant-access.patch"
  previous=${predecessors[$i]}
  if [[ -n $previous ]]; then
    # Remember the exact version removed, so rollback restores the exact v1–v5 predecessor.
    git -C "apps/$app" apply --reverse "$previous"
    removed_previous+=("$app")
    removed_patch_files+=("$previous")
  fi
  git -C "apps/$app" apply --check "$patch_file"
  git -C "apps/$app" apply "$patch_file"
  applied+=("$app")
done
trap - EXIT
git -C apps/suite submodule update --init --recursive --depth 1
grep -qxF suite sites/apps.txt || printf '\nsuite\n' >> sites/apps.txt
bench pip install -e apps/suite
yarn --cwd apps/suite install --frozen-lockfile
bench build --app suite --production
