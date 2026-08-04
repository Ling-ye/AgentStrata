#!/usr/bin/env bash
# Retired: importing desktop credentials creates a shared refresh-token lineage.
set -euo pipefail

usage() {
    cat <<'EOF'
This command is retired and never reads or copies desktop Codex credentials.

Use two independent device authorizations instead:
  python -m chatcopilot bot codex-auth login \
    --bot bots/<bot-id>/bot.yaml --lane all
EOF
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
    usage
    exit 0
fi

echo "[ERR] code=desktop_auth_import_retired" >&2
usage >&2
exit 1
