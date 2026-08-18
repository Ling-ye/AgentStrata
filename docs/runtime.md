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

Native、LangGraph 和 Codex backend 共享 `agent/turn.py` 的事件、工具结果、生命周期
intent 和最终 `AgentResult` 语义。Backend 由 BotSpec 固定选择，不存在按消息文本切换
backend 的第二套路由器。

AgentSession 使用 soft cap、健康检查和 hard cap：

- 迭代 soft cap 到达后检查重复调用与连续失败；健康时可继续。
- hard iteration cap 无条件停止。
- soft timeout 到达后检查最近工具活跃；hard timeout 无条件停止。
- 缺失 tool result 的 orphan tool call 会在下次模型调用或截断前补成结构化错误。

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
shared-channel session、sender injection 与同步 hook；本地合成 ingress 测试只覆盖本机边界，不代表
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
contract 和受保护状态中，不会被最后一个说话人或同名符号链接改写。QQ 群禁用共享
`MEMORY.md` 和 member-visible `task_...` diagnostics。普通成员不能启动后台 job；Owner 的后台
job 控制面按 actor 写入 `.conversation-state/jobs/`，不会暴露到 `shared/jobs`。当前群交流风格
直接复用现有 persona 分层：`group_<id>/PERSONA.md` 是普通 group 层，由真实 Owner 通过现有
`persona_show/set/append/clear` 管理，群聊默认 `scope=group`；User/Admin 不能读取或修改
group/global 层。所有同群成员的后续 prompt 都获得该 group 层，但不会自动获得 global/user
persona、私有 memory 或私有 Wiki/RAG。

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
