#!/usr/bin/env bash
# install_wsl_env.sh - Install the isolated AgentStrata runtime on WSL/Linux.
#
# Usage:
#   bash deploy/wsl/install_wsl_env.sh
#   bash deploy/wsl/install_wsl_env.sh --with-console
#   bash deploy/wsl/install_wsl_env.sh --no-system-packages
#   bash deploy/wsl/install_wsl_env.sh --dry-run
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." >/dev/null 2>&1 && pwd)"

UV_VERSION="0.12.5"
PYTHON_VERSION="3.13.15"
NODE_VERSION="24.20.0"
CC_CONNECT_VERSION="1.4.0-beta.3"
CC_CONNECT_PKG="cc-connect@$CC_CONNECT_VERSION"
RUNTIME_ROOT="${AGENTSTRATA_RUNTIME_ROOT:-$HOME/.local/share/agentstrata}"
UV_INSTALL_DIR="$RUNTIME_ROOT/uv/$UV_VERSION"
PYTHON_INSTALL_DIR="$RUNTIME_ROOT/python"
NODE_TOOLS_ROOT="$RUNTIME_ROOT/node-tools"
CC_CONNECT_DIR="$NODE_TOOLS_ROOT/cc-connect-$CC_CONNECT_VERSION"
CC_CONNECT_BIN="$CC_CONNECT_DIR/node_modules/.bin/cc-connect"
CC_CONNECT_NATIVE_BIN="$CC_CONNECT_DIR/node_modules/cc-connect/bin/cc-connect"
NODE_TOOLS_SOURCE="$REPO_ROOT/deploy/wsl/node-tools"
ARTIFACT_CACHE="$RUNTIME_ROOT/cache/artifacts"

INSTALL_SYSTEM_PACKAGES=1
INSTALL_CC_CONNECT=1
INSTALL_CONSOLE=0
SKIP_WEB=0
INIT_ENV=0
VERIFY=1
DRY_RUN=0
VENV_DIR="$REPO_ROOT/.venv"

usage() {
    sed -n '2,11p' "$0"
    cat <<'EOF'

Options:
  --with-console          Also install/repair the optional Console service.
  --skip-web              Pass --skip-web when --with-console is used.
  --no-system-packages    Do not install apt packages; isolated runtimes are still installed.
  --skip-cc-connect       Do not install the private Node.js/cc-connect toolchain.
  --skip-lark-cli         Deprecated no-op; Lark CLI is never installed here.
  --cc-connect-pkg PKG    Compatibility flag; only cc-connect@1.4.0-beta.3 is accepted.
  --venv DIR              Python environment path (default: .venv).
  --init-env              Copy deploy/wsl/env.example to ~/.chatcopilot.env if absent.
  --no-verify             Skip import and BotSpec validation checks.
  --dry-run, -n           Print downloads, checksums, and targets without writing.
  -h, --help              Show this help.
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --with-console) INSTALL_CONSOLE=1 ;;
        --skip-web) SKIP_WEB=1 ;;
        --no-system-packages) INSTALL_SYSTEM_PACKAGES=0 ;;
        --skip-cc-connect) INSTALL_CC_CONNECT=0 ;;
        --skip-lark-cli) ;;
        --cc-connect-pkg)
            [ "$#" -ge 2 ] || { echo "[ERR] --cc-connect-pkg needs a value" >&2; exit 2; }
            if [ "$2" != "$CC_CONNECT_PKG" ]; then
                echo "[ERR] only the locked package $CC_CONNECT_PKG is supported" >&2
                exit 2
            fi
            shift ;;
        --venv)
            [ "$#" -ge 2 ] || { echo "[ERR] --venv needs a value" >&2; exit 2; }
            VENV_DIR="$2"
            shift ;;
        --init-env) INIT_ENV=1 ;;
        --no-verify) VERIFY=0 ;;
        --dry-run|-n) DRY_RUN=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "[ERR] unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

if [ "$(id -u)" -eq 0 ]; then
    echo "[ERR] do not run the user runtime installer as root; invoke it as the deployment user" >&2
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

# Dependency installers and package lifecycle scripts receive a small allowlist
# instead of the caller's environment. Proxy variables remain available only as
# explicit network configuration and may not contain URL userinfo.
sanitize_dependency_environment() {
    local name value
    local -a preserved=(
        "HOME=$HOME"
        "PATH=$PATH"
        "USER=${USER:-$(id -un)}"
        "LOGNAME=${LOGNAME:-$(id -un)}"
        "LANG=${LANG:-C.UTF-8}"
        "TERM=${TERM:-dumb}"
        "AGENTSTRATA_RUNTIME_ROOT=$RUNTIME_ROOT"
    )
    for name in TMPDIR SSL_CERT_FILE SSL_CERT_DIR CURL_CA_BUNDLE \
        HTTP_PROXY HTTPS_PROXY NO_PROXY ALL_PROXY \
        http_proxy https_proxy no_proxy all_proxy; do
        value="${!name-}"
        [ -n "$value" ] || continue
        case "$name" in
            *PROXY|*proxy)
                case "$value" in
                    *://*@*)
                        err "authenticated proxy URL is not accepted in the dependency environment; configure a credential-free local proxy"
                        return 1
                        ;;
                esac
                ;;
        esac
        preserved+=("$name=$value")
    done
    while IFS= read -r name; do
        unset "$name" 2>/dev/null || true
    done < <(compgen -e)
    for value in "${preserved[@]}"; do
        export "$value"
    done
}

sanitize_dependency_environment

run() {
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '[DRY-RUN]'
        printf ' %q' "$@"
        printf '\n'
        return 0
    fi
    "$@"
}

need() { command -v "$1" >/dev/null 2>&1; }

sudo_cmd() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
        return $?
    fi
    if ! need sudo; then
        err "sudo is required for apt installation. Re-run with --no-system-packages to skip it."
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

ensure_repo() {
    if [ ! -d "$REPO_ROOT/src/chatcopilot" ] || [ ! -f "$REPO_ROOT/pyproject.toml" ]; then
        err "not an AgentStrata source repo: $REPO_ROOT"
        exit 1
    fi
    if [ ! -f "$REPO_ROOT/uv.lock" ]; then
        err "missing locked Python dependency graph: $REPO_ROOT/uv.lock"
        exit 1
    fi
    if [ ! -f "$NODE_TOOLS_SOURCE/package.json" ] || [ ! -f "$NODE_TOOLS_SOURCE/package-lock.json" ]; then
        err "missing locked Node tool manifests: $NODE_TOOLS_SOURCE"
        exit 1
    fi
    if grep -qi microsoft /proc/version 2>/dev/null; then
        ok "WSL detected"
    else
        warn "WSL was not detected; continuing on the supported Linux-compatible path."
    fi
    ok "source repo: $REPO_ROOT"
}

select_architecture() {
    case "$(uname -m)" in
        x86_64|amd64)
            UV_TARGET="x86_64-unknown-linux-gnu"
            UV_SHA256="68a509da24b06b4223a1c0175fb5eb5bc79342b76cbeff0cfe51ac3f5b17b6b2"
            UV_BINARY_SHA256="b65f23a420c4acc96427efb30e5ed9bc0f7e25d2d712000f6ede77c1a0de5f46"
            NODE_ARCH="x64"
            NODE_SHA256="2f2c0da162318f0de47665410c7c8c2ed3d36c8f3105de4bbc61176c70a7cbf2"
            NODE_BINARY_SHA256="89af8424dd53e560b1933f87ba650d8bf57c83ca5a04600eefb31f416aabbae7"
            ;;
        aarch64|arm64)
            UV_TARGET="aarch64-unknown-linux-gnu"
            UV_SHA256="9bf43b4d1a07665bf64d4c4e710930b382321a785e0eb10aac07f46471f86a31"
            UV_BINARY_SHA256="92804e2f635c1791bb497437d94b15970f0d6d74811979315624cbb0f45b778d"
            NODE_ARCH="arm64"
            NODE_SHA256="5f4ddab610c1ab2016b3c227cebdbf6d9495161487e4739c7b90090595f465f7"
            NODE_BINARY_SHA256="23a5637c2470fde09fcc1acc77c1b92e04e3d7e3e6e80ff7df6f5831958d1477"
            ;;
        *)
            err "unsupported architecture: $(uname -m); supported: x86_64/amd64, aarch64/arm64"
            exit 1
            ;;
    esac
    UV_URL="https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-$UV_TARGET.tar.gz"
    NODE_ARCHIVE="node-v$NODE_VERSION-linux-$NODE_ARCH"
    NODE_URL="https://nodejs.org/dist/v$NODE_VERSION/$NODE_ARCHIVE.tar.xz"
    NODE_INSTALL_DIR="$RUNTIME_ROOT/node/$NODE_ARCHIVE"
    UV_BIN="$UV_INSTALL_DIR/bin/uv"
    NODE_BIN="$NODE_INSTALL_DIR/bin/node"
    NPM_BIN="$NODE_INSTALL_DIR/bin/npm"
    NODE_ARCHIVE_CACHE="$ARTIFACT_CACHE/$NODE_ARCHIVE.tar.xz"
    NODE_COMMAND_PATH="$NODE_INSTALL_DIR/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
}

install_system_packages() {
    if [ "$INSTALL_SYSTEM_PACKAGES" -eq 0 ]; then
        warn "apt package installation skipped"
        return 0
    fi
    if ! need dpkg || ! need apt-get; then
        err "automatic base-package installation requires Ubuntu/Debian apt"
        exit 1
    fi

    local pkgs=(curl ca-certificates git jq xz-utils rsync dbus-user-session)
    local missing=()
    local pkg
    for pkg in "${pkgs[@]}"; do
        if ! dpkg -s "$pkg" >/dev/null 2>&1; then
            missing+=("$pkg")
        fi
    done
    if [ "${#missing[@]}" -eq 0 ]; then
        ok "minimal apt packages already installed"
        return 0
    fi

    info "installing minimal apt packages: ${missing[*]}"
    run_sudo apt-get update
    run_sudo apt-get install -y "${missing[@]}"
}

require_download_tools() {
    if [ "$DRY_RUN" -eq 1 ]; then
        return 0
    fi
    local missing=()
    local tool
    for tool in curl tar sha256sum xz; do
        need "$tool" || missing+=("$tool")
    done
    if [ "${#missing[@]}" -gt 0 ]; then
        err "required download tools are missing: ${missing[*]}"
        exit 1
    fi
}

download_verified() {
    local url="$1"
    local expected="$2"
    local destination="$3"
    # Runtime artifacts are immutable GETs verified below, so retrying TLS EOF
    # and other transfer failures is safe and keeps transient network errors recoverable.
    curl --disable --fail --location --retry 5 --retry-all-errors \
        --retry-delay 2 --retry-max-time 180 --connect-timeout 20 \
        --proto '=https' --tlsv1.2 --output "$destination" "$url"
    local actual
    actual="$(sha256sum "$destination" | awk '{print $1}')"
    if [ "$actual" != "$expected" ]; then
        err "checksum mismatch for downloaded runtime (expected $expected, got $actual)"
        return 1
    fi
}

private_executable_matches() {
    local path="$1"
    local expected="$2"
    [ -f "$path" ] && [ ! -L "$path" ] && [ -x "$path" ] || return 1
    [ "$(stat -c '%u' "$path" 2>/dev/null || true)" = "$(id -u)" ] || return 1
    [ "$(stat -c '%h' "$path" 2>/dev/null || true)" = "1" ] || return 1
    [ "$(sha256sum "$path" | awk '{print $1}')" = "$expected" ]
}

ensure_cached_archive() {
    local url="$1"
    local expected="$2"
    local target="$3"
    local parent
    parent="$(dirname "$target")"
    if [ -e "$target" ] || [ -L "$target" ]; then
        if [ ! -f "$target" ] || [ -L "$target" ] \
            || [ "$(stat -c '%u' "$target" 2>/dev/null || true)" != "$(id -u)" ] \
            || [ "$(stat -c '%h' "$target" 2>/dev/null || true)" != "1" ] \
            || [ "$(stat -c '%a' "$target" 2>/dev/null || true)" != "600" ]; then
            err "cached runtime archive has unsafe metadata: $target"
            return 1
        fi
        if [ "$(sha256sum "$target" | awk '{print $1}')" != "$expected" ]; then
            err "cached runtime archive checksum mismatch: $target"
            return 1
        fi
        return 0
    fi

    mkdir -p "$parent"
    if [ ! -d "$parent" ] || [ -L "$parent" ] \
        || [ "$(stat -c '%u' "$parent" 2>/dev/null || true)" != "$(id -u)" ]; then
        err "runtime artifact cache directory is unsafe: $parent"
        return 1
    fi
    local temporary
    temporary="$(mktemp "$parent/.archive.XXXXXX")"
    chmod 600 "$temporary"
    if ! download_verified "$url" "$expected" "$temporary"; then
        rm -f -- "$temporary"
        return 1
    fi
    if ! mv -T "$temporary" "$target"; then
        rm -f -- "$temporary"
        return 1
    fi
}

node_tree_matches_archive() {
    local archive="$1"
    local temp_dir
    temp_dir="$(mktemp -d)"
    trap 'rm -rf -- "$temp_dir"' RETURN
    tar -xJf "$archive" -C "$temp_dir"
    if ! diff -qr --no-dereference "$temp_dir/$NODE_ARCHIVE" "$NODE_INSTALL_DIR" >/dev/null 2>&1; then
        trap - RETURN
        rm -rf -- "$temp_dir"
        return 1
    fi
    trap - RETURN
    rm -rf -- "$temp_dir"
}

print_runtime_plan() {
    info "uv $UV_VERSION: $UV_URL"
    info "uv sha256: $UV_SHA256"
    info "uv target: $UV_INSTALL_DIR"
    info "managed Python: $PYTHON_VERSION -> $PYTHON_INSTALL_DIR"
    if [ "$INSTALL_CC_CONNECT" -eq 1 ] || [ "$INSTALL_CONSOLE" -eq 1 ]; then
        info "Node.js $NODE_VERSION: $NODE_URL"
        info "Node.js sha256: $NODE_SHA256"
        info "Node.js target: $NODE_INSTALL_DIR"
    else
        info "Node.js/cc-connect: not required by the Gateway QQ runtime"
    fi
    if [ "$INSTALL_CC_CONNECT" -eq 1 ]; then
        info "cc-connect $CC_CONNECT_VERSION target: $CC_CONNECT_DIR"
    fi
    info "Python environment target: $VENV_DIR"
}

install_uv() {
    if [ -e "$UV_BIN" ] || [ -L "$UV_BIN" ]; then
        if ! private_executable_matches "$UV_BIN" "$UV_BINARY_SHA256"; then
            err "existing uv failed the locked binary integrity check: $UV_BIN"
            return 1
        fi
        local installed_uv_version
        installed_uv_version="$(
            env -u PYTHONHOME -u PYTHONPATH -u PYTHONSTARTUP -u PYTHONINSPECT \
                "$UV_BIN" --version 2>/dev/null || true
        )"
        case "$installed_uv_version" in
            "uv $UV_VERSION"|"uv $UV_VERSION "*) ;;
            *)
                err "existing uv failed the locked version check: $UV_BIN"
                return 1
                ;;
        esac
        ok "uv $UV_VERSION integrity verified: $UV_BIN"
        return 0
    fi
    if [ "$DRY_RUN" -eq 1 ]; then
        run curl --fail --location --output /tmp/agentstrata-uv.tar.gz "$UV_URL"
        printf '[DRY-RUN] verify sha256 %s\n' "$UV_SHA256"
        run install -m 0755 "uv-$UV_TARGET/uv" "$UV_BIN"
        return 0
    fi

    local temp_dir archive extracted
    temp_dir="$(mktemp -d)"
    archive="$temp_dir/uv.tar.gz"
    extracted="$temp_dir/uv-$UV_TARGET/uv"
    trap 'rm -rf -- "$temp_dir"' RETURN
    download_verified "$UV_URL" "$UV_SHA256" "$archive"
    tar -xzf "$archive" -C "$temp_dir"
    [ -x "$extracted" ] || { err "uv archive did not contain the expected binary"; return 1; }
    if [ "$(sha256sum "$extracted" | awk '{print $1}')" != "$UV_BINARY_SHA256" ]; then
        err "uv extracted binary checksum mismatch"
        return 1
    fi
    mkdir -p "$UV_INSTALL_DIR/bin"
    install -m 0755 "$extracted" "$UV_BIN"
    private_executable_matches "$UV_BIN" "$UV_BINARY_SHA256" || {
        err "installed uv failed the locked binary integrity check"
        return 1
    }
    trap - RETURN
    rm -rf -- "$temp_dir"
    ok "uv $UV_VERSION installed: $UV_BIN"
}

install_node() {
    if [ -e "$NODE_INSTALL_DIR" ]; then
        if [ -L "$NODE_INSTALL_DIR" ] \
            || [ "$(stat -c '%u' "$NODE_INSTALL_DIR" 2>/dev/null || true)" != "$(id -u)" ] \
            || ! private_executable_matches "$NODE_BIN" "$NODE_BINARY_SHA256" \
            || [ "$($NODE_BIN --version 2>/dev/null || true)" != "v$NODE_VERSION" ]; then
            err "existing private Node failed the locked integrity check: $NODE_INSTALL_DIR"
            return 1
        fi
        if [ "$DRY_RUN" -eq 1 ]; then
            ok "Node.js $NODE_VERSION binary integrity verified: $NODE_BIN"
            info "a real run will compare the complete Node tree with the locked archive"
            return 0
        fi
        ensure_cached_archive "$NODE_URL" "$NODE_SHA256" "$NODE_ARCHIVE_CACHE"
        if ! node_tree_matches_archive "$NODE_ARCHIVE_CACHE"; then
            err "existing private Node tree differs from the locked archive: $NODE_INSTALL_DIR"
            return 1
        fi
        ok "Node.js $NODE_VERSION integrity verified: $NODE_BIN"
        return 0
    fi
    if [ "$DRY_RUN" -eq 1 ]; then
        run curl --fail --location --output /tmp/agentstrata-node.tar.xz "$NODE_URL"
        printf '[DRY-RUN] verify sha256 %s\n' "$NODE_SHA256"
        run tar -xJf /tmp/agentstrata-node.tar.xz -C "$RUNTIME_ROOT/node"
        return 0
    fi

    local temp_dir extracted
    temp_dir="$(mktemp -d)"
    extracted="$temp_dir/$NODE_ARCHIVE"
    trap 'rm -rf -- "$temp_dir"' RETURN
    ensure_cached_archive "$NODE_URL" "$NODE_SHA256" "$NODE_ARCHIVE_CACHE"
    tar -xJf "$NODE_ARCHIVE_CACHE" -C "$temp_dir"
    [ -x "$extracted/bin/node" ] || { err "Node.js archive did not contain the expected binary"; return 1; }
    if [ "$(sha256sum "$extracted/bin/node" | awk '{print $1}')" != "$NODE_BINARY_SHA256" ]; then
        err "Node.js extracted binary checksum mismatch"
        return 1
    fi
    mkdir -p "$RUNTIME_ROOT/node"
    mv "$extracted" "$NODE_INSTALL_DIR"
    if ! private_executable_matches "$NODE_BIN" "$NODE_BINARY_SHA256" \
        || ! node_tree_matches_archive "$NODE_ARCHIVE_CACHE"; then
        err "installed private Node failed the locked integrity check"
        return 1
    fi
    trap - RETURN
    rm -rf -- "$temp_dir"
    ok "Node.js $NODE_VERSION installed: $NODE_BIN"
}

install_node_tools() {
    if [ "$INSTALL_CC_CONNECT" -eq 0 ] && [ "$INSTALL_CONSOLE" -eq 0 ]; then
        warn "private Node.js/cc-connect installation skipped"
        return 0
    fi
    install_node
    if [ "$INSTALL_CC_CONNECT" -eq 0 ]; then
        warn "cc-connect installation skipped; private Node.js remains available for Console"
        return 0
    fi

    if [ -e "$CC_CONNECT_DIR" ] || [ -L "$CC_CONNECT_DIR" ]; then
        if [ ! -d "$CC_CONNECT_DIR" ] || [ -L "$CC_CONNECT_DIR" ] \
            || [ "$(stat -c '%u' "$CC_CONNECT_DIR" 2>/dev/null || true)" != "$(id -u)" ]; then
            err "existing cc-connect directory has unsafe metadata: $CC_CONNECT_DIR"
            return 1
        fi
    fi

    run mkdir -p "$CC_CONNECT_DIR" "$RUNTIME_ROOT/cache/npm" \
        "$RUNTIME_ROOT/cache/npm-home" "$RUNTIME_ROOT/cache/npm-config"
    run cp "$NODE_TOOLS_SOURCE/package.json" "$CC_CONNECT_DIR/package.json"
    run cp "$NODE_TOOLS_SOURCE/package-lock.json" "$CC_CONNECT_DIR/package-lock.json"
    run env HOME="$RUNTIME_ROOT/cache/npm-home" \
        XDG_CONFIG_HOME="$RUNTIME_ROOT/cache/npm-config" \
        PATH="$NODE_COMMAND_PATH" NODE_OPTIONS= NODE_PATH= \
        NPM_CONFIG_USERCONFIG=/dev/null NPM_CONFIG_GLOBALCONFIG=/dev/null \
        "$NPM_BIN" ci --prefix "$CC_CONNECT_DIR" --omit=dev --no-audit --no-fund \
        --cache "$RUNTIME_ROOT/cache/npm"
    if [ "$DRY_RUN" -eq 0 ]; then
        if [ ! -x "$CC_CONNECT_BIN" ] || [ ! -x "$CC_CONNECT_NATIVE_BIN" ]; then
            err "cc-connect binary is missing after npm ci: $CC_CONNECT_NATIVE_BIN"
            return 1
        fi
        if [ "$("$NODE_BIN" -p "require('$CC_CONNECT_DIR/node_modules/cc-connect/package.json').version" 2>/dev/null || true)" != "$CC_CONNECT_VERSION" ]; then
            err "cc-connect version differs from the locked package after npm ci"
            return 1
        fi
        case "$("$CC_CONNECT_NATIVE_BIN" --version 2>/dev/null | head -n 1 || true)" in
            "cc-connect v$CC_CONNECT_VERSION") ;;
            *)
                err "cc-connect native binary version differs from the locked package"
                return 1
                ;;
        esac
    fi
    [ "$DRY_RUN" -eq 1 ] || ok "cc-connect $CC_CONNECT_VERSION installed: $CC_CONNECT_BIN"
}

install_python_env() {
    install_uv
    run mkdir -p "$RUNTIME_ROOT/cache/uv-home" "$RUNTIME_ROOT/cache/uv-config"
    local uv_env=(
        env
        "HOME=$RUNTIME_ROOT/cache/uv-home"
        "XDG_CONFIG_HOME=$RUNTIME_ROOT/cache/uv-config"
        "UV_PYTHON_INSTALL_DIR=$PYTHON_INSTALL_DIR"
        "UV_PYTHON_PREFERENCE=only-managed"
        "UV_PYTHON_DOWNLOADS=automatic"
        "UV_PROJECT_ENVIRONMENT=$VENV_DIR"
    )
    run "${uv_env[@]}" "$UV_BIN" python install --no-bin "$PYTHON_VERSION" --no-config
    run "${uv_env[@]}" "$UV_BIN" sync --frozen --python "$PYTHON_VERSION" --extra agent --extra acp --no-config
    [ "$DRY_RUN" -eq 1 ] || ok "isolated Python $PYTHON_VERSION environment ready: $VENV_DIR"
}

prepare_runtime_dirs() {
    run mkdir -p \
        "$HOME/chatcopilot-workspaces/default/downloads" \
        "$HOME/chatcopilot-workspaces/default/results" \
        "$HOME/chatcopilot-workspaces/default/uploads" \
        "$HOME/chatcopilot-logs"

    if [ "$INIT_ENV" -eq 1 ]; then
        local env_file="$HOME/.chatcopilot.env"
        if [ -f "$env_file" ]; then
            warn "$env_file already exists; leaving it unchanged"
        else
            run cp "$REPO_ROOT/deploy/wsl/env.example" "$env_file"
            run chmod 600 "$env_file"
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
    info "checking Agent, Gateway, and optional ACP edge imports"
    "$py" -c "from chatcopilot.run import main; from chatcopilot.gateway.server import GatewayWebSocketServer; from chatcopilot.protocols.acp.server import GatewayAcpAgent; print('ok')" >/dev/null
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
    args+=(--skip-bots)
    info "installing/repairing optional Console"
    run "$VENV_DIR/bin/python" -m ensurepip --upgrade
    run env PATH="$NODE_COMMAND_PATH" \
        bash "$REPO_ROOT/deploy/wsl/deploy_console.sh" "${args[@]}"
}

echo
printf "%s=== AgentStrata isolated WSL/Linux runtime ===%s\n" "$C_BOLD" "$C_END"
echo

ensure_repo
select_architecture
print_runtime_plan
install_system_packages
require_download_tools
install_node_tools
install_python_env
prepare_runtime_dirs
verify_installation
install_console

echo
if [ "$DRY_RUN" -eq 1 ]; then
    ok "dry-run completed; no files or packages were changed"
else
    ok "isolated runtime is ready"
fi
echo "  python:      $VENV_DIR/bin/python"
if [ "$INSTALL_CC_CONNECT" -eq 1 ]; then
    echo "  cc-connect:  $CC_CONNECT_BIN"
fi
echo
