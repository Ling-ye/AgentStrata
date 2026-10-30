#!/usr/bin/env bash
# Public command entry; the shared implementation belongs to deploy.
set -uo pipefail
APPLICATION_SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." >/dev/null 2>&1 && pwd)"
exec bash "$APPLICATION_SOURCE_ROOT/deploy/wsl/instance_control.sh" "$@"
