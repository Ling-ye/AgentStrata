#!/usr/bin/env bash
# deploy_all.sh - WSL-first full deployment entry for AgentStrata.
#
# This script is intended to run from the WSL source checkout. It prepares the
# source environment, reconciles shared Docker services, delegates each bundled
# bot's complete register/restart/enable lifecycle to update_instance.sh, and
# prints final status checks.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." >/dev/null 2>&1 && pwd)"

SKIP_DOCKER=0
SKIP_BOTS=0
DRY_RUN=0
FAILURES=0
DOCKER_START_TIMEOUT="${CHATCOPILOT_DOCKER_START_TIMEOUT:-180}"

BOTS=(
    lingye-copilot-qq
)

usage() {
    sed -n '2,12p' "$0"
    cat <<'EOF'

Usage:
  bash deploy/wsl/deploy_all.sh [--skip-docker] [--skip-bots] [--docker-timeout SECONDS] [--dry-run]

Options:
  --skip-docker   Do not reconcile shared Docker services.
  --skip-bots     Do not update bundled bot instances.
  --docker-timeout SECONDS
                  Limit shared Docker service startup; timeout is non-fatal (default: 180).
  --dry-run, -n   Print commands without changing the system.
  -h, --help      Show this help.
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --skip-docker) SKIP_DOCKER=1 ;;
        --skip-bots) SKIP_BOTS=1 ;;
        --docker-timeout)
            [ "$#" -ge 2 ] || { echo "[ERR] --docker-timeout needs a value" >&2; exit 2; }
            DOCKER_START_TIMEOUT="$2"
            shift ;;
        --dry-run|-n) DRY_RUN=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "[ERR] unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

info() { printf "\033[1;36m[*]\033[0m %s\n" "$*"; }
ok()   { printf "\033[1;32m[OK]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[WARN]\033[0m %s\n" "$*"; }
err()  { printf "\033[1;31m[ERR]\033[0m %s\n" "$*" >&2; }

run() {
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '[DRY-RUN]'
        printf ' %q' "$@"
        printf '\n'
        return 0
    fi
    "$@"
}

run_docker_start() {
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '[DRY-RUN] timeout %ss bash %q start\n' "$DOCKER_START_TIMEOUT" "$REPO_ROOT/deploy/docker/services.sh"
        return 0
    fi
    if ! [[ "$DOCKER_START_TIMEOUT" =~ ^[0-9]+$ ]] || [ "$DOCKER_START_TIMEOUT" -le 0 ]; then
        err "invalid Docker timeout: $DOCKER_START_TIMEOUT"
        return 1
    fi
    info "Docker shared-service startup timeout: ${DOCKER_START_TIMEOUT}s"
    timeout "${DOCKER_START_TIMEOUT}s" bash "$REPO_ROOT/deploy/docker/services.sh" start
    local rc=$?
    if [ "$rc" -eq 0 ]; then
        return 0
    fi
    if [ "$rc" -eq 124 ]; then
        warn "Docker shared-service startup timed out after ${DOCKER_START_TIMEOUT}s; continuing with bot deployment."
    else
        warn "Docker shared-service startup failed with exit code $rc; continuing with bot deployment."
    fi
    warn "Run 'bash deploy/docker/services.sh status' and 'bash deploy/docker/services.sh doctor all' after fixing Docker/network/login dependencies."
    FAILURES=1
    return 0
}

require_file() {
    local path="$1"
    local label="$2"
    if [ ! -f "$path" ]; then
        err "$label not found: $path"
        return 1
    fi
    if [ ! -s "$path" ]; then
        warn "$label exists but is empty: $path"
    else
        ok "$label present: $path"
    fi
}

ensure_repo() {
    if [ ! -d "$REPO_ROOT/src/chatcopilot" ] || [ ! -f "$REPO_ROOT/pyproject.toml" ]; then
        err "not an AgentStrata source repo: $REPO_ROOT"
        exit 1
    fi
    if [ "$REPO_ROOT" != "$HOME/ChatCopilot" ]; then
        warn "source repo is $REPO_ROOT; WSL-first docs assume $HOME/ChatCopilot"
    fi
    ok "source repo: $REPO_ROOT"
}

check_secret_files() {
    info "checking local env files without printing secrets"
    if [ "$SKIP_DOCKER" -eq 0 ]; then
        require_file "$REPO_ROOT/deploy/docker/.env" "Docker env"
    fi
    if [ "$SKIP_BOTS" -eq 0 ]; then
        local bot
        for bot in "${BOTS[@]}"; do
            require_file "$REPO_ROOT/bots/$bot/local.env" "$bot local env"
        done
    fi
}

status_checks() {
    info "final status checks"
    run bash "$REPO_ROOT/deploy/wsl/deploy_console.sh" --status
    if [ "$SKIP_DOCKER" -eq 0 ]; then
        run bash "$REPO_ROOT/deploy/docker/services.sh" status
    fi
    if [ "$SKIP_BOTS" -eq 0 ]; then
        local py="$REPO_ROOT/.venv/bin/python"
        local bot
        for bot in "${BOTS[@]}"; do
            if [ -x "$py" ]; then
                run "$py" -m console.control status --instance "$bot" --json
            else
                warn "source venv python not found, skipping console status for $bot: $py"
            fi
        done
    fi
}

cd "$REPO_ROOT"

echo
printf "\033[1m=== AgentStrata WSL full deploy ===\033[0m\n"
echo

ensure_repo
check_secret_files

info "step 1/3: prepare WSL source environment and Console"
run bash "$REPO_ROOT/deploy/wsl/install_wsl_env.sh" --with-console --skip-cc-connect

if [ "$SKIP_DOCKER" -eq 0 ]; then
    info "step 2/3: reconcile shared Docker services"
    run_docker_start
else
    warn "step 2/3 skipped: shared Docker services"
fi

if [ "$SKIP_BOTS" -eq 0 ]; then
    info "step 3/3: update and activate bundled bot instances"
    for bot in "${BOTS[@]}"; do
        if ! run bash "$REPO_ROOT/deploy/wsl/update_instance.sh" --instance "$bot" --enable; then
            warn "bot update/activation failed: $bot"
            FAILURES=1
        fi
    done
else
    warn "step 3/3 skipped: bot instances"
fi

status_checks

echo
if [ "$FAILURES" -eq 0 ]; then
    ok "WSL full deploy flow completed"
else
    warn "WSL full deploy flow completed with unresolved external/configuration issues"
    exit 1
fi
