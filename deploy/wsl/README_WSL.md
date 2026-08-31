# WSL 手动排障

这份文档只处理正常运维入口无法解决的 WSL 异常。日常安装后操作、状态、更新、重启、
日志、QQ gateway、Codex 认证和诊断命令统一见
[`../../docs/operations.md`](../../docs/operations.md)；首次安装与数据布局见
[`../../docs/deployment.md`](../../docs/deployment.md)。

所有 Linux 命令默认在 AgentStrata 源仓根目录执行。不要把 Windows 进程的当前目录
设为 `\\wsl.localhost\...` 后再启动项目命令。Windows 原生不是受支持的部署面；
PowerShell 片段只负责进入 WSL 或唤醒发行版。

## 源仓位于 `/mnt/c`

引导部署会拒绝从 Windows 挂载文件系统运行，因为 Unix owner、`0600`、原子替换和
systemd 使用的路径语义不能在那里得到可靠保证。保留原目录用于备份，在 WSL 的 Linux
文件系统重新克隆后运行引导入口：

```bash
cd "$HOME"
git clone https://github.com/Ling-ye/AgentStrata.git
cd AgentStrata
bash deploy/wsl/quickstart.sh
```

不要通过复制 `local.env` 到公共或 Windows 路径来迁移秘密；在新目录重新运行向导填写，
或用受限权限的本机秘密传输方式单独迁移。

## 先收集证据

先使用稳定入口，不要直接修改实例副本：

```bash
python -m console.control status --instance <id> --json
systemctl --user status chatcopilot@<id>.service --no-pager -l
journalctl --user -u chatcopilot@<id>.service -n 120 --no-page
bash deploy/wsl/dump.sh --instance <id> --mode quick
```

Codex 隔离任务还要检查：

```bash
systemctl --user status chatcopilot-code-worker@<id>.service --no-pager -l
journalctl --user -u chatcopilot-code-worker@<id>.service -n 120 --no-page
```

确认失败阶段后再进入下面对应章节。

## systemd user bus 不可用

先检查 PID 1：

```bash
ps -p 1 -o comm=
```

若结果不是 `systemd`，按 [`../../docs/deployment.md#WSL-systemd`](../../docs/deployment.md#wsl-systemd)
启用 systemd、从 Windows 执行 `wsl --shutdown`，重新打开 WSL 后再运行
`bash deploy/wsl/quickstart.sh --resume`。不要尝试在当前 shell 内启动一个替代 init。

PID 1 是 systemd 也不代表当前用户的 bus 可用：

```bash
systemctl is-system-running
systemctl --user is-system-running
test -S "/run/user/$(id -u)/bus"
dpkg -s dbus-user-session
```

系统 manager 正常、但用户命令报 `Failed to connect to bus` 时：

```bash
sudo apt-get install -y dbus-user-session
sudo systemctl restart "user@$(id -u).service"
```

WSL 长时间运行后，第一次重启 user manager 可能短暂返回
`219/CGROUP: Device or resource busy`。等待一秒，再执行：

```bash
sudo systemctl reset-failed "user@$(id -u).service"
sudo systemctl start "user@$(id -u).service"
systemctl --user is-system-running
```

引导安装会安装 `dbus-user-session`；`setup_wsl_root.sh --legacy-feishu` 只用于 Feishu legacy edge。
`console/setup_console.sh` 在注册服务前会拒绝缺包或不可达的 user bus。

## 实例启动失败

前台复现部署后实例的启动过程：

```bash
cd ~/ChatCopilot-<id>/deploy/wsl
bash start.sh --apply-config
bash status.sh --instance <id>
```

`~/ChatCopilot-<id>` 是现有默认 `deploy.wsl_home`；实例声明了其他路径时使用实际值。
不要在实例副本中修代码或配置，修复应发生在源仓，然后运行统一更新入口。

常见信号：

- `local.env` 缺键：回到源仓补齐 `bots/<id>/local.env`，运行 `bot doctor` 和
  `update_instance.sh`。不要复制共享 `deploy/wsl/env.example` 代替 Bot 自己的模板。
- 选中了错误 BotSpec：用当前源码重新运行 `console/systemd/register.sh <id>`。主 unit
  必须显式固定实例 ID 与部署后的 BotSpec 路径，不能从同一部署树任取第一个文件。
- 运行时 env 不一致：用 `python -m chatcopilot bot provision-env --bot
  bots/<id>/bot.yaml --dry-run` 检查，再运行实例更新；不要手写
  `~/.chatcopilot-<id>.env`。

## QQ 收到消息但不回复

按顺序检查：

```bash
bash deploy/wsl/qq_gateway.sh status --instance <id>
bash deploy/wsl/qq_gateway.sh logs --instance <id>
systemctl --user status chatcopilot@<id>.service --no-pager -l
journalctl --user -u chatcopilot@<id>.service -n 120 --no-page
```

只有 BotSpec 启用 `dev.code_tasks` 时才检查 `chatcopilot-code-worker@<id>.service`；
通用 starter 不运行 worker。

常见信号：

- `OneBot upstream unavailable`：在 localhost WebUI 完成 NapCat 登录，并启用 `3001`
  的 OneBot 正向 WebSocket。
- token 缺失或错配：回到
  [`../../docs/operations.md#qq--napcat`](../../docs/operations.md#qq--napcat)
  执行 `sync-token`，不要手工拼接 token 同步命令。
- 启动报告 `QQ_REQUIRE_AT_IN_GROUP` 或 `QQ_AT_ALL_COUNTS` 已废弃：从 bot-local
  `local.env` 删除该键，再更新实例；群聊明确 @ 是 Gateway Channel 的固定触发条件。
- 配置群号后仍只有个别用户可用：确认群号写在私有 `QQ_ALLOW_GROUPS`，没有误写到只
  接受发送者 QQ 号的 `QQ_ALLOW_FROM`，再更新实例并检查 Gateway 的授权与 task evidence。
- OneBot 健康但 Bot service 失败：检查主 service 的 journald 中 Gateway、模型和 Channel
  错误，并确认 systemd `MainPID` 是当前实例的 `python -m chatcopilot run --bot ...`。

## Feishu legacy edge 的 cc-connect 修复

出现 `EACCES`、`Auto-install failed`、系统 Node 版本漂移或
`/usr/lib/node_modules/cc-connect` 时，不要改用 root/global npm。此段只适用于明确部署的
Feishu legacy edge；QQ Gateway 路径不会安装或启动 Node/cc-connect。重新对账 legacy 工具链：

```bash
bash deploy/wsl/install_wsl_env.sh
bash deploy/wsl/update_instance.sh --instance <id>
```

Bot 私有 env 中的 `CHATCOPILOT_CC_CONNECT_BIN` 应指向引导安装生成的项目私有固定版本
可执行文件。不要修改 shell profile，也不要让 systemd 依赖 root 全局 npm 安装。

## code-worker 启动失败

`chatcopilot-code-worker@<id>` 若以 `218/CAPABILITIES` 循环退出，说明已安装的用户
unit 仍含 WSL 不支持的 capability 加固项：

```bash
bash console/systemd/register.sh <id>
bash console/scripts/ctl.sh restart <id>
```

当前注册脚本会移除 WSL 不支持的 `ProtectKernelLogs` / `ProtectKernelModules`，保留
其他兼容加固，并只把允许的 Codex 配置传入 worker。QQ、LLM 等平台凭据不会进入
worker 环境。

managed Codex lane 报认证错误时，不要复制桌面 `auth.json`。使用
[`../../docs/operations.md#codex-main--worker-认证`](../../docs/operations.md#codex-main--worker-认证)
的登录和状态命令。

## Windows PowerShell 入口

从普通 Windows 目录调用 `wsl.exe`，再让 WSL 内部切换到 Linux 源仓：

```powershell
$env:CHATCOPILOT_WSL_REPO = '~/ChatCopilot'
wsl -d Ubuntu-22.04 --exec bash -lc 'cd "$CHATCOPILOT_WSL_REPO" && git status --short'
```

`~/ChatCopilot` 是兼容默认值；源码仓在其他位置时显式设置
`CHATCOPILOT_WSL_REPO`。可选 helper 还读取 `CHATCOPILOT_WSL_DISTRO`，默认
`Ubuntu-22.04`。

临时调用：

```powershell
& '\\wsl.localhost\Ubuntu-22.04\home\<user>\ChatCopilot\deploy\wsl\win\ccwsl.ps1' git status
& '\\wsl.localhost\Ubuntu-22.04\home\<user>\ChatCopilot\deploy\wsl\win\ccwsl.ps1' 'git status --short && pytest'
```

安装到 PowerShell profile：

```powershell
& '\\wsl.localhost\Ubuntu-22.04\home\<user>\ChatCopilot\deploy\wsl\win\install-ccwsl-profile.ps1'
```

新开 PowerShell 后：

```powershell
ccwsl git status
ccwsl pytest
ccwsl bash deploy/docker/services.sh status
```

需要由 bash 解释 `&&`、`|` 或重定向时，把完整命令作为一个字符串传给 `ccwsl`。

## 诊断包边界

`dump.sh` 默认写入 `_wsl_debug/<timestamp>/`，并脱敏 prompt、工具参数、平台标识、
认证路径与 secret。只有明确需要时才加 `--include-env`；无论是否启用，都要在分享前
人工检查归档。任务级证据优先使用 `console.control diagnose`，不要先扩大到全实例 env。
