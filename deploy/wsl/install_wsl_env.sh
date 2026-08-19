#!/usr/bin/env bash
# install_wsl_env.sh - Prepare a WSL source checkout for AgentStrata.
#
# This is the developer/operator bootstrap entry for running AgentStrata directly
# from the WSL source repo. Instance rollout remains in update_instance.sh.
#
# Usage:
#   bash deploy/wsl/install_wsl_env.sh
#   bash deploy/wsl/install_wsl_env.sh --with-console
#   bash deploy/wsl/install_wsl_env.sh --no-system-packages
#   bash deploy/wsl/install_wsl_env.sh --dry-run
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." >/dev/null 2>&1 && pwd)"

INSTALL_SYSTEM_PACKAGES=1
INSTALL_CC_CONNECT=1
INSTALL_LARK_CLI=1
INSTALL_CONSOLE=0
SKIP_WEB=0
INIT_ENV=0
VERIFY=1
DRY_RUN=0
CC_CONNECT_PKG="cc-connect@1.4.0-beta.3"
VENV_DIR="$REPO_ROOT/.venv"

usage() {
    sed -n '2,16p' "$0"
    cat <<'EOF'

Options:
  --with-console          Also install/repair the Console service.
  --skip-web              Pass --skip-web when --with-console is used.
  --no-system-packages    Do not install apt packages or Node.js.
  --skip-cc-connect       Do not install cc-connect.
  --skip-lark-cli         Do not install @larksuite/cli.
  --cc-connect-pkg PKG    npm package for cc-connect (default: cc-connect@1.4.0-beta.3).
  --venv DIR              Python virtualenv path (default: .venv).
  --init-env              Copy deploy/wsl/env.example to ~/.chatcopilot.env if absent.
  --no-verify             Skip import and BotSpec validation checks.
  --dry-run, -n           Print commands without changing the system.
  -h, --help              Show this help.
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --with-console) INSTALL_CONSOLE=1 ;;
        --skip-web) SKIP_WEB=1 ;;
        --no-system-packages) INSTALL_SYSTEM_PACKAGES=0 ;;
        --skip-cc-connect) INSTALL_CC_CONNECT=0 ;;
        --skip-lark-cli) INSTALL_LARK_CLI=0 ;;
        --cc-connect-pkg)
            [ "$#" -ge 2 ] || { echo "[ERR] --cc-connect-pkg needs a value" >&2; exit 2; }
            CC_CONNECT_PKG="$2"; shift ;;
        --venv)
            [ "$#" -ge 2 ] || { echo "[ERR] --venv needs a value" >&2; exit 2; }
            VENV_DIR="$2"; shift ;;
        --init-env) INIT_ENV=1 ;;
        --no-verify) VERIFY=0 ;;
        --dry-run|-n) DRY_RUN=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "[ERR] unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

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

run() {
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '[DRY-RUN]'
        printf ' %q' "$@"
        printf '\n'
        return 0
    fi
    "$@"
}

run_shell() {
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '[DRY-RUN] bash -lc %q\n' "$1"
        return 0
    fi
    bash -lc "$1"
}

need() { command -v "$1" >/dev/null 2>&1; }

ensure_repo() {
    if [ ! -d "$REPO_ROOT/src/chatcopilot" ] || [ ! -f "$REPO_ROOT/pyproject.toml" ]; then
        err "not an AgentStrata source repo: $REPO_ROOT"
        exit 1
    fi
    if grep -qi microsoft /proc/version 2>/dev/null; then
        ok "WSL detected"
    else
        warn "WSL was not detected from /proc/version; continuing because this is a Linux-compatible script."
    fi
    ok "source repo: $REPO_ROOT"
}

sudo_cmd() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
        return $?
    fi
    if ! need sudo; then
        err "sudo is required for system package installation. Re-run with --no-system-packages to skip it."
        exit 1
    fi
    sudo "$@"
}

run_sudo() {
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '[DRY-RUN]'
        if [ "$(id -u)" -ne 0 ]; then
            printf ' sudo'
        fi
        printf ' %q' "$@"
        printf '\n'
        return 0
    fi
    sudo_cmd "$@"
}

install_system_packages() {
    if [ "$INSTALL_SYSTEM_PACKAGES" -eq 0 ]; then
        warn "system package installation skipped"
        return 0
    fi

    local pkgs=(
        python3 python3-pip python3-venv python3-dev
        build-essential pkg-config curl ca-certificates
        git jq unzip rsync dbus-user-session
    )
    local missing=()
    local pkg
    for pkg in "${pkgs[@]}"; do
        if ! dpkg -s "$pkg" >/dev/null 2>&1; then
            missing+=("$pkg")
        fi
    done

    if [ "${#missing[@]}" -gt 0 ]; then
        info "installing apt packages: ${missing[*]}"
        run_sudo apt-get update
        run_sudo apt-get install -y "${missing[@]}"
    else
        ok "apt base packages already installed"
    fi

    local node_ok=0
    if need node; then
        local node_major
        node_major="$(node --version | sed 's/^v//; s/\..*//')"
        if [ "${node_major:-0}" -ge 18 ] 2>/dev/null; then
            node_ok=1
        fi
    fi
    if [ "$node_ok" -eq 1 ]; then
        ok "Node.js $(node --version) / npm $(npm --version)"
    else
        info "installing NodeSource Node.js 20.x"
        run_shell "curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -"
        run_sudo apt-get install -y nodejs
    fi
}

prepare_path() {
    mkdir -p "$HOME/.npm-global" "$HOME/.local/bin"
    export PATH="$HOME/.npm-global/bin:$HOME/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH"
    if [ ! -f "$HOME/.bashrc" ] || ! grep -qF "ChatCopilot: npm-global PATH" "$HOME/.bashrc"; then
        run_shell "printf '\\n# ChatCopilot: npm-global PATH\\nexport PATH=\"\$HOME/.npm-global/bin:\$PATH\"\\n' >> \"\$HOME/.bashrc\""
    fi
}

install_node_tools() {
    if ! need npm; then
        warn "npm not found; skipping npm global tools"
        return 0
    fi

    local prefix
    prefix="$(npm config get prefix 2>/dev/null || true)"
    if [ "$prefix" != "$HOME/.npm-global" ]; then
        run npm config set prefix "$HOME/.npm-global"
        ok "npm global prefix: $HOME/.npm-global"
    else
        ok "npm global prefix already set"
    fi

    if [ "$INSTALL_CC_CONNECT" -eq 1 ]; then
        local cc_connect_user_bin="$HOME/.npm-global/bin/cc-connect"
        if [ -x "$cc_connect_user_bin" ]; then
            ok "cc-connect already available: $("$cc_connect_user_bin" --version 2>&1 | head -n1 || true)"
        else
            info "installing $CC_CONNECT_PKG"
            run npm install -g "$CC_CONNECT_PKG"
        fi
    else
        warn "cc-connect installation skipped"
    fi

    if [ "$INSTALL_LARK_CLI" -eq 1 ]; then
        if need lark-cli; then
            ok "lark-cli already available"
        else
            info "installing @larksuite/cli"
            run npm install -g @larksuite/cli || warn "lark-cli installation failed; Feishu sheet download tools may be unavailable"
        fi
    else
        warn "lark-cli installation skipped"
    fi
}

install_python_env() {
    if ! need python3; then
        err "python3 not found. Install system packages first or re-run without --no-system-packages."
        exit 1
    fi
    if ! python3 -m venv --help >/dev/null 2>&1; then
        err "python3 venv is unavailable. Install python3-venv."
        exit 1
    fi

    info "preparing venv: $VENV_DIR"
    if [ ! -d "$VENV_DIR" ]; then
        run python3 -m venv "$VENV_DIR"
    fi

    local pip="$VENV_DIR/bin/python -m pip"
    run_shell "$pip install --upgrade pip"
    run_shell "$pip install -r '$REPO_ROOT/src/chatcopilot/agent/requirements.txt'"
    run_shell "$pip install -r '$REPO_ROOT/src/chatcopilot/middleware/acp/requirements.txt'"
    run_shell "$pip install -r '$REPO_ROOT/requirements.txt'"
    if [ -f "$REPO_ROOT/console/requirements.txt" ]; then
        run_shell "$pip install -r '$REPO_ROOT/console/requirements.txt'"
    fi
    run_shell "$pip install -e '$REPO_ROOT[test]'"
    ok "Python environment ready: $VENV_DIR"
}

prepare_runtime_dirs() {
    run mkdir -p \
        "$HOME/chatcopilot-workspaces/default/downloads" \
        "$HOME/chatcopilot-workspaces/default/results" \
        "$HOME/chatcopilot-workspaces/default/uploads" \
        "$HOME/chatcopilot-logs"
    ok "workspace/log directories ready"

    # Keep source checkout file modes stable; scripts are invoked via `bash ...`.
    ok "source scripts are invoked through bash; chmod normalization skipped"

    if [ "$INIT_ENV" -eq 1 ]; then
        local env_file="$HOME/.chatcopilot.env"
        if [ -f "$env_file" ]; then
            warn "$env_file already exists; leaving it unchanged"
        else
            run cp "$REPO_ROOT/deploy/wsl/env.example" "$env_file"
            run chmod 600 "$env_file"
            ok "created env template: $env_file"
        fi
    fi
}

verify_installation() {
    if [ "$VERIFY" -eq 0 ]; then
        warn "verification skipped"
        return 0
    fi
    if [ "$DRY_RUN" -eq 1 ]; then
        warn "verification skipped in dry-run mode"
        return 0
    fi

    local py="$VENV_DIR/bin/python"
    info "checking imports"
    "$py" -c "from chatcopilot.run import main; from chatcopilot.middleware.acp.server import main as acp_main; print('ok')" >/dev/null

    info "validating BotSpec files"
    "$py" -m chatcopilot botspec validate "$REPO_ROOT/bots/lingye-copilot-qq/bot.yaml"
    ok "verification passed"
}

install_console() {
    if [ "$INSTALL_CONSOLE" -eq 0 ]; then
        return 0
    fi
    local args=()
    [ "$SKIP_WEB" -eq 1 ] && args+=(--skip-web)
    # Environment installation owns only Console setup. Bot deployment is a
    # separate stage in deploy_all.sh; avoid running every instance twice.
    args+=(--skip-bots)
    info "installing/repairing Console"
    run bash "$REPO_ROOT/deploy/wsl/deploy_console.sh" "${args[@]}"
}

echo
printf "%s=== AgentStrata WSL environment install ===%s\n" "$C_BOLD" "$C_END"
echo

ensure_repo
prepare_path
install_system_packages
install_node_tools
install_python_env
prepare_runtime_dirs
verify_installation
install_console

echo
ok "WSL environment is ready"
echo
echo "Next useful commands:"
echo "  source $VENV_DIR/bin/activate"
echo "  python -m chatcopilot bot list"
echo "  python -m chatcopilot run --bot bots/lingye-copilot-qq/bot.yaml"
echo "  bash deploy/wsl/deploy_console.sh"
