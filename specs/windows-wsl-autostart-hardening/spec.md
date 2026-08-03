---
id: windows-wsl-autostart-hardening
type: architecture
status: implemented
created: 2026-07-23
---

# Windows 到 WSL 自启动加固

## Summary

- [KNOWN][HIGH] systemd user unit、linger、Docker restart policy 只能在 WSL 已启动后生效；Windows 冷启动后必须有宿主触发器唤醒目标发行版。
- [KNOWN][HIGH] AgentStrata 使用当前 Windows 用户拥有的 WSL distro，不使用 SYSTEM/最高权限，也不保存 Windows 密码。
- [KNOWN][HIGH] QQ Bot 在 OneBot 强 token 验收前继续保持 systemd disabled；安装 WSL 唤醒任务不得绕过服务自身门禁。

## Design

- [INFERRED][HIGH] 提供幂等 PowerShell installer，在当前用户 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` 注册隐藏 PowerShell launcher；launcher 保存在 `%LOCALAPPDATA%\ChatCopilot` 并执行 `%SystemRoot%\System32\wsl.exe -d <distro> --exec /bin/true`。
- [INFERRED][HIGH] installer 在注册前验证 distro 存在并限制 distro 名字符；不使用 Task Scheduler，因为当前主机的 limited interactive task 实测返回 `0xFFFFFFFF`，而同一动作交互执行成功。
- [INFERRED][HIGH] 同一脚本提供 `-Status`、`-Probe` 与 `-Uninstall`，只操作精确 Run value、launcher/status 文件和本规格旧版创建的精确 task；状态输出不包含凭据。
- [INFERRED][HIGH] WSL 内仍由 systemd user unit、linger 与 Docker `restart: unless-stopped` 决定实际服务；Windows HKCU Run launcher 只负责唤醒 distro。

## Acceptance

- [KNOWN][HIGH] 当前用户登录时通过隐藏 PowerShell launcher 启动指定 distro，不要求管理员、不持久化密码。
- [KNOWN][HIGH] 重复安装更新同一 Run value/launcher，不创建重复项；`-Status` 可报告命令与最近结果，`-Probe` 可即时验收，`-Uninstall` 可精确移除。
- [KNOWN][HIGH] `-Probe` 返回成功，WSL 可响应，Docker/systemd 按各自启用状态恢复。
- [KNOWN][HIGH] QQ Bot service 若 disabled 或 token 门禁失败，Windows launcher 不会把它强行启动。
- [KNOWN][HIGH] token 验收后使用 `update_instance.sh --enable`；脚本必须先完成 provision、同步、重建和成功 restart，最后才设置 unit enabled。

## Verification

- [COMPUTED][HIGH] Task Scheduler 方案曾成功注册为 Interactive/Limited，但手工触发返回 `0xFFFFFFFF`；同一 `wsl.exe` 命令交互执行返回 `0`，因此该方案已被证伪并必须移除。
- [COMPUTED][HIGH] PowerShell 脚本由 `-File` 成功解析执行；静态契约测试通过，重复安装只保留一个 HKCU Run value，旧 `ChatCopilot-Start-WSL` task 已不存在。
- [COMPUTED][HIGH] 安装后的实际 launcher 内容调用 `%SystemRoot%\System32\wsl.exe -d Ubuntu-22.04 --exec /bin/true`；`-Probe` 返回 `0`，`-Status` 记录最近 `exit_code=0`。
- [COMPUTED][HIGH] WSL 内 Docker service 为 enabled、user linger 为 `yes`；QQ service 因强 token 尚未验收而保持 disabled/inactive，Windows launcher 未绕过该门禁。
