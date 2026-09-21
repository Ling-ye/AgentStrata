#!/usr/bin/env bash
# Internal systemd user-manager helpers for deploy_console.sh.
# Required host symbols: uid, DRY_RUN, DBUS_SESSION_BUS_ADDRESS,
# info, ok, warn, err, and run_or_print.

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    echo "[ERR] console_deploy_systemd.sh is an internal library" >&2
    exit 2
fi

user_systemd_ready() {
    systemctl --user show-environment >/dev/null 2>&1
}

print_user_systemd_recovery() {
    local user_unit="user@$uid.service"
    echo "  检查：systemctl status $user_unit --no-pager -l" >&2
    echo "  修复：sudo systemctl restart $user_unit" >&2
    echo "  若 restart 失败：" >&2
    echo "    sudo systemctl reset-failed $user_unit" >&2
    echo "    sudo systemctl start $user_unit" >&2
}

wait_for_user_systemd() {
    local attempt
    for attempt in $(seq 1 10); do
        if user_systemd_ready; then
            return 0
        fi
        sleep 1
    done
    return 1
}

recover_user_systemd() {
    local user_unit="user@$uid.service"
    local pid1
    local -a privileged=()

    pid1="$(ps -p 1 -o comm= 2>/dev/null || true)"
    if [ "$pid1" != "systemd" ]; then
        err "PID 1 不是 systemd（当前为：${pid1:-unknown}），无法恢复 systemd user bus。"
        echo "  请先在 /etc/wsl.conf 启用 systemd，从 Windows 执行 wsl --shutdown 后重试。" >&2
        return 1
    fi
    if ! dpkg -s dbus-user-session >/dev/null 2>&1; then
        err "缺少 dbus-user-session，无法恢复 systemd user bus。"
        echo "  请先执行：sudo apt-get install -y dbus-user-session" >&2
        return 1
    fi

    if [ "$DRY_RUN" -eq 1 ]; then
        warn "systemd user bus 当前不可用；dry-run 仅展示恢复动作"
        if [ "$uid" -eq 0 ]; then
            run_or_print systemctl restart "$user_unit"
            run_or_print sleep 1
            run_or_print systemctl reset-failed "$user_unit"
            run_or_print systemctl start "$user_unit"
        else
            run_or_print sudo systemctl restart "$user_unit"
            run_or_print sleep 1
            run_or_print sudo systemctl reset-failed "$user_unit"
            run_or_print sudo systemctl start "$user_unit"
        fi
        return 0
    fi

    if [ "$uid" -ne 0 ]; then
        if ! command -v sudo >/dev/null 2>&1; then
            err "缺少 sudo，无法重启 $user_unit。"
            print_user_systemd_recovery
            return 1
        fi
        privileged=(sudo -n)
    fi

    warn "systemd user bus 不可用；将重启当前用户的 systemd manager"
    if [ "$uid" -ne 0 ]; then
        info "需要 sudo 权限恢复 $user_unit ..."
        if [ -t 0 ]; then
            if ! sudo -v; then
                err "未取得 sudo 权限，无法恢复 systemd user bus。"
                print_user_systemd_recovery
                return 1
            fi
        elif ! sudo -n -v; then
            err "非交互环境没有可用的 sudo 凭据，无法恢复 systemd user bus。"
            print_user_systemd_recovery
            return 1
        fi
    fi

    if ! "${privileged[@]}" systemctl restart "$user_unit"; then
        warn "$user_unit restart 失败；等待 1 秒后执行 reset-failed + start"
        sleep 1
        if ! "${privileged[@]}" systemctl reset-failed "$user_unit"; then
            err "$user_unit reset-failed 失败。"
            print_user_systemd_recovery
            return 1
        fi
        if ! "${privileged[@]}" systemctl start "$user_unit"; then
            err "$user_unit start 失败。"
            print_user_systemd_recovery
            return 1
        fi
    fi

    info "waiting for systemd user bus ..."
    if ! wait_for_user_systemd; then
        err "systemd user bus 自动恢复后仍不可达：$DBUS_SESSION_BUS_ADDRESS"
        print_user_systemd_recovery
        return 1
    fi
    ok "systemd --user 已自动恢复"
}

check_systemd_available() {
    local mode="${1:-repair}"
    if ! command -v systemctl >/dev/null 2>&1; then
        err "systemctl not found. Enable WSL systemd first."
        return 1
    fi
    if user_systemd_ready; then
        ok "systemd --user is available"
        return 0
    fi
    if [ "$mode" = "readonly" ]; then
        err "systemd user bus 不可达：$DBUS_SESSION_BUS_ADDRESS"
        print_user_systemd_recovery
        return 1
    fi
    recover_user_systemd
}
