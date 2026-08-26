#!/usr/bin/env bash
# update_instance.sh - one-click update for an AgentStrata bot instance.
#
# Runs the adaptive update path for an instance:
#   1. Generate runtime env from bots/<id>/local.env.
#   2. Sync code from the control repo to the instance wsl_home.
#   3. Rebuild dependencies only when their inputs changed; otherwise render config.
#   4. Restart chatcopilot@<id>; with --enable, enable it only after a successful start.
set -uo pipefail

INSTANCE=""
SRC=""
SYNC_SRC=""
DST=""
BOT=""
DRY_RUN=0
ENABLE_SERVICE=0
CHANGED_FILES=""

usage() {
    sed -n '2,16p' "$0"
    cat <<'EOF'

Usage:
  bash deploy/wsl/update_instance.sh --instance <id> [--src ~/ChatCopilot] [--sync-src /tmp/overlay --changed-files manifest.txt] [--dst ~/ChatCopilot-<id>] [--bot bots/<id>/bot.yaml] [--enable] [--dry-run]
EOF
}

expand_path() {
    local value="${1:-}"
    case "$value" in
        "~") printf '%s\n' "$HOME" ;;
        "~/"*) printf '%s\n' "$HOME/${value#"~/"}" ;;
        *) printf '%s\n' "$value" ;;
    esac
}

ensure_source_cli_env() {
    local venv_dir="$SRC/.venv"
    local venv_py="$venv_dir/bin/python"
    local req="$SRC/src/chatcopilot/agent/requirements.txt"
    local force_refresh="${1:-0}"
    local rc

    if [ ! -x "$venv_py" ]; then
        echo "[update] source venv missing, creating: $venv_dir"
        if ! command -v python3 >/dev/null 2>&1; then
            echo "[ERR] 未找到 python3，无法创建 $venv_dir" >&2
            return 1
        fi
        python3 -m venv "$venv_dir"
        rc=$?
        if [ "$rc" -ne 0 ]; then
            echo "[ERR] 创建 venv 失败：$venv_dir" >&2
            echo "      请确认 WSL 已安装 python3-venv，或检查 Python 环境。" >&2
            return "$rc"
        fi
    fi

    if [ "$force_refresh" = 1 ] || ! "$venv_py" -c "import yaml" >/dev/null 2>&1; then
        if [ "$force_refresh" = 1 ]; then
            echo "[update] refreshing source CLI requirements for full update"
        else
            echo "[update] source venv missing required CLI dependencies, installing requirements"
        fi
        if [ ! -f "$req" ]; then
            echo "[ERR] requirements not found: $req" >&2
            return 1
        fi
        "$venv_py" -m pip install --quiet --upgrade pip
        rc=$?
        if [ "$rc" -ne 0 ]; then
            echo "[ERR] pip upgrade failed in $venv_dir" >&2
            echo "      请检查网络或 pip 源配置。" >&2
            return "$rc"
        fi
        "$venv_py" -m pip install --quiet -r "$req"
        rc=$?
        if [ "$rc" -ne 0 ]; then
            echo "[ERR] 依赖安装失败：$req" >&2
            echo "      请检查网络或 pip 源配置。" >&2
            return "$rc"
        fi
    fi

    PY="$venv_py"
}

fail_stage() {
    local stage="$1"
    local rc="${2:-1}"
    if [ "$rc" -eq 0 ]; then
        rc=1
    fi
    echo "[ERR] $stage failed (exit $rc)" >&2
    exit "$rc"
}

# Compare the inputs that can change the installed Python environment. Source
# code itself is loaded from the synchronized runtime tree, so it does not need
# another pip install while the existing venv is healthy.
REBUILD_INPUTS=(
    "pyproject.toml"
    "requirements.txt"
    "src/chatcopilot/agent/requirements.txt"
    "src/chatcopilot/middleware/acp/requirements.txt"
    "deploy/wsl/bootstrap_wsl.sh"
    "deploy/wsl/setup_wsl_user.sh"
)
UPDATE_MODE="fast"
UPDATE_REASON="runtime venv and dependency inputs are unchanged"
UPDATE_ERROR=""

select_update_mode() {
    local rel source_file runtime_file selected

    if [ ! -x "$DST/.venv/bin/python" ]; then
        UPDATE_MODE="full"
        UPDATE_REASON="runtime venv is missing"
    fi

    for rel in "${REBUILD_INPUTS[@]}"; do
        selected=1
        if [ -n "$CHANGED_FILES" ] && ! grep -Fqx "$rel" "$CHANGED_FILES"; then
            selected=0
        fi
        source_file="$SYNC_SRC/$rel"
        if [ "$selected" -eq 1 ] && [ ! -f "$source_file" ]; then
            UPDATE_ERROR="$rel is selected but missing from update source"
            return
        fi
        runtime_file="$DST/$rel"
        if [ ! -f "$runtime_file" ]; then
            if [ "$selected" -eq 0 ]; then
                UPDATE_ERROR="$rel is missing from runtime but absent from changed-files manifest; rerun with a complete update source"
                return
            fi
            if [ "$UPDATE_MODE" = "fast" ]; then
                UPDATE_MODE="full"
                UPDATE_REASON="$rel is missing from runtime"
            fi
            continue
        fi
        if [ "$selected" -eq 0 ]; then
            continue
        fi
        if ! cmp -s "$source_file" "$runtime_file"; then
            if [ "$UPDATE_MODE" = "fast" ]; then
                UPDATE_MODE="full"
                UPDATE_REASON="$rel changed"
            fi
        fi
    done
}

validate_provision_bot_consistency() {
    if [ -z "$CHANGED_FILES" ] || [ -z "$BOT_REL" ]; then
        return
    fi
    EXPECTED_BOT="$INSTANCE_BOT"
    if grep -Fqx "$BOT_REL" "$CHANGED_FILES"; then
        EXPECTED_BOT="$SYNC_SRC/$BOT_REL"
    fi
    if [ ! -f "$EXPECTED_BOT" ]; then
        echo "[ERR] expected BotSpec is missing for changed-files update: $EXPECTED_BOT" >&2
        fail_stage "provision runtime env" 1
    fi
    if ! cmp -s "$BOT_FOR_CMD" "$EXPECTED_BOT"; then
        echo "[ERR] BotSpec used for provision differs from the changed-files deployment target: $BOT_REL" >&2
        echo "      Recreate the changed-files update so runtime env and deployed BotSpec use the same content." >&2
        fail_stage "provision runtime env" 1
    fi
}

while [ $# -gt 0 ]; do
    case "$1" in
        --instance) INSTANCE="${2:-}"; shift 2 ;;
        --src) SRC="${2:-}"; shift 2 ;;
        --sync-src) SYNC_SRC="${2:-}"; shift 2 ;;
        --changed-files) CHANGED_FILES="${2:-}"; shift 2 ;;
        --dst) DST="${2:-}"; shift 2 ;;
        --bot) BOT="${2:-}"; shift 2 ;;
        --enable) ENABLE_SERVICE=1; shift ;;
        --dry-run|-n) DRY_RUN=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "[ERR] unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

if [ -z "$INSTANCE" ]; then
    echo "[ERR] missing --instance <id>" >&2
    exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" >/dev/null 2>&1 && pwd)"
DEFAULT_SRC="$(cd "$SCRIPT_DIR/../.." >/dev/null 2>&1 && pwd)"
SRC="$(expand_path "${SRC:-$DEFAULT_SRC}")"
SYNC_SRC="$(expand_path "${SYNC_SRC:-$SRC}")"
DST="$(expand_path "${DST:-$HOME/ChatCopilot-$INSTANCE}")"
BOT="${BOT:-bots/$INSTANCE/bot.yaml}"

if [[ "$BOT" = ~* ]]; then
    BOT="$(expand_path "$BOT")"
fi

BOT_FOR_CMD="$BOT"
BOT_REL="$BOT"
if [[ "$BOT" != /* ]]; then
    BOT_FOR_CMD="$SRC/$BOT"
else
    case "$BOT" in
        "$SRC"/*) BOT_REL="${BOT#"$SRC"/}" ;;
        *) BOT_REL="" ;;
    esac
fi
INSTANCE_BOT="$BOT"
if [[ "$BOT" = /* ]]; then
    case "$BOT" in
        "$SRC"/*) INSTANCE_BOT="$DST/${BOT#"$SRC"/}" ;;
        *) INSTANCE_BOT="$BOT" ;;
    esac
else
    INSTANCE_BOT="$DST/$BOT"
fi

LOCAL_ENV="$(dirname "$BOT_FOR_CMD")/local.env"

echo "[update] instance: $INSTANCE"
echo "[update] src:      $SRC"
echo "[update] sync src: $SYNC_SRC"
echo "[update] dst:      $DST"
echo "[update] bot:      $BOT_FOR_CMD"
echo "[update] env:      $LOCAL_ENV"

if [ ! -d "$SRC/src/chatcopilot" ] || [ ! -f "$SRC/pyproject.toml" ]; then
    echo "[ERR] src does not look like AgentStrata repo: $SRC" >&2
    exit 1
fi
if [ ! -f "$BOT_FOR_CMD" ]; then
    echo "[ERR] bot spec not found: $BOT_FOR_CMD" >&2
    exit 1
fi
if [ ! -f "$LOCAL_ENV" ]; then
    echo "[ERR] local env not found: $LOCAL_ENV" >&2
    echo "      Create it from local.env.example and fill real secrets, then rerun." >&2
    exit 1
fi
if [ ! -f "$SRC/deploy/wsl/sync_code.sh" ]; then
    echo "[ERR] sync script not found: $SRC/deploy/wsl/sync_code.sh" >&2
    exit 1
fi
if [ -n "$CHANGED_FILES" ] && [ ! -f "$CHANGED_FILES" ]; then
    echo "[ERR] changed-files manifest not found: $CHANGED_FILES" >&2
    exit 1
fi

VENV_PY="$SRC/.venv/bin/python"
select_update_mode
if [ -n "$UPDATE_ERROR" ]; then
    echo "[ERR] $UPDATE_ERROR" >&2
    fail_stage "sync code to instance" 1
fi
validate_provision_bot_consistency
echo "[update] mode:     $UPDATE_MODE ($UPDATE_REASON)"

if [ "$DRY_RUN" = 1 ]; then
    echo "[DRY-RUN] selected update mode: $UPDATE_MODE ($UPDATE_REASON)"
    echo "[DRY-RUN] would ensure source venv: '$SRC/.venv'"
    echo "[DRY-RUN] would create missing venv with: python3 -m venv '$SRC/.venv'"
    echo "[DRY-RUN] would check: '$VENV_PY' -c 'import yaml'"
    if [ "$UPDATE_MODE" = "full" ]; then
        echo "[DRY-RUN] would refresh source CLI deps: '$VENV_PY' -m pip install --quiet -r '$SRC/src/chatcopilot/agent/requirements.txt'"
    else
        echo "[DRY-RUN] would install missing source CLI deps if required: '$VENV_PY' -m pip install --quiet -r '$SRC/src/chatcopilot/agent/requirements.txt'"
    fi
    echo "[DRY-RUN] would export: PYTHONPATH='$SRC/src\${PYTHONPATH:+:\$PYTHONPATH}'"
    echo "[DRY-RUN] would run: '$VENV_PY' -m chatcopilot bot provision-env --bot '$BOT_FOR_CMD'"
    if [ -n "$CHANGED_FILES" ]; then
        echo "[DRY-RUN] would run: bash '$SRC/deploy/wsl/sync_code.sh' --src '$SYNC_SRC' --dst '$DST' --files-from '$CHANGED_FILES'"
    else
        echo "[DRY-RUN] would run: bash '$SRC/deploy/wsl/sync_code.sh' --src '$SYNC_SRC' --dst '$DST'"
    fi
    if [ "$UPDATE_MODE" = "full" ]; then
        echo "[DRY-RUN] would run full rebuild: CHATCOPILOT_BOT_SPEC='$INSTANCE_BOT' CHATCOPILOT_INSTANCE_ID='$INSTANCE' bash '$DST/deploy/wsl/bootstrap_wsl.sh'"
    else
        echo "[DRY-RUN] would run fast config render: CHATCOPILOT_BOT_SPEC='$INSTANCE_BOT' CHATCOPILOT_INSTANCE_ID='$INSTANCE' bash '$DST/deploy/wsl/_apply_config.sh'"
    fi
    echo "[DRY-RUN] would run: bash '$SRC/console/scripts/ctl.sh' restart '$INSTANCE'"
    if [ "$ENABLE_SERVICE" = 1 ]; then
        echo "[DRY-RUN] after a successful restart, would run: systemctl --user enable 'chatcopilot@$INSTANCE.service'"
    fi
    exit 0
fi

echo "[update] step 1/4: provision runtime env"
if [ "$UPDATE_MODE" = "full" ]; then
    ensure_source_cli_env 1
else
    ensure_source_cli_env
fi
rc=$?
if [ "$rc" -ne 0 ]; then
    fail_stage "provision runtime env" "$rc"
fi
validate_provision_bot_consistency
(
    cd "$SRC" || exit 1
    export PYTHONPATH="$SRC/src${PYTHONPATH:+:$PYTHONPATH}"
    "$PY" -m chatcopilot bot provision-env --bot "$BOT_FOR_CMD"
)
rc=$?
if [ "$rc" -ne 0 ]; then
    fail_stage "provision runtime env" "$rc"
fi

echo "[update] step 2/4: sync code to instance"
if [ -n "$CHANGED_FILES" ]; then
    bash "$SRC/deploy/wsl/sync_code.sh" --src "$SYNC_SRC" --dst "$DST" --files-from "$CHANGED_FILES"
else
    bash "$SRC/deploy/wsl/sync_code.sh" --src "$SYNC_SRC" --dst "$DST"
fi
rc=$?
if [ "$rc" -ne 0 ]; then
    fail_stage "sync code to instance" "$rc"
fi

if [ "$UPDATE_MODE" = "full" ]; then
    echo "[update] step 3/4: rebuild environment"
    BOOTSTRAP="$DST/deploy/wsl/bootstrap_wsl.sh"
    if [ ! -f "$BOOTSTRAP" ]; then
        echo "[ERR] bootstrap script not found after sync: $BOOTSTRAP" >&2
        fail_stage "rebuild environment" 1
    fi
    (
        cd "$DST" || exit 1
        CHATCOPILOT_BOT_SPEC="$INSTANCE_BOT" CHATCOPILOT_INSTANCE_ID="$INSTANCE" bash "$BOOTSTRAP"
    )
    rc=$?
    if [ "$rc" -ne 0 ]; then
        fail_stage "rebuild environment" "$rc"
    fi
else
    echo "[update] step 3/4: render runtime config"
    APPLY_CONFIG="$DST/deploy/wsl/_apply_config.sh"
    if [ ! -f "$APPLY_CONFIG" ]; then
        echo "[ERR] config render script not found after sync: $APPLY_CONFIG" >&2
        fail_stage "render runtime config" 1
    fi
    (
        cd "$DST" || exit 1
        CHATCOPILOT_BOT_SPEC="$INSTANCE_BOT" CHATCOPILOT_INSTANCE_ID="$INSTANCE" bash "$APPLY_CONFIG"
    )
    rc=$?
    if [ "$rc" -ne 0 ]; then
        fail_stage "render runtime config" "$rc"
    fi
fi

echo "[update] step 4/4: restart service"
CTL="$SRC/console/scripts/ctl.sh"
if [ ! -f "$CTL" ]; then
    echo "[ERR] control script not found: $CTL" >&2
    fail_stage "restart service" 1
fi
if ! command -v systemctl >/dev/null 2>&1; then
    echo "[ERR] systemctl not found" >&2
    fail_stage "restart service" 1
fi
UNIT="chatcopilot@${INSTANCE}.service"
CODE_WORKER_UNIT="chatcopilot-code-worker@${INSTANCE}.service"
REGISTER="$SRC/console/systemd/register.sh"
if [ ! -f "$REGISTER" ]; then
    echo "[ERR] service register script is missing: $REGISTER" >&2
    fail_stage "register service" 1
fi
user_uid="$(id -u)"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$user_uid}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=/run/user/$user_uid/bus}"
bash "$REGISTER" "$INSTANCE"
rc=$?
if [ "$rc" -ne 0 ]; then
    fail_stage "register service" "$rc"
fi
bash "$CTL" restart "$INSTANCE"
rc=$?
if [ "$rc" -ne 0 ]; then
    fail_stage "restart service" "$rc"
fi
if ! systemctl --user is-active --quiet "$UNIT"; then
    echo "[ERR] service is not active after restart: $UNIT" >&2
    fail_stage "restart service" 1
fi
if [ "$ENABLE_SERVICE" = 1 ]; then
    if ! systemctl --user enable "$UNIT" "$CODE_WORKER_UNIT"; then
        echo "[ERR] services started but could not be enabled: $UNIT / $CODE_WORKER_UNIT" >&2
        fail_stage "enable services" 1
    fi
    echo "[OK] services enabled after successful restart: $UNIT / $CODE_WORKER_UNIT"
fi
if systemctl --user cat "$CODE_WORKER_UNIT" >/dev/null 2>&1; then
    if ! systemctl --user restart "$CODE_WORKER_UNIT"; then
        echo "[ERR] code worker failed to restart: $CODE_WORKER_UNIT" >&2
        fail_stage "restart code worker" 1
    fi
fi

echo "[OK] update complete: $INSTANCE"
