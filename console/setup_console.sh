#!/usr/bin/env bash
# setup_console.sh — 一次性把「运维控制台」装成 WSL 原生 systemd 服务。
#
# 跑完后控制台后端开机自启，浏览器开 http://localhost:8910 即可，无需再手动
# python -m console.backend。日常在 WSL 源仓直接运行 deploy/wsl/deploy_console.sh
# 或 deploy/wsl/update_instance.sh。
#
# 做的事：
#   1. 从 uv.lock 对账 Console Python 环境
#   2. npm ci + build 前端（产物 console/web/dist，后端同源托管）
#   3. 渲染并安装 Evaluation / Console 两个 systemd --user 单元
#   4. 开 lingering，先启动并验证 Evaluation，再启动 Console
#
# 用法（WSL 终端，在控制仓库根或 console/ 下均可）：
#   bash console/setup_console.sh
#   bash console/setup_console.sh --skip-web   # 跳过前端 build（仅调后端时）
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." >/dev/null 2>&1 && pwd)"
USER_UNIT_DIR="$HOME/.config/systemd/user"
UNIT_NAME="chatcopilot-console.service"
EVALUATION_UNIT_NAME="chatcopilot-evaluation.service"
TEMPLATE="$SCRIPT_DIR/systemd/$UNIT_NAME"
EVALUATION_TEMPLATE="$SCRIPT_DIR/systemd/$EVALUATION_UNIT_NAME"
EVALUATION_ROOT="$REPO_ROOT/reports/evals/evaluations"
MAINTENANCE_HELD=0
MAINTENANCE_LEASE_ID=""

SKIP_WEB=0
for arg in "$@"; do
    case "$arg" in
        --skip-web) SKIP_WEB=1 ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        *) echo "[ERR] 未知参数：$arg" >&2; exit 2 ;;
    esac
done

info() { printf "\033[1;36m[*]\033[0m %s\n" "$*"; }
ok()   { printf "\033[1;32m[OK]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[WARN]\033[0m %s\n" "$*"; }
err()  { printf "\033[1;31m[ERR]\033[0m %s\n" "$*" >&2; }

print_service_diagnostics() {
    echo >&2
    warn "排查命令："
    echo "  systemctl --user status $EVALUATION_UNIT_NAME --no-pager -l" >&2
    echo "  journalctl --user -u $EVALUATION_UNIT_NAME --no-pager -n 120" >&2
    echo "  systemctl --user status $UNIT_NAME --no-pager -l" >&2
    echo "  journalctl --user -u $UNIT_NAME --no-pager -n 120" >&2
    echo "  bash $REPO_ROOT/deploy/wsl/deploy_console.sh --status" >&2
}

uid="$(id -u)"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$uid}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=/run/user/$uid/bus}"
EVALUATION_SOCKET="$XDG_RUNTIME_DIR/agentstrata-evaluation/service.sock"

wait_for_evaluation() {
    local attempt
    for attempt in $(seq 1 20); do
        if "$VENV/bin/python" -m chatcopilot.evals.service health \
            --socket "$EVALUATION_SOCKET" >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done
    return 1
}

maintenance_enter() {
    MAINTENANCE_LEASE_ID="$(
        PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}" \
            "$VENV/bin/python" -m chatcopilot.evals.service maintenance enter \
            --socket "$EVALUATION_SOCKET"
    )" || return 1
    if [[ ! "$MAINTENANCE_LEASE_ID" =~ ^[0-9a-f]{32}$ ]]; then
        MAINTENANCE_LEASE_ID=""
        return 1
    fi
    MAINTENANCE_HELD=1
}

maintenance_leave() {
    if [ "$MAINTENANCE_HELD" -ne 1 ] || [ -z "$MAINTENANCE_LEASE_ID" ]; then
        return 0
    fi
    if [ ! -x "$VENV/bin/python" ] || ! \
        PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}" \
            "$VENV/bin/python" -m chatcopilot.evals.service maintenance leave \
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
        err "无法释放 Evaluation maintenance lease：$lease_id"
        echo "  恢复 Evaluation service 后执行：" >&2
        echo "  $VENV/bin/python -m chatcopilot.evals.service maintenance leave --socket $EVALUATION_SOCKET --lease-id $lease_id" >&2
        status=1
    fi
    exit "$status"
}

if ! dpkg -s dbus-user-session >/dev/null 2>&1; then
    err "缺少 dbus-user-session，systemctl --user 没有稳定的用户总线。"
    echo "  修复：sudo apt-get install -y dbus-user-session" >&2
    echo "  然后重开 WSL，或执行：sudo systemctl restart user@$uid.service" >&2
    exit 1
fi
user_systemd_state="$(systemctl --user is-system-running 2>/dev/null || true)"
case "$user_systemd_state" in
running|degraded|starting) ;;
*)
    err "systemd user bus 不可达：$DBUS_SESSION_BUS_ADDRESS"
    echo "  检查：systemctl status user@$uid.service --no-pager -l" >&2
    echo "  修复：sudo systemctl restart user@$uid.service" >&2
    echo "  若返回 219/CGROUP，等待 1 秒后执行：" >&2
    echo "    sudo systemctl reset-failed user@$uid.service" >&2
    echo "    sudo systemctl start user@$uid.service" >&2
    exit 1
    ;;
esac

RUNTIME_INSTALLER="$REPO_ROOT/deploy/wsl/install_wsl_env.sh"
if [ ! -d "$REPO_ROOT/console" ] || [ ! -f "$REPO_ROOT/uv.lock" ] || \
    [ ! -f "$RUNTIME_INSTALLER" ]; then
    err "这里不像 AgentStrata 控制仓库：$REPO_ROOT（缺 uv.lock 或运行环境安装器）"
    exit 1
fi
info "控制仓库：$REPO_ROOT"

# ---- 1. venv + 依赖 ----
VENV="$REPO_ROOT/.venv"
evaluation_unit_installed=0
if [ -f "$USER_UNIT_DIR/$EVALUATION_UNIT_NAME" ] || \
    systemctl --user cat "$EVALUATION_UNIT_NAME" >/dev/null 2>&1; then
    evaluation_unit_installed=1
fi
if [ "$evaluation_unit_installed" -eq 1 ]; then
    if ! systemctl --user is-active --quiet "$EVALUATION_UNIT_NAME"; then
        err "Evaluation service 已安装但未运行；无法证明空闲，拒绝更新运行代码。"
        echo "  请先恢复 $EVALUATION_UNIT_NAME，再重试。" >&2
        exit 1
    fi
    if [ ! -x "$VENV/bin/python" ]; then
        err "Evaluation service 已安装，但现有 Python 环境不可用；拒绝绕过空闲证明。"
        exit 1
    fi
    if ! maintenance_enter; then
        err "检测到活动 Evaluation、已有维护租约或无法证明服务空闲；拒绝更新运行代码。"
        echo "  请等待评测结束或先通过 Console 取消，再重试。" >&2
        exit 1
    fi
    trap release_maintenance_on_exit EXIT
elif systemctl --user is-active --quiet "$UNIT_NAME"; then
    err "首次安装 Evaluation service 时检测到仍在运行的旧 Console；无法排除旧 manager 创建评测。"
    echo "  请先停止 $UNIT_NAME，确认没有活动评测，再重试。" >&2
    exit 1
elif [ -d "$EVALUATION_ROOT" ] && \
    find "$EVALUATION_ROOT" -maxdepth 1 \
        \( -name '.active-*.json' -o -name '.maintenance.json' \) \
        -print -quit | grep -q .; then
    err "首次安装前发现活动 claim 或 maintenance marker；拒绝更新运行代码。"
    exit 1
fi
info "从 uv.lock 对账控制仓库与 Console 依赖..."
if ! bash "$RUNTIME_INSTALLER" --no-system-packages --skip-cc-connect \
    --with-console-deps --venv "$VENV" --no-verify; then
    err "锁定 Console Python 环境安装失败"
    exit 1
fi
ok "锁定 Python 环境与 Console 依赖就位"

info "控制台 Python 导入自检..."
if ! PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}" "$VENV/bin/python" - <<'PY'
import console.backend.app  # noqa: F401
import console.control.cli  # noqa: F401
import console.control.yaml_editor  # noqa: F401
import chatcopilot.evals.service  # noqa: F401
import chatcopilot.platforms.base  # noqa: F401
PY
then
    err "控制台导入自检失败。通常是 src-layout 路径或依赖缺失。"
    echo "  可手动复现：" >&2
    echo "  PYTHONPATH=$REPO_ROOT/src $VENV/bin/python -c 'import console.backend.app'" >&2
    exit 1
fi
ok "控制台导入自检通过"

# ---- 2. 前端 build ----
if [ "$SKIP_WEB" -eq 1 ]; then
    warn "已 --skip-web：跳过前端 build（确保 console/web/dist 已存在，否则后端无界面可托管）"
elif command -v npm >/dev/null 2>&1; then
    info "构建前端（npm ci + build）..."
    if npm --prefix "$REPO_ROOT/console/web" ci && npm --prefix "$REPO_ROOT/console/web" run build; then
        ok "前端已构建到 console/web/dist"
    else
        err "前端构建失败（可先 --skip-web 装后端，稍后再 build）"
        exit 1
    fi
else
    warn "未找到 npm，跳过前端 build。请在能装 Node 的环境构建 console/web 后再访问界面。"
fi

# ---- 3. 私有 Evaluation 目录 + systemd 单元 ----
if ! EVALUATION_ROOT="$EVALUATION_ROOT" python3 - <<'PY'
import os
import stat
from pathlib import Path

target = Path(os.environ["EVALUATION_ROOT"])
current = Path(target.anchor)
for part in target.parts[1:]:
    current /= part
    try:
        metadata = current.lstat()
    except FileNotFoundError:
        continue
    if stat.S_ISLNK(metadata.st_mode):
        raise SystemExit(1)
PY
then
    err "拒绝包含符号链接祖先的 Evaluation 目录：$EVALUATION_ROOT"
    exit 1
fi
if ! mkdir -p "$EVALUATION_ROOT" || ! chmod 700 "$EVALUATION_ROOT"; then
    err "无法创建私有 Evaluation 目录：$EVALUATION_ROOT"
    exit 1
fi

if [ ! -f "$TEMPLATE" ] || [ ! -f "$EVALUATION_TEMPLATE" ]; then
    err "找不到单元模板：$TEMPLATE 或 $EVALUATION_TEMPLATE"
    exit 1
fi
mkdir -p "$USER_UNIT_DIR"
sed "s#%h/ChatCopilot#$REPO_ROOT#g" "$TEMPLATE" > "$USER_UNIT_DIR/$UNIT_NAME"
sed "s#%h/ChatCopilot#$REPO_ROOT#g" "$EVALUATION_TEMPLATE" \
    > "$USER_UNIT_DIR/$EVALUATION_UNIT_NAME"
chmod 644 "$USER_UNIT_DIR/$UNIT_NAME" "$USER_UNIT_DIR/$EVALUATION_UNIT_NAME"
ok "已安装单元：$USER_UNIT_DIR/$UNIT_NAME"
ok "已安装单元：$USER_UNIT_DIR/$EVALUATION_UNIT_NAME"

# ---- 4. lingering + Evaluation 健康门禁 + Console ----
if command -v loginctl >/dev/null 2>&1; then
    linger="$(loginctl show-user "$USER" -p Linger 2>/dev/null | cut -d= -f2)"
    if [ "$linger" != "yes" ]; then
        loginctl enable-linger "$USER" 2>/dev/null \
            && ok "已开启 lingering（关终端 / WSL 重启后控制台仍自启）" \
            || warn "无法自动开启 lingering，请手动：sudo loginctl enable-linger $USER"
    else
        ok "lingering 已开启"
    fi
fi

if ! systemctl --user daemon-reload 2>/dev/null; then
    warn "daemon-reload 失败（检查 XDG_RUNTIME_DIR）"
fi
if ! systemctl --user enable "$EVALUATION_UNIT_NAME" "$UNIT_NAME" 2>/dev/null; then
    err "enable 失败。"
    print_service_diagnostics
    exit 1
fi
if ! systemctl --user restart "$EVALUATION_UNIT_NAME" 2>/dev/null; then
    err "Evaluation service restart 失败。"
    print_service_diagnostics
    exit 1
fi
if ! wait_for_evaluation; then
    err "Evaluation service Unix socket 健康检查失败：$EVALUATION_SOCKET"
    print_service_diagnostics
    exit 1
fi
ok "Evaluation service 已重启并通过 Unix socket 健康检查"

if ! systemctl --user restart "$UNIT_NAME" 2>/dev/null; then
    err "Console restart 失败。"
    print_service_diagnostics
    exit 1
fi
ok "控制台已重启；两个服务均已设为开机自启"

if ! maintenance_leave; then
    err "服务已更新，但 Evaluation maintenance lease 仍处于活动状态。"
    exit 1
fi
trap - EXIT

echo
ok "完成。浏览器打开： http://localhost:8910"
echo "  Evaluation：systemctl --user status $EVALUATION_UNIT_NAME"
echo "  查看状态：systemctl --user status $UNIT_NAME"
echo "  看日志：  journalctl --user -u $UNIT_NAME -f"
