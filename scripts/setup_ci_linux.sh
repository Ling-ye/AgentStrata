#!/usr/bin/env bash
# Prepare the disposable Ubuntu 24.04 GitHub runner, not a deployment host.
set -euo pipefail

if [[ "${GITHUB_ACTIONS:-}" != true || "${RUNNER_ENVIRONMENT:-}" != github-hosted ]]; then
  printf 'ERROR: this setup is only for disposable GitHub-hosted runners\n' >&2
  exit 2
fi

sudo apt-get update
sudo apt-get install --yes --no-install-recommends bubblewrap ripgrep apparmor

# Noble restricts user namespaces for unconfined programs. Grant only bwrap the
# ability to construct its sandbox; leave the system-wide restriction enabled.
sudo tee /etc/apparmor.d/bwrap > /dev/null <<'PROFILE'
abi <abi/4.0>,
include <tunables/global>
profile bwrap /usr/bin/bwrap flags=(unconfined) {
  userns,
}
PROFILE
sudo apparmor_parser --replace /etc/apparmor.d/bwrap
bwrap --ro-bind / / --unshare-pid -- /bin/true
rg --version
