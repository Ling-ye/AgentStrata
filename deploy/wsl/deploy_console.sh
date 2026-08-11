#!/usr/bin/env bash
# deploy_console.sh - WSL-side installer, repair, and update entry for Console UI.
#
# Run this from the Linux / WSL source repo. It is the canonical entry for the
# console service on http://localhost:8910.
#
# Usage:
#   bash deploy/wsl/deploy_console.sh                 # install/repair console
#   bash deploy/wsl/deploy_console.sh --update-only   # rebuild web + restart Evaluation / Console
#   bash deploy/wsl/deploy_console.sh --skip-web      # skip web build
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
MAINTENANCE_HELD=0
MAINTENANCE_LEASE_ID=""

usage() {
    sed -n '2,18p' "$0"
}

for arg in "$@"; do
    case "$arg" in
        --skip-web) SKIP_WEB=1 ;;
        --restart-only) RESTART_ONLY=1 ;;
        --status) STATUS_ONLY=1 ;;
        --update-only) UPDATE_ONLY=1 ;;
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

check_repo() {
    if [ ! -d "$REPO_ROOT/console" ] || [ ! -d "$REPO_ROOT/src/chatcopilot" ]; then
        err "not an AgentStrata control repo: $REPO_ROOT"
        err "Clone or checkout AgentStrata in WSL first, then run this script from that repo."
        exit 1
    fi
}

check_systemd_available() {
    if ! command -v systemctl >/dev/null 2>&1; then
        err "systemctl not found. Enable WSL systemd first."
        exit 1
    fi
    if ! systemctl --user show-environment >/dev/null 2>&1; then
        warn "systemctl --user is not available now; service operations may fail."
    else
        ok "systemd --user is available"
    fi
}

preflight_common() {
    check_repo
    info "control repo: $REPO_ROOT"

    if ! command -v python3 >/dev/null 2>&1; then
        err "python3 not found. Install: sudo apt install -y python3 python3-venv"
        exit 1
    fi
    if ! python3 -m venv --help >/dev/null 2>&1; then
        err "python3 venv is unavailable. Install: sudo apt install -y python3-venv"
        exit 1
    fi
    ok "python3 / venv is available"

    check_systemd_available

    if ! command -v rsync >/dev/null 2>&1; then
        warn "rsync not found; instance sync needs it: sudo apt install -y rsync"
    fi
}

preflight_web() {
    if [ "$SKIP_WEB" -eq 1 ]; then
        warn "--skip-web set; web build will be skipped"
        return 0
    fi
    if ! command -v npm >/dev/null 2>&1; then
        if [ "$UPDATE_ONLY" -eq 1 ]; then
            err "npm not found; cannot rebuild Console web. Install Node/npm or use --skip-web."
            exit 1
        fi
        warn "npm not found; install/repair will skip web build. Install Node/npm to rebuild UI assets."
        SKIP_WEB=1
        return 0
    fi
    ok "npm is available"
}

http_status() {
    local url="$1"
    if command -v curl >/dev/null 2>&1; then
        curl -fsS "$url" >/dev/null
        return $?
    fi
    python3 - "$url" <<'PY'
import sys
from urllib.request import urlopen

with urlopen(sys.argv[1], timeout=5) as resp:
    raise SystemExit(0 if 200 <= resp.status < 300 else 1)
PY
}

evaluation_health() {
    local python="$REPO_ROOT/.venv/bin/python"
    if [ "$DRY_RUN" -eq 1 ]; then
        run_or_print "$python" -m chatcopilot.evals.service health \
            --socket "$EVALUATION_SOCKET"
        return 0
    fi
    if [ ! -x "$python" ]; then
        return 1
    fi
    PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}" \
        "$python" -m chatcopilot.evals.service health \
        --socket "$EVALUATION_SOCKET" >/dev/null 2>&1
}

maintenance_enter() {
    local python="$REPO_ROOT/.venv/bin/python"
    if [ "$DRY_RUN" -eq 1 ]; then
        MAINTENANCE_LEASE_ID="00000000000000000000000000000000"
        run_or_print "$python" -m chatcopilot.evals.service health \
            --socket "$EVALUATION_SOCKET"
        run_or_print "$python" -m chatcopilot.evals.service maintenance enter \
            --socket "$EVALUATION_SOCKET" \
            --lease-id "$MAINTENANCE_LEASE_ID"
        MAINTENANCE_HELD=1
        return 0
    fi
    if [ ! -x "$python" ]; then
        return 1
    fi
    MAINTENANCE_LEASE_ID="$(
        PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}" \
            "$python" -m chatcopilot.evals.service maintenance enter \
            --socket "$EVALUATION_SOCKET"
    )" || return 1
    if [[ ! "$MAINTENANCE_LEASE_ID" =~ ^[0-9a-f]{32}$ ]]; then
        MAINTENANCE_LEASE_ID=""
        return 1
    fi
    MAINTENANCE_HELD=1
}

maintenance_leave() {
    local python="$REPO_ROOT/.venv/bin/python"
    if [ "$MAINTENANCE_HELD" -ne 1 ] || [ -z "$MAINTENANCE_LEASE_ID" ]; then
        return 0
    fi
    if [ "$DRY_RUN" -eq 1 ]; then
        run_or_print "$python" -m chatcopilot.evals.service maintenance leave \
            --socket "$EVALUATION_SOCKET" \
            --lease-id "$MAINTENANCE_LEASE_ID"
    elif [ ! -x "$python" ] || ! \
        PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}" \
            "$python" -m chatcopilot.evals.service maintenance leave \
            --socket "$EVALUATION_SOCKET" \
            --lease-id "$MAINTENANCE_LEASE_ID"; then
        return 1
    fi
    MAINTENANCE_HELD=0
    MAINTENANCE_LEASE_ID=""
}

release_maintenance_on_exit() {
    local status=$?
    local lease_id="$MAINTENANCE_LEASE_ID"
    trap - EXIT
    if [ "$MAINTENANCE_HELD" -eq 1 ] && ! maintenance_leave; then
        err "failed to release Evaluation maintenance lease: $lease_id"
        echo "  Recover the service, then run:" >&2
        echo "  $REPO_ROOT/.venv/bin/python -m chatcopilot.evals.service maintenance leave --socket $EVALUATION_SOCKET --lease-id $lease_id" >&2
        status=1
    fi
    exit "$status"
}

wait_for_unit() {
    local unit="$1" attempts="$2" attempt
    for attempt in $(seq 1 "$attempts"); do
        if systemctl --user is-active --quiet "$unit"; then
            return 0
        fi
        sleep 1
    done
    return 1
}

wait_for_evaluation() {
    local attempt
    if [ "$DRY_RUN" -eq 1 ]; then
        evaluation_health
        return $?
    fi
    for attempt in $(seq 1 20); do
        if evaluation_health; then
            return 0
        fi
        sleep 1
    done
    return 1
}

wait_for_http() {
    local url="$1" attempts="$2" attempt
    for attempt in $(seq 1 "$attempts"); do
        if http_status "$url" >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done
    return 1
}

print_diagnostics() {
    echo
    warn "diagnostic commands:"
    echo "  systemctl --user status $EVALUATION_UNIT_NAME --no-pager -l"
    echo "  journalctl --user -u $EVALUATION_UNIT_NAME --no-pager -n 120"
    echo "  systemctl --user status $UNIT_NAME --no-pager -l"
    echo "  journalctl --user -u $UNIT_NAME --no-pager -n 120"
    echo "  bash $REPO_ROOT/deploy/wsl/deploy_console.sh --status"
}

check_status() {
    info "checking $EVALUATION_UNIT_NAME ..."
    if ! wait_for_unit "$EVALUATION_UNIT_NAME" 5; then
        err "$EVALUATION_UNIT_NAME is not running"
        print_diagnostics
        return 1
    fi
    ok "$EVALUATION_UNIT_NAME is running"

    info "checking Evaluation Unix socket ..."
    if ! wait_for_evaluation; then
        err "Evaluation service is unavailable through $EVALUATION_SOCKET"
        print_diagnostics
        return 1
    fi
    ok "Evaluation Unix socket is healthy: $EVALUATION_SOCKET"

    info "checking $UNIT_NAME ..."
    if ! wait_for_unit "$UNIT_NAME" 5; then
        err "$UNIT_NAME is not running"
        print_diagnostics
        return 1
    fi
    ok "$UNIT_NAME is running"

    info "checking $CONSOLE_URL ..."
    if ! wait_for_http "$CONSOLE_URL" 15; then
        err "Console HTTP API is unavailable: $CONSOLE_URL"
        print_diagnostics
        return 1
    fi
    ok "Console HTTP API is available: http://localhost:8910"

    info "checking Console Evaluation BFF ..."
    if ! wait_for_http "$EVALUATION_BFF_URL" 5; then
        err "Console Evaluation BFF is unavailable: $EVALUATION_BFF_URL"
        print_diagnostics
        return 1
    fi
    ok "Console Evaluation BFF reaches the Unix socket service"
}

build_web() {
    if [ "$SKIP_WEB" -eq 1 ]; then
        warn "skip web build"
        return 0
    fi
    info "building Console web (npm ci + build) ..."
    run_or_print npm --prefix "$REPO_ROOT/console/web" ci || return $?
    run_or_print npm --prefix "$REPO_ROOT/console/web" run build || return $?
    ok "Console web built into console/web/dist"
}

install_or_repair_console() {
    local args=()
    if [ "$SKIP_WEB" -eq 1 ]; then
        args+=(--skip-web)
    fi
    run_or_print bash "$REPO_ROOT/console/setup_console.sh" "${args[@]}" || return $?
}

restart_console() {
    info "restarting $UNIT_NAME ..."
    run_or_print systemctl --user restart "$UNIT_NAME" || return $?
    ok "$UNIT_NAME restart requested"
}

restart_evaluation() {
    info "restarting $EVALUATION_UNIT_NAME ..."
    run_or_print systemctl --user restart "$EVALUATION_UNIT_NAME" || return $?
    ok "$EVALUATION_UNIT_NAME restart requested"
    info "waiting for Evaluation Unix socket health ..."
    if ! wait_for_evaluation; then
        err "Evaluation service did not become healthy: $EVALUATION_SOCKET"
        print_diagnostics
        return 1
    fi
    ok "Evaluation Unix socket is healthy"
}

echo
printf "%s=== AgentStrata Console deploy ===%s\n" "$C_BOLD" "$C_END"
echo

if [ "$STATUS_ONLY" -eq 1 ]; then
    check_repo
    check_status
    exit $?
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
fi

if [ "$DRY_RUN" -eq 1 ]; then
    echo
    ok "dry-run completed; no system changes were made"
    exit 0
fi

check_status || exit $?

echo
ok "done. Open http://localhost:8910"
