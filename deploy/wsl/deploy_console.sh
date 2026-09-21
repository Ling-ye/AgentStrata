#!/usr/bin/env bash
# deploy_console.sh - WSL-side installer, repair, and update entry for Console UI.
#
# Run this from the Linux / WSL source repo. It is the canonical entry for the
# console service on http://localhost:8910.
#
# Usage:
#   bash deploy/wsl/deploy_console.sh                 # install/repair console + update every bot
#   bash deploy/wsl/deploy_console.sh --update-only   # sync dependencies + rebuild web + restart Evaluation / Console
#   bash deploy/wsl/deploy_console.sh --skip-web      # skip web build
#   bash deploy/wsl/deploy_console.sh --skip-bots     # install/repair console only
#   bash deploy/wsl/deploy_console.sh --restart-only  # only restart service
#   bash deploy/wsl/deploy_console.sh --status        # health check only
#   bash deploy/wsl/deploy_console.sh --dry-run
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." >/dev/null 2>&1 && pwd)"
UNIT_NAME="chatcopilot-console.service"
EVALUATION_UNIT_NAME="chatcopilot-evaluation.service"
CONSOLE_URL="http://127.0.0.1:8910/api/bots"
EVALUATION_BFF_URL="http://127.0.0.1:8910/api/evals/health"

SKIP_WEB=0
RESTART_ONLY=0
STATUS_ONLY=0
UPDATE_ONLY=0
DRY_RUN=0
SKIP_BOTS=0
MAINTENANCE_HELD=0
MAINTENANCE_LEASE_ID=""
BOT_UPDATE_COUNT=0
BOT_UPDATE_FAILURES=()

usage() {
    sed -n '2,18p' "$0"
}

for arg in "$@"; do
    case "$arg" in
        --skip-web) SKIP_WEB=1 ;;
        --restart-only) RESTART_ONLY=1 ;;
        --status) STATUS_ONLY=1 ;;
        --update-only) UPDATE_ONLY=1 ;;
        --skip-bots) SKIP_BOTS=1 ;;
        --dry-run|-n) DRY_RUN=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "[ERR] unknown argument: $arg" >&2; usage >&2; exit 2 ;;
    esac
done

if [ "$STATUS_ONLY" -eq 1 ] && { [ "$RESTART_ONLY" -eq 1 ] || [ "$UPDATE_ONLY" -eq 1 ] || [ "$DRY_RUN" -eq 1 ]; }; then
    echo "[ERR] --status cannot be combined with update/restart/dry-run modes" >&2
    exit 2
fi
if [ "$RESTART_ONLY" -eq 1 ] && [ "$UPDATE_ONLY" -eq 1 ]; then
    echo "[ERR] --restart-only and --update-only are mutually exclusive" >&2
    exit 2
fi
if [ "$SKIP_BOTS" -eq 1 ] && { [ "$STATUS_ONLY" -eq 1 ] || [ "$RESTART_ONLY" -eq 1 ] || [ "$UPDATE_ONLY" -eq 1 ]; }; then
    echo "[ERR] --skip-bots is only valid for the default full deploy mode" >&2
    exit 2
fi

if [ -t 1 ]; then
    C_INFO=$'\033[1;36m'; C_OK=$'\033[1;32m'; C_WARN=$'\033[1;33m'
    C_ERR=$'\033[1;31m'; C_BOLD=$'\033[1m'; C_END=$'\033[0m'
else
    C_INFO=""; C_OK=""; C_WARN=""; C_ERR=""; C_BOLD=""; C_END=""
fi

info() { printf "%s[*]%s %s\n" "$C_INFO" "$C_END" "$*"; }
ok()   { printf "%s[OK]%s %s\n" "$C_OK" "$C_END" "$*"; }
warn() { printf "%s[WARN]%s %s\n" "$C_WARN" "$C_END" "$*"; }
err()  { printf "%s[ERR]%s %s\n" "$C_ERR" "$C_END" "$*" >&2; }

uid="$(id -u)"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$uid}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=/run/user/$uid/bus}"
EVALUATION_SOCKET="$XDG_RUNTIME_DIR/agentstrata-evaluation/service.sock"

run_or_print() {
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '[DRY-RUN]'
        printf ' %q' "$@"
        printf '\n'
        return 0
    fi
    "$@"
}

for deploy_library in \
    "$SCRIPT_DIR/lib/console_deploy_systemd.sh" \
    "$SCRIPT_DIR/lib/console_deploy_services.sh" \
    "$SCRIPT_DIR/lib/console_deploy_bots.sh"; do
    if [ ! -r "$deploy_library" ]; then
        err "missing Console deploy library: $deploy_library"
        exit 1
    fi
    # shellcheck source=/dev/null
    source "$deploy_library" || {
        err "failed to load Console deploy library: $deploy_library"
        exit 1
    }
done
unset deploy_library

check_repo() {
    if [ ! -d "$REPO_ROOT/console" ] || [ ! -d "$REPO_ROOT/src/chatcopilot" ]; then
        err "not an AgentStrata control repo: $REPO_ROOT"
        err "Clone or checkout AgentStrata in WSL first, then run this script from that repo."
        exit 1
    fi
}

preflight_common() {
    check_repo
    info "control repo: $REPO_ROOT"

    if ! command -v python3 >/dev/null 2>&1; then
        err "python3 not found. Install: sudo apt install -y python3 python3-venv"
        exit 1
    fi
    ok "python3 is available"

    check_systemd_available || exit $?

    if ! command -v rsync >/dev/null 2>&1; then
        warn "rsync not found; instance sync needs it: sudo apt install -y rsync"
    fi
}

echo
printf "%s=== AgentStrata Console deploy ===%s\n" "$C_BOLD" "$C_END"
echo

if [ "$STATUS_ONLY" -eq 1 ]; then
    check_repo
    check_systemd_available readonly || exit $?
    check_status
    exit $?
fi

# The outer Harness guard holds creation/resume through the existing Evaluation lease.
# Read-only and Console-only restart operations do not need an update guard.
if [ "$DRY_RUN" -eq 0 ] && [ "$RESTART_ONLY" -eq 0 ] && \
   [ -x "$REPO_ROOT/.venv/bin/python" ] && \
   [ "${CHATCOPILOT_HARNESS_MAINTENANCE_HELD:-0}" != 1 ]; then
    exec env PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}" \
        "$REPO_ROOT/.venv/bin/python" -m chatcopilot.harness --repository-root "$REPO_ROOT" \
        maintenance -- bash "$0" "$@"
fi

preflight_common
preflight_web

if [ "$RESTART_ONLY" -eq 1 ]; then
    restart_console || exit $?
elif [ "$UPDATE_ONLY" -eq 1 ]; then
    info "updating Evaluation and Console services ..."
    trap release_maintenance_on_exit EXIT
    info "atomically entering Evaluation maintenance ..."
    if ! maintenance_enter; then
        err "Evaluation is active or idle cannot be proven; update refused"
        exit 1
    fi
    run_or_print bash "$REPO_ROOT/deploy/wsl/install_wsl_env.sh" \
        --no-system-packages --skip-cc-connect --with-console-deps \
        --venv "$REPO_ROOT/.venv" --no-verify || { err "dependency sync failed; update stopped"; exit 1; }
    build_web || { err "web build failed; services were not restarted"; exit 1; }
    restart_evaluation || exit $?
    restart_console || exit $?
    if ! maintenance_leave; then
        err "updated services are healthy, but the Evaluation maintenance lease remains active"
        exit 1
    fi
    trap - EXIT
else
    info "installing/repairing Console service ..."
    install_or_repair_console || exit $?
    if [ "$SKIP_BOTS" -eq 1 ]; then
        warn "--skip-bots set; bot runtimes will not be updated"
    else
        update_all_bots || BOT_UPDATE_RESULT=$?
    fi
fi

if [ "$DRY_RUN" -eq 1 ]; then
    echo
    if [ "${BOT_UPDATE_RESULT:-0}" -ne 0 ]; then
        err "dry-run found one or more invalid bot deployment entries"
        exit "$BOT_UPDATE_RESULT"
    fi
    ok "dry-run completed; no system changes were made"
    exit 0
fi

check_status || exit $?

if [ "${BOT_UPDATE_RESULT:-0}" -ne 0 ]; then
    err "Console is healthy, but one or more bot updates failed"
    exit "$BOT_UPDATE_RESULT"
fi

echo
ok "done. Open http://localhost:8910"
