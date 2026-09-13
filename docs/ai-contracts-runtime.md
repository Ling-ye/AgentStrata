# 运行时、身份与资源契约

本文由根协作入口按任务引用。先定位与本次变更有关的章节，再读取对应规格；跨领域变更需同时读取有关契约。

返回 [AI 协作入口](../AGENTS.md)；用户操作见 [文档中心](README.md)。

## 章节索引

- [Core 并发槽位](#rule-1)
- [Gateway/Application 交接](#rule-2)
- [Backend 创建具体 session](#rule-3)
- [配置投影归 BotSpec](#rule-4)
- [任务运行四层观测](#rule-5)
- [Channel 资源抓取](#rule-6)
- [新增 Gateway 通道](#rule-7)
- [QQ Gateway 迁移不得削弱安全保证](#rule-8)
- [Legacy 平台身份归 adapter](#rule-9)
- [QQ 群会话身份与逐轮身份分离](#rule-10)
- [QQ 群共享上下文与目录](#rule-11)
- [统一执行权限](#rule-12)
- [项目硬链接与执行终态](#rule-13)
- [开发命令超时快照](#rule-14)
- [文件检查职责](#rule-15)
- [QQ Gateway 是唯一准入 owner](#rule-16)
- [QQ 群准入不提升项目权限](#rule-17)
- [Owner 斜杠指令准入与生命周期](#rule-18)
- [平台技术能力由 Channel/adapter 声明，实例开关由 BotSpec 声明](#rule-19)
- [纯文本附件兜底只识别本地文件引用](#rule-20)
- [QQ 身份与 OneBot 边界](#rule-21)
- [QQ 外部平台检查](#rule-22)
- [任务诊断与 Gateway durable state 分层](#rule-23)
- [ACP 是 Gateway client edge](#rule-24)

## 架构与契约入口

## 项目一句话

AgentStrata 是单代码库、多机器人平台：每个 `bots/<bot-id>/` 实例以 `prompts` / `tools` / `agents` / `context` 四面声明提示词、能力、主 Agent/委托和上下文，并用 `platform` / `llm` / `workspace` / `deploy` / `access` 声明运行包络，共享底层基础设施。Python import namespace 与既有 `CHATCOPILOT_*` 环境变量作为兼容契约继续保留。

当前内置实例：

- `lingye-copilot-qq`：QQ / NapCat / OneBot 通用助手 + 按源搜索 + 直接开发能力（dev tools + mcp-server-git）。

通用飞书 adapter 与 `feishu.document` / `feishu.sheet` / `feishu.bitable` /
`feishu.wiki` / `feishu.messaging` 工具包继续公开，但不绑定任何组织、租户或具体项目。

## 运行时分层

AgentStrata 由机器人运行时，以及启动装配、控制观测、独立测评等配套部分组成。
机器人运行时按四个职责层组织，各层通过结构化契约协作，由实例宿主管理生命周期：

```text
渠道适配层（Channel）
  ↓
网关层（Gateway）
  ↓
应用层（Application）
  ↓
Agent 层
```

- Channel 负责原生连接、结构化事件校验与转换、平台资源获取实现和实际投递回执，不分配角色。
- Gateway 负责主体采信、准入与角色策略调用、run、持久化和交付协调。
- Application 负责 actor 会话、工作区、上下文准备和交换提交；Agent 负责 Backend、模型、工具和委托执行。
- 启动装配与实例宿主位于四层之外；`GatewayRuntimeHost` 管理整实例构建和启停，保留现有入口。
  `AgentRuntime` 仅指 Agent 执行引擎，装配不要求独立进程。
- Console 是控制观测入口，只读取配置及运行投影、调用控制 API；Evaluation 拥有独立生命周期，
  按目标复用隔离的 Agent 或消息链。二者都不属于机器人消息必经层。

箭头表示入站职责顺序，源码依赖由 `scripts/check_architecture.py` 约束。Gateway 向 Channel
注入入站回调，Channel 不反向 import Gateway。授权、模型访问、工具和存储按契约参与，
`contracts` 与 `core` 是支撑模块。ACP 是可直连 Gateway 的本地协议入口；旧 ACP 宿主只保留 Feishu，QQ Relay 启动链已删除。
BotSpec 负责配置解释，Application 的装配函数将配置投影为 Agent 运行输入。Console 配置与观测
分组不等同于四层，既有 `layer`、实体 ID 和历史快照保持。长期基线见
[`runtime-four-layer-definition`](../specs/runtime-four-layer-definition/spec.md)，SDD 遵循方式见 [docs/sdd.md](sdd.md)。

跨层契约只通过这些模块：

| 契约 | 模块 |
| --- | --- |
| `Role` / `AssistantMode` / `SessionIdentity` | `contracts/identity.py` |
| `WorkspaceRef` / `WorkspaceView` | `contracts/workspace.py` |
| `AgentTask` / `AgentEvent` / `AgentResult` | `contracts/agent.py` |
| `PreparedTurn` / `TurnOutcome` / `ExchangeRef` | `contracts/turns.py` |
| `ToolDef` / `ToolResult` / `ToolContext` | `contracts/tools.py` |
| `AdapterApprovalEnvelope` | `contracts/adapter_approval.py` |
| `Principal` / authorization and approval DTO | `contracts/authorization.py` |
| `CanonicalInboundEvent` / resource / outbound / delivery DTO | `contracts/gateway.py` |
| `FetchedResource` / `ResourceFetcherPort` | `contracts/resources.py` |
| Gateway wire frames and typed RPC DTO | `contracts/{gateway_protocol,gateway_rpc}.py` |
| Cooperative cancellation | `contracts/cancellation.py` |
| MCP / RAG / subagent / skill / tool pack DTO | `contracts/{runtime,subagents,skills,tool_packs}.py` |
| 开发任务范围 | `contracts/development.py` |

<a id="rule-1"></a>

## Core 并发槽位

- **Core 并发槽位**：`FileTokenLimiter` 在同一进程间锁内检查容量并创建 token，任务期间持有 token 文件锁。TTL 只能清理未锁定的遗留 token；禁止用文件名排序或年龄驱逐活跃持有者。相同限流目录的参与者使用一致版本和容量配置。

<a id="rule-2"></a>

## Gateway/Application 交接

- **Gateway/Application 交接**：Application 的 `ActorTurnExecutor` 准备回合、管理 actor 和待确认交换，`execute()` 只返回 `TurnOutcome(result, exchange)`，不暴露 actor_state。`ExchangeRef` 是绑定本进程、本轮、session 和 Principal 的不透明引用；Gateway 保留准入、run、取消、outbox、交付和 writer generation。Provider 确认且 generation 仍有效后调用 `commit_exchange()`，Application 复检 envelope/receipt 绑定并幂等提交；未确认群交换由 `discard_exchange()` 丢弃并逐出 actor。交付已确认而 journal 失败不能改写为未送达或自动重发。

<a id="rule-3"></a>

## Backend 创建具体 session

- **Backend 创建具体 session**：通用 AgentRuntime 只准备公共输入，Native/LangGraph/Codex adapter 创建各自 session；`BackendOpenRequest.options` 只承载类型化目录、隔离、恢复和角色提示参数，不传构造函数。

<a id="rule-4"></a>

## 配置投影归 BotSpec

- **配置投影归 BotSpec**：`botspec/inspection.py` 解释 BotSpec 字段、环境引用并生成配置投影；`core/inspection.py` 只做通用序列化和指纹。Console 分层配置保留记忆、RAG、MCP、子 Agent 等基础配置；Wiki、Skills、搜索 Provider、工具包和具体工具归能力与工具，历史任务保留执行时快照与原实体 ID。

<a id="rule-5"></a>

## 任务运行四层观测

- **任务运行四层观测**：任务流按真实 Channel/Gateway/Application/Agent 交接分段，运行职责标记与配置 layer 分开。新增边界用成对 Started/Finished 及真实 trace/span，正文走有界详情与现有留存；准入且 run 建立后才归档平台输入，观测投影不进入业务 ingress 序列化/指纹。Agent 公开输出独立于投递保存，交换提交只报告实际结果；Codex 只标适配器可见范围，隐藏推理不采集。观测失败不能改变权限、执行或交付事实。规格见 `specs/console-layered-observability/spec.md`。 Console 不再从旧事件名补造调用关联或回退读取 Gateway 业务状态库；任务流只消费当前四层观测版本。

<a id="rule-6"></a>

## Channel 资源抓取

- **Channel 资源抓取**：QQ CDN 实现位于 `channels/qq_onebot/resources.py`，只通过 `contracts/resources.py` 的 `ResourceFetcherPort`/`FetchedResource` 交接有界字节。Application 负责票据、actor/workspace 绑定及原子文件发布；移动实现不得弱化 DNS/TLS、大小和文件校验。

<a id="rule-7"></a>

## 新增 Gateway 通道

- **新增 Gateway 通道**：新的原生传输放在 `channels/<name>/`，实现连接生命周期、codec、provider capability、资源获取与回执，并在实例装配入口显式接线；不能分配 AgentStrata 角色或授权工具。`platforms/<name>/adapter.py` 的 `ADAPTER` 自动发现只保留给尚未迁移的 legacy edge；平台分支仅限装配入口，不进入共享执行逻辑。

<a id="rule-8"></a>

## QQ Gateway 迁移不得削弱安全保证

- **QQ Gateway 迁移不得削弱安全保证**：QQ BotSpec 使用顶层 `gateway` 与 `channels.qq`；每个
  实例由 systemd 以前台 `python -m chatcopilot run --bot <exact-bot>` 运行唯一 Gateway，并
  在组装 Agent、推进 writer generation、连接 Channel 或监听端口前取得 state root 下的非阻塞
  singleton lease；竞争或 owner/mode/symlink/hardlink/inode 校验失败必须关闭，构建、取消、回滚和
  shutdown 都必须释放 descriptor。实例宿主通过 QQ Channel 连接用户独立维护的回环 NapCat/OneBot provider。
  Channel 校验账号、发送者、会话和结构化 @；Gateway 采信绑定证据、完成准入和角色计算并持久化
  受理记录与 run，Application 复检资源绑定。这些门禁必须先于 Agent、模型、工具、附件或
  journal 副作用；迁移不得删除或弱化既有 fail-closed、actor isolation、权限审核和
  evidence 分级保证。QQ 推荐部署不安装、渲染或启动 Node、cc-connect 或 QQ @ Relay；ACP 是
  可选本地 Gateway client edge，不拥有 Channel、平台身份、准入、权限或 Agent runtime。

<a id="rule-9"></a>

## Legacy 平台身份归 adapter

- **Legacy 平台身份归 adapter**：只有 Feishu 等尚未迁移的 legacy edge 继续由 adapter 归一化 `session_key` / hook 字段；这些字段不能进入 QQ Gateway 的身份、准入或资源路径。

<a id="rule-10"></a>

## QQ 群会话身份与逐轮身份分离

- **QQ 群会话身份与逐轮身份分离**：QQ Channel 从已认证 OneBot 结构化帧产生不可变 transport evidence；稳定群号只形成 `ConversationIdentity`，稳定发送者另形成当前 `Principal`。账号、event/message ID、sender、conversation、connection generation 与帧摘要必须绑定，显示名和 provider 实现名不参与授权。Channel 在生成规范事件前校验结构化 @，Gateway 在资源 materialization、task、Agent、模型、工具和 journal 副作用前完成主体采信、准入与角色计算；缺失、畸形、跨账号、跨会话、重复 ID 漂移或发送者不匹配时失败关闭。本地 fake OneBot 测试不能替代真实两账号 QQ ingress E2E。

<a id="rule-11"></a>

## QQ 群共享上下文与目录

- **QQ 群共享上下文与目录**：同一 QQ 群共享有界 conversation journal 和 `<workspace-root>/group_<safe-chat-id>/shared/` 中的普通文件，不同群、QQ 私聊与其它平台继续隔离；旧 `group_<id>/user_<id>/` 不自动迁移，也不能从 shared root 穿越。说话人变化时选择该 actor 绑定的执行 `SessionState`，通过 journal 注入群历史，不得复用其他 actor 的 executor、Codex resume、调用者身份或受保护任务。成员可写的 shared root 不保存权威 `IDENTITY.json`、`MEMORY.md`、backend state、job/task 控制记录或 persona；权威群 persona 与群 memory 位于 workspace 根的 `.conversation-state/persistent/` 保护域，以平台、会话类型和稳定群号摘要寻址，不暴露原始群号。群 Codex 只在同一 live actor session 内 resume；未获得 provider acknowledgement 的交换必须逐出对应 live actor state，不能污染下一轮；成功投递后的 journal 写入使用稳定 outbound identity 幂等。

<a id="rule-12"></a>

## 统一执行权限

- **统一执行权限**：业务权限只有 Owner/member。Owner 在实例资源与已配置项目范围内使用全部已装配工具，三个 Backend、群聊与私聊一致；Admin/User 只使用明确声明 `access: member` 的公共查询、当前会话普通文件及记忆 read/append。ToolDef 默认 `access: owner`，不再按工具名、Backend、private_chat_only 或旧访问模式叠加 Owner 限制。Application 下发 ExecutionScope；文件工具与命令进程必须执行资源范围，cwd 不能代替隔离。Owner Codex 可写获准目录；所有角色恢复 Codex 默认原生功能；成员原生读写只限当前普通工作区，项目仍仅授权 Owner。内层权限配置禁止原生命令读取 Codex auth.json 与 MCP relay 配置，外层 bubblewrap 执行资源挂载。保持 actor/resume 隔离，群输出独立脱敏，不替换可信角色。权威人格、记忆和状态仍经管理服务操作。规格见 `specs/runtime-permissions-simplification/spec.md`。

<a id="rule-13"></a>

## 项目硬链接与执行终态

- **项目硬链接与执行终态**：挂载装配不遍历文件树扫描硬链接；普通文件在执行路径范围内允许硬链接读取、原子替换和删除，接受已放入普通共享目录的 inode 内容通过该路径可见。权威状态仍保留单链接等对象校验，解压链接不得越出目标目录。Gateway 按 AgentResult 保存原始停止原因，`llm_error` 为失败；已确认投递保持独立事实，不因执行失败重发。规格见 `specs/gateway-execution-outcomes/spec.md`。

<a id="rule-14"></a>

## 开发命令超时快照

- **开发命令超时快照**：装配阶段解析 `context.dev.shell` 和已有环境覆盖，CommandTimeouts 随 AgentRuntime、ExecutionScope 及后台请求传递；绑定工具不得重读环境或回退重建默认预算。路径 guard 负责范围，文件 I/O 与可信交付各自调用共享单文件校验；交付的 allow/deny 策略仍有效。规格见 `specs/command-timeout-snapshots/spec.md`。

<a id="rule-15"></a>

## 文件检查职责

- **文件检查职责**：Core 的 `file_integrity` 统一普通文件元数据和受信源码哈希读取；Evaluation 的 `private_files` 统一私有产物元数据检查，在实际 I/O 边界复用，不能用一次入口检查替代读写期间的身份复检。TAR 使用标准库 data filter 保留目标目录约束，允许包内链接且不设应用解压容量上限。规格见 `specs/file-boundary-simplification/spec.md`。

<a id="rule-16"></a>

## QQ Gateway 是唯一准入 owner

- **QQ Gateway 是唯一准入 owner**：Gateway 在认证 OneBot transport 后、资源下载和 Agent 副作用前完成准入。机器人加入的群无需白名单，有效群消息在结构化 @、发送者与会话身份校验后允许。`QQ_ALLOW_FROM` 只声明允许私聊的稳定发送者 ID；缺失或空值拒绝私聊，精确 `*` 允许全部私聊，有限名单只接受逗号分隔数字 ID。`QQ_ALLOW_GROUPS` 已删除，不解析、不参与准入、不生成配置或导出到新 runtime env。群准入不授予私聊权限或提升 Owner/Admin。旧 QQ BotSpec 准入字段及 `QQ_REQUIRE_AT_IN_GROUP` / `QQ_AT_ALL_COUNTS` 仍拒绝；ACP client 不解释 QQ 身份、名单或角色。规格见 `specs/qq-group-open-admission/spec.md`。

<a id="rule-17"></a>

## QQ 群准入不提升项目权限

- **QQ 群准入不提升项目权限**：开放群准入不得把群成员提升为 Owner/Admin；每轮角色只按稳定发送者 ID 解析。真实 Owner 在私聊和群聊都保持 Owner prompt、工具、Codex 和代码任务权限，群聊输出与工具 payload 按公开群场景脱敏，不自动注入任何 actor 的私聊 memory/Wiki/RAG。User/Admin 群成员只保留公开搜索、当前群共享普通文件、当前群受保护 memory 的 read/append 和获准同步能力，不能读取项目/主机/配置/内部资料/其他用户数据、读取或修改任何 persona、清空整份群 memory、访问 Owner job 或获得高级工具。拒绝只写有界授权审计事实，不保存原始正文或 provider 资源 URL；通过准入后才持久化 ingress 和启动 task。任何 admitted-intake、task 或受保护状态持久化失败都必须在模型、工具和资源 materialization 副作用前失败关闭。非法工具访问声明在装配时失败关闭；未显式声明成员访问的新工具默认仅限 Owner。

<a id="rule-18"></a>

## Owner 斜杠指令准入与生命周期

- **Owner 斜杠指令准入与生命周期**：去除平台 envelope 后，用户正文去除前导空白并以 ASCII `/name` token 开头、后接空白或正文结束时才识别为斜杠指令；绝对路径、URL、`//name` 和正文中间的 slash 仍是普通输入。所有已识别指令一律只允许本轮认证 Gateway `Principal`、准入和身份激活共同确认的可信 Owner；统一门禁位于身份激活之后、资源 materialization 及 Session/Agent/模型/工具之前，群准入、昵称、历史回合或共享 session 不得提升权限。`/help` 必须从当前 Bot 实际注册且启用的同一命令目录生成；`/state` 只投影当前会话和可信 runtime 绑定的当前 Bot systemd unit 的有界脱敏状态。`/restart` 不接受目标或参数，只重启当前 Bot unit，不清理 workspace、journal、memory、persona、backend resume 或 task/job 状态，也不操作外部 OneBot provider；仅在接受回复送达和指令 task 终态持久化后，才允许通过 Bot cgroup 外的 systemd transient unit 延迟执行，任何身份、投递、持久化、systemd、同实例 transient-unit 冲突或调度异常都失败关闭，禁止用进程内后台任务、`nohup` 或 `setsid` 降级，也不得把“请求已接受”描述为“重启已完成”。timer 注册后的回执落盘失败只能 best-effort 停止 transient units；即使目标 generation 尚未变化也不得声称已撤销，因为 systemd manager 可能已经排队 restart。

<a id="rule-19"></a>

## 平台技术能力由 Channel/adapter 声明，实例开关由 BotSpec 声明

- **平台技术能力由 Channel/adapter 声明，实例开关由 BotSpec 声明**：Gateway QQ 使用 `channels.qq`；Feishu legacy 使用 adapter；`chat.file_uploads` / `chat.private_workspace` 属于 `tools.features`。

<a id="rule-20"></a>

## 纯文本附件兜底只识别本地文件引用

- **纯文本附件兜底只识别本地文件引用**：匹配路径或文件名前先排除 `http://` / `https://` URL。

<a id="rule-21"></a>

## QQ 身份与 OneBot 边界

- **QQ 身份与 OneBot 边界**： QQ Owner/Admin 只按稳定 `user_id` 授权，昵称不参与匹配；飞书 adapter 保留姓名兜底。 `QQ_ACCESS_TOKEN` 必填且必须为 32–128 位 URL-safe 字符；`sync-token` 幂等复用或生成强 token，只替换 bot-owned `local.env` 的对应键并保留全部其他键，再同步运行时 env 与 NapCat `3001` 配置；WebUI 管理 token 是另一凭据，只用于登录 localhost 管理面板。 OneBot `3001`、WebUI `6099` 只绑定 `127.0.0.1`；控制台 WebUI 登录只调用安全 `bootstrap`，正式 start/restart 仍在任何停止动作前校验强 token。 双向探针必须实际执行 OneBot 动作，以兼容 NapCat 握手后发送 `1403` 再关闭的拒绝语义；provision、渲染、gateway 与实例启动遇到空/弱 token、非回环 URL 或双向认证失败时必须 fail closed。

<a id="rule-22"></a>

## QQ 外部平台检查

- **QQ 外部平台检查**：QQ/NapCat/OneBot 的真实连通性不属于 Agent Evaluation，不创建 Evaluation/Trial、不调用模型、不影响 Agent verdict。新 Gateway 的 hermetic integration 只在随机回环端口使用假 OneBot provider、真实 Channel/Gateway 和确定性 Agent，不能解释成真实 QQ/Agent E2E。旧 `qq_message_flow` suite 必须显式列出 `qq_platform/napcat/cc_connect/agent_model` 替代层并标记 legacy。可选群消息探针必须同时提供 `--send-message` 与单次 `--confirm-external-write`，目标只能来自 bot-local `CHATCOPILOT_EXTERNAL_CHECK_QQ_GROUP_ID`。缺少独立发送 QQ 时，真实入站 Agent 往返必须报告 `not_tested`，不得用模拟帧或 Bot 自发消息冒充端到端通过。

<a id="rule-23"></a>

## 任务诊断与 Gateway durable state 分层

- **任务诊断与 Gateway durable state 分层**：只有经过 transport verification、identity 与 admission 的消息才能创建 `task_...` 并进入 Agent；拒绝只保留有界、无正文的 authorization decision receipt。Gateway SQLite 另行拥有 ingress、session、run、event cursor、outbox 与 delivery receipt，不能把 task JSON 当成平台投递事实。`job_...` 仍是后台长任务，Owner job 按 actor digest 位于受保护状态；群内 workspace 不可读取。Gateway 运行端持续写独立观测索引与任务正文，Console 只读查询分层配置快照、分页历史、阶段、指标、审批和回执；正文按终态结束时间保留 30 天，活动及恢复任务不清理，摘要长期保留；禁止推进 generation、读取原始 ingress 或套用 legacy ACP 八层证据。无 run 关联键的准入审计只能作为实例审计；诊断写失败不改变权限或交付结果，历史缺记录与截断必须显示。

<a id="rule-24"></a>

## ACP 是 Gateway client edge

- **ACP 是 Gateway client edge**：`protocols/acp/server.py` 只映射 ACP 帧、session lifecycle、prompt/cancel 与 Gateway typed RPC；它不能 import 或重新拥有 Agent、QQ、BotSpec、authorization、workspace 或 task runtime。连接中断恢复使用原始 params/idempotency key、`runs.get` / `runs.latest` 与 `deliveries.get`，不得以新输入替代旧 run。
