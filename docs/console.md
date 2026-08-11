# 运维控制台

运维控制台是 AgentStrata 在 WSL 中管理多机器人实例的 Web 入口。默认地址是 `http://localhost:8910`，后端由 `chatcopilot-console.service` 托管，前端产物位于 `console/web/dist` 并由 FastAPI 同源挂载。日常启停、更新、日志和诊断命令统一见 [`operations.md`](operations.md)。

## 技术栈

- 前端：React 18 + Rsbuild/Rspack + Arco Design + TanStack Query。
- 后端：FastAPI 路由 + `console/control/**` 控制层。
- 长任务和日志：后端 SSE 流，前端用专用 hook 展示到任务/日志抽屉。
- 构建命令：`cd console/web; npm run build`。

## 主要页面

- **运维总览**：调用 `/api/overview` 汇总机器人、基础设施服务和后台任务健康状态，展示摘要指标和「需要关注」问题队列。
- **服务管理**：调用 `/api/infra` 展示共享 Docker MCP、平台网关等外部依赖，支持启停、重启、Pull、日志、登录和诊断。
- **机器人实例**：展示每个 BotSpec 实例的部署、注册、运行、平台连接、日志、任务、更新和诊断入口。
- **组件目录**：按 `tools` / `prompts` / `agents` / `context` 四个 surface 只读浏览工具包、运行特性、MCP 服务、提示词、Agent preset、workflow DTO 和上下文来源；数据只来自 `chatcopilot.component_catalog` 的精确 pack/tool 投影，不直接读取 Agent/BotSpec 内部 registry 或自行 import 工具模块。
- [KNOWN][HIGH] **评测中心**：固定为「新建评测 / 评测记录 / 任务集」三个页签。Agent Profile 对比和 BFCL / GAIA / IFEval Suite 运行统一为 `Evaluation` 资源；记录页负责筛选、查看详情、取消、删除、重跑和导出，任务集页统一展示 Profile、Suite 数据准备状态与 Case coverage。报告统一保存在 `reports/evals/evaluations/<evaluation-id>/`。

Console 后端的进程执行、YAML 投影和 job/task/log 可观测读取分别位于 `process_executor.py`、`yaml_io.py` 和 `observability.py`，`operations.py` 只保留控制面编排与兼容导出。前端路由按页面懒加载；Evals 的详情组件/展示函数位于 `features/evals/`，BotToolEditor 的模型与状态 hook 位于 `features/bots/tool-editor/`。
- **设置**：控制台自身更新、控制台后端日志等全局维护入口。

## 任务可观测工作台

机器人实例的“任务”入口使用主从工作台，不再展示横向 13 列表格。左侧只加载最近
50 个 `schema_version=2` 任务，按“运行中 / 需要关注 / 最近完成”分组并在浏览器内
搜索；旧任务不迁移，也不会进入该列表。右侧按 Span 层级展示路由、模型、工具、
subagent 和后台 Job 阶段。列表和选中任务每 3 秒轮询，运行中的墙钟耗时由浏览器
每秒刷新；此链路不使用 SSE。

只读 API：

- `GET /api/bots/{instance_id}/tasks?limit=50`：v2 任务摘要，服务端硬限制最多 50 条。
- `GET /api/bots/{instance_id}/tasks/{task_id}`：步骤树、分类耗时、固定预测、实际累计
  Token、Job 状态和本地价格表计算的实际用量费用估算。
- `GET /api/bots/{instance_id}/tasks/{task_id}/events`：按需读取任务执行事件与关联
  Job 阶段事件。损坏的 JSON/JSONL 记录被跳过，非法或越界 ID 被拒绝。

Token 口径：

- `prompt_tokens` 是总输入，`cached_tokens` / `cache_read_tokens` 是输入子集；
  `non_cached_input_tokens = prompt_tokens - cached_tokens`，Cache 不再加进
  `total_tokens`。
- 任务实际累计只汇总叶子 LLM 调用一次。父 Span 的 `inclusive_usage` 用于解释
  分支成本，不能再与任务总量相加。
- 输入粗估包含消息、system prompt 和工具 Schema。步骤输出/Cache 与任务总基线
  都要求同 Bot、模型、上下文（步骤另隔离 main/subagent）至少 20 个有效样本，
  最多读取最近 200 个样本并取中位数。任务基线首次可计算后固定，运行中只更新
  实际累计；冷启动显示“样本不足”或“粗估”。
- 费用是基于已发生调用和本地模型价格表的估算，不是供应商账单；没有价格的模型
  明确显示未配置，不推导预计费用。

原始事件保留工具参数、精简结果和错误，不保存文本流增量或供应商私有
`reasoning_content`。它沿用每实例 30 天 / 1 GiB 的诊断清理策略。控制台仍匿名监听
`0.0.0.0:8910`，本次没有新增认证或原始事件访问门禁：任何能够访问该端口的人都能
读取这些事件，部署方必须用主机网络边界控制暴露范围。

## NapCat WebUI 登录

- [KNOWN][HIGH] 服务管理中的“WebUI 登录”调用 `POST /api/infra/napcat:<instance>/webui-session`；后端通过 `qq_gateway.sh bootstrap` 幂等启动或修正回环容器，等待 `localhost:6099` 就绪后返回含 WebUI 管理 token 的登录链接。
- [KNOWN][HIGH] WebUI 管理 token 来自 NapCat 容器日志，只用于进入管理面板，不是正向 OneBot WebSocket 的 `QQ_ACCESS_TOKEN`；相关响应带 `Cache-Control: no-store`。
- [KNOWN][HIGH] 已停止容器仍可通过 `GET /api/infra/napcat:<instance>/webui-token` 恢复历史 WebUI token；容器不存在或日志尚未产生 token 时返回明确错误。
- [KNOWN][HIGH] NapCat 的正式“启动/重启”继续要求合法 `QQ_ACCESS_TOKEN` 并通过双向 OneBot 探针；WebUI bootstrap 不启动 QQ Bot service，也不降低该门禁。
- [KNOWN][HIGH] 缺失或错配 OneBot token 时先在 WSL 执行 `bash deploy/wsl/qq_gateway.sh sync-token --instance <id>`，再运行实例更新；控制台的 gateway 输出会移除 ANSI 控制序列，启动等待期的临时探针错误不会混入成功响应。

## Evaluation 评测中心

统一资源名与状态口径见 [`evaluation-glossary.md`](evaluation-glossary.md)。

[KNOWN][HIGH] `Evaluation` 是唯一运行资源，使用 `evaluation_id` 标识，并以 `kind: comparison | suite` 区分执行方式。生命周期状态固定为 `queued / running / completed / partial / cancelled / interrupted / error`；通过/失败和 Codex/Native/平局只属于结果，不混入生命周期。

- `comparison`：选择 Bot、Profile 与 `quick / standard / custom` preset。Quick 和 Standard 使用服务端固定默认值，不接受执行参数覆盖；Custom 必须显式提供 Targets、Case refs、重复次数、预算和 seed。MVP Profile `agent-comparison-mvp` 仍覆盖 IFEval 指令遵循、GAIA smoke、确定性工具调用和隔离代码修复，不生成“智能总分”。
- `suite`：选择 BFCL、GAIA 或 IFEval，可指定任意 Case、dry-run 和 GAIA judge。官方 Suite 数据仍按需准备；Profile Case 使用稳定版本化定义，不依赖官方数据缓存。

[KNOWN][HIGH] 新建评测只保留一个 Bot 选择器和一个「开始评测」动作。`POST /api/evals/evaluations` 在落盘和启动子进程前原子执行 fail-closed 预检；阻断响应使用 `code/message/checks`，前端展开具体检查项，不再把对象转成 `[object Object]`。字段变化会清除旧阻断状态，旧异步响应不能回写到已切换的 Bot、Suite、表单或记录；SSE 重连按稳定事件内容去重。同一 Bot 的活动 Evaluation 通过持久化 claim 跨 Console manager 和进程互斥，受管进程真正退出前禁止删除、重跑或创建下一条。

[KNOWN][HIGH] Target 记录 executor、backend、model、reasoning effort 和包含已解析 Bot runtime 行为摘要的稳定 fingerprint；逐 Trial checkpoint 必须完成整个 Target 组后才参与胜负聚合。Resume 在任何写入前校验完整请求、Case 快照、Target fingerprint 和已有 Trial 结构，不能修改请求后混用旧 Trial；已完成 Evaluation 不可 Resume，未 checkpoint 的 workspace 残留会在重跑前清理。非 Resume 只接受新目录或严格匹配且无证据文件的 Console bootstrap。评测只在子进程内覆盖 backend，不修改 BotSpec 或线上会话；Case 工具默认拒绝，代码写入只发生在 Evaluation 的隔离 workspace。外部 Case ID 不直接形成 workspace 或 artifact 路径，包含 `/` 时仍可作为原始领域标识查询。Case coverage 按 Bot + Case + Target fingerprint 聚合，因此模型、Bot runtime 或执行策略变化后不会误用旧覆盖记录。

CLI 的 prepare、validate 和 run 命令统一见 [`operations.md#evaluation`](operations.md#evaluation)；本页只维护 Console 与 API 契约。

[KNOWN][HIGH] Evaluation 目录只保留权威的 `request.json`、`state.json`、`result.json`、逐 Trial 证据、Core 单写的 `progress.jsonl`、脱敏 `run.log` 和 Markdown 报告。Evaluation 目录、activity claim 和权威 artifact 不接受符号链接，读取、流式传输、导出、取消和删除前核对 `evaluation_id`；遗留 worker 只有在 argv 中唯一 `--output` 与记录目录规范路径精确相等时才可发送信号。原始事件不落盘；事件、回答、工具参数、启动错误和报告在写 checkpoint 前过滤凭据字段、通用 token、已知 secret 和机器绝对路径。CLI 布尔值、列表和预算使用严格类型并拒绝 NaN/Infinity，`evaluation_id` 与 Console 统一为 1–128 位字母、数字、下划线或连字符；报告比较只接受 lifecycle completed、同 kind、同 Profile/Suite、同 Case/Trial 样本、同 Judge 且 executor/backend 语义可比的 Evaluation。费用无法可靠取得时显示 `unknown`。

评测 API：

- `GET /api/evals/profiles`
- `GET /api/evals/suites`
- `POST /api/evals/suites/{suite_id}/prepare`
- `GET /api/evals/cases/coverage`
- `POST/GET /api/evals/evaluations`
- `GET /api/evals/evaluations/{evaluation_id}`
- `GET /api/evals/evaluations/{evaluation_id}/cases/{case_ref}`
- `GET /api/evals/evaluations/{evaluation_id}/stream`
- `POST /api/evals/evaluations/{evaluation_id}/cancel`
- `POST /api/evals/evaluations/{evaluation_id}/rerun`
- `DELETE /api/evals/evaluations/{evaluation_id}`
- `GET /api/evals/evaluations/{evaluation_id}/export/{json|markdown}`

## 运维入口与配置更新

实例更新、Console 更新、状态、重启和日志的完整命令集中在
[`operations.md`](operations.md)。控制台中的“更新并重启”和“更新控制台”分别调用
`update_instance.sh` 与 `deploy_console.sh --update-only`，不维护第二套运维流程。

[KNOWN][HIGH] 「能力与工具」Tab 以 `tools` / `prompts` / `agents` / `context`
四面展示当前配置。可编辑项写回 WSL 源仓中的 `bots/<id>/bot.yaml` 和
`bots/<id>/mcp/servers.yaml`；“保存并重启”复用统一实例更新入口，通常走不重复安装
依赖的快速路径，Git 提交仍由操作者在源仓完成。

[KNOWN][HIGH] 工具配置“保存并重启”先取得同实例 TaskManager 串行资格，再在任务内写配置和调用统一更新；已有活动任务时返回 409 且不得修改配置。机器人更新 SSE 只有收到服务端 `end` 事件才读取最终 Task：成功后才清除编辑器未保存状态、刷新配置并关闭任务抽屉，失败时保留当前草稿、标红并显示最后错误；传输断线只显示重连提示并由 EventSource 自动重连，不得伪装成任务终止。更新脚本只即时检查主 systemd 服务 active，不把 QQ、飞书等平台通道连接作为任务成功条件。

机器人实例的运行操作区只在状态明确为“未注册”时显示“注册服务”；已注册实例不提供“重注册”按钮。需要修复 systemd 注册配置时，使用下表对应的底层脚本。

### systemd 不可用

控制台依赖 `systemctl --user` 管理实例。WSL 的 PID 1 为 systemd 并不代表用户总线
可用；`user@<uid>.service` 活着但 `/run/user/<uid>/bus` 缺失时，面板仍会正确显示
“systemd 不可用”。WSL 引导必须安装 `dbus-user-session`。修复命令与
`219/CGROUP` 的一次性重试步骤见 `deploy/wsl/README_WSL.md`。

### code-worker 启动失败

`chatcopilot-code-worker@<id>` 若以 `218/CAPABILITIES` 循环退出，说明安装的
用户 unit 仍包含 WSL 不支持的内核 capability 加固项。使用当前源码重新运行
`bash console/systemd/register.sh <id>`，再重启实例。注册会保留兼容的 systemd
加固，并从 `local.env` 的 `export KEY=value` 形式提取允许进入 worker 的 Codex
配置；QQ、LLM 等平台凭据不会进入 worker 环境。

主 unit 的实例 ID 与部署后 BotSpec 路径由注册配置显式固定。同一 `wsl_home`
包含多个 `bots/*/bot.yaml` 时，不得退回选择任意首个 BotSpec。

### Codex 独立 lane 登录

控制台不提供 Codex 登录 UI 或 API。main / worker 的独立 device auth 与安全状态检查
统一使用 [`operations.md#codex-main--worker-认证`](operations.md#codex-main--worker-认证)
中的 CLI；凭据布局、lease 和 resume 失效契约见 [`runtime.md`](runtime.md) 与
[`bot-spec.md`](bot-spec.md)。

## 工具配置 DTO

`GET /api/bots/{id}/tools` 和 `PUT /api/bots/{id}/tools` 使用以下四面 BotSpec DTO：

```json
{
  "tools": {
    "packs": ["workspace.read_write"],
    "features": ["chat.file_uploads"],
    "hide": ["dangerous_tool"],
    "mcp": { "servers": [{ "ref": "searxng-search", "enabled": true }] }
  },
  "agents": {
    "presets": ["mcp_query"],
    "workflows": []
  }
}
```

机器人 inventory 使用展示字段 `tool_packs`、`tool_features`、`hidden_tools`、`agent_presets`、`workflows` 和 `config`；`config` 展示 `prompts`、`context.rag`、`context.memory_store`、`context.codebases`、`context.playbooks` 等只读配置状态。当前内置 workflow registry 可为空，控制台仍保留 DTO 字段以兼容后续注册。

## 按钮与底层入口

| 控制台动作 | 底层入口 |
| --- | --- |
| 首次部署 | 写 `bots/<id>/local.env`，再执行平台准备、同步、重建、注册、启动 |
| 注册服务（仅未注册实例显示） | `bash console/systemd/register.sh <id>` |
| 启动 / 停止 / 重启 | `bash console/scripts/ctl.sh <verb> <id>` |
| 更新并重启 / 工具配置“保存并重启” | [KNOWN][HIGH] `bash deploy/wsl/update_instance.sh --instance <id>`；默认快路径，依赖或安装脚本变化、实例 venv 缺失时完整 bootstrap |
| 更新控制台 | `bash deploy/wsl/deploy_console.sh --update-only` |
| 实例日志 | `/api/bots/{id}/logs/stream` SSE |
| 控制台日志 | `/api/console/logs/stream` SSE |
| 任务流 | `/api/tasks/{task_id}/stream` SSE |
| 实例诊断 | `bash deploy/wsl/dump.sh --instance <id>` |
| NapCat WebUI 登录 | `POST /api/infra/napcat:<id>/webui-session` → `qq_gateway.sh bootstrap` |

## 前端协作规则

- 修改控制台前端先读 `docs/ai-frontend.md` 和 `.cursor/rules/70-frontend-design.mdc`。
- 优先使用 Arco 原生组件；旧 UI 语义兼容层已移除，不允许新增 Semi 风格 prop 适配接口。
- 服务端读取、轮询、刷新优先走 TanStack Query；SSE 流仍用专用 hook。
- 修改 `console/web/**` 后至少运行 `npm run build`，并尽量用浏览器检查桌面和窄屏布局。
