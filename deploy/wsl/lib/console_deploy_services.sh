#!/usr/bin/env bash
# Internal Evaluation/Console lifecycle helpers for deploy_console.sh.
# Required host symbols: REPO_ROOT, UNIT_NAME, EVALUATION_UNIT_NAME,
# CONSOLE_URL, EVALUATION_BFF_URL, EVALUATION_SOCKET, DRY_RUN, SKIP_WEB,
# MAINTENANCE_HELD, MAINTENANCE_LEASE_ID, logging helpers, and run_or_print.

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    echo "[ERR] console_deploy_services.sh is an internal library" >&2
    exit 2
fi

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
