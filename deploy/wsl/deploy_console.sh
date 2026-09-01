#!/usr/bin/env bash
# deploy_console.sh - WSL-side installer, repair, and update entry for Console UI.
#
# Run this from the Linux / WSL source repo. It is the canonical entry for the
# console service on http://localhost:8910.
#
# Usage:
#   bash deploy/wsl/deploy_console.sh                 # install/repair console + update every bot
#   bash deploy/wsl/deploy_console.sh --update-only   # rebuild web + restart Evaluation / Console
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
    ok "python3 is available"

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

read_deploy_value() {
    local bot="$1" name="$2"
    python3 - "$bot" "$name" <<'PY'
import sys
from pathlib import Path

bot = Path(sys.argv[1])
want = sys.argv[2]
section = ""
for raw in bot.read_text(encoding="utf-8", errors="replace").splitlines():
    line = raw.split("#", 1)[0].rstrip()
    if not line.strip():
        continue
    if not raw[:1].isspace() and ":" in line:
        section = line.split(":", 1)[0].strip()
        continue
    if section == "deploy" and raw[:1].isspace() and ":" in line:
        key, value = line.split(":", 1)
        if key.strip() == want:
            print(value.strip().strip('"').strip("'"))
            break
PY
}

update_all_bots() {
    local bot instance wsl_home rc index
    local -A seen_instances=()
    local -a bot_files=()
    local -a instances=()
    local -a destinations=()
    while IFS= read -r -d '' bot; do
        bot_files+=("$bot")
    done < <(
        find "$REPO_ROOT/bots" -mindepth 2 -maxdepth 2 -name bot.yaml \
            -type f -print0 2>/dev/null | sort -z
    )
    if [ "${#bot_files[@]}" -eq 0 ]; then
        err "no BotSpecs found under $REPO_ROOT/bots/*/bot.yaml"
        return 1
    fi

    BOT_UPDATE_COUNT="${#bot_files[@]}"
    info "validating all $BOT_UPDATE_COUNT discovered bot deployment(s) ..."
    for bot in "${bot_files[@]}"; do
        if ! instance="$(read_deploy_value "$bot" instance_id)"; then
            err "cannot read deploy.instance_id from $bot"
            BOT_UPDATE_FAILURES+=("${bot#"$REPO_ROOT"/}")
            continue
        fi
        [ -z "$instance" ] && instance="$(basename "$(dirname "$bot")")"
        if [[ ! "$instance" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$ ]]; then
            err "invalid deploy.instance_id in $bot"
            BOT_UPDATE_FAILURES+=("${bot#"$REPO_ROOT"/}")
            continue
        fi
        if [ -n "${seen_instances[$instance]:-}" ]; then
            err "duplicate deploy.instance_id '$instance': $bot"
            BOT_UPDATE_FAILURES+=("$instance")
            continue
        fi
        if ! wsl_home="$(read_deploy_value "$bot" wsl_home)"; then
            err "cannot read deploy.wsl_home from $bot"
            BOT_UPDATE_FAILURES+=("${bot#"$REPO_ROOT"/}")
            continue
        fi
        [ -z "$wsl_home" ] && wsl_home="~/ChatCopilot-$instance"
        seen_instances["$instance"]="$bot"
        instances+=("$instance")
        destinations+=("$wsl_home")
    done
    if [ "${#BOT_UPDATE_FAILURES[@]}" -gt 0 ]; then
        err "bot inventory validation failed: ${BOT_UPDATE_FAILURES[*]}"
        return 1
    fi

    info "updating all $BOT_UPDATE_COUNT validated bot instance(s) ..."
    for index in "${!bot_files[@]}"; do
        bot="${bot_files[$index]}"
        instance="${instances[$index]}"
        info "updating bot $instance from ${bot#"$REPO_ROOT"/} ..."
        if run_or_print bash "$REPO_ROOT/deploy/wsl/update_instance.sh" \
            --instance "$instance" --src "$REPO_ROOT" \
            --dst "${destinations[$index]}" --bot "$bot"; then
            ok "bot $instance updated and verified"
        else
            rc=$?
            err "bot $instance update failed (exit $rc); continuing with remaining bots"
            BOT_UPDATE_FAILURES+=("$instance")
        fi
    done

    if [ "${#BOT_UPDATE_FAILURES[@]}" -gt 0 ]; then
        err "${#BOT_UPDATE_FAILURES[@]} of $BOT_UPDATE_COUNT bot update(s) failed: ${BOT_UPDATE_FAILURES[*]}"
        return 1
    fi
    ok "all $BOT_UPDATE_COUNT bot instance(s) updated"
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
