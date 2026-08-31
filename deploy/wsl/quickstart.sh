#!/usr/bin/env bash
# quickstart.sh - Guided first deployment for one generic QQ assistant.
set -uo pipefail

EXIT_READY=0
EXIT_FAILED=1
EXIT_USAGE=2
EXIT_NEEDS_USER_ACTION=3

BOT_ID="my-assistant-qq"
DISPLAY_NAME="我的助手"
DRY_RUN=0
RESUME=0
INSTALL_DOCKER=1
SYSTEMD_PID1_READY=0
SYSTEMD_USER_BUS_READY=0
SYSTEMD_USER_BUS_CHECKABLE=1
NETWORK_READY=0
CHECKS=()
PYTHON_BIN=""
unset PYTHONHOME PYTHONSTARTUP PYTHONINSPECT PYTHONPATH

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." >/dev/null 2>&1 && pwd)"

usage() {
    cat <<'EOF'
Usage: bash deploy/wsl/quickstart.sh [options]

Options:
  --bot-id ID             Bot ID (default: my-assistant-qq)
  --display-name NAME     Display name (default: 我的助手)
  --dry-run               Read-only checks and a change preview
  --resume                Resume from BotSpec, env, Docker and systemd state
  --no-install-docker     Never install or modify Docker
  -h, --help              Show this help

Exit codes: 0=ready, 1=failed, 2=usage_error, 3=needs_user_action
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --bot-id)
            [ "$#" -ge 2 ] || { echo "[ERR] --bot-id requires a value" >&2; exit "$EXIT_USAGE"; }
            BOT_ID="$2"; shift 2 ;;
        --display-name)
            [ "$#" -ge 2 ] || { echo "[ERR] --display-name requires a value" >&2; exit "$EXIT_USAGE"; }
            DISPLAY_NAME="$2"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        --resume) RESUME=1; shift ;;
        --no-install-docker) INSTALL_DOCKER=0; shift ;;
        -h|--help) usage; exit "$EXIT_READY" ;;
        *) echo "[ERR] unknown argument: $1" >&2; usage >&2; exit "$EXIT_USAGE" ;;
    esac
done

if [ "${#BOT_ID}" -lt 2 ] || [ "${#BOT_ID}" -gt 63 ] \
    || [[ ! "$BOT_ID" =~ ^[a-z][a-z0-9]*(-[a-z0-9]+)*$ ]]; then
    echo "[ERR] --bot-id must be 2-63 character kebab-case starting with a letter" >&2
    exit "$EXIT_USAGE"
fi
if [ -z "${DISPLAY_NAME//[[:space:]]/}" ] \
    || [[ "$DISPLAY_NAME" == *$'\n'* ]] \
    || [[ "$DISPLAY_NAME" == *$'\r'* ]]; then
    echo "[ERR] --display-name must be non-empty single-line text" >&2
    exit "$EXIT_USAGE"
fi
printf -v RESUME_COMMAND \
    'bash deploy/wsl/quickstart.sh --bot-id %q --display-name %q --resume' \
    "$BOT_ID" "$DISPLAY_NAME"
if [ "$(id -u)" -eq 0 ]; then
    echo "[ERR] 不要用 sudo/root 运行整个向导；需要提权时脚本会单独调用 sudo。" >&2
    exit "$EXIT_USAGE"
fi

if [ -t 1 ]; then
    C_INFO=$'\033[1;36m'; C_OK=$'\033[1;32m'; C_WARN=$'\033[1;33m'
    C_ERR=$'\033[1;31m'; C_END=$'\033[0m'
else
    C_INFO=""; C_OK=""; C_WARN=""; C_ERR=""; C_END=""
fi

info() { printf '%s[*]%s %s\n' "$C_INFO" "$C_END" "$*"; }
ok() { printf '%s[OK]%s %s\n' "$C_OK" "$C_END" "$*"; }
warn() { printf '%s[WARN]%s %s\n' "$C_WARN" "$C_END" "$*"; }
err() { printf '%s[ERR]%s %s\n' "$C_ERR" "$C_END" "$*" >&2; }

add_check() {
    CHECKS+=("$1"$'\x1f'"$2"$'\x1f'"$3"$'\x1f'"$4")
}

emit_report() {
    local overall="$1"
    local python_bin="$PYTHON_BIN"
    if [ -z "$python_bin" ] || [ ! -x "$python_bin" ]; then
        python_bin="/usr/bin/python3"
    fi
    if [ ! -x "$python_bin" ]; then
        printf '{"schema_version":"agentstrata-deployment-check/v1","overall":"%s","bot_id":"%s","checks":[]}\n' \
            "$overall" "$BOT_ID"
        return
    fi
    "$python_bin" -I - "$overall" "$BOT_ID" "${CHECKS[@]}" <<'PY'
import json
import sys

checks = []
for raw in sys.argv[3:]:
    check_id, status, message, remediation = raw.split("\x1f", 3)
    checks.append({
        "id": check_id,
        "status": status,
        "message": message[:512],
        "remediation": remediation[:512],
    })
print(json.dumps({
    "schema_version": "agentstrata-deployment-check/v1",
    "overall": sys.argv[1],
    "bot_id": sys.argv[2],
    "checks": checks,
}, ensure_ascii=False, allow_nan=False, sort_keys=True))
PY
}

finish_needs_action() {
    local remediation="$3"
    remediation="${remediation:+$remediation；}然后运行：$RESUME_COMMAND"
    add_check "$1" "fail" "$2" "$remediation"
    emit_report "needs_user_action"
    exit "$EXIT_NEEDS_USER_ACTION"
}

finish_failed() {
    add_check "$1" "fail" "$2" "$3"
    emit_report "failed"
    exit "$EXIT_FAILED"
}

handle_interrupt() {
    trap - INT TERM
    add_check "interrupted" "fail" "用户中断，已提交的分阶段变更保持可恢复" \
        "重新运行：$RESUME_COMMAND"
    emit_report "needs_user_action"
    exit "$EXIT_NEEDS_USER_ACTION"
}
trap handle_interrupt INT TERM

confirm() {
    local prompt="$1" reply
    if [ ! -t 0 ] || [ ! -t 1 ]; then
        return 1
    fi
    printf '%s [y/N] ' "$prompt" > /dev/tty
    IFS= read -r reply < /dev/tty || return 1
    [[ "$reply" =~ ^[Yy]([Ee][Ss])?$ ]]
}

need_sudo() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    else
        command -v sudo >/dev/null 2>&1 || return 127
        sudo "$@"
    fi
}

read_os_release() {
    [ -r /etc/os-release ] || finish_failed "distribution" "无法读取 /etc/os-release" "在受支持的 Ubuntu 或 Debian 中重试"
    DISTRO_ID="$(sed -n 's/^ID=//p' /etc/os-release | head -n1 | tr -d '\"')"
    DISTRO_VERSION="$(sed -n 's/^VERSION_ID=//p' /etc/os-release | head -n1 | tr -d '\"')"
    DISTRO_CODENAME="$(sed -n 's/^VERSION_CODENAME=//p' /etc/os-release | head -n1 | tr -d '\"')"
    case "$DISTRO_ID:$DISTRO_VERSION" in
        ubuntu:22.04|ubuntu:24.04|ubuntu:26.04|debian:11|debian:12|debian:13) ;;
        *)
            finish_failed "distribution" "不支持自动安装：$DISTRO_ID $DISTRO_VERSION" \
                "手工准备 Docker 和项目运行时，或改用 Ubuntu 22.04/24.04/26.04 或 Debian 11/12/13"
            ;;
    esac
    [ -n "$DISTRO_CODENAME" ] || finish_failed "distribution" "发行版缺少 VERSION_CODENAME" "修复 /etc/os-release 或手工安装 Docker"
    add_check "distribution" "pass" "$DISTRO_ID $DISTRO_VERSION" ""
}

read_architecture() {
    case "$(uname -m)" in
        x86_64|amd64) RUNTIME_ARCH="x64"; APT_ARCH="amd64" ;;
        aarch64|arm64) RUNTIME_ARCH="arm64"; APT_ARCH="arm64" ;;
        *) finish_failed "architecture" "不支持的架构：$(uname -m)" "使用 amd64/x86_64 或 arm64/aarch64" ;;
    esac
    add_check "architecture" "pass" "$APT_ARCH" ""
}

check_repository() {
    [ -f "$REPO_ROOT/pyproject.toml" ] && [ -d "$REPO_ROOT/src/chatcopilot" ] \
        || finish_failed "repository" "当前目录不是 AgentStrata 源码仓库" "重新 clone 后在仓库根目录运行"
    local git_root repo_real git_root_real
    git_root="$(git -C "$REPO_ROOT" rev-parse --show-toplevel 2>/dev/null || true)"
    repo_real="$(realpath -e "$REPO_ROOT" 2>/dev/null || true)"
    git_root_real="$(realpath -e "$git_root" 2>/dev/null || true)"
    [ -n "$repo_real" ] && [ "$git_root_real" = "$repo_real" ] \
        || finish_failed "repository" "无法确认 Git 仓库根目录" "从官方仓库完整 clone 后重试"
    if grep -qi microsoft /proc/sys/kernel/osrelease /proc/version 2>/dev/null; then
        IS_WSL=1
        if ! grep -qi 'wsl2' /proc/sys/kernel/osrelease 2>/dev/null; then
            finish_needs_action "wsl_version" "检测到 WSL1；新手部署只支持 WSL2" \
                "在 Windows PowerShell 运行 wsl -l -v，再运行 wsl --set-version <发行版名称> 2"
        fi
        add_check "wsl_version" "pass" "WSL2" ""
        case "$repo_real" in
            /mnt/[a-zA-Z]|/mnt/[a-zA-Z]/*)
                finish_failed "repository_filesystem" "WSL 源仓位于 Windows 文件系统" \
                    "cd \"\$HOME\" && git clone https://github.com/Ling-ye/AgentStrata.git"
                ;;
        esac
        command -v findmnt >/dev/null 2>&1 \
            || finish_failed "repository_filesystem" "无法确认 WSL 源仓所在文件系统" \
                "安装 util-linux 后重试"
        local mount_info repo_fs repo_source repo_options
        mount_info="$(findmnt -T "$repo_real" -n -o FSTYPE,SOURCE,OPTIONS 2>/dev/null || true)"
        read -r repo_fs repo_source repo_options <<< "$mount_info"
        [ -n "$repo_fs" ] && [ -n "$repo_source" ] \
            || finish_failed "repository_filesystem" "无法确认 WSL 源仓挂载类型" \
                "将仓库克隆到 \$HOME 下的 WSL Linux 文件系统"
        if [ "$repo_fs" = "drvfs" ] \
            || { [ "$repo_fs" = "9p" ] \
                && { [[ "$repo_source" =~ ^[A-Za-z]:\\ ]] \
                    || [[ "$repo_options" == *"aname=drvfs"* ]]; }; }; then
            finish_failed "repository_filesystem" "WSL 源仓位于 Windows DrvFS/9P 文件系统" \
                "cd \"\$HOME\" && git clone https://github.com/Ling-ye/AgentStrata.git"
        fi
    else
        IS_WSL=0
    fi
    local free_kib
    free_kib="$(df -Pk "$REPO_ROOT" | awk 'NR==2 {print $4}')"
    if ! [[ "$free_kib" =~ ^[0-9]+$ ]] || [ "$free_kib" -lt 5242880 ]; then
        finish_failed "disk_space" "可用磁盘不足 5 GiB" "清理 Linux/WSL 文件系统后重试"
    fi
    add_check "repository_filesystem" "pass" "源仓位于 Linux 文件系统" ""
    add_check "disk_space" "pass" "可用磁盘不少于 5 GiB" ""
}

check_systemd() {
    local pid_one
    pid_one="$(ps -p 1 -o comm= 2>/dev/null | tr -d '[:space:]')"
    if [ "$pid_one" != "systemd" ]; then
        if [ "$DRY_RUN" -eq 1 ]; then
            if [ "$IS_WSL" -eq 1 ]; then
                cat >&2 <<'EOF'
[ACTION] WSL 当前未使用 systemd。请在 /etc/wsl.conf 配置：
[boot]
systemd=true

然后在 Windows PowerShell 执行 wsl --shutdown，重新打开 WSL 并运行：
EOF
                printf '%s\n' "$RESUME_COMMAND" >&2
            else
                warn "PID 1 不是 systemd；真实部署会在任何写入前暂停。"
            fi
            add_check "systemd_pid1" "fail" "PID 1 不是 systemd" \
                "启用 systemd 后运行：$RESUME_COMMAND"
            return 0
        fi
        if [ "$IS_WSL" -eq 1 ]; then
            cat >&2 <<'EOF'
[ACTION] WSL 当前未使用 systemd。请在 /etc/wsl.conf 配置：
[boot]
systemd=true

然后在 Windows PowerShell 执行 wsl --shutdown，重新打开 WSL 并运行：
EOF
            printf '%s\n' "$RESUME_COMMAND" >&2
            finish_needs_action "systemd_pid1" "WSL PID 1 不是 systemd" "配置 /etc/wsl.conf并执行 wsl --shutdown"
        fi
        finish_needs_action "systemd_pid1" "PID 1 不是 systemd" "换用 systemd 管理的 Linux 主机后重试"
    fi
    SYSTEMD_PID1_READY=1
    add_check "systemd_pid1" "pass" "PID 1 为 systemd" ""
    command -v systemctl >/dev/null 2>&1 \
        || {
            if [ "$DRY_RUN" -eq 1 ]; then
                add_check "systemd_user_bus" "fail" "缺少 systemctl" "安装 systemd 和 dbus-user-session"
                SYSTEMD_USER_BUS_CHECKABLE=0
                return 0
            fi
            finish_failed "systemd_user_bus" "缺少 systemctl" "安装 systemd 和 dbus-user-session"
        }
}

collect_packages() {
    BASE_PACKAGES=(ca-certificates curl git jq xz-utils rsync dbus-user-session)
    MISSING_PACKAGES=()
    local package
    for package in "${BASE_PACKAGES[@]}"; do
        dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -Fq 'install ok installed' \
            || MISSING_PACKAGES+=("$package")
    done
    DOCKER_CONFLICTS=()
    for package in docker.io docker-compose docker-compose-v2 podman-docker containerd runc; do
        dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -Fq 'install ok installed' \
            && DOCKER_CONFLICTS+=("$package")
    done
}

package_is_missing() {
    local expected="$1" package
    for package in "${MISSING_PACKAGES[@]}"; do
        [ "$package" = "$expected" ] && return 0
    done
    return 1
}

check_systemd_user_bus() {
    local phase="${1:-preflight}"
    [ "$SYSTEMD_PID1_READY" -eq 1 ] || return 0
    [ "$SYSTEMD_USER_BUS_CHECKABLE" -eq 1 ] || return 0
    if systemctl --user show-environment >/dev/null 2>&1; then
        SYSTEMD_USER_BUS_READY=1
        add_check "systemd_user_bus" "pass" "systemd user bus 可用" ""
        return 0
    fi
    if [ "$phase" = "preflight" ] && package_is_missing dbus-user-session; then
        if [ "$DRY_RUN" -eq 1 ]; then
            add_check "systemd_user_bus" "not_tested" \
                "dbus-user-session 缺失；真实运行会先安装再复检 user bus" \
                "确认安装后，若仍不可用则重新登录并运行：$RESUME_COMMAND"
        fi
        return 0
    fi
    if [ "$DRY_RUN" -eq 1 ]; then
        add_check "systemd_user_bus" "fail" "systemd user bus 不可用" \
            "安装 dbus-user-session 并重新登录"
        return 0
    fi
    finish_needs_action "systemd_user_bus" "systemd user bus 不可用" \
        "安装 dbus-user-session并重新登录终端"
}

check_docker_endpoint() {
    local endpoint context_name
    # Docker gives DOCKER_CONTEXT precedence over DOCKER_HOST. Mirror that
    # precedence exactly so a local-looking host cannot mask a remote context.
    if [ -n "${DOCKER_CONTEXT:-}" ]; then
        command -v docker >/dev/null 2>&1 \
            || finish_failed "docker_context" "Docker CLI 尚未安装且 DOCKER_CONTEXT 已显式设置，无法在任何变更前证明为本地 endpoint" \
                "unset DOCKER_CONTEXT 后运行：$RESUME_COMMAND"
        context_name="$DOCKER_CONTEXT"
        endpoint="$(docker context inspect "$context_name" --format '{{.Endpoints.docker.Host}}' 2>/dev/null)" \
            || finish_failed "docker_context" "无法读取显式 Docker context" "unset DOCKER_CONTEXT 并切换到本地 Docker 后运行：$RESUME_COMMAND"
    elif [ -n "${DOCKER_HOST:-}" ]; then
        endpoint="$DOCKER_HOST"
    else
        if ! command -v docker >/dev/null 2>&1; then
            if [ -n "${DOCKER_CONFIG:-}" ] \
                || [ -e "$HOME/.docker/config.json" ] \
                || [ -L "$HOME/.docker/config.json" ]; then
                finish_failed "docker_context" "Docker CLI 尚未安装且存在无法预先解析的 Docker 磁盘配置" \
                    "临时移开 ~/.docker/config.json 并 unset DOCKER_CONFIG，确认无需远程 context 后运行：$RESUME_COMMAND"
            fi
            return 0
        fi
        context_name="$(docker context show 2>/dev/null)" \
            || finish_failed "docker_context" "无法确认 Docker context" "切换到本地 Docker context 后运行：$RESUME_COMMAND"
        endpoint="$(docker context inspect "$context_name" --format '{{.Endpoints.docker.Host}}' 2>/dev/null)" \
            || finish_failed "docker_context" "无法读取 Docker endpoint" "切换到本地 Docker context 后运行：$RESUME_COMMAND"
    fi
    case "$endpoint" in
        unix://*) add_check "docker_context" "pass" "Docker endpoint 为本地 Unix socket" "" ;;
        *)
            finish_failed "docker_context" "拒绝远端 Docker endpoint" \
                "unset DOCKER_HOST，并切换到本地 Docker 后运行：$RESUME_COMMAND"
            ;;
    esac
}

show_plan() {
    echo
    info "部署变更预览"
    printf '  host:       %s %s / %s\n' "$DISTRO_ID" "$DISTRO_VERSION" "$APT_ARCH"
    printf '  repository: %s\n' "$REPO_ROOT"
    printf '  bot:        bots/%s (display=%s)\n' "$BOT_ID" "$DISPLAY_NAME"
    printf '  apt:        %s\n' "${MISSING_PACKAGES[*]:-(none)}"
    printf '  Python:     uv 0.12.5 managed CPython 3.13.15 -> %s/.venv\n' "$REPO_ROOT"
    printf '  Node:       24.20.0 -> %s/.local/share/agentstrata/node\n' "$HOME"
    printf '  cc-connect: 1.4.0-beta.3 via npm ci -> %s/.local/share/agentstrata/node-tools\n' "$HOME"
    if docker info >/dev/null 2>&1; then
        printf '  Docker:     reuse current working Docker context\n'
    elif [ "$INSTALL_DOCKER" -eq 0 ]; then
        printf '  Docker:     unavailable; automatic installation disabled\n'
    else
        printf '  Docker:     official %s apt repository + docker-ce/cli/containerd/buildx/compose\n' "$DISTRO_ID"
        printf '  conflicts:  %s\n' "${DOCKER_CONFLICTS[*]:-(none)}"
        printf '  group:      docker group membership requires a separate confirmation\n'
    fi
    printf '  Docker data: never purge Docker or delete images, containers, volumes, or /var/lib/docker\n'
    printf '  services:   NapCat loopback ports + chatcopilot@%s.service\n' "$BOT_ID"
    echo
}

ensure_base_packages() {
    [ "${#MISSING_PACKAGES[@]}" -eq 0 ] && return 0
    info "安装基础包：${MISSING_PACKAGES[*]}"
    need_sudo apt-get update || return 1
    need_sudo apt-get install -y "${MISSING_PACKAGES[@]}" || return 1
}

install_docker_engine() {
    local key_tmp source_tmp
    local docker_packages=(docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin)
    key_tmp="$(mktemp)" || return 1
    source_tmp="$(mktemp)" || { rm -f -- "$key_tmp"; return 1; }
    if ! curl --disable --fail --silent --show-error --location \
        --proto '=https' --tlsv1.2 --max-time 60 \
        "https://download.docker.com/linux/$DISTRO_ID/gpg" -o "$key_tmp"; then
        rm -f -- "$key_tmp" "$source_tmp"
        return 1
    fi
    printf 'Types: deb\nURIs: https://download.docker.com/linux/%s\nSuites: %s\nComponents: stable\nArchitectures: %s\nSigned-By: /etc/apt/keyrings/docker.asc\n' \
        "$DISTRO_ID" "$DISTRO_CODENAME" "$APT_ARCH" > "$source_tmp"
    need_sudo install -m 0755 -d /etc/apt/keyrings \
        && need_sudo install -m 0644 "$key_tmp" /etc/apt/keyrings/docker.asc \
        && need_sudo install -m 0644 "$source_tmp" /etc/apt/sources.list.d/docker.sources
    local install_rc=$?
    rm -f -- "$key_tmp" "$source_tmp"
    [ "$install_rc" -eq 0 ] || return "$install_rc"
    need_sudo apt-get update || return 1
    need_sudo apt-get install --download-only -y "${docker_packages[@]}" || return 1

    if [ "${#DOCKER_CONFLICTS[@]}" -gt 0 ]; then
        warn "发现 Docker 官方包冲突：${DOCKER_CONFLICTS[*]}"
        warn "目标 Docker 包已下载；只会 remove 上述冲突包，不 purge 或删除 Docker 数据。"
        confirm "允许移除上述冲突包并立即安装 Docker 官方包？" \
            || finish_needs_action "docker_conflicts" "用户未确认移除 Docker 冲突包" "人工处理 Docker 冲突包"
        need_sudo apt-get remove -y "${DOCKER_CONFLICTS[@]}" || return 1
    fi
    need_sudo apt-get install -y "${docker_packages[@]}" || return 1
    need_sudo systemctl enable --now docker || return 1
}

ensure_docker() {
    # DOCKER_HOST is authoritative even before a CLI is installed. Validate it
    # before the first daemon call, then resolve the installed CLI context again
    # after an automatic installation so no later command can target a remote host.
    check_docker_endpoint
    if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
        add_check "docker" "pass" "已复用可用的 Docker" ""
        return 0
    fi
    if [ "$INSTALL_DOCKER" -eq 0 ]; then
        finish_needs_action "docker" "Docker 不可用且禁止自动安装" "启用 Docker Desktop WSL 集成或安装 Docker"
    fi
    if ! command -v docker >/dev/null 2>&1; then
        install_docker_engine || finish_failed "docker_install" "Docker 官方 apt 安装失败" "检查网络和 apt 日志后运行：$RESUME_COMMAND"
    elif systemctl is-active --quiet docker 2>/dev/null; then
        :
    elif systemctl cat docker.service >/dev/null 2>&1; then
        need_sudo systemctl start docker \
            || finish_failed "docker_daemon" "Docker daemon 启动失败" "sudo systemctl status docker"
    fi
    check_docker_endpoint
    if docker info >/dev/null 2>&1; then
        add_check "docker" "pass" "Docker daemon 可用" ""
        return 0
    fi
    if [ "$(id -u)" -ne 0 ] && ! id -nG | tr ' ' '\n' | grep -Fxq docker; then
        warn "docker 组可以完全控制 Docker daemon，权限近似 root。"
        confirm "允许将当前用户加入 docker 组？" \
            || finish_needs_action "docker_group" "用户未确认 docker 组权限" "人工准备可用 Docker"
        need_sudo usermod -aG docker "$(id -un)" \
            || finish_failed "docker_group" "加入 docker 组失败" "sudo usermod -aG docker \"\$USER\""
        finish_needs_action "docker_group" "docker 组已写入，当前终端尚未获得新权限" "重新打开终端"
    fi
    finish_needs_action "docker" "docker info 当前不可用" "启动 Docker Desktop WSL 集成或 sudo systemctl start docker"
}

check_network() {
    local phase="${1:-required}"
    if ! command -v curl >/dev/null 2>&1; then
        if [ "$phase" = "preflight" ] && package_is_missing curl; then
            add_check "network" "not_tested" \
                "curl 缺失；真实运行会先安装再复检公开下载源" \
                "确认基础包安装后自动复检"
            return 0
        fi
        if [ "$DRY_RUN" -eq 1 ]; then
            add_check "network" "fail" "缺少 curl，无法检查公开下载源" "安装 curl 后重试"
            return 0
        fi
        finish_failed "network" "缺少 curl，无法检查公开下载源" "安装 curl 后运行：$RESUME_COMMAND"
    fi
    if ! curl --disable -fsSI --max-time 15 https://github.com >/dev/null 2>&1; then
        if [ "$DRY_RUN" -eq 1 ]; then
            add_check "network" "fail" "公开下载源当前不可达" "检查 DNS/代理后重试"
            return 0
        fi
        finish_failed "network" "无法访问公开下载源" "检查 DNS/代理后运行：$RESUME_COMMAND"
    fi
    NETWORK_READY=1
    add_check "network" "pass" "公开下载源可达" ""
}

check_guided_environment_overrides() {
    local name
    for name in \
        AGENTSTRATA_RUNTIME_ROOT \
        AGENTSTRATA_DEPLOY_PYTHON \
        NAPCAT_IMAGE \
        NAPCAT_DISABLE_BYPASS \
        NAPCAT_DISABLE_MULTI_PROCESS \
        NAPCAT_SHM_SIZE \
        NAPCAT_QQ_DATA_VOLUME \
        NAPCAT_CONFIG_VOLUME \
        NAPCAT_QUICK_PASSWORD \
        NAPCAT_QUICK_PASSWORD_MD5; do
        if [ -n "${!name-}" ]; then
            finish_failed "starter_profile" "新手流程不接受继承的 $name 路径或高级覆盖" \
                "unset $name 后运行：$RESUME_COMMAND"
        fi
    done
}

run_bot_cli() {
    env -i \
        HOME="$HOME" USER="${USER:-$(id -un)}" LOGNAME="${LOGNAME:-$(id -un)}" \
        LANG="${LANG:-C.UTF-8}" TERM="${TERM:-dumb}" PATH="$PATH" \
        CHATCOPILOT_HOME="$REPO_ROOT" PYTHONPATH="$REPO_ROOT/src" \
        "$PYTHON_BIN" -m chatcopilot bot "$@"
}

validate_resume_bot() {
    PYTHONDONTWRITEBYTECODE=1 CHATCOPILOT_HOME="$REPO_ROOT" PYTHONPATH="$REPO_ROOT/src" \
        "$PYTHON_BIN" - "$REPO_ROOT/bots/$BOT_ID/bot.yaml" <<'PY'
import sys
from chatcopilot.botspec.loader import load_botspec, validate_botspec
from chatcopilot.botspec.provisioning import is_guided_starter_spec

spec = load_botspec(sys.argv[1])
errors = [item for item in validate_botspec(spec) if item.level == "error"]
if errors:
    raise SystemExit("starter_botspec_invalid")
if not is_guided_starter_spec(spec):
    raise SystemExit("starter_profile_required")
PY
}

parse_guided_env_assignment() {
    local input="$1" length index character state="plain" decoded="" remainder
    while [[ "$input" == [[:space:]]* ]]; do
        input="${input:1}"
    done
    if [[ "$input" =~ ^export[[:space:]]+ ]]; then
        input="${input:${#BASH_REMATCH[0]}}"
    fi
    if [[ ! "$input" =~ ^([A-Za-z_][A-Za-z0-9_]*)= ]]; then
        return 1
    fi
    PARSED_ENV_KEY="${BASH_REMATCH[1]}"
    input="${input:${#BASH_REMATCH[0]}}"
    length=${#input}
    index=0
    while [ "$index" -lt "$length" ]; do
        character="${input:index:1}"
        case "$state" in
            plain)
                case "$character" in
                    "'") state="single" ;;
                    '"') state="double" ;;
                    \\)
                        index=$((index + 1))
                        [ "$index" -lt "$length" ] || return 1
                        decoded+="${input:index:1}"
                        ;;
                    ' '|$'\t') break ;;
                    *) decoded+="$character" ;;
                esac
                ;;
            single)
                if [ "$character" = "'" ]; then
                    state="plain"
                else
                    decoded+="$character"
                fi
                ;;
            double)
                case "$character" in
                    '"') state="plain" ;;
                    \\)
                        index=$((index + 1))
                        [ "$index" -lt "$length" ] || return 1
                        decoded+="${input:index:1}"
                        ;;
                    *) decoded+="$character" ;;
                esac
                ;;
        esac
        index=$((index + 1))
    done
    [ "$state" = "plain" ] || return 1
    remainder="${input:index}"
    while [[ "$remainder" == [[:space:]]* ]]; do
        remainder="${remainder:1}"
    done
    [ -z "$remainder" ] || [[ "$remainder" == \#* ]] || return 1
    PARSED_ENV_VALUE="$decoded"
}

trim_guided_scalar() {
    local value="$1"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    printf '%s' "$value"
}

valid_guided_port() {
    local value="$1"
    [[ "$value" =~ ^[0-9]{1,5}$ ]] || return 1
    [ "$((10#$value))" -ge 1 ] && [ "$((10#$value))" -le 65535 ]
}

valid_guided_numeric_list() {
    local value="$1" allow_all="$2" item
    value="$(trim_guided_scalar "$value")"
    [ -n "$value" ] || return 0
    if [ "$allow_all" = "1" ] && [ "$value" = "*" ]; then
        return 0
    fi
    local -a items=()
    IFS=',' read -r -a items <<< "$value"
    [ "${#items[@]}" -gt 0 ] || return 1
    for item in "${items[@]}"; do
        item="$(trim_guided_scalar "$item")"
        [[ "$item" =~ ^[0-9]+$ ]] || return 1
    done
}

valid_guided_loopback_ws_url() {
    local value="$1" port
    value="$(trim_guided_scalar "$value")"
    if [[ "$value" =~ ^wss?://(localhost|127\.0\.0\.1|\[::1\]):([0-9]{1,5})(/[^[:space:]]*)?$ ]]; then
        port="${BASH_REMATCH[2]}"
        valid_guided_port "$port"
        return
    fi
    return 1
}

valid_guided_llm_base_url() {
    local value="$1" remainder authority port="" http_scheme="http" https_scheme="https"
    local http_prefix="${http_scheme}://" https_prefix="${https_scheme}://"
    value="$(trim_guided_scalar "$value")"
    [ -n "$value" ] || return 0
    [[ "$value" != *[[:space:]?#]* ]] || return 1
    if [[ "$value" == "$http_prefix"* ]]; then
        if [[ "$value" =~ ^${http_prefix}(localhost|127\.0\.0\.1|\[::1\])(:([0-9]{1,5}))?(/[^[:space:]?#]*)?$ ]]; then
            port="${BASH_REMATCH[3]}"
            [ -z "$port" ] || valid_guided_port "$port"
            return
        fi
        return 1
    fi
    [[ "$value" == "$https_prefix"* ]] || return 1
    remainder="${value#"$https_prefix"}"
    authority="${remainder%%/*}"
    [ -n "$authority" ] && [[ "$authority" != *@* ]] || return 1
    if [[ "$authority" == \[* ]]; then
        [[ "$authority" =~ ^\[[^][]+\](:([0-9]{1,5}))?$ ]] || return 1
        port="${BASH_REMATCH[2]}"
    elif [[ "$authority" == *:* ]]; then
        [[ "$authority" =~ ^[^:]+:([0-9]{1,5})$ ]] || return 1
        port="${BASH_REMATCH[1]}"
    fi
    [ -z "$port" ] || valid_guided_port "$port"
}

validate_guided_env_value() {
    local key="$1" value="$2" trimmed
    trimmed="$(trim_guided_scalar "$value")"
    case "$key" in
        CHATCOPILOT_CHAT_BASE_URL)
            valid_guided_llm_base_url "$trimmed"
            ;;
        CHATCOPILOT_ADD_OWNER_IDS)
            valid_guided_numeric_list "$trimmed" 0
            ;;
        QQ_ACCOUNT)
            [ -z "$trimmed" ] || [[ "$trimmed" =~ ^[0-9]+$ ]]
            ;;
        QQ_ALLOW_FROM|QQ_ALLOW_GROUPS)
            valid_guided_numeric_list "$trimmed" 1
            ;;
        QQ_WS_URL|QQ_AT_PROXY_URL)
            [ -z "$trimmed" ] || valid_guided_loopback_ws_url "$trimmed"
            ;;
        QQ_WEBUI_PORT)
            [ -z "$trimmed" ] || valid_guided_port "$trimmed"
            ;;
        QQ_IMAGE_MAX_BYTES|QQ_IMAGE_SEND_TIMEOUT_SECONDS)
            [ -z "$trimmed" ] || [[ "$trimmed" =~ ^[1-9][0-9]*$ ]]
            ;;
        *) return 0 ;;
    esac
}

validate_resume_paths() {
    local bots_root_real bot_dir_real expected_real prompt_file line key
    [ ! -L "$BOT_DIR" ] \
        || finish_failed "starter_profile" "拒绝符号链接 Bot 目录" "在仓库 bots/ 下使用普通目录"
    if [ ! -f "$BOT_DIR/bot.yaml" ] || [ -L "$BOT_DIR/bot.yaml" ] \
        || [ ! -r "$BOT_DIR/bot.yaml" ] \
        || [ "$(stat -c '%u' "$BOT_DIR/bot.yaml" 2>/dev/null || true)" != "$(id -u)" ] \
        || [ "$(stat -c '%h' "$BOT_DIR/bot.yaml" 2>/dev/null || true)" != "1" ]; then
        finish_failed "starter_profile" "BotSpec 必须是当前用户拥有的普通单链接文件" \
            "在仓库内恢复普通 bots/$BOT_ID/bot.yaml"
    fi
    command -v iconv >/dev/null 2>&1 \
        || finish_failed "starter_profile" "缺少用于无依赖 UTF-8 预检的 iconv" "安装 libc-bin 后运行：$RESUME_COMMAND"
    iconv -f UTF-8 -t UTF-8 "$BOT_DIR/bot.yaml" >/dev/null 2>&1 \
        || finish_failed "starter_profile" "BotSpec 不是有效 UTF-8" \
            "将 bots/$BOT_ID/bot.yaml 恢复为 UTF-8 文本"
    if [ -e "$BOT_DIR/local.env" ] || [ -L "$BOT_DIR/local.env" ]; then
        if [ ! -f "$BOT_DIR/local.env" ] || [ -L "$BOT_DIR/local.env" ] \
            || [ ! -r "$BOT_DIR/local.env" ] \
            || [ "$(stat -c '%u' "$BOT_DIR/local.env" 2>/dev/null || true)" != "$(id -u)" ] \
            || [ "$(stat -c '%h' "$BOT_DIR/local.env" 2>/dev/null || true)" != "1" ] \
            || [ "$(stat -c '%a' "$BOT_DIR/local.env" 2>/dev/null || true)" != "600" ]; then
            finish_failed "private_config" "local.env 必须是当前用户拥有的 0600 普通单链接文件" \
                "恢复安全的 bots/$BOT_ID/local.env 后运行：$RESUME_COMMAND"
        fi
        iconv -f UTF-8 -t UTF-8 "$BOT_DIR/local.env" >/dev/null 2>&1 \
            || finish_failed "private_config" "local.env 不是有效 UTF-8" \
                "将 bots/$BOT_ID/local.env 恢复为 UTF-8 文本"
        local -A local_env_keys=()
        while IFS= read -r line || [ -n "$line" ]; do
            [[ "$line" =~ ^[[:space:]]*$ || "$line" =~ ^[[:space:]]*# ]] && continue
            if ! parse_guided_env_assignment "$line"; then
                finish_failed "private_config" "local.env 包含新手流程不支持的赋值语法" \
                    "使用 bot configure 重新生成简单 local.env"
            fi
            key="$PARSED_ENV_KEY"
            if [ -n "${local_env_keys[$key]+x}" ]; then
                finish_failed "private_config" "local.env 包含重复键：$key" \
                    "保留唯一的 $key 赋值后运行：$RESUME_COMMAND"
            fi
            local_env_keys["$key"]=1
            case "$key" in
                CHATCOPILOT_CHAT_API_KEY|CHATCOPILOT_CHAT_BASE_URL|CHATCOPILOT_CHAT_MODEL|\
                CHATCOPILOT_ADD_OWNER_IDS|QQ_ACCOUNT|QQ_WS_URL|QQ_ACCESS_TOKEN|\
                QQ_ALLOW_FROM|QQ_ALLOW_GROUPS|QQ_AT_PROXY_URL|QQ_WEBUI_PORT|\
                QQ_IMAGE_MAX_BYTES|QQ_IMAGE_SEND_TIMEOUT_SECONDS) ;;
                *)
                    local key_label="$key"
                    if [[ "$key" == "CHATCOPILOT_CC_CONNECT_BIN" ]]; then
                        key_label="cc-connect executable override ($key)"
                    fi
                    finish_failed "starter_profile" "新手恢复流程不接受高级配置键：$key_label" \
                        "移除 $key，或改用 docs/deployment.md 的高级流程"
                    ;;
            esac
            validate_guided_env_value "$key" "$PARSED_ENV_VALUE" \
                || finish_failed "private_config" "local.env 包含无效的 $key 配置" \
                    "使用 bot configure 修正 $key 后运行：$RESUME_COMMAND"
        done < "$BOT_DIR/local.env"
    fi
    if [ ! -d "$BOT_DIR/prompts" ] || [ -L "$BOT_DIR/prompts" ] \
        || [ "$(stat -c '%u' "$BOT_DIR/prompts" 2>/dev/null || true)" != "$(id -u)" ]; then
        finish_failed "starter_profile" "starter prompts 必须是 Bot 目录内当前用户拥有的普通目录" \
            "在 bots/$BOT_ID/prompts/ 恢复 starter 提示词目录"
    fi
    for prompt_file in identity.md response-style.md refusal-style.md; do
        if [ ! -f "$BOT_DIR/prompts/$prompt_file" ] \
            || [ -L "$BOT_DIR/prompts/$prompt_file" ] \
            || [ ! -r "$BOT_DIR/prompts/$prompt_file" ] \
            || [ "$(stat -c '%u' "$BOT_DIR/prompts/$prompt_file" 2>/dev/null || true)" != "$(id -u)" ] \
            || [ "$(stat -c '%h' "$BOT_DIR/prompts/$prompt_file" 2>/dev/null || true)" != "1" ]; then
            finish_failed "starter_profile" "starter prompt 必须是当前用户拥有的普通单链接文件：$prompt_file" \
                "在 bots/$BOT_ID/prompts/ 恢复 starter 提示词文件"
        fi
        iconv -f UTF-8 -t UTF-8 "$BOT_DIR/prompts/$prompt_file" >/dev/null 2>&1 \
            || finish_failed "starter_profile" "starter prompt 不是有效 UTF-8：$prompt_file" \
                "将 bots/$BOT_ID/prompts/$prompt_file 恢复为 UTF-8 文本"
    done
    bots_root_real="$(realpath -e "$REPO_ROOT/bots")" \
        || finish_failed "starter_profile" "无法确认 bots 根目录" "从完整仓库 clone 后重试"
    bot_dir_real="$(realpath -e "$BOT_DIR")" \
        || finish_failed "starter_profile" "无法确认 Bot 目录" "检查 bots/$BOT_ID"
    expected_real="$bots_root_real/$BOT_ID"
    [ "$bot_dir_real" = "$expected_real" ] \
        || finish_failed "starter_profile" "Bot 目录越出 canonical bots 根" "在仓库 bots/$BOT_ID 下恢复 starter"
}

validate_resume_text_shape() {
    awk -v bot_id="$BOT_ID" '
        function fail() { invalid = 1; exit 1 }

        # The generated display name is a JSON string. Validate that scalar
        # without accepting YAML tags, anchors, aliases, comments, or flow data.
        function valid_json_string(value,    size, position, character, escaped, hex) {
            size = length(value)
            if (size < 2 || substr(value, 1, 1) != "\"" \
                    || substr(value, size, 1) != "\"") return 0
            for (position = 2; position < size; position++) {
                character = substr(value, position, 1)
                if (character == "\"") return 0
                if (character == "\\") {
                    position++
                    if (position >= size) return 0
                    escaped = substr(value, position, 1)
                    if (escaped == "\"" || escaped == "\\" || escaped == "/" \
                            || escaped == "b" || escaped == "f" || escaped == "n" \
                            || escaped == "r" || escaped == "t") continue
                    if (escaped != "u" || position + 4 >= size) return 0
                    hex = substr(value, position + 1, 4)
                    if (hex !~ /^[0-9A-Fa-f]{4}$/) return 0
                    position += 4
                } else if (character ~ /[[:cntrl:]]/) {
                    return 0
                }
            }
            return 1
        }

        /\t/ { fail() }
        /^[ ]*$/ { next }
        /^[ ]*#/ { next }

        /^[^ ]/ {
            section = ""
            subsection = ""
            if ($0 == "id: " bot_id) { bot_id_count++; next }
            if (index($0, "display_name: ") == 1) {
                value = $0
                sub(/^display_name: /, "", value)
                if (!valid_json_string(value)) fail()
                display_name_count++
                next
            }
            if ($0 == "platform:") { section = "platform"; platform_section++; next }
            if ($0 == "llm:") { section = "llm"; llm_section++; next }
            if ($0 == "prompts:") { section = "prompts"; prompts_section++; next }
            if ($0 == "tools:") { section = "tools"; tools_section++; next }
            if ($0 == "context:") { section = "context"; context_section++; next }
            if ($0 == "agents:") { section = "agents"; agents_section++; next }
            if ($0 == "workspace:") { section = "workspace"; workspace_section++; next }
            if ($0 == "deploy:") { section = "deploy"; deploy_section++; next }
            if ($0 == "access:") { section = "access"; access_section++; next }
            fail()
        }

        section == "platform" {
            if ($0 == "  type: qq") { platform_type++; next }
            if ($0 == "  adapter: qq_acp") { platform_adapter++; next }
            fail()
        }
        section == "llm" {
            if ($0 == "  chat:") { subsection = "chat"; llm_chat++; next }
            if (subsection == "chat" && $0 == "    env_prefix: CHATCOPILOT_CHAT") {
                llm_prefix++; next
            }
            fail()
        }
        section == "prompts" {
            if ($0 == "  schema_version: 2") { prompt_schema++; next }
            if ($0 == "  identity: prompts/identity.md") { prompt_identity++; next }
            if ($0 == "  response_style: prompts/response-style.md") { prompt_response++; next }
            if ($0 == "  refusal_style: prompts/refusal-style.md") { prompt_refusal++; next }
            fail()
        }
        section == "tools" {
            if ($0 == "  packs:") { subsection = "packs"; packs_section++; next }
            if ($0 == "  features:") { subsection = "features"; features_section++; next }
            if (subsection == "packs" && $0 == "  - workspace.read_write") {
                pack_workspace++; next
            }
            if (subsection == "packs" && $0 == "  - memory.chat") {
                pack_memory++; next
            }
            if (subsection == "features" && $0 == "  - chat.file_uploads") {
                feature_uploads++; next
            }
            if (subsection == "features" && $0 == "  - chat.private_workspace") {
                feature_workspace++; next
            }
            fail()
        }
        section == "context" {
            if ($0 == "  memory_store:") { subsection = "memory"; memory_section++; next }
            if (subsection == "memory" && $0 == "    provider: markdown") {
                memory_provider++; next
            }
            if (subsection == "memory" && $0 == "    namespace: " bot_id) {
                memory_namespace++; next
            }
            fail()
        }
        section == "agents" {
            if ($0 == "  backend: native") { backend++; next }
            # This exact empty-list scalar is emitted by the starter generator;
            # all other YAML flow syntax is rejected by the section whitelists.
            if ($0 == "  presets: []") { presets++; next }
            fail()
        }
        section == "workspace" {
            if ($0 == "  root_env: CHATCOPILOT_WORKSPACE_ROOT") { workspace_root++; next }
            fail()
        }
        section == "deploy" {
            if ($0 == "  target: wsl2") { deploy_target++; next }
            if ($0 == "  instance_id: " bot_id) { deploy_instance++; next }
            if ($0 == "  wsl_home: ~/ChatCopilot-" bot_id) { deploy_home++; next }
            if ($0 == "  workspace_root: ~/chatcopilot-workspaces/" bot_id) {
                deploy_workspace++; next
            }
            if ($0 == "  log_dir: ~/chatcopilot-logs/" bot_id) { deploy_log++; next }
            if ($0 == "  env_file: ~/.chatcopilot-" bot_id ".env") { deploy_env++; next }
            if ($0 == "  cc_connect_config_dir: ~/.chatcopilot-runtime/" bot_id "/.cc-connect") {
                deploy_cc_config++; next
            }
            if ($0 == "  project_name: chatcopilot-" bot_id) { deploy_project++; next }
            fail()
        }
        section == "access" {
            if ($0 == "  owner_only_project_access: true") { owner_access++; next }
            fail()
        }
        { fail() }

        END {
            if (invalid) exit 1
            if (bot_id_count != 1 || display_name_count != 1 \
                || platform_section != 1 || llm_section != 1 || prompts_section != 1 \
                || tools_section != 1 || context_section != 1 || agents_section != 1 \
                || workspace_section != 1 || deploy_section != 1 || access_section != 1 \
                || platform_type != 1 || platform_adapter != 1 \
                || llm_chat != 1 || llm_prefix != 1 \
                || prompt_schema != 1 || prompt_identity != 1 || prompt_response != 1 \
                || prompt_refusal != 1 || packs_section != 1 || features_section != 1 \
                || pack_workspace != 1 || pack_memory != 1 \
                || feature_uploads != 1 || feature_workspace != 1 \
                || memory_section != 1 || memory_provider != 1 || memory_namespace != 1 \
                || backend != 1 || presets != 1 || workspace_root != 1 \
                || deploy_target != 1 || deploy_instance != 1 || deploy_home != 1 \
                || deploy_workspace != 1 || deploy_log != 1 || deploy_env != 1 \
                || deploy_cc_config != 1 || deploy_project != 1 || owner_access != 1) exit 1
        }
    ' "$BOT_DIR/bot.yaml"
}

preflight_resume_bot() {
    validate_resume_paths
    validate_resume_text_shape \
        || finish_failed "starter_profile" "BotSpec 不符合 canonical starter 形状" "使用 docs/deployment.md 的高级部署流程"
}

doctor_ready() {
    local report
    report="$(run_bot_cli doctor --bot "$REPO_ROOT/bots/$BOT_ID/bot.yaml" --json 2>/dev/null)" || return 1
    printf '%s' "$report" | "$PYTHON_BIN" -c \
        'import json,sys; raise SystemExit(0 if json.load(sys.stdin).get("overall") == "ready" else 1)'
}

read_local_webui_port() {
    PYTHONPATH="$REPO_ROOT/src" "$PYTHON_BIN" - "$REPO_ROOT/bots/$BOT_ID/local.env" <<'PY'
import sys
from pathlib import Path
from chatcopilot.botspec.provisioning import read_local_env_for_provision

path = Path(sys.argv[1])
values = read_local_env_for_provision(path, allowed_parent=path.parent)
print(values.get("QQ_WEBUI_PORT") or "6099")
PY
}

webui_command() {
    PYTHONPATH="$REPO_ROOT/src" "$PYTHON_BIN" -m chatcopilot.platforms.qq.webui_session \
        "$@" --container "napcat-$BOT_ID" --host localhost --port "$WEBUI_PORT" --json
}

probe_relay_boundary() {
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$REPO_ROOT/src" \
        "$PYTHON_BIN" - "$BOT_DIR/local.env" <<'PY'
import asyncio
import sys
from pathlib import Path

from chatcopilot.botspec.provisioning import read_local_env_for_provision
from chatcopilot.platforms.qq.boundary import (
    require_access_token,
    require_loopback_websocket_url,
)
from chatcopilot.platforms.qq.gateway_health import probe_onebot_boundary

path = Path(sys.argv[1])
try:
    values = read_local_env_for_provision(path, allowed_parent=path.parent)
    token = require_access_token(values.get("QQ_ACCESS_TOKEN"))
    url = require_loopback_websocket_url(
        values.get("QQ_AT_PROXY_URL") or "ws://127.0.0.1:3002",
        env_key="QQ_AT_PROXY_URL",
    )
    asyncio.run(probe_onebot_boundary(url, token))
except Exception:
    raise SystemExit(1) from None
PY
}

probe_cc_connect_main_process() {
    local unit="$1" first_pid second_pid expected_node expected_entry expected_home node_arch
    case "$APT_ARCH" in
        amd64) node_arch="x64" ;;
        arm64) node_arch="arm64" ;;
        *) return 1 ;;
    esac
    expected_node="$HOME/.local/share/agentstrata/node/node-v24.20.0-linux-$node_arch/bin/node"
    expected_entry="$(readlink -f "$HOME/.local/share/agentstrata/node-tools/cc-connect-1.4.0-beta.3/node_modules/.bin/cc-connect" 2>/dev/null)" \
        || return 1
    expected_home="$HOME/.chatcopilot-runtime/$BOT_ID"

    first_pid="$(systemctl --user show "$unit" --property=MainPID --value 2>/dev/null)" \
        || return 1
    [[ "$first_pid" =~ ^[1-9][0-9]*$ ]] || return 1
    [ "$(stat -c '%u' "/proc/$first_pid" 2>/dev/null || true)" = "$(id -u)" ] \
        || return 1
    tr '\0' '\n' < "/proc/$first_pid/cmdline" 2>/dev/null \
        | grep -Fxq "$expected_node" || return 1
    tr '\0' '\n' < "/proc/$first_pid/cmdline" 2>/dev/null \
        | grep -Fxq "$expected_entry" || return 1
    tr '\0' '\n' < "/proc/$first_pid/environ" 2>/dev/null \
        | grep -Fxq "HOME=$expected_home" || return 1
    tr '\0' '\n' < "/proc/$first_pid/environ" 2>/dev/null \
        | grep -Fxq "CHATCOPILOT_INSTANCE_ID=$BOT_ID" || return 1

    # Type=simple can be briefly active before exec fails. Require the same
    # instance-bound Node/cc-connect MainPID to survive a bounded observation.
    sleep 2
    systemctl --user is-active --quiet "$unit" || return 1
    second_pid="$(systemctl --user show "$unit" --property=MainPID --value 2>/dev/null)" \
        || return 1
    [ "$second_pid" = "$first_pid" ] || return 1
    kill -0 "$second_pid" 2>/dev/null
}

read_webui_url() {
    local payload
    payload="$(webui_command webui-url)" || return 1
    printf '%s' "$payload" | "$PYTHON_BIN" -c \
        'import json,sys; value=json.load(sys.stdin).get("url", ""); raise SystemExit(1) if not value else print(value)'
}

await_qq_login() {
    if webui_command login-status --wait-seconds 0 >/dev/null 2>&1; then
        ok "NapCat 已登录，无需重新扫码"
        return 0
    fi
    [ -t 0 ] && [ -t 1 ] \
        || finish_needs_action "napcat_login" "非交互式终端不显示 WebUI token" "改用可信交互式终端"
    local webui_url
    webui_url="$(read_webui_url)" \
        || finish_needs_action "napcat_webui" "尚未从有界 NapCat 日志获取本地 WebUI session" "等待 NapCat WebUI session 出现在有界日志中"
    printf '\n仅在当前可信终端显示一次 NapCat WebUI 链接：\n%s\n\n' "$webui_url" > /dev/tty
    unset webui_url
    printf '请在浏览器完成 QQ 扫码和手机确认，然后回到此终端按回车。' > /dev/tty
    IFS= read -r _ < /dev/tty \
        || finish_needs_action "napcat_login" "等待扫码时终端中断" "完成扫码"
    if ! webui_command login-status --wait-seconds 120 --interval-seconds 2 >/dev/null 2>&1; then
        finish_needs_action "napcat_login" "NapCat 登录未在有界时间内确认" "在本地 WebUI 刷新二维码并完成登录"
    fi
}

BOT_DIR="$REPO_ROOT/bots/$BOT_ID"
RESUME_HAS_BOT=0
if [ "$RESUME" -eq 0 ] && { [ -e "$BOT_DIR" ] || [ -L "$BOT_DIR" ]; }; then
    finish_failed "starter_scaffold" "目标 Bot 已存在，拒绝覆盖" \
        "如果它由本向导创建，请运行：$RESUME_COMMAND"
fi
if [ "$RESUME" -eq 1 ]; then
    if [ -e "$BOT_DIR" ] || [ -L "$BOT_DIR" ]; then
        if [ ! -f "$BOT_DIR/bot.yaml" ]; then
            finish_failed "starter_profile" "--resume 发现不完整 Bot 目录，缺少普通 bot.yaml" \
                "检查 bots/$BOT_ID；恢复完整 starter 后运行：$RESUME_COMMAND"
        fi
        RESUME_HAS_BOT=1
        preflight_resume_bot
    else
        add_check "starter_scaffold" "not_tested" \
            "尚未创建 Bot；将从系统前置条件后的 scaffold 阶段恢复" \
            "继续运行：$RESUME_COMMAND；无需手工创建 YAML"
    fi
fi

check_guided_environment_overrides
read_os_release
read_architecture
check_repository
check_systemd
collect_packages
check_systemd_user_bus preflight
check_network preflight
check_docker_endpoint
show_plan

if [ "$DRY_RUN" -eq 1 ]; then
    if ! bash "$SCRIPT_DIR/install_wsl_env.sh" --no-system-packages --dry-run --no-verify; then
        finish_failed "isolated_runtime_plan" "无法生成隔离运行时零写入预览" \
            "检查 uv.lock 与 deploy/wsl/node-tools 锁文件"
    fi
    add_check "deployment_mutation" "not_tested" "dry-run 未执行任何写入" "确认预览后运行：$RESUME_COMMAND"
    emit_report "needs_user_action"
    exit "$EXIT_NEEDS_USER_ACTION"
fi

[ -t 0 ] && [ -t 1 ] \
    || finish_needs_action "interactive_tty" "需要可信交互式终端确认变更和读取秘密" "改用交互式 Linux/WSL 终端"

confirm "继续执行上述 apt、用户级运行时、文件和 systemd 变更？" \
    || finish_needs_action "change_confirmation" "用户未确认部署变更" "重新运行并确认变更，或先使用 --dry-run"

ensure_base_packages \
    || finish_failed "base_packages" "基础 apt 包安装失败" "修复 apt 后运行：$RESUME_COMMAND"
if [ "$SYSTEMD_USER_BUS_READY" -ne 1 ]; then
    check_systemd_user_bus after-install
fi
if [ "$NETWORK_READY" -ne 1 ]; then
    check_network after-install
fi

if ! bash "$SCRIPT_DIR/install_wsl_env.sh" --no-system-packages; then
    finish_failed "isolated_runtime" "用户级 Python/Node/cc-connect 安装失败" "检查下载和校验和错误后运行：$RESUME_COMMAND"
fi
PYTHON_BIN="$REPO_ROOT/.venv/bin/python"
[ -x "$PYTHON_BIN" ] \
    || finish_failed "isolated_runtime" "缺少项目隔离 Python" "bash deploy/wsl/install_wsl_env.sh --no-system-packages"
add_check "isolated_runtime" "pass" "固定用户级 Python/Node/cc-connect 已就绪" ""

if [ "$RESUME_HAS_BOT" -eq 1 ]; then
    validate_resume_bot \
        || finish_failed "starter_profile" "已有 Bot 不是可由新手流程恢复的 QQ/Native starter" "使用 docs/deployment.md 的高级部署流程"
fi

ensure_docker

if [ ! -f "$BOT_DIR/bot.yaml" ]; then
    run_bot_cli new "$BOT_ID" --platform qq --preset starter --display-name "$DISPLAY_NAME" \
        || finish_failed "starter_scaffold" "starter Bot 创建失败" "检查 Bot ID 和 bots/ 目录后重试"
fi
add_check "starter_profile" "pass" "QQ/Native starter BotSpec 有效" ""

if ! doctor_ready; then
    run_bot_cli configure --bot "$BOT_DIR/bot.yaml"
    configure_rc=$?
    if [ "$configure_rc" -eq "$EXIT_NEEDS_USER_ACTION" ]; then
        finish_needs_action "private_config" "私有配置录入未完成" "在交互式终端继续配置"
    elif [ "$configure_rc" -ne 0 ]; then
        finish_failed "private_config" "私有配置验证失败，原文件未被部分覆盖" "修正输入后运行：$RESUME_COMMAND"
    fi
fi
doctor_ready \
    || finish_failed "private_config" "配置写入后 doctor 仍未就绪" "python -m chatcopilot bot doctor --bot bots/$BOT_ID/bot.yaml --json"
add_check "private_config" "pass" "BotSpec 派生配置已通过校验" ""

if ! run_bot_cli provision-env --bot "$BOT_DIR/bot.yaml"; then
    finish_failed "runtime_env" "无法从非执行 local.env 解析边界生成运行时 env" "检查 bots/$BOT_ID/local.env 后运行：$RESUME_COMMAND"
fi
add_check "runtime_env" "pass" "运行时 env 已由安全 parser 生成" ""

if ! bash "$SCRIPT_DIR/qq_gateway.sh" bootstrap --instance "$BOT_ID"; then
    finish_failed "napcat_bootstrap" "NapCat bootstrap 失败" "bash deploy/wsl/qq_gateway.sh bootstrap --instance $BOT_ID"
fi
WEBUI_PORT="$(read_local_webui_port)" \
    || finish_failed "napcat_webui" "无法读取 NapCat WebUI 回环端口" "检查 bots/$BOT_ID/local.env"
await_qq_login
add_check "napcat_login" "pass" "NapCat WebUI 已确认 QQ 登录态" ""

if ! bash "$SCRIPT_DIR/qq_gateway.sh" sync-token --instance "$BOT_ID"; then
    finish_failed "onebot_token_sync" "OneBot token 同步或双向认证失败" "bash deploy/wsl/qq_gateway.sh sync-token --instance $BOT_ID"
fi
if ! bash "$SCRIPT_DIR/qq_gateway.sh" status --instance "$BOT_ID"; then
    finish_failed "onebot_readonly" "NapCat 或经过认证的只读 OneBot 检查失败" "bash deploy/wsl/qq_gateway.sh status --instance $BOT_ID"
fi
add_check "onebot_readonly" "pass" "NapCat 和经过认证的只读 OneBot 边界已就绪" ""

if ! bash "$SCRIPT_DIR/update_instance.sh" --instance "$BOT_ID" --enable; then
    finish_failed "instance_deployment" "统一实例更新失败" "bash deploy/wsl/update_instance.sh --instance $BOT_ID --enable"
fi

MAIN_UNIT="chatcopilot@$BOT_ID.service"
if ! systemctl --user is-enabled --quiet "$MAIN_UNIT" \
    || ! systemctl --user is-active --quiet "$MAIN_UNIT"; then
    finish_failed "systemd_main_unit" "$MAIN_UNIT 未同时 enabled/active" "systemctl --user status $MAIN_UNIT"
fi
add_check "systemd_main_unit" "pass" "$MAIN_UNIT enabled/active" ""

if ! probe_cc_connect_main_process "$MAIN_UNIT"; then
    finish_failed "cc_connect_process" "cc-connect 未以固定私有 Node 形成稳定且绑定当前实例的 MainPID" \
        "journalctl --user -u $MAIN_UNIT -n 100"
fi
add_check "cc_connect_process" "pass" "cc-connect 以固定私有 Node 运行，MainPID 在有界观察内稳定且绑定当前实例" ""

if ! probe_relay_boundary; then
    finish_failed "qq_relay" "QQ @ Relay 未通过回环认证与只读 OneBot 探针" \
        "journalctl --user -u $MAIN_UNIT -n 100"
fi
add_check "qq_relay" "pass" "QQ @ Relay 通过回环认证与只读 OneBot 探针" ""

add_check "llm_live_call" "not_tested" "未调用付费模型" "手工向机器人发送一条消息"
add_check "qq_external_send" "not_tested" "未向 QQ 发送外部消息" "手工向机器人发送一条消息"
add_check "qq_inbound_agent_roundtrip" "not_tested" "未执行真实 QQ 入站 Agent 往返" "私聊机器人或在获准群内明确 @ 机器人"

echo
ok "本地部署边界已就绪：$BOT_ID"
echo "请自行私聊机器人，或在获准群内明确 @ 机器人。"
echo "llm_live_call=not_tested"
echo "qq_external_send=not_tested"
echo "qq_inbound_agent_roundtrip=not_tested"
emit_report "ready"
exit "$EXIT_READY"
