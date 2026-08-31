#!/usr/bin/env bash
# setup_wsl_root.sh — Feishu legacy edge 的 root 依赖入口。
# QQ Gateway 必须使用 quickstart；只有明确维护 legacy edge 时传 --legacy-feishu。
set -euo pipefail

if [ "${1:-}" != "--legacy-feishu" ]; then
    echo "[ERR] 此脚本只保留给 Feishu legacy edge：sudo bash $0 --legacy-feishu" >&2
    echo "[ERR] QQ Gateway 请运行 deploy/wsl/quickstart.sh；不会安装系统 Node/cc-connect。" >&2
    exit 2
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "[ERR] 本脚本需要用 sudo 跑：sudo bash $0" >&2
    exit 1
fi

# 真正执行 npm/node 用户态命令的用户（脚本以 sudo 调起，SUDO_USER 是原用户）
REAL_USER="${SUDO_USER:-$USER}"
echo "[INFO] 目标用户: $REAL_USER"

# 1. APT 基础包
APT_PKGS=(
    python3 python3-pip python3-venv python3-dev
    build-essential curl unzip jq ca-certificates rsync
    dbus-user-session
)

NEED_INSTALL=()
for p in "${APT_PKGS[@]}"; do
    if ! dpkg -s "$p" >/dev/null 2>&1; then
        NEED_INSTALL+=("$p")
    fi
done

if [ ${#NEED_INSTALL[@]} -gt 0 ]; then
    echo "[INFO] apt 缺少: ${NEED_INSTALL[*]}"
    apt update
    apt install -y "${NEED_INSTALL[@]}"
    echo "[OK] apt 基础包就绪"
else
    echo "[SKIP] apt 基础包已就绪"
fi

# 2. NodeSource Node.js 20.x（Ubuntu 自带的可能太旧，cc-connect 要 18+）
NODE_OK=0
if command -v node >/dev/null 2>&1; then
    NODE_VER=$(node --version | sed 's/v//')
    NODE_MAJ=${NODE_VER%%.*}
    if [ "$NODE_MAJ" -ge 18 ] 2>/dev/null; then
        NODE_OK=1
    fi
fi

if [ "$NODE_OK" -eq 0 ]; then
    echo "[INFO] 安装 NodeSource Node.js 20.x"
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
    apt install -y nodejs
    echo "[OK] Node.js: $(node --version) / npm: $(npm --version)"
else
    echo "[SKIP] Node.js 已就绪（$(node --version)）"
fi

echo
echo "[OK] root 阶段完成。下一步：回到 PowerShell，让 Cursor 继续跑用户态步骤。"
