# AgentStrata 架构

AgentStrata 是单代码库、多机器人运行平台。`bots/<bot-id>/bot.yaml` 选择实例
能力，公共基础设施通过稳定 contracts 连接，平台与领域实现不能反向渗入 Agent。

## 分层与依赖

```text
deploy / console / CLI
  ↓
gateway / protocols
  ↓
application
  ↓
agent / channels / authorization / external_tools / platforms / botspec
  ↓
contracts
```

依赖只允许从上层面向下层契约：

- `contracts` 定义身份、workspace、Agent task/event/result、工具、MCP、Skill、
  subagent、runtime 和 tool-pack DTO。
- `agent` 实现主循环、backend、上下文、工具执行、搜索与 subagent，不 import
  BotSpec、middleware 或具体平台。
- `external_tools` 实现领域工具，只依赖 contracts、core 或 shared helper。
- `channels` 实现 Gateway 原生传输的连接、codec、capability 与 provider receipt，
  不分配 AgentStrata 角色或工具权限。
- `authorization` 从可信 transport evidence 生成 `Principal`，唯一负责准入、角色策略、
  approval binding 与审计 receipt。
- `platforms` 保留尚未迁移到 Channel 的 legacy adapter；新 Gateway 平台不再经此层接入。
- `botspec` 解析实例声明并组装 runtime；它不把实例类型硬编码进 Agent。
- `application` 拥有 actor session、workspace、资源 materialization、turn pipeline，
  并把 `BotRuntimeContext` 唯一投影为 Agent runtime；它以 catalog 驱动的
  `interactive` / `detached` profile 和 typed overrides 表达 ACP、后台任务与
  Evaluation 的运行边界；新的 session capability 默认关闭、需显式选择，只有既有
  delegation/search 保留经审计的兼容默认。两个 profile 不提供推测性别名；受信
  capability factory 模块统一导出固定的 `build_provider`，runtime/session 生命周期入口
  只共享内部物化与校验逻辑。
- `gateway` 组合 Channel、authorization 与 application，拥有长期 session、run、
  durable ingress/outbox、delivery receipt、typed RPC 和 writer generation。
- `protocols` 是 ACP 等本地协议 edge；ACP 只作为认证 Gateway client，不拥有平台、
  authorization、Agent 或 workspace runtime。
- `middleware` 只保留尚未迁移的 legacy edge 和与 Gateway 无关的既有能力。
- `deploy`、`console` 与 CLI 是操作面，不定义跨层业务契约。

核心契约入口：

| 领域 | 模块 |
| --- | --- |
| 身份、角色与 conversation/turn 来源 | `contracts/identity.py` |
| Workspace | `contracts/workspace.py` |
| Agent task/event/result | `contracts/agent.py` |
| 工具 | `contracts/tools.py` |
| Adapter approval | `contracts/adapter_approval.py` |
| Principal、authorization 与 approval | `contracts/authorization.py` |
| Gateway event、resource、outbound 与 delivery | `contracts/gateway.py` |
| Gateway wire frames / typed RPC | `contracts/{gateway_protocol,gateway_rpc}.py` |
| Cooperative cancellation | `contracts/cancellation.py` |
| Runtime、subagent、Skill、tool pack | `contracts/{runtime,subagents,skills,tool_packs}.py` |
| 开发任务范围 | `contracts/development.py` |

## BotSpec 组合

每个实例由四个主要表面和运行包络组成：

- `prompts`：persona、refusal、role 和 mode 提示词。
- `tools`：本地 tool pack、MCP binding、运行特性和隐藏工具。
- `agents`：`native` / `langgraph` / `codex` 主 backend，preset、预算和 Codex
  访问策略。
- `context`：RAG、Wiki、memory、playbook、代码仓库和开发范围。
- `gateway` / `channels`：每实例 Gateway wire/state 配置与原生 Channel 声明。
- `platform`：只用于 Feishu 等 legacy adapter edge。
- `llm` / `workspace` / `deploy` / `access`：模型、目录、部署与访问控制。

BotSpec 只声明 tool-pack id。具体目录在 `tool_packs/catalog.py`，catalog 只定位显式
`ToolProvider` 模块，精确工具成员由领域 provider 自己声明；builtin 与 external 使用
同一注册机制。静态和会话动态工具统一进入 `agent/tools/registry`，Agent 与 Console 通过
同源 Registry 快照或 `component_catalog` 投影读取工具面。

Playbook reader 在 runtime 物化时绑定当前 Bot 的不可变 Skill 索引，不存在进程级
Skill registry。会话 payload filter 与后台提交器由宿主在 `new_session()` 时显式传入，
避免 runtime 级可变回调成为第二条注入路径。MCP facade 只公开可工作的 provider 与错误
类型；MCP admin 工具直接从 `external_tools.mcp_admin` 的 canonical provider 进入 catalog。

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

## Gateway、Channel 与会话

每个启用 `gateway` 的 Bot 由一个长期、回环监听的 Gateway 进程拥有 Channel 生命周期、
typed WebSocket RPC、session、run、事件游标、durable ingress/outbox、delivery receipt 和
writer generation。进程先以 state root 下的 `0600` 普通文件取得非阻塞 POSIX singleton
lease，再组装 Agent、推进 writer generation、连接 Channel 或监听端口；竞争、符号链接、
硬链接、owner/mode/inode 漂移均失败关闭，所有构建失败、取消、回滚和 shutdown 路径都释放
descriptor。客户端先接收 `connect.challenge`，再用一次性 nonce、版本范围、client
identity、scope 和强实例 token 完成 `connect`；后续 frame 只允许严格的 `req` / `res` /
`event`。ACP 是其中一个 session-scoped client edge，不拥有 Channel 或 Agent runtime。

QQ v1 Channel 直接连接用户独立维护的回环 OneBot v11 provider。强 OneBot token 只证明
事件来自配置的 provider 信任域；Channel 还必须以真实 `get_login_info` 动作确认 Bot 账号。
收到消息后，codec 从结构化帧生成不可变 event 与 transport evidence，绑定 connection
generation、account、event/message ID、sender、conversation 和 frame digest。群触发只接受
明确指向当前 Bot 账号的结构化 `at` segment；`at all`、显示名文本和 CQ-looking 文本均无效。

Authorization 在任何资源下载、task、Agent、模型、工具或 journal 副作用前，从该 evidence
构造可信 `Principal` 并解释 `QQ_ALLOW_FROM` / `QQ_ALLOW_GROUPS`。稳定群号只形成
`ConversationIdentity`，当前稳定 sender 决定 actor 与 role；群白名单只授予准入，不能提升
Owner/Admin。拒绝仅保存有界、无正文的授权审计 receipt，不保留 provider URL；通过后才把
完整 canonical event 与 Principal 持久化为 ingress。新 writer 只恢复从未 claim 的
`accepted` ingress；中断的 `processing` 进入 `recovery_required`，不会冒险重复 Agent 或工具副作用。

同一 QQ 群共享 `group_<safe-chat-id>/shared/` 的普通文件和受保护的有界 conversation
journal；QQ 私聊、不同群与其它平台继续隔离。执行 session、role、caller identity、Codex
resume、task/job control、persona/memory authority 与工具权限始终按 actor 分离。群 Agent
生成回复后，Gateway 先持久化 outbound，再请求 provider；只有取得 provider acknowledgement
才把交换写入 journal。未确认投递、取消、失败或 stale generation 会逐出本次 live actor
session，避免下一轮继承未公开的回答；journal 以稳定 outbound identity 幂等，便于补偿而不
重复历史。SQLite provider receipt 仍不等于 QQ 客户端展示或用户已读。

成员可写 shared root 不保存权威 `IDENTITY.json`、`MEMORY.md`、backend state、job/task
控制记录或 persona。群 memory/persona 和 Owner job 位于 `.conversation-state/` 的保护域；
群成员不能经 workspace 发现。`private_chat_only` 工具不会因 Owner 在群内而绕过频道限制，
任何 actor 的私聊 memory、私有 Wiki/RAG 也不会自动进入群 prompt。

OneBot media 先成为 event-bound `ResourceTicket`，包含允许大小、类型、event 与 Principal
绑定；只有 admission 与资源授权通过后才按 DNS pinning、公开地址、TLS hostname/peer、
domain allowlist、无 redirect 和字节上限下载或 materialize。旧 cc-connect basename inbox、
文本尾缀和 sender envelope 不进入 QQ Gateway。

工具授权保持三层：Registry 可见 schema、executor-time filter、domain-handler revalidation。
Gateway 已提供 durable approval storage、精确 actor/conversation/operation/params/policy binding
以及 `approvals.list` / `approvals.resolve` RPC；通用工具自动发起、暂停和恢复的完整人审工作流
尚未接线，不能把基础设施存在写成每个敏感工具都已进入人工审批。

Feishu 继续通过隔离的 legacy adapter edge 提供文档、表格、多维表格、Wiki、消息和文件
能力；其 cc-connect 依赖不得回流到 QQ 推荐部署。账号、群号、用户 ID、tenant 端点和凭据
都属于部署环境。

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
