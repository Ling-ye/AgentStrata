# Agent、工具与上下文契约

本文由根协作入口按任务引用。先定位与本次变更有关的章节，再读取对应规格；跨领域变更需同时读取有关契约。

返回 [AI 协作入口](../AGENTS.md)；用户操作见 [文档中心](README.md)。

## 渐进式资料读取

本次修改涉及正文投影、历史摘要或回读时，先读
[渐进式披露规格](../specs/progressive-disclosure/spec.md)。Skill/MCP 执行结果保留完整 data，
模型视图只提供一份正文；会话内回读使用已过滤快照并复检原工具权限。长结果预览不能冒充
完整证据。记忆注入、信任分区、执行权限和模型策略保持既有契约。

## 章节索引

- [Agent 流式观测](#rule-1)
- [Agent 层禁止 import](#rule-2)
- [External tools 禁止 import](#rule-3)
- [Contracts 层禁止 import](#rule-4)
- [BotSpec 四面模型](#rule-5)
- [配置解析与模型生命周期](#rule-6)
- [唯一 PromptPlan 契约](#rule-7)
- [LLM 三槽配置](#rule-8)
- [工具发现统一走 `agent/tools/registry`](#rule-9)
- [职业情报 provider 不是关注列表](#rule-10)
- [大模块保留 facade](#rule-11)
- [当前导出与 Legacy 退出](#rule-12)
- [Subagent 是 Agent 层基础能力](#rule-13)
- [人格与会话记忆独立授权](#rule-14)
- [Lingye 固定 Codex backend](#rule-15)
- [主会话与可选代码任务](#rule-16)
- [主 Agent backend](#rule-17)
- [统一上下文可观测性](#rule-18)
- [Subagent](#rule-19)
- [Task pack](#rule-20)
- [按源搜索](#rule-21)
- [统一搜索入口](#rule-22)
- [Codex mutation 与 PR 交付](#rule-23)
- [Codebase (legacy)](#rule-24)
- [RAG](#rule-25)
- [私有 Wiki](#rule-26)
- [PromptPlan 缓存与预算](#rule-27)
- [双层预算机制](#rule-28)
- [Tool call 完整性修复](#rule-29)

<a id="rule-1"></a>

## Agent 流式观测

- **Agent 流式观测**：主 Codex 使用每回合隔离的 App Server stdio，沿用 actor、PromptPlan、ExecutionScope 和凭据租约。公开消息、摘要和命令输出通过 AgentContentDelta 进入既有观测索引，Console SSE 只读续传；过程不进入渠道最终回复，不采集 raw/encrypted reasoning，不重放已开始的 turn。独立 worker/research 保留 exec，规格见 `specs/agent-streaming-observability/spec.md`。

<a id="rule-2"></a>

## Agent 层禁止 import

- **Agent 层禁止 import**：`chatcopilot.botspec.*` / `chatcopilot.platforms.*` / `chatcopilot.middleware.*` / middleware `Workspace` 实现 / `BotRuntimeContext` / ACP 帧。共享 DTO/ports 只能从 `chatcopilot.contracts` 取；策略通过 hook 注入，如 `tool_payload_filter`、`background_submitter`、`file_sender`。

<a id="rule-3"></a>

## External tools 禁止 import

- **External tools 禁止 import**：`chatcopilot.agent.*` / `chatcopilot.botspec.*` / `chatcopilot.middleware.*` / `chatcopilot.platforms.*`；共享工具契约从 `chatcopilot.contracts`、`chatcopilot.core` 或 `external_tools/shared` re-export 取。

<a id="rule-4"></a>

## Contracts 层禁止 import

- **Contracts 层禁止 import**：`chatcopilot.agent.*` / `chatcopilot.middleware.*` / `chatcopilot.platforms.*` / `chatcopilot.botspec.*` / `chatcopilot.external_tools.*`。

<a id="rule-5"></a>

## BotSpec 四面模型

- **BotSpec 四面模型**：`prompts` 管机器人提示词，`tools` 管本地工具包/MCP/工具特性/隐藏工具，`agents` 管主 Agent backend（`native` / `langgraph` / `codex`）、subagent 与搜索能力，`context` 管 RAG、可写私有 Wiki、记忆存储、代码仓库、playbooks 和 dev tools 配置（`context.dev`）。当前内置 workflow registry 为空，文档和配置示例不要写不存在的 `coding` / `research` workflow。

<a id="rule-6"></a>

## 配置解析与模型生命周期

- **配置解析与模型生命周期**：Application 在 `project_agent_runtime()` 捕获配置与环境、解析研究/搜索/子 Agent 模型覆盖和搜索凭据，再由 `materialize_agent_runtime()` 创建运行对象。执行路径复用实例客户端，不重新读取模型覆盖环境；同配置客户端在实例内复用，由 `AgentRuntime.close()` 去重关闭，组装失败回收已创建资源。`LLMClient` 与共享限流仍归 Core。

<a id="rule-7"></a>

## 唯一 PromptPlan 契约

- **唯一 PromptPlan 契约**：BotSpec `prompts.schema_version` 只接受 `2`，Bot 文件只声明 `identity/response_style/refusal_style/role_styles/mode_styles`，不得声明安全、授权、记忆、人格持久化、搜索触发或工具规则。middleware 只提供可信结构化输入，所有 main Agent、subagent、backend 和 Evaluation 模型入口都经唯一 `PromptPlanBuilder`；Native/LangGraph/Codex renderer 只渲染不可变 plan，禁止追加第二份规则。Prompt trust 必须保持 `host policy / runtime facts / bot instructions / untrusted data` 四分区：只有宿主策略和可信运行时事实进入 Native system envelope，Bot identity/style/Skills 使用独立 user-context envelope；Codex 使用 schema v2 的独立字段。Bot 文本只能形成 identity/style，persona、memory、journal、网页和用户正文始终是不可信数据。禁止恢复旧 prompt assembler、旧导出、旧字段转换、自由文本 capability fragments 或 backend appendix。

<a id="rule-8"></a>

## LLM 三槽配置

- **LLM 三槽配置**：BotSpec 的 `llm.chat / llm.research / llm.code` 分别声明日常模型前缀、研究模型前缀和 Codex 路由策略；非密钥默认值进入版本库，secret 留在 `local.env`。research 只覆盖实际提供的字段，其余配置继承 chat；机器 env 仍是最高优先级。`llm.code.reasoning_effort` 与 `llm.code.profiles` 形成对话可选白名单，`/model` 只修改当前 ACP session 的主 Codex lane，不能改变共享 chat LLM 或独立 code-worker。启用 `dev.code_tasks` 的实例必须用 `llm.code.code_task_profile` 引用现有 profile；worker 启动时从实例前缀 env 解析该 profile，再内部派生 `CHATCOPILOT_CODE_MODEL` / `CHATCOPILOT_CODE_REASONING_EFFORT`，不得从 `local.env` 直接导入这两个全局变量。

<a id="rule-9"></a>

## 工具发现统一走 `agent/tools/registry`

- **工具发现统一走 `agent/tools/registry`**；具体工具包 catalog 位于 `tool_packs/catalog.py`，只把 pack id 映射到显式 `ToolProvider` 模块，不再复制工具名。领域 provider 自己声明 pack 与完整 `ToolDef`；静态、MCP、搜索、委托、人格和 session-local 工具均注册到同一个 `ToolRegistry`，Agent 与 Console 消费同源快照。重复 provider、pack、tool，缺失 provider，非法 schema 或旧 handler 签名都在物化阶段失败关闭。`scripts/check_component_catalog.py` 验证 pack、feature、MCP、subagent、workflow 和跨 surface 工具名一致性。`contracts.tool_packs` 只保留 DTO；控制台和控制面只读 `component_catalog`，不直接 import `agent.subagents.*` 或 `botspec.registry`；BotSpec 只声明 `tools.packs`，不让 Agent 层 import BotSpec 或中间件类型。`playbooks.reader` 在 runtime 物化时闭包绑定当前 Bot 的不可变 Skill 索引，不得恢复进程级可变 Skill registry。

<a id="rule-10"></a>

## 职业情报 provider 不是关注列表

- **职业情报 provider 不是关注列表**： `career.intelligence` 的默认 watchlist 必须为空；只有用户显式目标或 workspace-local watchlist 才能触发查询。 经过审阅的公开 provider 只作为能力目录：直接源仅读取公开招聘端点，失败时返回结构化 research fallback；已知公司 fallback 写入必须校验官方域名和职位详情页，禁止把稳定 tenant 招聘端点、个人目标或社区/搜索页固化为官方岗位。

<a id="rule-11"></a>

## 大模块保留 facade

- **大模块保留 facade**：`agent/mcp/client.py`、`agent/tools/builtin/workspace_tools.py`、`agent/subagents/registry.py`、`agent/search/coordinator.py` 是稳定入口；新增职责放到同层子模块，不把 runner/stateless/serialization/workspace handler/subagent definition/delegate/workflow/search factory/circuit/result helper 逻辑塞回 facade。

<a id="rule-12"></a>

## 当前导出与 Legacy 退出

- **当前导出与 Legacy 退出**：内部代码使用 canonical imports：`core.config` / `core.llm_client` / `core.concurrency`、`core.mcp_catalog`、`core.workspace_runtime`、`component_catalog`、`contracts.agent` 和 `agent.search`。L01 的 15 个旧转发文件已删除，完整名单与替换关系见 `specs/legacy-l01-import-removal/spec.md`；生产代码、测试和安装包均不得恢复旧模块、空存根或动态回退。`agent.research`、`agent.tools.workspace_context`、`external_tools.shared.tool_spec`、`middleware.mcp.session_gateway` 仍在后续审议范围，不能借 L01 顺带删除。旧 Codex turn routing 模块也不得恢复。

<a id="rule-13"></a>

## Subagent 是 Agent 层基础能力

- **Subagent 是 Agent 层基础能力**：BotSpec 只通过 `agents` 声明 preset、workflow 和预算；主 Agent 通过委托工具调用；subagent 禁止 import middleware、platforms、Workspace。

<a id="rule-14"></a>

## 人格与会话记忆独立授权

- **人格与会话记忆独立授权**：BotSpec 只通过 `tools.packs: persona.control` 向 Owner 主 Agent 注入 session-bound `persona_manage`；自然语言与 `/persona` 原样进入主 Agent，不得恢复 `PersonaCandidateDetector`、解释器、命令 parser 或宿主前置短路，也不得把该工具投影给 subagent。Registry 可见性和 handler 都复检真实 Owner；`set/append/research` 的草案要求直接取自当前可信 `ToolContext.request_text`，不接受模型重复填写 requirement；`global` 由主 Agent 根据当前明确要求选择，不用关键词名单判定，模型不能提供 actor/chat/path/receipt。所有非清空人格操作的完整 Markdown 只由 `PersonaDraftAgent` 生成；宿主不得拼接人格正文，`append` 也必须读取当前层后由 Agent 生成完整替换文档。命名人物由 Agent 使用统一搜索自行查询、消歧并选择实际使用来源，再做唯一一次原子 `set`；无歌词专用 schema、候选库或响应装饰器。明确更新或清空可直接写；只在需求或作用域不清楚时设置 `defer_confirmation=true`，建立绑定真实 actor/chat/scope/hash/TTL 的提案；只有当前真实 raw user text 精确等于 `/persona confirm` 才能确认，cancel 可自然语言。只有 `ToolResult.data.committed=true` 和其中真实 mutation receipt 才能声称已保存或清空；写后 PromptPlan 刷新失败仍必须如实保留 committed receipt。群聊按 `global → group`、私聊按 `global → user` 加载，群内 show 只返回状态/哈希；非 Owner 不能读取或修改。Owner 要求的模仿强度不自动弱化，persona 和网页证据仍不能改变 transport 身份、角色、准入、scope、路径、工具、凭据或执行事实。当前私聊发送者或当前群的非空 memory 每轮作为不可信历史数据注入；所有准入用户可 read/append，私聊与群聊 memory 都只有 Owner 可 clear。秘密、群内个人隐私、persona 和权限指令在持久化入口拒绝；长期价值与临时性由 Agent 判断，不用临时关键词硬拒绝。权威文件只位于 `.conversation-state/persistent/{persona,memory}/`，目录 `0700`、文件 `0600`、no-follow、单硬链接、锁和原子替换异常时失败关闭；旧 persona 和旧 p2p memory 路径完全忽略，不自动迁移或回退读取；磁盘旧数据不由运行时清理。

<a id="rule-15"></a>

## Lingye 固定 Codex backend

- **Lingye 固定 Codex backend**： `lingye-copilot-qq` 使用 `agents.backend: codex`；选择作用于整个实例，不按角色、命令或单回合切换。 切回 Native 或 LangGraph 必须修改 BotSpec 并重新部署；部署前删除旧 backend 状态，失败后不恢复旧会话。 Native 的会话、工具执行、仓库任务和发布能力是长期保留的一等能力。

<a id="rule-16"></a>

## 主会话与可选代码任务

- **主会话与可选代码任务**：旧 `agents.codex.owner_access/member_access`、`access.owner_only_project_access` 已删除，校验给出迁移错误。Owner 主会话按宿主 ExecutionScope 直接读写项目；`start/get/cancel/resume_code_task` 保留为 Owner 可选独立任务。独立 systemd code-worker 从远端默认分支创建任务私有 clone，在 bwrap 中使用固定 Codex 二进制与专用 worker 凭据，不能读取个人 MCP、个人 `CODEX_HOME`、AgentStrata Session Gateway 或 GitHub token。 验证通过后仅由受信宿主提交任务分支、非强制 push 并创建草稿 PR；不覆盖源仓、不修改运行副本、不重启、不部署、不 merge。 GitHub fine-grained PAT 必须在 clone 前和交付前解析为 `local.env` 明确配置的预期 actor；`delivery.json` 绑定 canonical actor，缺失、不匹配或漂移均失败关闭。Git author/committer 使用独立的公开 AgentStrata AI Coding Bot 身份，commit 正文和 Draft PR 顶部保留 repository owner、AI generation 与 human-review-required provenance。 `CHATCOPILOT_CODEX_BOT_HOME` 的 main `auth.json` 与 `worker/auth.json` 必须分别 device auth 并独立 lease；GitHub token 只从 owner-only `0700` 配置目录内的 single-link mode `0600` worker 文件读取，交付进程用 `O_NOFOLLOW` + `fstat` 单次载入；Git askpass 只使用任务期内的临时 `0600` 快照，原始 token 不进入 Codex 沙箱、worker env 或 Git remote。 caller 摘要、角色、策略或 credential generation 变化必须使旧 resume ID 失效，不能只信任 `role_hint`。

<a id="rule-17"></a>

## 主 Agent backend

- **主 Agent backend**：`agents.backend` 默认 `native`，也可设为 `langgraph` 或 `codex`；三个 backend 必须共享 `AgentTask` / `AgentEvent` / `AgentResult` 协议和现有工具注册/权限 hook。选择只发生在实例配置，不按回合自动切换。Native/LangGraph 复用 `agent/turn.py` 的 `TurnOps`；Codex 必须把公开 CLI JSONL 投影为相同事件、工具结果、生命周期 intent 和最终 `AgentResult`，不得让 Console 解析 backend 私有日志。

<a id="rule-18"></a>

## 统一上下文可观测性

- **统一上下文可观测性**： 主 Agent 与 subagent 的每次 turn 模型调用前必须发 `ContextSnapshotPrepared`；Native/LangGraph 纯文本请求捕获最终提交的 `exact_model_input`，含本地二进制资源或受限字段时降为 `partial` 并只保留 path-free receipt，Codex 捕获 AgentStrata stdin/tool/resource envelope 并以 `adapter_visible` + `provider_opaque` 标明原生 resume/内部 instructions 等不可见状态。隐藏 chain-of-thought 不进入事件或 artifact。Legacy 与 Evaluation 共享快照正文必须在首次落盘前脱敏，独立写入 private bounded artifact；Gateway 的私有观测原值遵循 Console 管理视图契约；`task.json` 只留摘要，Console 只通过 opaque snapshot ID 懒加载，不按 backend 分支。Topic classifier、search router 与 reranker 等独立 helper-model 调用本版本只保留既有 step/usage；确定性的 `ResponseIntegrityCheck` 记录摘要但不产生模型上下文 artifact。

<a id="rule-19"></a>

## Subagent

- **Subagent**：主 Agent 是唯一对用户负责的交付者；subagent 只通过委托工具执行内部任务，并必须用 `submit_result` 返回 `{ok,summary,findings,evidence,changes,commands_run,outputs,risks,next_steps,confidence,cache_summary}`。

<a id="rule-20"></a>

## Task pack

- **Task pack**：新委托使用 `objective/user_intent/deliverable/constraints/inputs/resources/acceptance_criteria/evidence_required/write_scope/excluded_context/cache_key_hint`；旧 `task` 只作为兼容别名。

<a id="rule-21"></a>

## 按源搜索

- **按源搜索**：`risk: search` MCP 为账号态或垂直来源生成受限 `search_<server-id>` delegate，例如 `search_xiaohongshu`。每个搜索 subagent 只能访问本 server 的 `search_only_tools`；Tavily、Brave 与 SearXNG 不再用 MCP wrapper。

<a id="rule-22"></a>

## 统一搜索入口

- **统一搜索入口**：启用 `agents.unified_search.enabled` 后，主 Agent 只调用 `search_information`；`web_fetch_page` / `browse_dynamic_page` 仅供该入口内部使用。URL、显式来源、quick、standard 单实体和 thorough 单实体请求由脚本路由；只有 thorough 多实体比较调用路由 LLM。结果先由脚本做 canonical URL/标题去重、来源权重与时间稳定排序；只有 thorough 多来源结果调用 LLM 做语义冲突和事实合并。所有结果记录 `decision_source` / `decision_reason`。Web 源三级降级：Tavily → Brave → SearXNG。
 - **直接搜索执行**：`agents.unified_search.providers` 按顺序声明 `id / kind / enabled / endpoint / credential_env / timeout_seconds / max_results`。Tavily、Brave 与 SearXNG 由有界进程内 HTTP client 执行，账号态或垂直来源继续直接调用 search-only MCP tool；两者都跳过 subagent LLM 并共享 `SearchCircuitBreaker`、deadline、结果归一化与多源降级。凭据 provider 只允许审核过的官方 HTTPS endpoint，SearXNG 只允许回环 endpoint，redirect 不得携带 credential。
 - **显式来源约束**：用户点名小红书 / XHS / Xiaohongshu 时，`ResearchRequest` 归一为 `source_hints=["experience"]`，router 只保留显式来源，避免静默回退到通用网页搜索。
 - **结果条目上限**：`_compact_results` 在字符长度截断基础上增加条目上限（`_MAX_RESULT_ITEMS = 15`），防止大量列表（如 47 条海报）撑爆 context。
  - **时间预算**：`SearchCoordinator` 接受 `max_wall_seconds`（有 `turn_timeout` 时取 `min(turn_timeout * 0.6, 180s)`，否则 fallback 到 180s 硬上限），所有步骤并行提交到 `ThreadPoolExecutor`，通过 `as_completed(timeout=remaining)` 统一 deadline；超时未完成的步骤标记 `time_budget_exhausted`；reranker 在 deadline 过后跳过。
  - **同源步骤上限**：Router 分解出的步骤若全部指向同一 logical source（如 3 个 `experience` 查询），上限收紧到 2 步（`_SINGLE_SOURCE_MAX_STEPS`），避免同源重叠查询消耗过多 subagent 预算。
  - **熔断器递增 TTL**：`SearchCircuitBreaker` 对 `mcp_quota_exceeded` 使用指数递增 TTL（1h → 2h → … → 24h 上限，env `CHATCOPILOT_SEARCH_QUOTA_MAX_TTL`），成功后重置。直接搜索和 delegate 路径共享同一 `SearchCircuitBreaker` 实例。
  - **浏览器降级**：`_needs_browser` 识别 HTTP 403/401/429 为浏览器可解决错误，自动尝试 Playwright 渲染。
  - **Router fallback 降级**：Router LLM 异常时 `thorough` 自动降到 `standard`，runner 同步降级 request.depth，避免 fallback plan 浪费步数和 subagent 预算。
  - **同 turn 不重复搜索**：唯一 `runtime.accuracy_and_search` layer 指示主 Agent 不在同一轮重复调用 `search_information`，避免双倍时间开销。
 - **同轮搜索硬保护**：`AgentSession` 会在同一轮首个成功 `search_information` 后拦截后续重复搜索，把上一次搜索结果作为工具结果回灌，并要求模型基于已有证据作答。
  - **搜索 subagent 快速退出**：搜索 subagent prompt 指示在遇到 quota/unavailable 等基础设施错误时立即 `submit_result(ok=false)`，禁止盲猜 URL 或重试。

<a id="rule-23"></a>

## Codex mutation 与 PR 交付

- **Codex mutation 与 PR 交付**：Owner 可用宿主绑定的文件、命令和委托工具直接修改项目；独立代码任务保留为可选方式。adapter_forge 仍消费一次性源码批准记录，其 selector 是该预设的任务范围。code-worker 使用全局 FIFO、独立 transient cgroup、远端干净 clone 和 bwrap；changed paths 必须通过 `context.dev`，一次完整门禁通过后由沙箱外受信交付器生成中文 commit、非强制 push 并创建草稿 PR，`delivery.json` 记录分支、commit 与 PR 证据。 Native/LangGraph 保持不 commit/push 的 `RepositoryTaskService`；Codex PR 不自动 merge、部署或重启。
  - **先方案后确认**： Owner 明确要求先分析、设计、评审或给方案并等待后续确认时，当前 turn 只返回方案且不得调用 `start_code_task`；同一 session 后续明确确认时只调用一次，并完整重述已批准范围与可观测验收条件。直接要求立即实现时不增加确认轮，孤立且无明确待确认方案的“确认”必须澄清。提示投影测试覆盖这三条模型契约；隔离的两轮产品能力 Case 只实测 plan→confirm 主路径，不得把单次通过描述为宿主侧一次性 proposal 门禁或真实 Draft PR E2E。
  - **验证工具链挂载**：bwrap 只把源仓 `.venv` 与经 manifest/父链校验的 `console/web/node_modules` 作为只读工具链映射到每条命令的临时候选树；前端构建仍在 `/workspace/console/web` 执行，任务不得改写宿主依赖。
  - **候选索引验证边界**：full validation 使用 job-private、只读挂载的权威 Git index 表示 `HEAD + exact task delta`，宿主 materialize/verify 只操作 disposable index copy；quick 前真实 index 必须等于 `HEAD`，pytest 等会创建临时仓库的检查不得继承候选 `GIT_INDEX_FILE`。每条 quick/full 使用独立 exact-materialized tree、`0700` HOME、无 profile/rc Bash 和独立网络 namespace；clone ignored 内容不进入验证。tree/home/index-copy/lock 必须在成功、失败和 resume 路径严格清理，遗留 symlink、foreign owner 或 inode 类型异常时失败关闭且不跟随。Console 依赖只在 source/task 的 `package.json` 与 `package-lock.json` 完全一致、父链无 symlink 且 source `console/web/node_modules` 存在时挂载。
  - **实例隔离与恢复**： `start_code_task` request 必须在任务目录可见前持久化非空 `instance_id`；每个 systemd worker 使用 BotSpec 派生的实例专属 workspace，只恢复与当前实例完全匹配的 request，missing/foreign identity 一律 fail closed。
  - **取消与交付边界**： cancel 与进入 `delivering` 必须共享状态锁；进入交付后不可取消，普通后台任务不得依赖该 POSIX 锁。 GitHub 返回的 PR `head.sha` 必须精确等于已验证 commit；远端分支恢复不得 force-push、改写 commit 或静默创建重复 PR。
  - **context.dev 接线**：BotSpec 只声明 `root_env` 与 `shell`，Application 捕获实际配置后生成执行资源；旧 allowed_paths/denied_paths 校验时报迁移错误。code-worker 继续复用现有配置解析与单次任务写入范围。
  - **路径解析与交付**：宿主根据可信角色绑定工作区和项目资源。Owner 文件工具默认相对已配置项目，成员相对当前会话工作区；绝对路径仍检查同一资源范围。命令通过 bwrap 限定可见与可写目录。运行副本只能通过显式部署或 self-update publisher 更新，代码任务的发布流程独立。

<a id="rule-24"></a>

## Codebase (legacy)

- **Codebase (legacy)**：`external_tools/codebase/` 中 `codebase.read` 只读检索仍可用；`codebase.change` 已从工具包 catalog 移除，托管写入流程由 dev tools 替代。

<a id="rule-25"></a>

## RAG

- **RAG**：只检索 BotSpec 声明的本地/私有知识源，不替代联网查证，也不写入长期 memory。

<a id="rule-26"></a>

## 私有 Wiki

- **私有 Wiki**：`context.wiki` 声明机器私有根目录 env、最低读取角色和私聊限制；`wiki.knowledge` 对 Owner 可用；自动私有上下文只在当前 Owner 私聊注入。`pages/` Markdown 是事实源，`sources/` 保存原始快照，`.index/wiki.db` 可重建。会话权限由 middleware 在 Retriever 和 tool schema 装配前强制执行；禁止仅靠 prompt 保密。V1 不包含 PDF/DOCX、飞书同步或自动 Git commit/push。

<a id="rule-27"></a>

## PromptPlan 缓存与预算

- **PromptPlan 缓存与预算**：固定 layer 按契约顺序构造并以 layer hash 形成稳定前缀；动态 persona、history 和 session facts 位于后层。tools schema 按 name 排序、properties 按 key 排序，工具投影 digest 来自最终可信工具集合。main Agent 与 subagent 使用同一个 `PromptLayer` 类型；subagent 只增加 `runtime.subagent` 与职责文本，不建立平行 prompt 体系。预算按固定策略、persona、history、用户正文和 tool schema 分桶，超限必须显式评审。

<a id="rule-28"></a>

## 双层预算机制

- **双层预算机制**：`AgentSession` 的迭代与超时均采用 **soft cap + 健康检查 + hard cap** 三层设计：
  - **迭代**：`max_tool_iterations`（默认 8）是 soft cap，到达后检查健康状态（无重复工具调用、无连续失败）；健康则继续执行，不健康则注入 wrap-up 指令让 LLM 总结后停止。`hard_iteration_cap` 默认为空，只有显式配置才成为硬上限；子 Agent 直接使用声明的轮数和时间，不隐式倍增。
  - **超时**：`turn_timeout_seconds` 是 soft timeout；到达后检查最近工具活跃度（`stall_window_seconds` 内有无工具完成）；有活跃则继续。`hard_timeout_seconds` 是无条件安全线。若只设 `turn_timeout_seconds` 不设 `hard_timeout_seconds`，保持旧行为（等价硬截断）。
  - **停滞检测**：最近 3 次工具调用 fingerprint 相同 → 判定为死循环；连续 2+ 次失败 → 判定为不健康。
  - **Subagent 自动继承**：subagent 的 `max_model_turns` 作为 soft cap，hard cap 自动计算为 `max(soft+4, soft*2)`；hard timeout 为 soft 的 3 倍。
  - **Env 覆盖**：`CHATCOPILOT_HARD_ITERATION_CAP`、`CHATCOPILOT_HARD_TIMEOUT_SECONDS`、`CHATCOPILOT_STALL_WINDOW_SECONDS`。

<a id="rule-29"></a>

## Tool call 完整性修复

- **Tool call 完整性修复**：`AgentSession._repair_orphan_tool_calls` 扫描 messages，为缺失 tool result 的 `tool_calls` 补全合成 error result（`ok: false, error: aborted`）。三处调用：`_timeout_result`（超时截断后）、tool_call_cap 返回前、每次 `llm.chat()` 前的防御性校验。确保跨 turn 累积的 messages 不会因 orphan `tool_calls` 导致 OpenAI API 400。
