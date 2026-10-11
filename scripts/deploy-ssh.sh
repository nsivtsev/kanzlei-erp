#!/usr/bin/env bash
set -euo pipefail
set +x
umask 077

for name in DEPLOY_HOST DEPLOY_SSH_PORT DEPLOY_SSH_USER DEPLOY_PATH DEPLOY_SSH_KEY DEPLOY_KNOWN_HOSTS RELEASE_IMAGE RELEASE_REVISION RELEASE_TAG; do
  if [[ -z ${!name:-} ]]; then
    echo "Missing $name" >&2
    exit 1
  fi
done
[[ $DEPLOY_HOST =~ ^[a-zA-Z0-9][a-zA-Z0-9.-]*$ ]]
[[ $DEPLOY_SSH_USER =~ ^[a-z_][a-z0-9_-]*$ ]]
[[ $DEPLOY_SSH_PORT =~ ^[0-9]+$ ]] && (( DEPLOY_SSH_PORT > 0 && DEPLOY_SSH_PORT < 65536 ))
[[ $DEPLOY_PATH =~ ^/[a-zA-Z0-9_./-]+$ && $DEPLOY_PATH != / && $DEPLOY_PATH != *..* ]]
[[ $RELEASE_IMAGE =~ ^ghcr\.io/nsivtsev/kanzlei-erp@sha256:[a-f0-9]{64}$ ]]
[[ $RELEASE_REVISION =~ ^[a-f0-9]{40}$ ]]
[[ $RELEASE_TAG =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]]
test -f release.tar.gz

ssh_dir=$(mktemp -d)
trap 'rm -rf "$ssh_dir"' EXIT
printf '%s\n' "$DEPLOY_SSH_KEY" > "$ssh_dir/key"
printf '%s\n' "$DEPLOY_KNOWN_HOSTS" > "$ssh_dir/known_hosts"
chmod 0600 "$ssh_dir/key" "$ssh_dir/known_hosts"
ssh-keygen -y -f "$ssh_dir/key" >/dev/null
ssh_options=(-i "$ssh_dir/key" -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$ssh_dir/known_hosts" -o ConnectTimeout=20 -o ServerAliveInterval=15 -o ServerAliveCountMax=4)
destination="$DEPLOY_SSH_USER@$DEPLOY_HOST"
run_id=${GITHUB_RUN_ID:-$(date +%s)}
attempt=${GITHUB_RUN_ATTEMPT:-1}
[[ $run_id =~ ^[0-9]+$ && $attempt =~ ^[0-9]+$ ]]
release_dir="$DEPLOY_PATH/releases/$RELEASE_TAG-$run_id-$attempt"
archive="$DEPLOY_PATH/release-$run_id-$attempt.tar.gz"
scp -P "$DEPLOY_SSH_PORT" "${ssh_options[@]}" release.tar.gz "$destination:$archive"
printf -v remote 'set -euo pipefail; umask 077; mkdir -p %q; tar -xzf %q -C %q; rm %q; exec python3 %q --root %q deploy --release %q --image %q --revision %q --tag %q' \
  "$release_dir" "$archive" "$release_dir" "$archive" "$release_dir/deploy/manage.py" "$DEPLOY_PATH" "$release_dir" "$RELEASE_IMAGE" "$RELEASE_REVISION" "$RELEASE_TAG"
ssh -p "$DEPLOY_SSH_PORT" "${ssh_options[@]}" "$destination" "bash -c $(printf '%q' "$remote")"
