# AgentStrata 架构

AgentStrata 是单代码库、多机器人运行平台。`bots/<bot-id>/bot.yaml` 选择实例
能力，公共基础设施通过稳定 contracts 连接，平台与领域实现不能反向渗入 Agent。

## 分层与依赖

```text
contracts
  ↑
agent / external_tools / platforms / botspec
  ↑
application assembly / middleware
  ↑
deploy / console / CLI
```

依赖只允许从上层面向下层契约：

- `contracts` 定义身份、workspace、Agent task/event/result、工具、MCP、Skill、
  subagent、runtime 和 tool-pack DTO。
- `agent` 实现主循环、backend、上下文、工具执行、搜索与 subagent，不 import
  BotSpec、middleware 或具体平台。
- `external_tools` 实现领域工具，只依赖 contracts、core 或 shared helper。
- `platforms` 为每个聊天系统提供 adapter；新平台通过
  `platforms/<name>/adapter.py` 的 `ADAPTER` 自动发现。
- `botspec` 解析实例声明并组装 runtime；它不把实例类型硬编码进 Agent。
- `middleware` 负责 ACP、MCP、通用 HTTP route registry、会话、workspace、权限和
  后台任务。
- `deploy`、`console` 与 CLI 是操作面，不定义跨层业务契约。

核心契约入口：

| 领域 | 模块 |
| --- | --- |
| 身份和角色 | `contracts/identity.py` |
| Workspace | `contracts/workspace.py` |
| Agent task/event/result | `contracts/agent.py` |
| 工具 | `contracts/tools.py` |
| Adapter approval | `contracts/adapter_approval.py` |
| Runtime、subagent、Skill、tool pack | `contracts/{runtime,subagents,skills,tool_packs}.py` |
| 开发任务范围 | `contracts/development.py` |

## BotSpec 组合

每个实例由四个主要表面和运行包络组成：

- `prompts`：persona、refusal、role 和 mode 提示词。
- `tools`：本地 tool pack、MCP binding、运行特性和隐藏工具。
- `agents`：`native` / `langgraph` / `codex` 主 backend，preset、预算和 Codex
  访问策略。
- `context`：RAG、Wiki、memory、playbook、代码仓库和开发范围。
- `platform` / `llm` / `workspace` / `deploy` / `access`：平台、模型、目录、部署与
  访问控制。

BotSpec 只声明 tool-pack id。具体目录在 `tool_packs/catalog.py`，每个 module binding
列出精确工具名；builtin 与 external 使用同一映射。工具发现统一走
`agent/tools/registry`，Console 通过 `component_catalog` 读取同一目录投影。

## Agent backend

三个 backend 共享 `AgentTask`、`AgentEvent`、`AgentResult` 和 turn runtime。backend
只在实例配置中选择，不按角色或单轮文本自动切换。

- Native：内置模型/工具循环。
- LangGraph：使用同一契约的图执行器。
- Codex：实例主会话使用 Codex；Owner 源码写入通过独立 code-worker 与草稿 PR
  交付，成员限制在个人 workspace。

主 Agent 是唯一向用户交付结果的执行者。Subagent 只通过 delegate 工具运行并用
`submit_result` 返回结构化结果。

## 工具、MCP 与搜索

Tool pack 通过 manifest 贡献 prompt fragment 和工具模块。通用公开 tool pack 包括
workspace、memory、persona、playbook、MCP 管理、Feishu、Wiki、职业情报、网页读取、
Windows/Unity 只读能力与受控开发工具。

MCP catalog 是经过审阅的静态目录。公开运行时不会自动下载、安装或启用第三方
MCP/Skill。`risk: search` 的 MCP binding 可产生只读搜索来源；统一搜索入口负责路由、
降级、去重、时间预算和来源合并。

## 平台与会话

QQ 和 Feishu adapter 把平台身份归一化为 `SessionIdentity`。Middleware 在每轮 prompt
边界组装角色、workspace、工具权限、文件发送器和后台通知 hook。群聊身份变化会重建
会话状态，Agent 不读取平台帧或 adapter 类型。

Feishu 保留通用文档、表格、多维表格、Wiki、消息和文件能力；QQ 通过本机
NapCat / OneBot 网关接入。账号、群号、用户 ID、tenant 端点和凭据都属于部署环境。

## HTTP 扩展

`agentstrata http-api-server` 启动通用 stdlib HTTP server。业务 route 只通过
`CHATCOPILOT_HTTP_ROUTE_MODULES` 显式注册；空 registry 仍提供 `/healthz`。默认认证
变量是 `CHATCOPILOT_HTTP_API_TOKEN`，route handler 不应绕过 service 层实现业务。

## Evaluation

Profile comparison 与 BFCL / GAIA / IFEval Suite 统一使用 `Evaluation`，以 `kind`
区分。生命周期状态只表示排队、运行和终态；Trial outcome 和 Case
Comparison 只表示评分结果。

```text
Console UI
  -> FastAPI BFF /api/evals/**
  -> same-user Unix domain socket
  -> chatcopilot.evals.service
  -> chatcopilot.evals.application
  -> managed worker
  -> Evaluation Core + reports/evals/evaluations/<evaluation-id>/
```

`chatcopilot.evals.application` 是受管 Evaluation 的唯一应用控制面，拥有 Bot
解析、无副作用预检、activity claim、worker supervision、lifecycle
state、取消、恢复、删除、coverage 和 Suite catalog。
`chatcopilot.evals.service` 用版本化 framed-JSON 协议将这些 use case 暴露到
本机 Unix socket；它不监听 TCP，也不依赖 `console.*`。Console 只保留页面、
HTTP/SSE 投影和错误状态映射，服务不可用时返回 `503`，不在进程内启动
备用 manager。

Socket 默认放在 `XDG_RUNTIME_DIR/agentstrata-evaluation/service.sock`；父目录与
socket 分别使用 `0700` 和 `0600`。Client 在连接前校验目录和
socket 的 owner、inode 类型、符号链接与权限；server 还会拒绝非同
UID peer。协议限制 frame 大小、请求 ID、方法集与
payload 形状。Profile、Suite、Case、数据准备、coverage、记录、事件和报告
都通过同一 client；凭据只由 Evaluation service 从 Bot 的私有 `local.env`
读取，不通过 UDS payload。

写操作采用显式 accepted 边界：server 只有在 client 收到绑定 request ID、操作和
Evaluation ID 的 accepted 帧后才执行 mutation。`start` / `rerun` 由 client 生成
稳定 Evaluation ID，并以规范请求指纹支持同请求幂等恢复；连接在 accepted 后丢失
时只用同一 ID 查询或重试，同 ID 请求漂移被拒绝。Suite 官方数据准备在独立子进程
中使用私有环境快照，长时间下载不会占用服务进程的全局环境锁。

Artifact 写入权按文件分配：

| 所有者 | 可写内容 |
| --- | --- |
| Evaluation application | `request.json`、`state.json`、activity claim、maintenance lease、取消标记 |
| Evaluation Core | `result.json`、`summary.md`、`progress.jsonl`、`trials/*.json` |
| managed worker | 脱敏 `run.log` |
| Console | 无；只通过 service client 读取和发起操作 |

受管 worker 在独立 session 中运行，不继承 Console cgroup 或 stdout pipe。
Console 启停不影响正在运行的 Evaluation。Evaluation service 重启时使用
state、claim 与 worker PID 重建观察；只有 worker argv 中唯一 `--output` 的规范
路径精确等于 Evaluation 目录时才能发送信号。PID 存在但身份无法证明时
保持 fail closed，不释放 claim、不定态且不杀进程。

运行代码更新不是“先查一次 idle 再继续”的两步操作。Evaluation application 在
与 `start()` 相同的跨进程 creation guard 内确认 lifecycle、claim 与 worker 都可
证明空闲，并写入私有 `.maintenance.json` lease；`start()` 在预检前和落盘前都
检查该 marker。Lease 跨 Evaluation service 重启保持，覆盖构建、两个 service
重启和健康检查的完整窗口，成功或可恢复失败后由相同 lease ID 释放。

Worker 启动前通过继承的单次 pipe 等待 application 放行。Service 只有在 PID
已持久化到 state 和 claim 后才释放 worker；若 service 在这段窗口崩溃，pipe
关闭会使 worker 在 Core 写入前退出，不会留下无 claim 的执行进程。受管根的
既存祖先链不得包含符号链接；目录与文件在敏感读取时通过 `lstat`、
`O_NOFOLLOW` 和 `fstat` 校验当前 UID、`0700` / `0600`、inode 类型与单硬链接。

这是同仓库、同版本、单机单用户边界。当前不引入外部评测引擎、实验
追踪平台、远程 evaluator、分布式 lease 或第二套报告存储。未来外部框架
只能在独立规格中以可选 adapter 或脱敏 exporter 接入，不得成为第二个
lifecycle owner。详细验收见
[`evaluation-service-boundary`](../specs/evaluation-service-boundary/spec.md)。

## 验证入口

```bash
python scripts/check_architecture.py
python scripts/check_sdd_specs.py
python -m chatcopilot botspec validate bots/lingye-copilot-qq/bot.yaml
python scripts/check_repo.py fast
```

字段参考见 [bot-spec.md](bot-spec.md)，运行时细节见 [runtime.md](runtime.md)，日常命令
见 [operations.md](operations.md)。
