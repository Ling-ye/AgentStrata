#!/usr/bin/env bash
# sync_code.sh — WSL 原生「源仓库 -> 实例 wsl_home」单向代码同步
#
# 控制台「更新代码并重启」按钮的底层：在 WSL 内按实例把 AgentStrata 控制仓库 ~/ChatCopilot
# 的代码推送到各实例 wsl_home（~/ChatCopilot-<id>），不必绕回 Windows。
#
# 用法（WSL 终端 / 控制台后端）：
#   bash sync_code.sh --src "$HOME/AgentStrata" --dst "$HOME/AgentStrata-sample-bot"
#   bash sync_code.sh --dst "$HOME/AgentStrata-sample-bot"          # src 默认本脚本所在仓库
#   bash sync_code.sh --src /tmp/overlay --dst ... --files-from changed_files.txt
#   bash sync_code.sh --dst ... --dry-run                                  # 只看清单不改
#
# 退出码：0 成功；非 0 失败（rsync / 后处理失败）。
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" >/dev/null 2>&1 && pwd)"
CONTROL_ROOT="$(cd "$SCRIPT_DIR/../.." >/dev/null 2>&1 && pwd)"
SRC=""
DST=""
DRY_RUN=0
FILES_FROM=""

while [ $# -gt 0 ]; do
    case "$1" in
        --src) SRC="${2:-}"; shift 2 ;;
        --dst) DST="${2:-}"; shift 2 ;;
        --files-from) FILES_FROM="${2:-}"; shift 2 ;;
        --dry-run|-n) DRY_RUN=1; shift ;;
        -h|--help) sed -n '2,21p' "$0"; exit 0 ;;
        *) echo "[ERR] 未知参数：$1（用 --help 看用法）" >&2; exit 2 ;;
    esac
done

# src 默认 = 本脚本所在仓库根（deploy/wsl/sync_code.sh -> ../..）
if [ -z "$SRC" ]; then
    SRC="$CONTROL_ROOT"
fi

if [ -z "$DST" ]; then
    echo "[ERR] 必须用 --dst 指定目标 wsl_home" >&2
    exit 2
fi

# 展开 ~ 前缀
case "$DST" in
    "~") DST="$HOME" ;;
    "~/"*) DST="$HOME/${DST#~/}" ;;
esac

if [ -z "$FILES_FROM" ] && { [ ! -d "$SRC/src/chatcopilot" ] || [ ! -d "$SRC/deploy/wsl" ] || [ ! -f "$SRC/pyproject.toml" ]; }; then
    echo "[ERR] 源目录不像 AgentStrata 仓库：$SRC" >&2
    echo "      期望存在 src/chatcopilot、deploy/wsl、pyproject.toml" >&2
    exit 1
fi
if [ -n "$FILES_FROM" ]; then
    if [ ! -f "$FILES_FROM" ]; then
        echo "[ERR] changed-files manifest not found: $FILES_FROM" >&2
        exit 1
    fi
    while IFS= read -r rel || [ -n "$rel" ]; do
        case "$rel" in
            ""|/*|../*|*/../*|*/..|.)
                echo "[ERR] invalid changed-files entry: $rel" >&2
                exit 1
                ;;
        esac
    done < "$FILES_FROM"
fi

# 护栏：dst 不得等于源（控制仓库）目录，否则会把控制台自身代码覆盖/删改，
# 等于让「更新机器人」误伤控制台。实例必须有独立 wsl_home（如 ~/ChatCopilot-<id>）。
src_real="$(realpath -m "$SRC")"
dst_real="$(realpath -m "$DST")"
if [ "$src_real" = "$dst_real" ]; then
    echo "[ERR] 目标等于源（控制仓库）目录，拒绝同步以防覆盖控制台：$dst_real" >&2
    echo "      请把该实例 bot.yaml 的 deploy.wsl_home 设为独立目录，再重跑 console/systemd/register.sh <id>。" >&2
    exit 1
fi

if ! command -v rsync >/dev/null 2>&1; then
    echo "[ERR] 未找到 rsync。安装：sudo apt install -y rsync" >&2
    exit 1
fi

MANIFEST_PY="$CONTROL_ROOT/src"
if [ ! -f "$MANIFEST_PY/chatcopilot/core/source_manifest.py" ]; then
    echo "[ERR] canonical source manifest module not found: $MANIFEST_PY" >&2
    exit 1
fi
TRANSFER_MANIFEST="$(mktemp)"
CURRENT_MANIFEST="$(mktemp)"
cleanup_manifests() {
    rm -f "$TRANSFER_MANIFEST" "$CURRENT_MANIFEST"
}
trap cleanup_manifests EXIT
if [ -n "$FILES_FROM" ]; then
    PYTHONPATH="$MANIFEST_PY${PYTHONPATH:+:$PYTHONPATH}" python3 \
        -m chatcopilot.core.source_manifest \
        --source "$SRC" --paths-from "$FILES_FROM" \
        --include-missing --output "$TRANSFER_MANIFEST" || exit $?
    PYTHONPATH="$MANIFEST_PY${PYTHONPATH:+:$PYTHONPATH}" python3 \
        -m chatcopilot.core.source_manifest \
        --source "$SRC" --paths-from "$FILES_FROM" \
        --output "$CURRENT_MANIFEST" || exit $?
else
    PYTHONPATH="$MANIFEST_PY${PYTHONPATH:+:$PYTHONPATH}" python3 \
        -m chatcopilot.core.source_manifest \
        --source "$SRC" --output "$CURRENT_MANIFEST" || exit $?
    cp "$CURRENT_MANIFEST" "$TRANSFER_MANIFEST"
fi

# 只同步中央 Git manifest 中的 tracked 与 untracked-nonignored 文件。构建目录、
# 评测报告、scratch、任务目录、local.env 与认证文件由同一 Python 规则统一排除。
RSYNC_FLAGS=(
    -av --checksum --delete-missing-args --ignore-missing-args
    --files-from="$TRANSFER_MANIFEST"
)
[ "$DRY_RUN" = 1 ] && RSYNC_FLAGS=(--dry-run "${RSYNC_FLAGS[@]}")

echo "[$(date +%H:%M:%S)] src: $SRC"
echo "[$(date +%H:%M:%S)] dst: $DST"
echo "[$(date +%H:%M:%S)] rsync 同步中..."
mkdir -p "$DST"
rsync "${RSYNC_FLAGS[@]}" "$SRC/" "$DST/"
rc=$?
if [ "$rc" -ne 0 ]; then
    echo "[ERR] rsync 失败（exit $rc）" >&2
    exit "$rc"
fi

FINALIZE_ARGS=(
    -m chatcopilot.core.source_manifest
    --source "$SRC"
    --output "$CURRENT_MANIFEST"
    --destination "$DST"
    --finalize
)
[ -n "$FILES_FROM" ] && FINALIZE_ARGS+=(--paths-from "$FILES_FROM")
[ "$DRY_RUN" = 1 ] && FINALIZE_ARGS+=(--dry-run)
PYTHONPATH="$MANIFEST_PY${PYTHONPATH:+:$PYTHONPATH}" \
    python3 "${FINALIZE_ARGS[@]}" || exit $?

if [ "$DRY_RUN" = 1 ]; then
    echo "[OK] DryRun 完成（未实际改动）"
    exit 0
fi

# 后处理：修 .sh 的 CRLF + BOM + 可执行位（源在 /mnt 上时常见 Windows 行尾）
echo "[$(date +%H:%M:%S)] 规整 deploy/wsl/*.sh 行尾与可执行位..."
if [ -d "$DST/deploy/wsl" ]; then
    (
        cd "$DST/deploy/wsl" || exit 0
        if command -v dos2unix >/dev/null 2>&1; then
            dos2unix *.sh >/dev/null 2>&1 || true
        else
            sed -i 's/\r$//' *.sh 2>/dev/null || true
        fi
        sed -i '1s/^\xef\xbb\xbf//' *.sh 2>/dev/null || true
        chmod +x *.sh 2>/dev/null || true
    )
fi

echo "[OK] 同步完成：$DST"
