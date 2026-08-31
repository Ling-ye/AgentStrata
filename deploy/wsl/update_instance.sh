#!/usr/bin/env bash
# update_instance.sh - one-click update for an AgentStrata bot instance.
#
# Runs the adaptive update path for an instance:
#   1. Generate runtime env from bots/<id>/local.env.
#   2. Sync code from the control repo to the instance wsl_home.
#   3. Reconcile the locked runtime on every update, rebuilding through bootstrap
#      when dependency inputs changed and using an idempotent frozen sync otherwise.
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
    local installer="$SRC/deploy/wsl/install_wsl_env.sh"
    if [ ! -f "$installer" ]; then
        echo "[ERR] locked runtime installer not found: $installer" >&2
        return 1
    fi
    echo "[update] reconciling source CLI from uv.lock"
    bash "$installer" --no-system-packages --skip-cc-connect \
        --venv "$venv_dir" --no-verify
    local refresh_rc=$?
    if [ "$refresh_rc" -ne 0 ]; then
        echo "[ERR] locked source CLI sync failed: $venv_dir" >&2
        return "$refresh_rc"
    fi
    if [ ! -x "$venv_py" ]; then
        echo "[ERR] locked source CLI sync did not produce Python: $venv_py" >&2
        return 1
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

read_code_worker_requirement() {
    local python_bin="$1"
    local bot_path="$2"
    CHATCOPILOT_REQUIREMENT_BOT="$bot_path" "$python_bin" - <<'PY'
import os
from pathlib import Path

import yaml

path = Path(os.environ["CHATCOPILOT_REQUIREMENT_BOT"])
data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
if not isinstance(data, dict):
    raise SystemExit(f"invalid BotSpec mapping: {path}")
tools = data.get("tools") or {}
if not isinstance(tools, dict):
    raise SystemExit(f"invalid tools mapping in BotSpec: {path}")
packs = tools.get("packs") or []
if not isinstance(packs, list) or not all(
    isinstance(pack, str) for pack in packs
):
    raise SystemExit(f"invalid tools.packs in BotSpec: {path}")
print("1" if "dev.code_tasks" in packs else "0")
PY
}

bot_uses_gateway() {
    local bot_path="$1"
    [ -r "$bot_path" ] || return 1
    awk '
        /^[^[:space:]#][^:]*:[[:space:]]*$/ {
            key = $0
            sub(/:.*/, "", key)
            if (key == "gateway") found = 1
        }
        END { exit(found ? 0 : 1) }
    ' "$bot_path"
}

# Compare the inputs that can change the installed Python environment. Source
# code itself is loaded from the synchronized runtime tree, so only the locked
# dependency inputs trigger another frozen runtime sync.
REBUILD_INPUTS=(
    "pyproject.toml"
    "uv.lock"
    "deploy/wsl/install_wsl_env.sh"
    "deploy/wsl/node-tools/package.json"
    "deploy/wsl/node-tools/package-lock.json"
    "deploy/wsl/bootstrap_wsl.sh"
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
GATEWAY_BACKED=0
if bot_uses_gateway "$BOT_FOR_CMD"; then
    GATEWAY_BACKED=1
fi
RUNTIME_INSTALL_ARGS=(--no-system-packages --venv "$DST/.venv" --no-verify)
if [ "$GATEWAY_BACKED" -eq 1 ]; then
    RUNTIME_INSTALL_ARGS+=(--skip-cc-connect)
fi
select_update_mode
if [ -n "$UPDATE_ERROR" ]; then
    echo "[ERR] $UPDATE_ERROR" >&2
    fail_stage "sync code to instance" 1
fi
validate_provision_bot_consistency
echo "[update] mode:     $UPDATE_MODE ($UPDATE_REASON)"

if [ "$DRY_RUN" = 1 ]; then
    echo "[DRY-RUN] selected update mode: $UPDATE_MODE ($UPDATE_REASON)"
    echo "[DRY-RUN] BotSpec dev.code_tasks requirement: deferred until locked source CLI reconciliation"
    echo "[DRY-RUN] would ensure source venv from: '$SRC/uv.lock'"
    echo "[DRY-RUN] would run locked installer: bash '$SRC/deploy/wsl/install_wsl_env.sh' --no-system-packages --skip-cc-connect --venv '$SRC/.venv' --no-verify"
    echo "[DRY-RUN] would reconcile source CLI with uv sync --frozen before executing '$VENV_PY'"
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
        echo "[DRY-RUN] would reconcile the locked runtime before runtime-config check"
        if [ "$GATEWAY_BACKED" -eq 1 ]; then
            echo "[DRY-RUN] would run: bash '$DST/deploy/wsl/install_wsl_env.sh' --no-system-packages --venv '$DST/.venv' --no-verify --skip-cc-connect"
            echo "[DRY-RUN] Gateway path does not render cc-connect/session-env configuration"
        else
            echo "[DRY-RUN] would run: bash '$DST/deploy/wsl/install_wsl_env.sh' --no-system-packages --venv '$DST/.venv' --no-verify"
            echo "[DRY-RUN] would run legacy config render: CHATCOPILOT_BOT_SPEC='$INSTANCE_BOT' CHATCOPILOT_INSTANCE_ID='$INSTANCE' bash '$DST/deploy/wsl/_apply_config.sh'"
        fi
    fi
    echo "[DRY-RUN] would run: bash '$SRC/console/scripts/ctl.sh' restart '$INSTANCE'"
    if [ "$ENABLE_SERVICE" = 1 ]; then
        echo "[DRY-RUN] after a successful restart, would run: systemctl --user enable 'chatcopilot@$INSTANCE.service'"
    fi
    exit 0
fi

echo "[update] step 1/4: provision runtime env"
ensure_source_cli_env
rc=$?
if [ "$rc" -ne 0 ]; then
    fail_stage "provision runtime env" "$rc"
fi
if ! REQUIRES_CODE_WORKER="$(read_code_worker_requirement "$PY" "$BOT_FOR_CMD")"; then
    fail_stage "resolve code worker requirement" 1
fi
case "$REQUIRES_CODE_WORKER" in
    0|1) ;;
    *)
        echo "[ERR] invalid code worker requirement for $BOT_FOR_CMD" >&2
        fail_stage "resolve code worker requirement" 1
        ;;
esac
echo "[update] code worker required: $REQUIRES_CODE_WORKER"
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
    echo "[update] step 3/4: reconcile locked runtime and runtime config"
    RUNTIME_INSTALLER="$DST/deploy/wsl/install_wsl_env.sh"
    APPLY_CONFIG="$DST/deploy/wsl/_apply_config.sh"
    if [ ! -f "$RUNTIME_INSTALLER" ]; then
        echo "[ERR] locked runtime installer not found after sync: $RUNTIME_INSTALLER" >&2
        fail_stage "reconcile runtime" 1
    fi
    if [ ! -f "$APPLY_CONFIG" ]; then
        echo "[ERR] config render script not found after sync: $APPLY_CONFIG" >&2
        fail_stage "render runtime config" 1
    fi
    (
        cd "$DST" || exit 1
        bash "$RUNTIME_INSTALLER" "${RUNTIME_INSTALL_ARGS[@]}" \
            || exit $?
        if [ "$GATEWAY_BACKED" -eq 1 ]; then
            echo "[update] Gateway instance: no cc-connect/session-env render"
        else
            CHATCOPILOT_BOT_SPEC="$INSTANCE_BOT" CHATCOPILOT_INSTANCE_ID="$INSTANCE" bash "$APPLY_CONFIG"
        fi
    )
    rc=$?
    if [ "$rc" -ne 0 ]; then
        fail_stage "reconcile runtime and render config" "$rc"
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
if [ "$REQUIRES_CODE_WORKER" = 1 ]; then
    if ! systemctl --user cat "$CODE_WORKER_UNIT" >/dev/null 2>&1; then
        echo "[ERR] required code worker unit is not registered: $CODE_WORKER_UNIT" >&2
        fail_stage "restart code worker" 1
    fi
    if ! systemctl --user restart "$CODE_WORKER_UNIT"; then
        echo "[ERR] code worker failed to restart: $CODE_WORKER_UNIT" >&2
        fail_stage "restart code worker" 1
    fi
    if ! systemctl --user is-active --quiet "$CODE_WORKER_UNIT"; then
        echo "[ERR] code worker is not active after restart: $CODE_WORKER_UNIT" >&2
        fail_stage "restart code worker" 1
    fi
fi
if [ "$ENABLE_SERVICE" = 1 ]; then
    if ! systemctl --user enable "$UNIT"; then
        echo "[ERR] service started but could not be enabled: $UNIT" >&2
        fail_stage "enable services" 1
    fi
    if [ "$REQUIRES_CODE_WORKER" = 1 ] \
        && ! systemctl --user enable "$CODE_WORKER_UNIT"; then
        echo "[ERR] code worker started but could not be enabled: $CODE_WORKER_UNIT" >&2
        fail_stage "enable services" 1
    fi
    if [ "$REQUIRES_CODE_WORKER" = 1 ]; then
        echo "[OK] services enabled after successful restart: $UNIT / $CODE_WORKER_UNIT"
    else
        echo "[OK] service enabled after successful restart: $UNIT (code worker not applicable)"
    fi
fi

echo "[OK] update complete: $INSTANCE"
