#!/usr/bin/env bash
# Internal Bot inventory/update helpers for deploy_console.sh.
# Required host symbols: REPO_ROOT, BOT_UPDATE_COUNT, BOT_UPDATE_FAILURES,
# info, ok, err, and run_or_print.

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    echo "[ERR] console_deploy_bots.sh is an internal library" >&2
    exit 2
fi

read_deploy_value() {
    local bot="$1" name="$2"
    python3 - "$bot" "$name" <<'PY'
import sys
from pathlib import Path

bot = Path(sys.argv[1])
want = sys.argv[2]
section = ""
for raw in bot.read_text(encoding="utf-8", errors="replace").splitlines():
    line = raw.split("#", 1)[0].rstrip()
    if not line.strip():
        continue
    if not raw[:1].isspace() and ":" in line:
        section = line.split(":", 1)[0].strip()
        continue
    if section == "deploy" and raw[:1].isspace() and ":" in line:
        key, value = line.split(":", 1)
        if key.strip() == want:
            print(value.strip().strip('"').strip("'"))
            break
PY
}

update_all_bots() {
    local bot instance wsl_home rc index
    local -A seen_instances=()
    local -a bot_files=()
    local -a instances=()
    local -a destinations=()
    while IFS= read -r -d '' bot; do
        bot_files+=("$bot")
    done < <(
        find "$REPO_ROOT/bots" -mindepth 2 -maxdepth 2 -name bot.yaml \
            -type f -print0 2>/dev/null | sort -z
    )
    if [ "${#bot_files[@]}" -eq 0 ]; then
        err "no BotSpecs found under $REPO_ROOT/bots/*/bot.yaml"
        return 1
    fi

    BOT_UPDATE_COUNT="${#bot_files[@]}"
    info "validating all $BOT_UPDATE_COUNT discovered bot deployment(s) ..."
    for bot in "${bot_files[@]}"; do
        if ! instance="$(read_deploy_value "$bot" instance_id)"; then
            err "cannot read deploy.instance_id from $bot"
            BOT_UPDATE_FAILURES+=("${bot#"$REPO_ROOT"/}")
            continue
        fi
        [ -z "$instance" ] && instance="$(basename "$(dirname "$bot")")"
        if [[ ! "$instance" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$ ]]; then
            err "invalid deploy.instance_id in $bot"
            BOT_UPDATE_FAILURES+=("${bot#"$REPO_ROOT"/}")
            continue
        fi
        if [ -n "${seen_instances[$instance]:-}" ]; then
            err "duplicate deploy.instance_id '$instance': $bot"
            BOT_UPDATE_FAILURES+=("$instance")
            continue
        fi
        if ! wsl_home="$(read_deploy_value "$bot" wsl_home)"; then
            err "cannot read deploy.wsl_home from $bot"
            BOT_UPDATE_FAILURES+=("${bot#"$REPO_ROOT"/}")
            continue
        fi
        [ -z "$wsl_home" ] && wsl_home="~/ChatCopilot-$instance"
        seen_instances["$instance"]="$bot"
        instances+=("$instance")
        destinations+=("$wsl_home")
    done
    if [ "${#BOT_UPDATE_FAILURES[@]}" -gt 0 ]; then
        err "bot inventory validation failed: ${BOT_UPDATE_FAILURES[*]}"
        return 1
    fi

    info "updating all $BOT_UPDATE_COUNT validated bot instance(s) ..."
    for index in "${!bot_files[@]}"; do
        bot="${bot_files[$index]}"
        instance="${instances[$index]}"
        info "updating bot $instance from ${bot#"$REPO_ROOT"/} ..."
        if run_or_print bash "$REPO_ROOT/deploy/wsl/update_instance.sh" \
            --instance "$instance" --src "$REPO_ROOT" \
            --dst "${destinations[$index]}" --bot "$bot"; then
            ok "bot $instance updated and verified"
        else
            rc=$?
            err "bot $instance update failed (exit $rc); continuing with remaining bots"
            BOT_UPDATE_FAILURES+=("$instance")
        fi
    done

    if [ "${#BOT_UPDATE_FAILURES[@]}" -gt 0 ]; then
        err "${#BOT_UPDATE_FAILURES[@]} of $BOT_UPDATE_COUNT bot update(s) failed: ${BOT_UPDATE_FAILURES[*]}"
        return 1
    fi
    ok "all $BOT_UPDATE_COUNT bot instance(s) updated"
}
