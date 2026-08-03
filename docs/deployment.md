# Linux / WSL 部署

AgentStrata 的生产脚本以 WSL/Linux 为运行面。本文只说明部署拓扑、首次安装、安全边界
和数据位置；安装完成后的更新、重启、日志、平台网关和诊断命令统一见
[`operations.md`](operations.md)。

## 拓扑与事实源

```text
AgentStrata 源仓
  -> 唯一应提交的工作区；BotSpec、prompt、代码、部署脚本

共享基础设施
  -> Console、Docker MCP、平台 gateway

实例副本（deploy.wsl_home）
  -> 实例 venv、渲染 env、cc-connect、ACP runtime、systemd user service
```

源仓通过 `deploy/wsl/sync_code.sh` 单向同步到实例副本。运行时 env 由
`bots/<id>/local.env` 与 BotSpec 生成；不要手工修改实例副本或
`~/.chatcopilot-<id>.env`。`local.env` 是机器私有文件，不进入 Git。

[KNOWN][HIGH] `chatcopilot` namespace、`CHATCOPILOT_*` 环境变量、systemd unit 名
与默认 `~/ChatCopilot*` 实例路径是兼容契约。它们不是当前产品品牌，不能为了统一
文案直接重命名。

## 前置条件

- Linux，或启用了 systemd 的 WSL 发行版。
- Python 3.10–3.13、Git、可用的模型与聊天平台账号。
- 使用 Console 时需要 Node/npm；使用共享 MCP 时需要 Docker。
- 所有项目命令在 Linux/WSL 源仓中运行，不从 `\\wsl.localhost\...` 作为 Windows
  进程当前目录启动。

Python 依赖以根目录 `pyproject.toml` 为事实源。分层 `requirements.txt` 是由
`scripts/sync_requirements.py` 生成的部署兼容清单，不应手工修改。

## 首次安装

准备源仓 venv、Node/npm、cc-connect、Console 依赖和 systemd user service：

```bash
bash deploy/wsl/install_wsl_env.sh --with-console
```

一键部署 Console、Docker MCP 和内置实例：

```bash
bash deploy/wsl/deploy_all.sh
```

先预览，或跳过暂不需要的组件：

```bash
bash deploy/wsl/deploy_all.sh --dry-run
bash deploy/wsl/deploy_all.sh --skip-docke
bash deploy/wsl/deploy_all.sh --skip-bots
bash deploy/wsl/deploy_all.sh --docker-timeout 60
```

等价的手动顺序：

```bash
bash deploy/wsl/install_wsl_env.sh --with-console
bash deploy/docker/services.sh start

bash deploy/wsl/update_instance.sh --instance lingye-copilot-qq --enable

bash console/systemd/register.sh --enable lingye-copilot-qq

bash console/scripts/ctl.sh start lingye-copilot-qq
```

Bot 是否自启由 `systemctl --user enable chatcopilot@<id>` 决定。一键脚本或 installe
失败后不要跳过失败阶段继续；按输出修复后重跑相同入口。

## 平台上线边界

### QQ / OneBot

[KNOWN][HIGH] OneBot `3001` 和 NapCat WebUI `6099` 只绑定 `127.0.0.1`。
`QQ_ACCESS_TOKEN` 必须是 32–128 位 URL-safe 强 token；WebUI 管理 token 是另一个
凭据，只用于登录 localhost 管理面板。

首次上线按“bootstrap → WebUI 登录 → sync-token → gateway start → instance update”
执行，完整命令见 [`operations.md#qq--napcat`](operations.md#qq--napcat)。
`sync-token` 只原子更新 Bot 私有 env 的对应键并保留其他配置。正式 start、restart、
status 和实例启用都必须通过双向 OneBot 动作探针；空/弱 token、非回环 URL、未登录
NapCat 或认证失败时 fail closed，不能先停止健康服务再尝试修复。

### Codex backend

[KNOWN][HIGH] managed `worktree` / `workspace` 使用实例私有的
`CHATCOPILOT_CODEX_BOT_HOME`。main 和 worker 拥有不同的权威 `auth.json`，必须分别
完成 device auth；不得导入或回退桌面/个人 `.codex`。Owner `worktree` 还必须在 ignored
`local.env` 配置 GitHub repository、fine-grained token 与 Git author，并重新执行
`console/systemd/register.sh`，让部署流程把 token 物化为 worker 专用 mode `0600` 文件。
完整登录、token 权限与重注册命令见
[`operations.md#codex-main--worker-认证`](operations.md#codex-main--worker-认证)。
[KNOWN][HIGH] `host` 与 `auto_publish` 已删除；code-worker 只从远端干净基线交付草稿 PR，不覆盖源仓、不 merge、不部署或重启。角色、credential generation 或 caller 策略变化会使旧 resume ID 失效。

## Windows 冷启动唤醒 WSL

[KNOWN][HIGH] WSL 内的 systemd、user linger 和 Docker restart policy 不会唤醒尚未
启动的发行版。installer 在当前用户的 HKCU Run 下注册隐藏 PowerShell launcher；它只
执行 `wsl.exe -d Ubuntu-22.04 --exec /bin/true`，不保存密码、不使用 SYSTEM/最高权限，
也不直接启动某个 Bot。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File "\\wsl.localhost\Ubuntu-22.04\home\<user>\ChatCopilot\deploy\wsl\win\install-wsl-autostart.ps1"

# 检查、探针或卸载
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File "\\wsl.localhost\Ubuntu-22.04\home\<user>\ChatCopilot\deploy\wsl\win\install-wsl-autostart.ps1" -Status
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File "\\wsl.localhost\Ubuntu-22.04\home\<user>\ChatCopilot\deploy\wsl\win\install-wsl-autostart.ps1" -Probe
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File "\\wsl.localhost\Ubuntu-22.04\home\<user>\ChatCopilot\deploy\wsl\win\install-wsl-autostart.ps1" -Uninstall
```

上述 `ChatCopilot` 路径是既有默认部署路径；仓库位于其他位置时替换为真实 WSL
路径。launcher 只唤醒发行版，Bot 是否自启仍由 systemd user unit 决定。QQ 强 token
尚未验收时应保持相应 unit disabled。

## 数据与路径

以 `<id>` 表示实例：

| 类型 | 位置 | 所有者 |
| --- | --- | --- |
| 源 BotSpec | `bots/<id>/bot.yaml` | Git 源仓 |
| 私有 env | `bots/<id>/local.env` | 操作者；不进 Git |
| 实例副本 | `deploy.wsl_home`，默认 `~/ChatCopilot-<id>` | 部署脚本 |
| 运行时 env | `~/.chatcopilot-<id>.env` | `provision-env` |
| workspace | `~/chatcopilot-workspaces/<id>` | 运行时 |
| 日志 | `~/chatcopilot-logs/<id>` | cc-connect / runtime |
| cc-connect home | `~/.chatcopilot-runtime/<id>` | cc-connect |
| Evaluation | `reports/evals/evaluations/<evaluation-id>/` | Evaluation Core |

不要在代码或版本化 YAML 中写机器绝对路径。机器路径经 BotSpec 的 `root_env` 或私有
env 提供。

## Secret 与第三方能力

- `provision-env` 不 source 或执行 `local.env`；只解析简单的 `KEY=value` /
  `export KEY=value`。值开头的 `~`、`$HOME`、`${HOME}` 会确定性展开，其他变量引用
  和命令替换保持字面量。
- secret 只放 `bots/<id>/local.env`、`deploy/docker/.env` 或机器 credential store。
- MCP YAML 使用 `${ENV_NAME}` 引用 secret，不写真实值。
- `local.env.example` 与 `.env.example` 只提供变量名和安全占位符。
- AgentStrata 不自动下载、安装或启用第三方 MCP/Skill。操作者审核源码、许可证、
  启动方式、secret 引用和远端写行为后，手工安装并绑定已审阅服务。

## 部署验收

```bash
python -m chatcopilot botspec validate bots/lingye-copilot-qq/bot.yaml
bash deploy/wsl/deploy_console.sh --status
bash deploy/docker/services.sh status
python -m console.control list --json
```

广泛修改运行时、打包、部署或 Console 后，再运行：

```bash
.venv/bin/python scripts/check_repo.py full
```

安装后的日常操作统一回到 [`operations.md`](operations.md)。只有正常入口无法恢复时，
再使用 [`../deploy/wsl/README_WSL.md`](../deploy/wsl/README_WSL.md) 的手动排障步骤。
