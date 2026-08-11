# AgentStrata 运维手册

这份手册是日常运维命令的唯一集中入口，覆盖安装后的状态检查、更新、重启、日志、
平台网关、Codex 认证、共享 Docker 服务、评测和诊断。首次安装的拓扑与安全设计见
[`deployment.md`](deployment.md)；遇到 systemd user bus、cc-connect 或 WSL/Windows
边界问题时再进入 [`../deploy/wsl/README_WSL.md`](../deploy/wsl/README_WSL.md)。

## 使用约定

除标明 PowerShell 的片段外，命令都在 Linux/WSL 的 **AgentStrata 源仓根目录**
执行。下文的 `<id>` 是 BotSpec 的实例 ID，例如 `lingye-copilot-qq`。

```bash
cd <path-to-AgentStrata>
```

先确认目标实例再执行写操作：

```bash
python -m chatcopilot bot list
python -m chatcopilot bot doctor --bot bots/<id>/bot.yaml
```

`local.env`、平台 token 和模型凭据不得写入命令输出、BotSpec 或 Git。Git 提交与
推送也不属于任何自动更新或发布动作。

## 一页速查

| 目标 | 命令 |
| --- | --- |
| 查看全部实例 | `python -m console.control list --json` |
| 查看实例状态 | `python -m console.control status --instance <id> --json` |
| 预览实例更新 | `bash deploy/wsl/update_instance.sh --instance <id> --dry-run` |
| 更新并重启实例 | `bash deploy/wsl/update_instance.sh --instance <id>` |
| 重启实例 | `bash console/scripts/ctl.sh restart <id>` |
| 查看实例日志 | `journalctl --user -u chatcopilot@<id>.service -n 120 --no-pager` |
| 跟随实例日志 | `journalctl --user -u chatcopilot@<id>.service -f` |
| 查看控制台 | `bash deploy/wsl/deploy_console.sh --status` |
| 更新控制台 | `bash deploy/wsl/deploy_console.sh --update-only` |
| 查看共享 Docker 服务 | `bash deploy/docker/services.sh status` |
| 收集诊断快照 | `bash deploy/wsl/dump.sh --instance <id> --mode quick` |

## 安装与启用

首次准备源仓环境和控制台：

```bash
bash deploy/wsl/install_wsl_env.sh --with-console
```

部署源仓环境、控制台、BotSpec 所需的共享 Docker 服务和内置 Bot 实例：

```bash
bash deploy/wsl/deploy_all.sh
```

先预览一键部署，或跳过部分组件：

```bash
bash deploy/wsl/deploy_all.sh --dry-run
bash deploy/wsl/deploy_all.sh --skip-docker
bash deploy/wsl/deploy_all.sh --skip-bots
```

完整安装说明与手动顺序见 [`deployment.md`](deployment.md)。

## Bot 实例

### 配置与体检

```bash
python -m chatcopilot botspec validate bots/<id>/bot.yaml
python -m chatcopilot bot doctor --bot bots/<id>/bot.yaml
python -m chatcopilot bot provision-env --bot bots/<id>/bot.yaml --dry-run
```

修改 BotSpec、prompt、MCP 绑定、`local.env`、Python 代码或依赖后：

```bash
bash deploy/wsl/update_instance.sh --instance <id> --dry-run
bash deploy/wsl/update_instance.sh --instance <id>
```

更新默认使用快速路径；只有实例 venv 缺失或依赖、安装脚本发生变化时才完整
bootstrap。命令失败会停止后续阶段，不提供自动回滚。首次验收成功并需要开机自启时：

```bash
bash deploy/wsl/update_instance.sh --instance <id> --enable
```

### 生命周期

```bash
bash console/systemd/register.sh --enable <id>
bash console/scripts/ctl.sh start <id>
bash console/scripts/ctl.sh stop <id>
bash console/scripts/ctl.sh restart <id>
bash console/scripts/ctl.sh status <id>
```

直接检查 systemd：

```bash
systemctl --user is-active chatcopilot@<id>.service
systemctl --user status chatcopilot@<id>.service --no-pager -l
journalctl --user -u chatcopilot@<id>.service -n 120 --no-page
```

Codex 隔离代码任务还使用独立 worker：

```bash
systemctl --user status chatcopilot-code-worker@<id>.service --no-pager -l
journalctl --user -u chatcopilot-code-worker@<id>.service -n 120 --no-page
```

## 运维控制台

控制台默认地址为 `http://localhost:8910`。

```bash
bash deploy/wsl/deploy_console.sh --status
bash deploy/wsl/deploy_console.sh --update-only
bash deploy/wsl/deploy_console.sh --restart-only
```

```bash
systemctl --user status chatcopilot-console --no-pager -l
journalctl --user -u chatcopilot-console -f
```

控制台页面、API、任务可观测和评测中心行为见 [`console.md`](console.md)。

## QQ / NapCat

[KNOWN][HIGH] OneBot `3001` 和 WebUI `6099` 只允许绑定回环地址。WebUI 管理
token 只用于登录管理面板，不是 `QQ_ACCESS_TOKEN`。

首次登录或修复回环容器：

```bash
bash deploy/wsl/qq_gateway.sh bootstrap --instance lingye-copilot-qq
```

在 `http://localhost:6099` 完成 NapCat 登录后，同步或生成强 OneBot token，再启动
gateway 和实例：

```bash
bash deploy/wsl/qq_gateway.sh sync-token --instance lingye-copilot-qq
bash deploy/wsl/qq_gateway.sh start --instance lingye-copilot-qq
bash deploy/wsl/update_instance.sh --instance lingye-copilot-qq --enable
```

日常状态、重启和日志：

```bash
bash deploy/wsl/qq_gateway.sh status --instance lingye-copilot-qq
bash deploy/wsl/qq_gateway.sh restart --instance lingye-copilot-qq
bash deploy/wsl/qq_gateway.sh logs --instance lingye-copilot-qq
```

[KNOWN][HIGH] `sync-token` 只原子更新 Bot 私有 `local.env` 中的
`QQ_ACCESS_TOKEN`，保留其他键，并同步运行时 env 与 NapCat `3001` 配置。
`start`、`restart` 和 `status` 都必须通过无 token 拒绝、带 token 可执行 OneBot
动作的双向探针；只完成 WebSocket 握手不算认证成功。

## Codex main / worker 认证

[KNOWN][HIGH] managed `worktree` / `workspace` 使用
`CHATCOPILOT_CODEX_BOT_HOME` 作为实例认证根。main 的权威凭据是根
`auth.json`，worker 的权威凭据是 `worker/auth.json`；即使使用同一账号，也必须
完成两次独立 device auth。

```bash
python -m chatcopilot bot codex-auth login \
  --bot bots/lingye-copilot-qq/bot.yaml --lane all

python -m chatcopilot bot codex-auth status \
  --bot bots/lingye-copilot-qq/bot.yaml --lane all --json
```

`--lane` 接受 `main`、`worker` 或 `all`。`all` 依次授权两条 lane，不复制同一
refresh token；单条 lane 可独立重登。登录先写私有 staging home，校验成功后原子
安装，失败不会覆盖已有可用凭据。`status --json` 只返回安全状态和非秘密错误码。

[KNOWN][HIGH] 缺失或非法凭据时运行时 fail closed。managed runtime 不得发现、
导入或回退桌面/个人 `.codex`；已退役的
`deploy/wsl/import_codex_desktop_auth.sh` 只会指向上述命令。

### 代码任务草稿 PR 凭据

[KNOWN][HIGH] 在目标 bot 的 ignored `local.env` 中配置 GitHub repository、fine-grained
token 和公开 Git author；不要把 token 写进 BotSpec、prompt 或仓库：

```bash
export CHATCOPILOT_CODE_TASK_GITHUB_REPOSITORY="Ling-ye/AgentStrata"
export CHATCOPILOT_CODE_TASK_GITHUB_TOKEN="github_pat_xxxxxxxxxxxx"
export CHATCOPILOT_CODE_TASK_GIT_AUTHOR_NAME="AgentStrata Bot"
export CHATCOPILOT_CODE_TASK_GIT_AUTHOR_EMAIL="agentstrata-bot@local"
```

token 最小 repository permissions 是 `Contents: Read and write`、
`Pull requests: Read and write` 与 `Metadata: Read`。当前 Lingye `context.dev` 不允许
`.github/workflows/**`，因此不需要 `Workflows` 写权限；不要授予 admin、delete 或 force-push
能力。

修改后重新生成 worker 私有 env/credential file 并重启恢复 worker：

```bash
bash console/systemd/register.sh --enable lingye-copilot-qq
systemctl --user restart chatcopilot-code-worker@lingye-copilot-qq.service
```

[KNOWN][HIGH] 注册时每个 worker 使用 BotSpec 的 `deploy.workspace_root`；缺省值也包含
`instance_id`。worker 启动复用 canonical BotSpec runtime env，恢复任务时只接受 request 中完全匹配的
`instance_id`，不会跨实例消费任务。

[KNOWN][HIGH] `register.sh` 把实例配置目录收紧为 mode `0700`，并把 token 原子物化为
single-link mode `0600` worker 文件；transient task unit 只接收文件路径。交付进程通过
`O_NOFOLLOW` + `fstat` 从同一 fd 单次读取，Git askpass 使用任务期内的临时 `0600` 快照；
Codex 进程、worker env、Git remote 与持久化诊断都不包含 token 明文。

## 共享 Docker 服务

```bash
bash deploy/docker/services.sh desired
bash deploy/docker/services.sh start
bash deploy/docker/services.sh status
bash deploy/docker/services.sh doctor all
bash deploy/docker/services.sh logs
```

按来源探针：

```bash
bash deploy/docker/services.sh probe searxng --keyword "上海 二郎拉面"
bash deploy/docker/services.sh probe playwright
bash deploy/docker/services.sh probe xhs --keyword "上海 二郎拉面"
python -m chatcopilot.agent.search.probe \
  --bot bots/lingye-copilot-qq/bot.yaml \
  --server xiaohongshu \
  --query "上海 二郎拉面 探店"
```

无参数 `start` 从启用的 BotSpec 解析 desired state：SearXNG provider 需要搜索引擎，
Playwright / 小红书 binding 需要各自服务；禁用项会被停止。Tavily、Brave 和 SearXNG
适配器不再各占一个 wrapper 容器。`doctor all` 只检查 desired 服务，功能可用性再由
`probe` 验证；小红书还需单独确认登录态。服务清单和登录细节见
[`../deploy/docker/README.md`](../deploy/docker/README.md)。

## 通用 HTTP API

```bash
export CHATCOPILOT_HTTP_API_TOKEN="<strong-random-token>"
agentstrata http-api-server --host=127.0.0.1 --port=8787
curl http://127.0.0.1:8787/healthz
```

HTTP route 由 `chatcopilot.http_routes` registry 发现。registry 为空时健康检查仍可用；
业务路由由部署者提供独立模块，并通过 `http_route_modules` 配置显式启用。token 只放
私有环境或 credential store，不写进 Git。默认保持回环监听；对外暴露时由受控反向
代理提供认证和 TLS。

## Evaluation

查看和准备评测：

```bash
python -m chatcopilot evals list
python -m chatcopilot evals describe --suite gaia
python -m chatcopilot evals prepare --suite bfcl
```

执行 Profile 对比或官方 Suite：

```bash
python -m chatcopilot evals run \
  --profile agent-comparison-mvp \
  --preset quick \
  --bot bots/lingye-copilot-qq/bot.yaml \
  --output reports/evals/evaluations/agent-quick

python -m chatcopilot evals run \
  --suite bfcl \
  --bot bots/lingye-copilot-qq/bot.yaml \
  --output reports/evals/evaluations/bfcl-smoke
```

统一资源名与状态口径见 [`evaluation-glossary.md`](evaluation-glossary.md)，控制台和
API 见 [`console.md`](console.md)。

## 诊断与事故处理

查询 `task_*` 或 `job_*`：

```bash
PYTHONPATH=src .venv/bin/python -m console.control diagnose \
  --id <task_or_job_id> \
  --out _wsl_debug/task-diagnostics/<task_or_job_id>
```

收集实例或全局快照：

```bash
bash deploy/wsl/dump.sh --instance <id> --mode quick
bash deploy/wsl/dump.sh --all-running
bash deploy/wsl/dump.sh --archive
```

分享诊断包前检查脱敏结果。只有明确需要时才使用 `--include-env`。

建议的排查顺序：

1. `bot doctor` 检查 BotSpec、私有 env 和平台凭据。
2. 查看主 service 与平台 gateway 状态。
3. 查看最近 120 行主 service 日志；Codex 任务再检查 code-worker。
4. 用 `console.control diagnose` 查询具体 `task_*` / `job_*`。
5. 收集 `dump.sh --mode quick` 快照。
6. systemd user bus、cc-connect、WSL 冷启动或 Windows 调用问题进入
   [`../deploy/wsl/README_WSL.md`](../deploy/wsl/README_WSL.md)。

## 兼容名称

以下名称仍是可调用或持久化契约，不代表当前产品品牌：

| 兼容名称 | 当前用途 |
| --- | --- |
| `python -m chatcopilot` | 与 `agentstrata` 指向同一 CLI |
| `CHATCOPILOT_*` | 已部署环境的稳定变量前缀 |
| `chatcopilot@<id>.service` | Bot 实例 systemd unit |
| `chatcopilot-console.service` | 控制台 systemd unit |
| `~/ChatCopilot*` | 既有部署和数据路径；新文档不把它当作项目名称 |
