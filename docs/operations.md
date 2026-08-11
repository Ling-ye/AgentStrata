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
| 查看 Evaluation service | `python -m chatcopilot.evals.service health --json` |
| 跟随 Evaluation service 日志 | `journalctl --user -u chatcopilot-evaluation.service -f` |
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

控制台默认地址为 `http://localhost:8910`。`deploy_console.sh --status`
同时验证 Console HTTP 和独立 Evaluation service。`--restart-only` 只重启
Console。`--update-only` 原子获取 Evaluation maintenance lease；service 在同一
创建锁内证明空闲并持久化 marker，之后直到 Console 与 Evaluation 都重启并通过
健康检查前拒绝新建 Evaluation。活动记录、未知 lifecycle、遗留 claim、身份不明
worker 或 service 不可达都会在构建和重启前失败关闭。

控制台页面中的“更新控制台”只通过 `systemd-run --user` 创建独立 transient
unit，再由该 unit 执行同一个 `deploy_console.sh --update-only`。`setsid` 和
`nohup` 不会脱离 `chatcopilot-console.service` 的 cgroup，因此不作为降级路径。
若 transient unit 无法创建，接口会在更新脚本运行和 maintenance lease 获取前返回
明确错误；此时在 WSL 终端手工执行下面的 `--update-only` 命令。

```bash
bash deploy/wsl/deploy_console.sh --status
bash deploy/wsl/deploy_console.sh --update-only
bash deploy/wsl/deploy_console.sh --restart-only
```

```bash
systemctl --user status chatcopilot-console --no-pager -l
journalctl --user -u chatcopilot-console -f
```

Evaluation 后端使用独立 user service 和同 UID Unix socket：

```bash
python -m chatcopilot.evals.service health --json
python -m chatcopilot.evals.service health --require-idle
python -m chatcopilot.evals.service maintenance status
systemctl --user status chatcopilot-evaluation.service --no-pager -l
journalctl --user -u chatcopilot-evaluation.service -f
```

`health --require-idle` 只用于诊断，单次检查不能替代代码更新所需的原子
maintenance lease。只重启 Evaluation service 且不改变运行代码时，可以显式
重启并立即验证 UDS health；存活 worker 会由新 service 重新观察：

```bash
systemctl --user restart chatcopilot-evaluation.service
python -m chatcopilot.evals.service health --json
```

受管 worker 不依赖 Console 的 lifespan、cgroup 或 stdout pipe。重启 Console
不会向 worker 发送信号；不涉及代码更新的 Evaluation service 重启会用 claim、
state 和 PID argv 重新观察身份匹配的存活 worker。PID 存在但身份无法证明时，
服务保持 fail closed，不取消、不释放 claim 且不将记录误判为终态。应用代码
更新必须使用 `deploy_console.sh --update-only`，不能用“检查一次 idle 后手工更新”
替代 maintenance lease。

若更新进程异常退出，trap 会优先释放租约；若此时 Evaluation service 也不可达，
marker 会保留并继续拒绝创建。恢复 service 后先读取租约，再使用同一 ID 释放：

```bash
python -m chatcopilot.evals.service maintenance status
python -m chatcopilot.evals.service maintenance leave --lease-id <lease-id>
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

控制台中的「新建评测 / 评测记录 / 任务集」全部通过
`chatcopilot-evaluation.service` 执行。服务是 activity claim、lifecycle state 和
managed worker 的唯一 owner；Console 只是 UI/BFF。先检查本机服务：

```bash
python -m chatcopilot.evals.service health --json
curl -fsS http://127.0.0.1:8910/api/evals/health
```

`ready=true` 表示 UDS 协议可用，`active_count` 是当前 `queued/running` 记录数。
Console API 返回 `503` 时，不要反复提交创建请求；按以下顺序检查：

```bash
systemctl --user status chatcopilot-evaluation.service --no-pager -l
journalctl --user -u chatcopilot-evaluation.service -n 120 --no-pager
python -m chatcopilot.evals.service health --json
```

日常的 Profile、Suite、Case、数据准备、coverage、SSE、导出、取消、重跑和
删除都走同一 service client。Suite 数据准备仍在 Console 任务抽屉中展示输出，
但实际准备操作由 Evaluation service 执行；关闭抽屉或重启 Console 不会成为
Evaluation worker 的取消信号。

CLI 的 `evals list/describe/prepare/run/compare` 保留为 standalone/CI 入口，不会
接管正在运行的 managed Evaluation。查看和准备 standalone 评测：

```bash
python -m chatcopilot evals list
python -m chatcopilot evals describe --suite gaia
python -m chatcopilot evals prepare --suite bfcl
```

执行 standalone Profile 对比或官方 Suite：

```bash
python -m chatcopilot evals run \
  --profile agent-comparison-mvp \
  --preset quick \
  --bot bots/lingye-copilot-qq/bot.yaml \
  --output reports/evals/manual/agent-quick

python -m chatcopilot evals run \
  --suite bfcl \
  --bot bots/lingye-copilot-qq/bot.yaml \
  --output reports/evals/manual/bfcl-smoke
```

`reports/evals/evaluations/` 保留给受管 service。Standalone CLI 使用显式
`--output`，并将记录放在 `reports/evals/manual/`。CLI 会拒绝缺失 `--output`
或指向受管根目录的执行请求，不能覆盖、resume 或修改 service 正在管理的目录；
两者的 artifact 写入模式不同。

受管目录的所有权固定为：application 写 `request.json`、`state.json`、claim 和
取消标记；Core 写 `result.json`、`summary.md`、`progress.jsonl` 和逐 Trial
证据；managed worker 写脱敏 `run.log`；Console 不写 Evaluation artifact。
手工修改这些文件会破坏 ID、fingerprint、checkpoint 和恢复校验。

当前服务只运行 AgentStrata 原生 Evaluation Core，不会安装或启用外部
评测引擎、实验追踪平台、remote evaluator 或 exporter。

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
2. 查看主 service 与平台 gateway 状态；评测问题同时检查 Evaluation service health。
3. 查看最近 120 行主 service 日志；Codex 任务再检查 code-worker，评测再检查 `chatcopilot-evaluation.service`。
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
| `chatcopilot-evaluation.service` | Evaluation application 与 managed worker supervisor systemd unit |
| `~/ChatCopilot*` | 既有部署和数据路径；新文档不把它当作项目名称 |
