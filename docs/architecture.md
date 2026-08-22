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
| 身份、角色与 conversation/turn 来源 | `contracts/identity.py` |
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

## PromptPlan 信任分区

所有 main Agent、subagent、backend 和 Evaluation 模型入口只消费一个不可变
`PromptPlan`。Layer kind 与 trust 是封闭映射，renderer 不能把内容移动到更高权限分区：

| 分区 | 内容 | Native / LangGraph | Codex |
| --- | --- | --- | --- |
| Host policy | runtime policy、capability policy | system envelope | `host_policy` |
| Runtime facts | 已认证身份、backend/model、时间等宿主事实 | system envelope | `runtime_facts` |
| Bot instructions | Bot identity/style、Skills 索引 | 独立 user-context envelope | `bot_instructions` |
| Untrusted data | persona、memory、journal、网页、用户输入 | 独立 user-context/user message | `untrusted_context` 与 JSON 用户字段 |

Codex envelope 使用 schema v2；render receipt 记录四个分区、各 layer 与最终渲染结果的
稳定摘要。Bot 文件即使由维护者提供，也只控制 identity/style，不获得授权或安全策略权限。

## Agent backend

三个 backend 共享 `AgentTask`、`AgentEvent`、`AgentResult` 和 turn runtime。backend
只在实例配置中选择，不按角色或单轮文本自动切换。

- Native：内置模型/工具循环。
- LangGraph：使用同一契约的图执行器。
- Codex：实例主会话使用 Codex；Owner 源码写入通过独立 code-worker 与草稿 PR
  交付。成员只获得当前 conversation workspace；QQ 私聊仍按用户隔离，QQ 群聊指向
  当前群的共享 workspace。QQ 群 Codex 额外使用 fail-closed bubblewrap：只读暴露精确
  shared root，禁用可直接写入的内建 shell/`apply_patch` 等路径，文件 mutation 只能通过
  actor-bound、workspace-scoped Session Gateway MCP 执行。

主 Agent 是唯一向用户交付结果的执行者。Subagent 只通过 delegate 工具运行并用
`submit_result` 返回结构化结果。

## 工具、MCP 与搜索

Tool pack 通过 component catalog 贡献精确工具绑定和结构化跨工具 policy。通用公开 tool pack 包括
workspace、memory、playbook、MCP 管理、Feishu、Wiki、职业情报、网页读取、
Windows/Unity 只读能力与受控开发工具。

显式启用的 tool pack 是部署契约，不是 best-effort 插件。绑定模块无法 import、没有非空
`TOOLS`、导出非 `ToolDef`、重复导出，或 catalog 声明的工具没有完整物化时，统一抛出包含
module、pack 和 tool 证据的 `ToolMaterializationError`。运行时不得把该错误降级成空工具列表；
无副作用预检可以把它转换为明确的失败检查。

MCP catalog 是经过审阅的静态目录。公开运行时不会自动下载、安装或启用第三方
MCP/Skill。`risk: search` 的 MCP binding 可产生只读搜索来源；统一搜索入口负责路由、
降级、去重、时间预算和来源合并。

## 平台与会话

平台 adapter 负责声明群 conversation 是按 actor 还是按 chat 隔离，并把平台字段归一化。
`ConversationIdentity` 只描述稳定的平台、会话类型和 chat ID；`TurnIdentity` 另行描述
当前消息的发送者、消息 ID 与 transport 来源。Middleware 在每轮 prompt 边界据此组装
角色、workspace、工具权限、文件发送器和后台通知 hook，Agent 不读取平台帧或 adapter
类型。

QQ adapter 使用 chat-scoped 群会话。同一群共享 `group_<safe-chat-id>/shared/` 和受保护、
有界的 conversation journal；QQ 私聊、不同 QQ 群以及其它平台仍隔离。共享 session key
只标识群，当前发送者必须由 cc-connect sender envelope 与同步 `message.received` hook 写入的
实例私有、有界加锁 transport attestation 队列双重绑定。Middleware 先交叉校验 envelope 和
attestation 的 actor/正文摘要并精确消费一条随机 ID 记录，再选择发送者绑定的执行 `SessionState`；群历史通过 journal 注入，不能
通过复用另一成员的 executor、caller identity 或 backend resume state 来共享上下文。
conversation journal、actor-scoped backend state 与 transcript 位于 shared root 的受保护兄弟
目录 `.conversation-state/`，不能作为群成员的普通 workspace 文件读取。journal 与持久化
epoch/sequence/摘要 metadata 成对验证；文件丢失、截断或旧快照会群级逐出 actor backend，
不得静默重建为一条新的 sequence 1 历史。群 Codex 在同一 live actor session 内可 resume；
actor cache 逐出或进程重启后从有界 journal 创建新 native thread，避免 process-local cursor
回到零时把旧 journal 再次注入已含相同内容的持久 thread。

授权始终按当前发送者计算，与群准入分离。稳定发送者是 Owner 时，私聊和 QQ 群都使用 Owner
prompt、工具、Codex 与代码任务投影；其他群成员仍使用 member 投影。所有人的普通 workspace
都是当前群的 shared root，但 executor、caller identity、backend state 和 Owner job 控制面按
actor 隔离。群是公开输出场景，因此 Owner 工具 payload 仍脱敏；当前群的 protected memory
作为不可信历史数据自动注入，但任何 actor 的私聊 memory 与私有 Wiki/RAG 不自动注入，标记
`private_chat_only` 的工具也不会因为 Owner 身份绕过频道限制。

QQ 群不在 member-writable shared root 写 `MEMORY.md`，而是按稳定群身份使用
`.conversation-state/persistent/memory/group/<digest>/MEMORY.md`。准入成员可 read/append，只有
Owner 可 clear；member-visible turn diagnostics 仍禁用。已接受回合的 Console task
记录按真实 actor 写在 `.conversation-state/task-actors/<actor-digest>/tasks/`，群任务与 workspace
工具均不能读取。准入拒绝保留同一 actor 分区的终态 task，但不激活执行 session；身份无效
只写入 `.conversation-state/task-intake/tasks/` 的脱敏失败记录。任何入站消息无法先建立 task
记录时失败关闭，不进入 Agent、附件或工具阶段。群人格使用通用 protected persona port 的
`global → group` 层；Owner 调用现有 `persona_*` 工具，User/Admin 无法读取或修改任何层。
Owner 后台 job 的 request/status/
result/log 位于 `.conversation-state/jobs/<actor-digest>/`，普通成员不能从 shared root 发现或控制。

cc-connect 的静态 `default/.cc-connect/attachments` inbox 只按 basename 落盘，不带 chat
或 message 绑定，因而 QQ shared-group 不从该 legacy inbox 推断文件归属；已经位于当前
shared root 的明确普通文件仍可经 workspace discovery 使用。basename-only 的新群附件会
同步失败关闭，也不能用 shared attachments 中的同名旧文件冒充；该拒绝记录一次且不安排
必然超时的异步 ack。只有 transport 提供 message-bound 路径或令牌后才可恢复群附件导入。

上述逐轮 sender 绑定依赖部署渲染同时启用 cc-connect 的 shared-channel session、
sender injection 与同步 message hook。实例私有 attestation state/lock 是非执行 JSON/锁文件，不能放在公共
`/tmp` 或由 wrapper `source`。本地合成 prompt 可以验证解析和失败关闭，但不等于真实两账号 QQ ingress
E2E。

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

`scripts/check_architecture.py` 对 `src/chatcopilot` 与 Console 下的 Python 源码做 AST
静态解析，覆盖可静态解析的绝对、相对 import；动态加载、非 Python 依赖和运行时调用关系
不在该图内：

- area policy 自身必须是 DAG，跨 area 导入只能沿声明方向；
- 受检 Python 模块的静态 import 图不允许包含两个及以上模块的强连通分量；
- 兼容 facade 只允许实现域和专门兼容测试引用，内部实现和普通测试使用 canonical surface；
- ACP 等跨域模块不能导入其它 owner 模块的私有符号。

单元测试调用与 CLI 相同的检查入口和规则集合，避免命令行通过而测试只覆盖旧前缀规则。
当前边界与验收事实源是
[`architecture-boundary-hardening`](../specs/architecture-boundary-hardening/spec.md)。

```bash
python scripts/check_architecture.py
python scripts/check_sdd_specs.py
python -m chatcopilot botspec validate bots/lingye-copilot-qq/bot.yaml
python scripts/check_repo.py fast
```

字段参考见 [bot-spec.md](bot-spec.md)，运行时细节见 [runtime.md](runtime.md)，日常命令
见 [operations.md](operations.md)。
