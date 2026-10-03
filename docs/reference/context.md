# 上下文、记忆与按需资料

修改资料组织、人格、记忆、搜索和结果回读时阅读。资料按需加载不改变它的信任等级或执行权限。

## 先索引，后正文

注册的 Skill 先提供已授权索引，命中任务后加载正文。工具完整结果与模型投影分开：长结果通过当前会话的引用分页读取，失效后返回明确的不可用信息，不伪造内容或重放副作用。

PromptPlan 保留 host policy、runtime facts、bot instructions、untrusted data 四个信任分区。用户资料、网页、工具输出和历史记录不能提升为宿主规则。

Codex App Server 的 developerInstructions 只承载前两个宿主分区及宿主执行策略；
Bot 指令、人格、历史和原请求通过 user 输入投递。开始与续接都投递当前宿主规则，
上下文快照记录两个实际消息，避免把历史里的能力否认当成当前工具权限或重复注入宿主正文。

## 源码入口

- [src/chatcopilot/agent/tools/result_reader.py](../../src/chatcopilot/agent/tools/result_reader.py)
- [src/chatcopilot/agent/memory](../../src/chatcopilot/agent/memory)
- [src/chatcopilot/core/memory_records.py](../../src/chatcopilot/core/memory_records.py)
- [src/chatcopilot/agent/persona](../../src/chatcopilot/agent/persona)
- [src/chatcopilot/agent/search](../../src/chatcopilot/agent/search)
- [src/chatcopilot/harness/evidence_context.py](../../src/chatcopilot/harness/evidence_context.py)

## 工具 schema 预算

主 Agent 的工具按会话授权后分为直接和延迟披露。Native/LangGraph 的上下文快照记录实际提交的基础工具与三个桥接 schema；Codex 只记录 adapter 交给 App Server 的动态工具声明，不能由此推断原生模型实际载入的 schema 或 Token。模型通过桥接读取的工具简介和参数仍是资料，不提升信任等级；预算和效果须分别看 schema 估算、模型调用次数、延迟与任务结果。参见[工具按需披露](tools.md#工具按需披露)。

## 人格与会话记忆独立授权

BotSpec 只通过 `tools.packs: persona.control` 向 Owner 主 Agent 注入 session-bound `persona_manage`；自然语言与 `/persona` 原样进入主 Agent，不得恢复 `PersonaCandidateDetector`、解释器、命令 parser 或宿主前置短路，也不得把该工具投影给 subagent。Registry 可见性和 handler 都复检真实 Owner；`set/append/research` 的草案要求直接取自当前可信 `ToolContext.request_text`，不接受模型重复填写 requirement；`global` 由主 Agent 根据当前明确要求选择，不用关键词名单判定，模型不能提供 actor/chat/path/receipt。所有非清空人格操作的完整 Markdown 只由 `PersonaDraftAgent` 生成；宿主不得拼接人格正文，`append` 也必须读取当前层后由 Agent 生成完整替换文档。命名人物由 Agent 使用统一搜索自行查询、消歧并选择实际使用来源，再做唯一一次原子 `set`；无歌词专用 schema、候选库或响应装饰器。明确更新或清空可直接写；只在需求或作用域不清楚时设置 `defer_confirmation=true`，建立绑定真实 actor/chat/scope/hash/TTL 的提案；只有当前真实 raw user text 精确等于 `/persona confirm` 才能确认，cancel 可自然语言。只有 `ToolResult.data.committed=true` 和其中真实 mutation receipt 才能声称已保存或清空；写后 PromptPlan 刷新失败仍必须如实保留 committed receipt。群聊按 `global → group`、私聊按 `global → user` 加载，群内 show 只返回状态/哈希；非 Owner 不能读取或修改。Owner 要求的模仿强度不自动弱化，persona 和网页证据仍不能改变 transport 身份、角色、准入、scope、路径、工具、凭据或执行事实。当前私聊发送者或当前群的长期记忆存为受保护的条目库；只把有界的稳定决定与当前问题相关条目作为不可信历史数据注入。准入成员可以读取和追加当前作用域记忆，Owner 可以更正或删除单条、清空当前作用域。群聊和私聊都可从当前已准入用户发言自动提炼；群聊仅接受明确属于全群的事实或决定。自动候选必须包含当前发言中的原文片段，通过秘密与群隐私校验；新证据可让旧条目退出有效召回，但普通成员的主动删除请求不能借此执行。记忆仍不能改变人格、角色、权限或系统规则。权威新库位于 `.conversation-state/persistent/memory/{group,user}/<digest>/memory.db`，旧 `MEMORY.md` 不再读取；停机归档流程见[记忆条目规格](../../specs/conversation-memory-records/spec.md)。

人格正文统一使用 32 KiB UTF-8 文件预算（含规范化后的末尾换行）；草案生成、写入和加载使用同一限制，不再另设 2,000 字符门槛。完整当前人格传给草案 Agent，避免更新时丢失后半部分。

记忆单条最多 4,000 字符，取消 1,000 条有效记录及固定 SQLite 页数的存储门槛。关键词评分在 SQLite 查询中排序，每次只返回有界页面；`read_memory` 支持 `offset` 与 `limit`，每页最多 100 条，并返回 `next_offset`。自动上下文仍选择有限的稳定决定与相关条目，合计最多 12,000 字符。存储格式与身份、秘密和写入回执校验保持原有契约，不自动清理旧数据。

升级运行实例前，先停止对应的 Bot user service，使用
`.venv/bin/python scripts/archive_legacy_memory.py --workspace-root <bot-workspace>`
预览旧受保护记忆的数量和哈希；核对后加
`--apply --unit <stopped-bot.service>` 执行。工具要求该 unit 已加载且处于 inactive，
并在每个旧文件的固定锁内校验哈希后重命名为 `MEMORY.md.archived`。
新库从空状态开始，归档文件不进入检索；失败时保留原件或已归档文件供人工恢复。

## 统一上下文可观测性

主 Agent 与 subagent 的每次 turn 模型调用前必须发 `ContextSnapshotPrepared`；Native/LangGraph 纯文本请求捕获最终提交的 `exact_model_input`，含本地二进制资源或受限字段时降为 `partial` 并只保留 path-free receipt，Codex 捕获 AgentStrata stdin/tool/resource envelope 并以 `adapter_visible` + `provider_opaque` 标明原生 resume/内部 instructions 等不可见状态。隐藏 chain-of-thought 不进入事件或 artifact。Legacy 与 Evaluation 共享快照正文必须在首次落盘前脱敏，独立写入 private bounded artifact；Gateway 的私有观测原值遵循 Console 管理视图契约；`task.json` 只留摘要，Console 只通过 opaque snapshot ID 懒加载，不按 backend 分支。Topic classifier、search router 与 reranker 等独立 helper-model 调用本版本只保留既有 step/usage；确定性的 `ResponseIntegrityCheck` 记录摘要但不产生模型上下文 artifact。

## 按源搜索

`risk: search` MCP 为账号态或垂直来源生成受限 `search_<server-id>` delegate，例如 `search_xiaohongshu`。每个搜索 subagent 只能访问本 server 的 `search_only_tools`；Tavily、Brave 与 SearXNG 不再用 MCP wrapper。

## 统一搜索入口

启用 `agents.unified_search.enabled` 后，主 Agent 只调用 `search_information`；`web_fetch_page` / `browse_dynamic_page` 仅供该入口内部使用。URL、显式来源、quick、standard 单实体和 thorough 单实体请求由脚本路由；只有 thorough 多实体比较调用路由 LLM。结果先由脚本做 canonical URL/标题去重、来源权重与时间稳定排序；只有 thorough 多来源结果调用 LLM 做语义冲突和事实合并。所有结果记录 `decision_source` / `decision_reason`。Web 源三级降级：Tavily → Brave → SearXNG。
 - **直接搜索执行**：`agents.unified_search.providers` 按顺序声明 `id / kind / enabled / endpoint / credential_env / timeout_seconds / max_results`。Tavily、Brave 与 SearXNG 由有界进程内 HTTP client 执行，账号态或垂直来源继续直接调用 search-only MCP tool；两者都跳过 subagent LLM 并共享 `SearchCircuitBreaker`、deadline、结果归一化与多源降级。凭据 provider 只允许审核过的官方 HTTPS endpoint，SearXNG 只允许回环 endpoint，redirect 不得携带 credential。
 - **显式来源约束**：用户点名小红书 / XHS / Xiaohongshu 时，`SearchRequest` 归一为 `source_hints=["experience"]`，router 只保留显式来源，避免静默回退到通用网页搜索。
 - **完整结果与预览**：预览仍按条目与字符预算压缩；已收集、去重排序后的完整结果存入当前 actor 的会话结果缓存，通过 `result_ref` 和 `read_tool_result` 分页读取。来源的相关性过滤与 HTTP 容量校验仍生效；缓存关闭或淘汰后明确返回引用失效，不自动重放搜索。
 - **网页全文**：静态抓取最多读取 2 MiB 响应字节，HTTP 等待 60 秒，超限明确失败，不把
   响应前缀称为完整正文。提取正文与模型预览分别保存；`max_chars` 只控制预览，允许 1 到
   50,000 个字符，越界报错。统一入口的页面 `content` 保留全文，`summary` 保留预览。
   既有会话缓存总容量仍为 16 MiB，过大聚合结果会标记 `not_cached_oversized`，不能承诺
   这类结果获得持久的全文引用；本次不改变缓存策略。
 - **完成度**：有可用证据与请求完整完成分别判断。任一计划步骤或请求页面失败、页面标记
   不完整、指定来源/URL 未覆盖或交叉核实未完成，均令搜索数据 `ok=false`，有证据时
   `limits.partial=true`；同时报告计划/完成步骤、页面和未读取请求 URL 数。
 - **时间预算**：统一搜索使用 `agents.unified_search.timeout_seconds`，并服从更小的上层回合预算。当前 QQ 实例为 600 秒，Tavily 请求 30 秒、SearXNG 请求 60 秒；并行步骤超时会标记 `time_budget_exhausted`。该调度预算不强杀正在执行的同步下游调用，实际返回仍受下游超时约束。
 - **搜索范围**：`agents.unified_search.limits` 声明 `max_urls`、`thorough_max_steps`、`thorough_max_deep_read_urls`，默认分别为 20、10、8。配置冻结后同时用于请求校验、路由和页面读取；不再固定最多 5 个 URL 或 3 类来源。quick/standard 的步骤预算仍为 1/3；显式来源需要足够步骤，预算不足时要求选择更深入的模式，不静默丢弃来源。模型参数不能提高宿主配置。
  - **熔断器递增 TTL**：`SearchCircuitBreaker` 对 `mcp_quota_exceeded` 使用指数递增 TTL（1h → 2h → … → 24h 上限，env `CHATCOPILOT_SEARCH_QUOTA_MAX_TTL`），成功后重置。直接搜索和 delegate 路径共享同一 `SearchCircuitBreaker` 实例。
  - **浏览器降级**：`_needs_browser` 识别 HTTP 403/401/429 为浏览器可解决错误，自动尝试 Playwright 渲染。
  - **Router fallback 降级**：Router LLM 异常时 `thorough` 自动降到 `standard`，runner 同步降级 request.depth，避免 fallback plan 浪费步数和 subagent 预算。
 - **同轮搜索硬保护**：Native/LangGraph 会在同一轮首个完整成功的 `search_information`
   后拦截后续重复搜索并回灌已有结果；部分证据不触发该保护，允许补查未完成内容。
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
