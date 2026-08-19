# AgentStrata 运行时

运行时把 BotSpec、平台身份、模型配置、工具、上下文和权限装配成一个可持续多轮会话。

## 启动路径

```text
bot.yaml
  → load_botspec / validate
  → assemble_runtime_context
  → apply_runtime_env
  → platform adapter + tool registry + backend
  → ACP server
  → SessionState
  → AgentTask / AgentEvent / AgentResult
```

`python -m chatcopilot run --bot bots/<id>/bot.yaml` 是运行入口。CLI 只接受 ACP
transport；平台连接由 cc-connect 和对应 adapter 处理。

## Turn runtime

Native、LangGraph 和 Codex backend 共享 `AgentTask` / `AgentEvent` / `AgentResult`
以及模型、工具、上下文和生命周期事件语义。Native 与 LangGraph 复用
`agent/turn.py` 的 `TurnOps`；Codex adapter 把 Codex CLI 的公开 JSONL 事件投影为同一
契约。Backend 由 BotSpec 固定选择，不存在按消息文本切换 backend 的第二套路由器。

AgentSession 使用 soft cap、健康检查和 hard cap：

- 迭代 soft cap 到达后检查重复调用与连续失败；健康时可继续。
- hard iteration cap 无条件停止。
- soft timeout 到达后检查最近工具活跃；hard timeout 无条件停止。
- 缺失 tool result 的 orphan tool call 会在下次模型调用或截断前补成结构化错误。

## 统一上下文可观测性

每次主 Agent 或 subagent 的 turn 模型调用前都会发出 `ContextSnapshotPrepared`：

- `session_messages` 是 AgentStrata 已知的完整会话 ledger；
- `effective_messages` 是 AgentStrata 在该次调用边界实际提交的输入；
- `tool_schemas` 与 path-free resource receipts 说明该次请求的工具面和多模态输入；
- backend、model、iteration、trace/span、上下文策略、token 粗估和模型选择元数据用于
  把快照与模型步骤关联。

Native 与 LangGraph 的 effective context 在 `LLMClient.chat` 前、完成上下文窗口选择、
工具结果摘要和预算提示后捕获；纯文本调用标记为 `exact_model_input`，含本地图片时
标记为 `partial`，正文不落盘二进制或路径，只保留每次模型迭代的 path-free receipt。
Codex 捕获 AgentStrata
实际写入 `codex exec` stdin 的 prompt envelope、已批准 MCP 工具面和资源 receipts，
标记为 `adapter_visible`；Codex 原生线程保存的历史、内部 instructions 和隐藏推理不由
AgentStrata 控制，必须显示为 `provider_opaque`，不能把空白当作完整上下文。

任务摘要只保存快照索引。正文先过滤 secret-bearing 字段、当前环境中的 secret 值、
Bearer/inline credential 与机器根路径，再原子写入
`tasks/<task-id>/contexts/<snapshot-id>.json`；目录和文件分别收紧为 `0700` / `0600`。
超出 8 MiB 的快照降为带 digest 和计数的显式 truncated manifest。隐藏
chain-of-thought 不持久化；Codex reasoning activity 只记录公开生命周期和状态。脱敏、
私有推理剔除和资源路径替换共享 node/item/聚合字符串预算；JSON 在 `loads` 前先检查
字节、深度、结构数量、字符串总量和异常长数字，预算耗尽时降为明确的 `partial` /
`truncated`，不会先无界复制或解析。

`events.jsonl` 同样在首次落盘前脱敏，并为每个任务分配单调 `sequence` 与稳定
`event_id`。事件 writer 先用 `O_DIRECTORY|O_NOFOLLOW`、owner、inode 和 containment
校验任务目录，再通过 `dir_fd` 打开私有 lock、sequence sidecar 与 JSONL；sequence 限制
为 int64，崩溃导致 sidecar 落后时以最后一条完整 JSONL 记录为准恢复。私有 sidecar 让
正常追加不必反复扫描增长中的 JSONL；密集 provider activity 只节流重写任务摘要，终态
会刷入保留范围内的最终状态和明确裁剪计数。上下文正文写入失败时 recorder
留下与 LLM span 关联的 `unavailable` 摘要；即使未来 backend 漏发快照，LLM start 也会
投影同类缺口。观测写入失败由 recorder 和安全事件 sink 隔离，不能改变主 Agent 的用户
结果。

为防止 provider 事件洪泛放大任务摘要，`task.json` 最多保留 500 条 provider activity
摘要，并对工具/步骤投影设置 1000 条硬上限；裁剪计数进入 `activity_summary` 和
`summary_limits`。LLM 调用、上下文快照和输入资源索引也分别有上限，达到上限时保留
最新调用，并返回 total/retained/truncated；总量降级仍优先保留不含路径的快照索引。
`events.jsonl` 每条记录最多 64 KiB，超大参数或结果改写为带 digest、
字节数和 trace/span 关联字段的 manifest。事件正文仍按 append-only 方式持久化，Console
只读取有界尾部。后台 Job 阶段事件使用相同的首次落盘脱敏、单事件上限和私有文件
约束，Console 对遗留事件再做一次读取侧脱敏。`task.json` / `turn.json` 本身也有 8 MiB
总上限；后台子任务的 summary、error 和 outputs 先按字段与条数收敛，超出内容以 digest
和 omission manifest 表示，不能让后续子任务终态因旧摘要过大而无法写入。task/job 的
目录及祖先均从可信 descriptor 逐级 `openat`，任何 symlink 都不能把可观测 artifact
重定向到 workspace 外；Console 读取 event tail 和 context 时也保持同一 descriptor 链，
不在校验后退回 path-based open。Job 的 request/status/result/notification JSON 统一使用私有、
无符号链接、8 MiB 有界读写；worker 在创建 executor 或启动更新子进程前验证 request
envelope，损坏或超限的终态 result 以不含正文的完整性 manifest 结束，并以实际持久化
manifest 决定 status、error code 和进程退出码。

后台子任务完成与 recorder 写 task/turn 共用 private completion lock。快速 child 在主 turn
关闭 job 注册边界前只合并结果，不能提前把任务标成终态；边界关闭后等待已注册 child
全部结束并只发一次 `task_finished`。主 turn 已失败时，迟到的成功 child 不会覆盖失败
provenance，任务保持可轮询直到最后一个 child 收口后以 failed 结束。

本版本不为 topic classifier、quality gate、search router 和 reranker 等独立 helper-model
调用保存完整上下文 artifact；它们继续使用既有 step/usage 观测。这里的“每次调用”仅指
共享 turn runtime 管理的主 Agent 与 subagent 模型边界，不能解释为进程内所有
`LLMClient.chat`。

## Session 与 workspace

平台 adapter 先声明群 conversation 采用 actor scope 还是 chat scope。运行时把稳定会话
范围表示为 `ConversationIdentity(platform, chat_kind, chat_id)`，把当前消息来源表示为
`TurnIdentity(sender_user_id, sender_user_name, message_id, source)`；conversation 与当前
说话人不是同一个权限对象。

QQ 群使用 chat scope：`qq:g:<group-id>` 共享 session key 只标识群，cc-connect 必须在每个
prompt 首行注入 sender envelope，并由同步 `message.received` hook 把真实 transport actor 与
原始正文摘要追加到实例私有、有界加锁的 JSON attestation 队列。state/lock 位于实例 `0700`
session-env 目录，按 exact session key 的 SHA-256 命名且自身为 `0600`；wrapper 通过严格 JSON
白名单 loader 只传递稳定 conversation 字段，绝不 shell source。Middleware 在 access gate、
附件导入、task 创建、工具或模型执行前交叉校验 envelope 的平台/群/发送者与 hook 的发送者/
正文摘要，并按随机记录 ID 精确消费一条；缺失、陈旧、
畸形、跨群或不匹配均失败关闭。稳定发送者 ID继续用于准入和角色计算，显示名不参与授权。共享群聊不再依赖刷新临时 env 后销毁并重建
单一 `SessionState`；每轮选择当前 actor 绑定的执行 session，避免权限 filter、file sender、
caller identity 或 Codex resume state 跨成员复用。群 actor 在同一 live execution session 内
保留 Codex resume；LRU 逐出或进程重启后不恢复旧 native thread，而是从有界 journal 建立新线程，
避免内存 cursor 归零后重复注入旧历史。部署必须同时渲染 cc-connect 的
shared-channel session、sender injection 与同步 hook；systemd 托管实例每次启动前必须从当前
BotSpec 重新渲染配置，避免 cc-connect 在重启后继续读取旧 hook 集。本地合成 ingress 测试只覆盖本机边界，不代表
真实两账号 QQ ingress E2E。

Workspace 与 conversation scope 一致：

- QQ 群：`<workspace-root>/group_<safe-chat-id>/shared/`，同群成员共享普通文件、已经明确
  归属该群的附件/结果和 conversation-local 职业数据。
- QQ 私聊：`<workspace-root>/p2p_<safe-user-id>/`，继续按用户隔离。
- 不同 QQ 群、QQ 私聊和其它平台彼此隔离；非 QQ adapter 默认保留 actor-scoped 群目录。
- 旧 `group_<id>/user_<id>/` 不自动迁移，shared root 不能枚举或穿越这些目录。

群 conversation 的已接受交换写入 shared root 的受保护兄弟目录 `.conversation-state/`
内的有界 journal；配对 metadata 持久化 epoch、单调 sequence 高水位、记录范围与内容摘要，
删除、截断或旧快照回退会让整个群 actor cache/backend 失败关闭，而不会从 sequence 1 静默重启。
journal 记录授权/审计使用的原始稳定 actor 与 transport source；模型侧只看到
conversation-scoped actor reference、明确标为不可信的有界历史与运行时生成的当前来源附录。
actor-scoped backend state 和 transcript 也位于该保护目录。拒绝准入或身份无效的消息不写入。
成员可写的 shared root 不保存权威 `IDENTITY.json`；群 scope 与逐轮 actor 只存在于 runtime
contract 和受保护状态中，不会被最后一个说话人或同名符号链接改写。QQ 群不在 member-writable
shared root 创建 `MEMORY.md`，而是按稳定群身份使用 `.conversation-state/persistent/memory/group/<digest>/MEMORY.md`；所有准入成员可读写，只有 Owner 可整份清空。member-visible `task_...` diagnostics 仍禁用；已接受回合的 Console task 记录按 actor
写入 `.conversation-state/task-actors/<actor-digest>/tasks/`，原始 actor ID 不形成路径段，群内
`get_task_status` 与 workspace 工具均不能读取。准入拒绝的消息仍在该 actor 分区写终态 task，
但不会创建 actor 执行 session；身份无效的消息只在 `.conversation-state/task-intake/tasks/`
写入不含原始正文、sender envelope 或发送者账号的脱敏失败 task。ACP 在准入、附件、模型和工具
副作用前要求任务记录创建成功；存储不可用时失败关闭。
普通成员不能启动后台 job；Owner 的后台
job 控制面按 actor 写入 `.conversation-state/jobs/`，不会暴露到 `shared/jobs`。当前群交流风格
使用 `.conversation-state/persistent/persona/global/PERSONA.md` 与 `persona/group/<digest>/PERSONA.md`
保护层，由真实 Owner 通过现有 `persona_show/set/append/clear` 管理，群聊默认 `scope=group`；
User/Admin 既看不到工具 schema，也不能直接执行。所有同群成员的后续 prompt 都按
`global → group` 获得最新人格，并获得当前群 memory，但不会获得任何 actor 的私聊 memory、
user persona 或私有 Wiki/RAG。

纯文本附件兜底只识别本地文件引用，先排除 `http://` 和 `https://` URL。文件回传、
后台通知、payload 过滤和任务提交都通过 hook 注入 Agent。

Feishu 支持用户文件流水线；附件最终进入当前会话 workspace 的
`.cc-connect/attachments/`。公开文档不假设任何具体机器人名称或 tenant。

cc-connect 的静态 `default/.cc-connect/attachments` inbox 只按 basename 落盘，没有 chat
或 message 绑定。QQ shared-group 因而拒绝从这个 legacy inbox 猜测导入，也不会把 shared
attachments 中同名旧文件认作本次上传；该 turn 会同步记录一次明确拒绝，不安排必然超时的
异步 ack。已经明确属于当前群的普通文件仍可通过共享空间清单与 workspace 工具使用；新的群
附件要等 transport 提供 message-bound 路径或令牌后才能安全启用。

## 工具与 subagent

工具 schema 按名称排序，properties 按 key 排序，以保持 prompt 前缀稳定。工具执行走
in-process `ToolExecutor`；MCP client 只负责外部连接。

Subagent 接收 TaskPack，使用受限 selector 和预算，最后必须调用 `submit_result`。
主 Agent 负责向用户解释、合并和交付；subagent 不能直接承担最终答复。

## Codex 主 backend 与代码任务

公开内置 QQ 实例使用 Codex backend：

- Owner 私聊和群聊主会话都保持 Owner Codex 投影并可只读访问源码 worktree，但源码
  mutation 必须调用 `start_code_task`；群聊工具 payload 仍按公开受众脱敏。
- QQ 群 User/Admin 只获得当前 shared workspace 与成员级 Codex 投影。私聊目录继续按用户
  隔离；群准入和共享目录都不会提升普通成员角色。
- QQ 群 Codex 必须运行在 fail-closed bubblewrap 中：精确 shared root 只读、actor state 与
  audit 独立、继承环境清空，项目 `.codex` 配置/规则被隐藏；内建 shell/`apply_patch` 等路径
  不能直接写群目录。所有 mutation 经 actor-bound、workspace-scoped Session Gateway MCP
  回到宿主侧复核权限与 containment。隔离或 gateway 配置失败时不降级运行。
- Owner 群后台任务的控制记录写入 actor-scoped `.conversation-state/jobs/`，不写
  member-writable `shared/jobs`；普通群成员不能发现、查询、取消或重放。
- code-worker 从远端默认分支创建任务私有 clone，在隔离环境验证。
- 验证通过后，受信交付器生成任务分支提交、非强制 push 并创建草稿 PR。
- 不自动 merge、部署、重启或修改操作者 checkout。

Codex CLI 以 `--json` 模式运行。公开的 turn/item 生命周期与 usage 被增量归一化为
共享 LLM/span 事件，因而 Console 不读取 Codex 私有目录或直接依赖 provider-specific
日志。MCP relay receipt 仍是 AgentStrata 工具执行结果的权威证据，并在 Codex 运行期间
由主等待循环实时投影权威开始/结束时间；provider activity span 只用于解释 Codex 侧
进度。Provider JSONL 与 relay callback 通过有界串行队列投影，独立 deadline 监督不会被
慢 callback 阻塞。超时后已经进入 in-process executor 的工具无法强制取消；原 trace 会
记录 `outcome_unknown_late_completion`，旧 relay generation 随即退休，迟到结果不会归入
下一轮。子进程输出只保留有界诊断尾部，超大单行 activity 会显示 omission；只要后续完整
final message 可解析就不改变成功结果，无法保留完整 final 时明确失败。

Codex relay 以 turn generation 绑定主 LLM trace；handler 执行委托工具时把 relay call
span 设为 nested subagent 的父级，只写线程本地有界事件队列，主等待循环再串行投影到
recorder。并行搜索的 delegate 同样继承父 trace、在 worker 内缓冲，并由 coordinator
按计划顺序回放。缓冲上限只产生带计数的 observability omission，不得改变原本成功的
工具或搜索结果，也不得让 worker 线程并发改写 task summary。

Worker 凭据、Codex auth 和 GitHub token 分离；原始 GitHub token不进入 Codex sandbox
或 Git remote。

## 搜索

启用统一搜索后主 Agent 只调用 `search_information`。Router 用确定性规则处理 URL、
显式来源和大多数深度；仅复杂多实体比较需要路由模型。结果经过 URL 归一化、去重、
来源权重和时间稳定排序。

Web 来源按 BotSpec 中的顺序降级。Tavily、Brave 与 SearXNG 的薄适配器在 Agent
进程内执行；账号态或垂直来源仍可使用 search-only MCP。两条路径共享 circuit
breaker、deadline、结果归一化和相关性过滤。URL 深读先静态 fetch，遇到 JS shell、
短内容或可由浏览器处理的 HTTP 状态再升到隔离的 Playwright 服务。

## 任务标识

- `task_...`：单轮对话任务，查询 `get_task_status`。
- `job_...`：后台长任务，查询 `get_job_status`。

ACP 对完整状态 ID 使用确定性回复，避免状态查询消耗 Agent 工具预算。
QQ shared-group 不创建上述两类 artifact；这既避免把 actor-bound 延迟执行错误地变成群级
能力，也防止普通 shared root 暴露工具参数、错误细节或绝对路径。

## HTTP runtime

`http-api-server` 保留中性的 route registry。`/healthz` 在空 registry 下也可用；
自定义模块通过 `CHATCOPILOT_HTTP_ROUTE_MODULES` 显式加载，默认 bearer token 变量为
`CHATCOPILOT_HTTP_API_TOKEN`。

## 快速验证

```bash
python -m chatcopilot botspec validate bots/lingye-copilot-qq/bot.yaml
python -m pytest tests/unit/test_bot_session.py tests/unit/test_main_agent_backend_unification.py -q
python -m pytest tests/integration/test_acp_streaming_updates.py -q
```

架构边界见 [architecture.md](architecture.md)，部署与运维见
[deployment.md](deployment.md) 和 [operations.md](operations.md)。
