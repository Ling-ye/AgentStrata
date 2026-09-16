# 上下文、记忆与按需资料

修改资料组织、人格、记忆、搜索和结果回读时阅读。资料按需加载不改变它的信任等级或执行权限。

## 先索引，后正文

注册的 Skill 先提供已授权索引，命中任务后加载正文。工具完整结果与模型投影分开：长结果通过当前会话的引用分页读取，失效后返回明确的不可用信息，不伪造内容或重放副作用。

PromptPlan 保留 host policy、runtime facts、bot instructions、untrusted data 四个信任分区。用户资料、网页、工具输出和历史记录不能提升为宿主规则。

## 源码入口

- [src/chatcopilot/agent/tools/result_reader.py](../../src/chatcopilot/agent/tools/result_reader.py)
- [src/chatcopilot/agent/memory](../../src/chatcopilot/agent/memory)
- [src/chatcopilot/agent/persona](../../src/chatcopilot/agent/persona)
- [src/chatcopilot/agent/search](../../src/chatcopilot/agent/search)
- [src/chatcopilot/harness/evidence_context.py](../../src/chatcopilot/harness/evidence_context.py)

## 人格与会话记忆独立授权

BotSpec 只通过 `tools.packs: persona.control` 向 Owner 主 Agent 注入 session-bound `persona_manage`；自然语言与 `/persona` 原样进入主 Agent，不得恢复 `PersonaCandidateDetector`、解释器、命令 parser 或宿主前置短路，也不得把该工具投影给 subagent。Registry 可见性和 handler 都复检真实 Owner；`set/append/research` 的草案要求直接取自当前可信 `ToolContext.request_text`，不接受模型重复填写 requirement；`global` 由主 Agent 根据当前明确要求选择，不用关键词名单判定，模型不能提供 actor/chat/path/receipt。所有非清空人格操作的完整 Markdown 只由 `PersonaDraftAgent` 生成；宿主不得拼接人格正文，`append` 也必须读取当前层后由 Agent 生成完整替换文档。命名人物由 Agent 使用统一搜索自行查询、消歧并选择实际使用来源，再做唯一一次原子 `set`；无歌词专用 schema、候选库或响应装饰器。明确更新或清空可直接写；只在需求或作用域不清楚时设置 `defer_confirmation=true`，建立绑定真实 actor/chat/scope/hash/TTL 的提案；只有当前真实 raw user text 精确等于 `/persona confirm` 才能确认，cancel 可自然语言。只有 `ToolResult.data.committed=true` 和其中真实 mutation receipt 才能声称已保存或清空；写后 PromptPlan 刷新失败仍必须如实保留 committed receipt。群聊按 `global → group`、私聊按 `global → user` 加载，群内 show 只返回状态/哈希；非 Owner 不能读取或修改。Owner 要求的模仿强度不自动弱化，persona 和网页证据仍不能改变 transport 身份、角色、准入、scope、路径、工具、凭据或执行事实。当前私聊发送者或当前群的非空 memory 每轮作为不可信历史数据注入；所有准入用户可 read/append，私聊与群聊 memory 都只有 Owner 可 clear。秘密、群内个人隐私、persona 和权限指令在持久化入口拒绝；长期价值与临时性由 Agent 判断，不用临时关键词硬拒绝。权威文件只位于 `.conversation-state/persistent/{persona,memory}/`，目录 `0700`、文件 `0600`、no-follow、单硬链接、锁和原子替换异常时失败关闭；旧 persona 和旧 p2p memory 路径完全忽略，不自动迁移或回退读取；磁盘旧数据不由运行时清理。

## 统一上下文可观测性

主 Agent 与 subagent 的每次 turn 模型调用前必须发 `ContextSnapshotPrepared`；Native/LangGraph 纯文本请求捕获最终提交的 `exact_model_input`，含本地二进制资源或受限字段时降为 `partial` 并只保留 path-free receipt，Codex 捕获 AgentStrata stdin/tool/resource envelope 并以 `adapter_visible` + `provider_opaque` 标明原生 resume/内部 instructions 等不可见状态。隐藏 chain-of-thought 不进入事件或 artifact。Legacy 与 Evaluation 共享快照正文必须在首次落盘前脱敏，独立写入 private bounded artifact；Gateway 的私有观测原值遵循 Console 管理视图契约；`task.json` 只留摘要，Console 只通过 opaque snapshot ID 懒加载，不按 backend 分支。Topic classifier、search router 与 reranker 等独立 helper-model 调用本版本只保留既有 step/usage；确定性的 `ResponseIntegrityCheck` 记录摘要但不产生模型上下文 artifact。

## 按源搜索

`risk: search` MCP 为账号态或垂直来源生成受限 `search_<server-id>` delegate，例如 `search_xiaohongshu`。每个搜索 subagent 只能访问本 server 的 `search_only_tools`；Tavily、Brave 与 SearXNG 不再用 MCP wrapper。

## 统一搜索入口

启用 `agents.unified_search.enabled` 后，主 Agent 只调用 `search_information`；`web_fetch_page` / `browse_dynamic_page` 仅供该入口内部使用。URL、显式来源、quick、standard 单实体和 thorough 单实体请求由脚本路由；只有 thorough 多实体比较调用路由 LLM。结果先由脚本做 canonical URL/标题去重、来源权重与时间稳定排序；只有 thorough 多来源结果调用 LLM 做语义冲突和事实合并。所有结果记录 `decision_source` / `decision_reason`。Web 源三级降级：Tavily → Brave → SearXNG。
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

## RAG

只检索 BotSpec 声明的本地/私有知识源，不替代联网查证，也不写入长期 memory。

## 私有 Wiki

`context.wiki` 声明机器私有根目录 env、最低读取角色和私聊限制；`wiki.knowledge` 对 Owner 可用；自动私有上下文只在当前 Owner 私聊注入。`pages/` Markdown 是事实源，`sources/` 保存原始快照，`.index/wiki.db` 可重建。会话权限由 middleware 在 Retriever 和 tool schema 装配前强制执行；禁止仅靠 prompt 保密。V1 不包含 PDF/DOCX、飞书同步或自动 Git commit/push。

## PromptPlan 缓存与预算

固定 layer 按契约顺序构造并以 layer hash 形成稳定前缀；动态 persona、history 和 session facts 位于后层。tools schema 按 name 排序、properties 按 key 排序，工具投影 digest 来自最终可信工具集合。main Agent 与 subagent 使用同一个 `PromptLayer` 类型；subagent 只增加 `runtime.subagent` 与职责文本，不建立平行 prompt 体系。预算按固定策略、persona、history、用户正文和 tool schema 分桶，超限必须显式评审。

## 双层预算机制

`AgentSession` 的迭代与超时均采用 **soft cap + 健康检查 + hard cap** 三层设计：
  - **迭代**：`max_tool_iterations`（默认 8）是 soft cap，到达后检查健康状态（无重复工具调用、无连续失败）；健康则继续执行，不健康则注入 wrap-up 指令让 LLM 总结后停止。`hard_iteration_cap` 默认为空，只有显式配置才成为硬上限；子 Agent 直接使用声明的轮数和时间，不隐式倍增。
  - **超时**：`turn_timeout_seconds` 是 soft timeout；到达后检查最近工具活跃度（`stall_window_seconds` 内有无工具完成）；有活跃则继续。`hard_timeout_seconds` 是无条件安全线。若只设 `turn_timeout_seconds` 不设 `hard_timeout_seconds`，保持旧行为（等价硬截断）。
  - **停滞检测**：最近 3 次工具调用 fingerprint 相同 → 判定为死循环；连续 2+ 次失败 → 判定为不健康。
  - **Subagent 自动继承**：subagent 的 `max_model_turns` 作为 soft cap，hard cap 自动计算为 `max(soft+4, soft*2)`；hard timeout 为 soft 的 3 倍。
  - **Env 覆盖**：`CHATCOPILOT_HARD_ITERATION_CAP`、`CHATCOPILOT_HARD_TIMEOUT_SECONDS`、`CHATCOPILOT_STALL_WINDOW_SECONDS`。
