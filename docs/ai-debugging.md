# AI 任务诊断

本文说明如何从稳定 ID 定位 Agent 回合、后台任务和隔离代码任务。常用命令先看
[`operations.md#诊断与事故处理`](operations.md#诊断与事故处理)；这里解释证据结构和
读取顺序。

## ID 与证据层级

| ID / 资源 | 含义 | 主要入口 |
| --- | --- | --- |
| `task_*` | 一轮对话任务 | `get_task_status` 或 `console.control diagnose` |
| `job_*` | 后台长任务，包括隔离代码任务 | `get_job_status` 或 `console.control diagnose` |
| Evaluation | 独立评测记录 | Console 评测中心或 `evals` CLI |

`task_*` 位于用户 workspace 的 `tasks/<task-id>/`，包含 `task.json`、`turn.json` 与
`events.jsonl`。`job_*` 位于 `jobs/<job-id>/`，包含请求、状态、结果与 worker 日志。
父 task 显示已委托不代表子 job 成功；必须继续核对关联 job 的终态。

## 唯一诊断入口

从 AgentStrata 源仓根目录执行：

```bash
PYTHONPATH=src .venv/bin/python -m console.control diagnose \
  --id <task_or_job_id> \
  --out _wsl_debug/task-diagnostics/<task_or_job_id>
```

该命令跨 BotSpec 实例定位 ID，并关联 task、trace、session、job、subagent 和对应时间窗
日志。输出默认脱敏并有大小上限。

证据包结构：

- `summary.md`：首屏状态、主要失败点和建议证据。
- `index.json`：关联关系、缺失项、截断和脱敏统计。
- `task/`：单轮输入输出与事件。
- `jobs/`：关联后台任务。
- `logs/`：任务时间窗附近的日志片段。
- `runtime.json`：实例、Git、模型和 MCP 健康摘要。

## 隔离代码任务

Codex `worktree` Owner 的源码 mutation 由 `start_code_task` 提交，返回值仍是可由
`get_code_task` 查询的 `job_*` ID。任务从 GitHub 远端默认分支创建私有 clone，不复制
操作者 checkout 的 tracked/untracked 改动。公开状态包含 `status`、`stage`、`attempt`、
心跳、资源、队列、changed files、checks、branch、commit、draft PR URL 和错误码。

代码任务目录中的关键证据：

- `request.json`：冻结的中文公开标题、私有用户目标、验收条件、caller 摘要和执行 profile。
- `status.json`：当前生命周期、stage、attempt、心跳、资源和错误码。
- `result.json`：终态摘要与产物。
- `changes.json`：相对任务远端 base commit 的文件 delta。
- `codex-events.jsonl` / `codex-session.json`：流式事件与 native session 证据。
- `supervisor.json` / `dispatch.json`：执行进程和 worker 调度身份。
- `validation.json`：任务 clone 中的门禁命令与结果。
- `delivery.json`：repository、base/task branch、base/commit SHA 与 draft PR 编号/URL。
- `cancel-request.json`：显式取消请求（存在时）。

终态为 `succeeded`、`failed`、`cancelled` 或 `interrupted`。失败、取消和中断任务可在
保留 clone 上追加 attempt；重新授权 worker 会保留 clone/attempt，但使旧 Codex session
ID 失效。仅 push/PR 交付失败时可省略新 prompt，直接幂等重试 delivery。

排查顺序：

1. 先读公开状态中的 `stage`、`error_code`、心跳和资源限制。
2. 再读 `request.json` 与 `delivery.json`，确认显式 repository、远端 base 与任务分支。
3. Codex 执行失败看 `codex-events.jsonl` 和私有 stderr；不要把原始认证错误贴给用户。
4. 变更越界看 `changes.json`，对照 `context.dev` 区分请求越界与模型生成意外文件。
5. 验证失败看 `validation.json`；门禁只针对任务远端基线 clone，不读取人工 dirty files。
6. `delivering` 失败看 `delivery.json`、远端 branch 和已有 PR；重试不得 force-push 或重复建 PR。
7. 缺 repository、author 或 token file 时先修私有 env 并重注册 worker；无需回滚本地源码或实例。

## 模型与 backend 先验检查

当问题是“为什么使用这个 backend 或模型”时，先运行确定性解释命令，不启动机器人，
也不读取 API key：

```bash
PYTHONPATH=src .venv/bin/python -m chatcopilot bot route-explain \
  --bot bots/<bot-id>/bot.yaml \
  --config bots/<bot-id>/local.env \
  "要检查的用户文本"
```

`route-explain` 是保留的 CLI 名称。输出中的 backend 由 BotSpec 实例固定，用户文本不能
跨 backend 路由。`main.model/main.reasoning_effort` 表示主 Codex 默认档，
`code_task.profile/model/reasoning_effort` 表示独立 code-worker 档；Codex backend 下的
`chat.model` 只是共享 chat 槽，不是 fallback。能力不足时检查当前 backend 与
ToolAccessPolicy，不寻找隐式 fallback，也不推断文本会触发模型切换。

## 读取顺序

1. 先读 `summary.md`，确认状态、失败阶段和建议证据。
2. 再读 `index.json`，确认关联、缺失、截断和脱敏情况。
3. 只打开摘要指向的必要文件，不批量读取完整 `logs/` 或累计 transcript。
4. 普通失败 job 优先读 `result.json`、`stderr.log`，再读 `stdout.log`。
5. 结论引用具体证据文件；证据不足时明确缺少什么。

## 何时升级到实例快照

单任务问题不要先收集 full dump。只有以下情况升级：

- ID 无法在任何实例定位。
- Bot 或 Console 无法启动，任务记录尚未创建。
- 问题涉及整个实例的进程、网络、磁盘或配置状态。

```bash
bash deploy/wsl/dump.sh --instance <id> --mode quick
```

`dump.sh` 默认不包含原始 env。`--include-env` 只在明确需要时人工使用。

## 现场保护

- 排查期间不要删除 task/job/Evaluation 目录、重启服务或清理日志来“重试看看”。
- 诊断包可能包含用户任务正文和机器路径；不要公开上传。
- 不在回复中披露凭据、完整用户身份、原始认证错误或无关会话内容。
- 先保留失败现场，再由正常运维入口恢复；不要直接编辑实例副本或状态 JSON。
