# AgentStrata 架构

AgentStrata 是单代码库、多机器人运行平台。`bots/<bot-id>/bot.yaml` 选择实例
能力，公共基础设施通过稳定 contracts 连接，平台与领域实现不能反向渗入 Agent。

## 分层与依赖

机器人运行时按四个职责层组织，消息入站顺序如下，结果沿相邻边界返回：

```text
Channel
  ↓
Gateway
  ↓
Application
  ↓
Agent
```

| 层 | 职责 |
| --- | --- |
| Channel | 原生连接、结构化平台事件校验与转换、provider capability、平台资源获取实现和实际投递；不分配 AgentStrata 角色或工具权限 |
| Gateway | 主体采信、准入和角色策略调用、持久 session/run、取消、ingress/outbox、delivery receipt、typed RPC 与 writer generation |
| Application | actor 执行会话、workspace、资源 materialization、上下文准备、Agent 调用和本轮交换提交或丢弃 |
| Agent | Backend、模型与工具循环、上下文窗口处理、搜索与 subagent 委托 |

四层表示消息处理职责，不要求所有调用依次穿过四层。Application 在资源授权后可经
`ResourceFetcherPort` 调用 Channel 的下载实现；Gateway 持久化 outbound 后才请求
Channel 投递，取得可信回执后再请求 Application 提交交换。授权、模型访问、工具和存储
通过契约支撑运行；`contracts` 与 `core` 提供基础类型和通用实现。长期 SDD 架构基线见
[机器人运行时四层架构基线](../specs/runtime-four-layer-definition/spec.md)，此前的配置集中与
会话交接实现见[分层职责精简重构](../specs/runtime-layer-responsibility-refactor/spec.md)。
后续相关规格必须按 [SDD 规范](sdd.md)引用该基线，说明职责、交接契约和依赖方向。

消息方向与 Python import 方向分别约束。Gateway 依赖 Channel 端口并注入入站回调，
Channel 不导入 Gateway；Gateway 调用 Application，Application 调用 Agent。
`scripts/check_architecture.py` 检查静态依赖，四层箭头不替代该检查：

- `channels` 只依赖基础模块，不 import Gateway、Application 或授权策略实现。
- `application` 不 import Channel、Gateway、protocols、middleware 或具体平台实现。
- `agent` 不 import BotSpec、Application、Gateway、Channel、middleware 或具体平台。
- `external_tools` 实现领域工具，只依赖 contracts、core 或 shared helper。
- `authorization` 提供 Principal 构造、准入与角色策略、approval binding 和审计契约；
  Gateway 调用这些策略决定准入，各执行入口保留自己的权限复检。
- `contracts` 定义身份、workspace、Agent task/event/result、工具、MCP、Skill、
  subagent、runtime 和 tool-pack DTO；`botspec` 解析实例及环境配置并生成配置投影。

启动装配、控制观测和独立测评是四层之外的配套职责：

- `run.py` 和 `gateway/runtime.py` 的唯一构建入口在机器人进程内完成启动装配；
  `GatewayRuntimeHost` 管理实例启停，不是第五个消息层。`AgentRuntime` 指 Agent 执行引擎。
  装配属于外部职责不要求另起进程，也不要求移动现有模块。
- `application/agent_runtime.py` 保留共享装配入口，将 `BotRuntimeContext` 投影为
  Agent 运行输入；它以 catalog 驱动的
  `interactive` / `detached` profile 和 typed overrides 表达 ACP、后台任务与
  Evaluation 的运行边界；新的 session capability 默认关闭、需显式选择，只有既有
  delegation/search 保留经审计的兼容默认。两个 profile 不提供推测性别名；受信
  capability factory 模块统一导出固定的 `build_provider`，runtime/session 生命周期入口
  只共享内部物化与校验逻辑。
- `console` 通过现有配置、运行投影和控制入口工作，不创建 Agent、模型客户端或 MCP 连接；
  历史观测读取不要求机器人进程在线。
- `evals` 拥有独立 service、worker、生命周期和记录；隔离 Agent 测试复用共享装配，
  不经过线上 Gateway session 或真实 Channel 投递。
- `protocols` 是 ACP 等本地协议 edge；ACP 可直接作为认证 Gateway client，不拥有平台、
  authorization、Agent 或 workspace runtime。
- `platforms` 与 `middleware` 保留尚未迁移的 legacy edge 和既有能力；新 Gateway
  平台通过 Channel 实现并在装配入口显式接线，不扩展 legacy adapter 路径。

核心契约入口：

| 领域 | 模块 |
| --- | --- |
| 身份、角色与 conversation/turn 来源 | `contracts/identity.py` |
| Workspace | `contracts/workspace.py` |
| Agent task/event/result | `contracts/agent.py` |
| 已准备回合、执行结果与交换引用 | `contracts/turns.py` |
| 工具 | `contracts/tools.py` |
| Adapter approval | `contracts/adapter_approval.py` |
| Principal、authorization 与 approval | `contracts/authorization.py` |
| Gateway event、resource、outbound 与 delivery | `contracts/gateway.py` |
| 有界资源字节与 Channel fetch port | `contracts/resources.py` |
| Gateway wire frames / typed RPC | `contracts/{gateway_protocol,gateway_rpc}.py` |
| Cooperative cancellation | `contracts/cancellation.py` |
| Runtime、subagent、Skill、tool pack | `contracts/{runtime,subagents,skills,tool_packs}.py` |
| 开发任务范围 | `contracts/development.py` |

## BotSpec 组合

每个实例由四个主要表面和运行包络组成：

- `prompts`：identity/style、refusal、role 和 mode 提示词。
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

Application 的 `project_agent_runtime()` 在组装边界捕获环境值，复制配置并解析研究、搜索和
子 Agent 的模型覆盖及搜索凭据；`materialize_agent_runtime()` 再据此创建运行对象。
逐轮搜索与委托复用实例持有的模型客户端，不重新解析这些环境覆盖。相同模型配置在同一 runtime
中复用客户端；`AgentRuntime.close()` 去重关闭模型客户端、MCP 和检索资源，组装失败也
回收已经创建的资源。`LLMClient` 和共享限流仍使用现有 Core 入口。

`botspec/inspection.py` 拥有 BotSpec 字段解释、环境引用和配置实体投影；
`core/inspection.py` 只提供通用序列化和指纹。Console 的分层配置展示当前基础配置，
记忆、RAG、MCP 与子 Agent 留在此页；Wiki、Skills、搜索 Provider、工具包和具体工具在
能力与工具页。历史任务保持执行时快照，页面分组不改变事件实体 ID 或配置指纹。

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

通用 `AgentRuntime` 准备 PromptPlan、工具与调用者输入，具体 session 由 backend adapter
创建。Native/LangGraph 的构造位于 `agent/backends/inprocess.py`；`BackendOpenRequest`
的类型化 options 仅传递目录、隔离、恢复与角色提示等实际参数，不传 session factory。

- Native：内置模型/工具循环。
- LangGraph：使用同一契约的图执行器。
- Codex：实例主会话使用 Codex；Owner 源码写入通过独立 code-worker 与草稿 PR
  交付。成员只获得当前 conversation workspace；QQ 私聊仍按用户隔离，QQ 群聊指向
  当前群的共享 workspace。QQ 群 Codex 额外使用 fail-closed bubblewrap：只读暴露精确
  shared root，禁用可直接写入的内建 shell/`apply_patch` 等路径，文件 mutation 只能通过
  actor-bound、workspace-scoped Session Gateway MCP 执行。

主 Agent 生成面向用户的结果，由宿主负责交付；Subagent 只通过 delegate 工具运行并用
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

每个启用 `gateway` 的 Bot 由同一实例宿主装配四层运行对象。现有 `GatewayRuntimeHost`
在机器人进程内管理启动与停止；Gateway 管理 Channel 的准备、启用、停止和健康状态，
Channel driver 管理具体连接、收发与重连。Gateway 的回环服务继续拥有 typed WebSocket RPC、
session、run、事件游标、durable ingress/outbox、delivery receipt 和 writer generation。
宿主先以 state root 下的 `0600` 普通文件取得非阻塞 POSIX singleton
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

Gateway 在任何资源下载、task、Agent、模型、工具或 journal 副作用前，调用 authorization
策略从该 evidence 构造可信 `Principal` 并解释 `QQ_ALLOW_FROM` / `QQ_ALLOW_GROUPS`。稳定群号只形成
`ConversationIdentity`，当前稳定 sender 决定 actor 与 role；群白名单只授予准入，不能提升
Owner/Admin。拒绝仅保存有界、无正文的授权审计 receipt，不保留 provider URL；通过后才把
完整 canonical event 与 Principal 持久化为 ingress。新 writer 只恢复从未 claim 的
`accepted` ingress；中断的 `processing` 进入 `recovery_required`，不会冒险重复 Agent 或工具副作用。

同一 QQ 群共享 `group_<safe-chat-id>/shared/` 的普通文件和受保护的有界 conversation
journal；QQ 私聊、不同群与其它平台继续隔离。执行 session、role、caller identity、Codex
resume、task/job control、persona/memory authority 与工具权限始终按 actor 分离。群 Agent
生成回复后，Gateway 先持久化 outbound，再请求 provider；只有取得 provider acknowledgement
并复查 writer generation，才通知 Application 提交交换。Application 的 `commit_exchange()`
复检本轮、session、Principal、outbound 和 receipt 绑定后，将交换按 outbound identity 幂等
写入 journal。未确认投递、取消、失败或 stale generation 通过 `discard_exchange()` 丢弃
未确认群交换并逐出对应 live actor session，避免下一轮继承未公开的回答。Provider 已确认而
journal 写入失败时保留交付事实和提交错误，不自动重发。SQLite provider receipt 仍不等于
QQ 客户端展示或用户已读。

`ActorTurnExecutor.prepare_client()` / `prepare_channel()` 负责创建 `PreparedTurn`；Channel
路径的工作区与资源准备由 Application 执行。`execute()` 只向 Gateway 返回
`TurnOutcome(result, exchange)`，其中 `ExchangeRef` 仅在创建它的进程内有效；actor 状态与
待确认交换由 Application 私有持有。Gateway 不访问 actor 内部状态，继续拥有准入、run、
取消、outbox 与交付事实。

成员可写 shared root 不保存权威 `IDENTITY.json`、`MEMORY.md`、backend state、job/task
控制记录或 persona。群 memory/persona 和 Owner job 位于 `.conversation-state/` 的保护域；
群成员不能经 workspace 发现。`private_chat_only` 工具不会因 Owner 在群内而绕过频道限制，
任何 actor 的私聊 memory、私有 Wiki/RAG 也不会自动进入群 prompt。

OneBot media 先成为 event-bound `ResourceTicket`，包含允许大小、类型、event 与 Principal
绑定；只有 admission 与资源授权通过后才按 DNS pinning、公开地址、TLS hostname/peer、
domain allowlist、无 redirect 和字节上限下载或 materialize。旧 cc-connect basename inbox、
文本尾缀和 sender envelope 不进入 QQ Gateway。

QQ CDN 下载实现位于 `channels/qq_onebot/resources.py`，通过
`contracts/resources.py` 的 `ResourceFetcherPort` 返回有界 `FetchedResource`；Application
负责票据与 actor/workspace 绑定、文件验证和原子发布，Channel 不依赖 Application。

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

Evaluation 在机器人四层消息链之外拥有独立生命周期。直接 Agent Trial 使用共享装配入口
创建隔离 Agent，会话、workspace 与记录均属于本次测评；需要验证消息链时使用对应的受控
测评路径，不复用线上实例的会话或交付状态。

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
四层职责的长期基线是
[`runtime-four-layer-definition`](../specs/runtime-four-layer-definition/spec.md)；
检查器的实现约束与历史硬化记录见
[`architecture-boundary-hardening`](../specs/architecture-boundary-hardening/spec.md)。

```bash
python scripts/check_architecture.py
python scripts/check_sdd_specs.py
python -m chatcopilot botspec validate bots/lingye-copilot-qq/bot.yaml
python scripts/check_repo.py fast
```

字段参考见 [bot-spec.md](bot-spec.md)，运行时细节见 [runtime.md](runtime.md)，日常命令
见 [operations.md](operations.md)。
