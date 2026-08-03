#!/usr/bin/env bash
# setup_console.sh — 一次性把「运维控制台」装成 WSL 原生 systemd 服务。
#
# 跑完后控制台后端开机自启，浏览器开 http://localhost:8910 即可，无需再手动
# python -m console.backend。日常在 WSL 源仓直接运行 deploy/wsl/deploy_console.sh
# 或 deploy/wsl/update_instance.sh。
#
# 做的事：
#   1. 建 venv + 装 console/requirements.txt
#   2. npm ci + build 前端（产物 console/web/dist，后端同源托管）
#   3. 渲染并安装 systemd --user 单元 chatcopilot-console.service（路径指向本仓库）
#   4. 开 lingering + enable --now
#
# 用法（WSL 终端，在控制仓库根或 console/ 下均可）：
#   bash console/setup_console.sh
#   bash console/setup_console.sh --skip-web   # 跳过前端 build（仅调后端时）
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." >/dev/null 2>&1 && pwd)"
USER_UNIT_DIR="$HOME/.config/systemd/user"
UNIT_NAME="chatcopilot-console.service"
TEMPLATE="$SCRIPT_DIR/systemd/$UNIT_NAME"

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
    echo "  systemctl --user status $UNIT_NAME --no-pager -l" >&2
    echo "  journalctl --user -u $UNIT_NAME --no-pager -n 120" >&2
    echo "  bash $REPO_ROOT/deploy/wsl/deploy_console.sh --status" >&2
}

uid="$(id -u)"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$uid}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=/run/user/$uid/bus}"

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

if [ ! -d "$REPO_ROOT/console" ] || [ ! -f "$REPO_ROOT/console/requirements.txt" ]; then
    err "这里不像 AgentStrata 控制仓库：$REPO_ROOT（缺 console/requirements.txt）"
    exit 1
fi
info "控制仓库：$REPO_ROOT"

# ---- 1. venv + 依赖 ----
VENV="$REPO_ROOT/.venv"
if [ ! -x "$VENV/bin/python" ]; then
    info "建 venv：$VENV"
    python3 -m venv "$VENV" || { err "python3 -m venv 失败（缺 python3-venv？）"; exit 1; }
fi
info "安装控制台依赖..."
"$VENV/bin/python" -m pip install --upgrade pip >/dev/null 2>&1 || true
if ! "$VENV/bin/python" -m pip install -r "$REPO_ROOT/console/requirements.txt"; then
    err "pip install 失败"
    exit 1
fi
ok "venv 与依赖就位"

info "控制台 Python 导入自检..."
if ! PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}" "$VENV/bin/python" - <<'PY'
import console.backend.app  # noqa: F401
import console.control.cli  # noqa: F401
import console.control.yaml_editor  # noqa: F401
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

# ---- 3. 渲染并安装 systemd 单元（把 %h/ChatCopilot 替换成真实仓库路径）----
if [ ! -f "$TEMPLATE" ]; then
    err "找不到单元模板：$TEMPLATE"
    exit 1
fi
mkdir -p "$USER_UNIT_DIR"
sed "s#%h/ChatCopilot#$REPO_ROOT#g" "$TEMPLATE" > "$USER_UNIT_DIR/$UNIT_NAME"
ok "已安装单元：$USER_UNIT_DIR/$UNIT_NAME"

# ---- 4. lingering + enable --now ----
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
if ! systemctl --user enable "$UNIT_NAME" 2>/dev/null; then
    err "enable 失败。"
    print_service_diagnostics
    exit 1
fi
if systemctl --user restart "$UNIT_NAME" 2>/dev/null; then
    ok "控制台已重启并设为开机自启"
else
    err "restart 失败。"
    print_service_diagnostics
    exit 1
fi

echo
ok "完成。浏览器打开： http://localhost:8910"
echo "  查看状态：systemctl --user status $UNIT_NAME"
echo "  看日志：  journalctl --user -u $UNIT_NAME -f"
