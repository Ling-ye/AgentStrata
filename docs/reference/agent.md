# Agent 与执行 Backend

修改模型、Backend、PromptPlan 或委托执行时阅读。BotSpec 选择能力，Application 完成实例到 Agent 的受控投影。

## 一条执行契约

Native、LangGraph 和 Codex 共用任务、事件、结果与 PromptPlan 契约。Backend 私有协议由 adapter 解释，平台身份与交付事实留在外层。能力不足时明确报不可用，不通过文本隐式切换 Backend。

## 源码入口

- [src/chatcopilot/agent/backends](../../src/chatcopilot/agent/backends)
- [src/chatcopilot/agent/context/prompt_plan.py](../../src/chatcopilot/agent/context/prompt_plan.py)
- [src/chatcopilot/contracts/agent.py](../../src/chatcopilot/contracts/agent.py)
- [src/chatcopilot/contracts/prompt.py](../../src/chatcopilot/contracts/prompt.py)
- [tests/unit/test_prompt_plan_architecture.py](../../tests/unit/test_prompt_plan_architecture.py)

## Agent 流式观测

主 Codex 使用每回合隔离的 App Server stdio，沿用 actor、PromptPlan、ExecutionScope 和凭据租约。公开消息、摘要和命令输出通过 AgentContentDelta 进入既有观测索引，Console SSE 只读续传；过程不进入渠道最终回复，不采集 raw/encrypted reasoning，不重放已开始的 turn。独立 worker/research 保留 exec，规格见 `docs/reference/observability.md`。

## Agent 层禁止 import

`chatcopilot.botspec.*` / `chatcopilot.platforms.*` / `chatcopilot.middleware.*` / middleware `Workspace` 实现 / `BotRuntimeContext` / ACP 帧。共享 DTO/ports 只能从 `chatcopilot.contracts` 取；策略通过 hook 注入，如 `tool_payload_filter`、`background_submitter`、`file_sender`。

## Contracts 层禁止 import

`chatcopilot.agent.*` / `chatcopilot.middleware.*` / `chatcopilot.platforms.*` / `chatcopilot.botspec.*` / `chatcopilot.external_tools.*`。

## 唯一 PromptPlan 契约

BotSpec `prompts.schema_version` 只接受 `2`，Bot 文件只声明 `identity/response_style/refusal_style/role_styles/mode_styles`，不得声明安全、授权、记忆、人格持久化、搜索触发或工具规则。

middleware 只提供可信结构化输入，所有 main Agent、subagent、backend 和 Evaluation 模型入口都经唯一 `PromptPlanBuilder`；Native/LangGraph/Codex renderer 只渲染不可变 plan，禁止追加第二份规则。

Prompt trust 必须保持 `host policy / runtime facts / bot instructions / untrusted data` 四分区：只有宿主策略和可信运行时事实进入 Native system envelope，Bot identity/style/Skills 使用独立 user-context envelope；Codex 使用 schema v2 的独立字段。

Bot 文本只能形成 identity/style，persona、memory、journal、网页和用户正文始终是不可信数据。

禁止恢复旧 prompt assembler、旧导出、旧字段转换、自由文本 capability fragments 或 backend appendix。

## LLM 三槽配置

BotSpec 的 `llm.chat / llm.research / llm.code` 分别声明日常模型前缀、研究模型前缀和 Codex 路由策略；非密钥默认值进入版本库，secret 留在 `local.env`。research 只覆盖实际提供的字段，其余配置继承 chat；机器 env 仍是最高优先级。`llm.code.reasoning_effort` 与 `llm.code.profiles` 形成对话可选白名单，`/model` 只修改当前 ACP session 的主 Codex lane，不能改变共享 chat LLM 或独立 code-worker。启用 `dev.code_tasks` 的实例必须用 `llm.code.code_task_profile` 引用现有 profile；worker 启动时从实例前缀 env 解析该 profile，再内部派生 `CHATCOPILOT_CODE_MODEL` / `CHATCOPILOT_CODE_REASONING_EFFORT`，不得从 `local.env` 直接导入这两个全局变量。

## 大模块保留 facade

`agent/mcp/client.py`、`agent/tools/builtin/workspace_tools.py`、`agent/subagents/registry.py`、`agent/search/coordinator.py` 是稳定入口；新增职责放到同层子模块，不把 runner/stateless/serialization/workspace handler/subagent definition/delegate/workflow/search factory/circuit/result helper 逻辑塞回 facade。

## 当前导出与 Legacy 退出

内部代码使用 canonical imports：`core.config` / `core.llm_client` / `core.concurrency`、`core.mcp_catalog`、`core.workspace_runtime`、`component_catalog`、`contracts.agent` 和 `agent.search`。L01 的 15 个旧转发文件已删除，完整名单与替换关系见 `docs/reference/runtime.md`；生产代码、测试和安装包均不得恢复旧模块、空存根或动态回退。`agent.research`、`agent.tools.workspace_context`、`external_tools.shared.tool_spec`、`middleware.mcp.session_gateway` 仍在后续审议范围，不能借 L01 顺带删除。旧 Codex turn routing 模块也不得恢复。

## Subagent 是 Agent 层基础能力

BotSpec 只通过 `agents` 声明 preset、workflow 和预算；主 Agent 通过委托工具调用；subagent 禁止 import middleware、platforms、Workspace。

## Lingye 固定 Codex backend

`lingye-copilot-qq` 使用 `agents.backend: codex`；选择作用于整个实例，不按角色、命令或单回合切换。 切回 Native 或 LangGraph 必须修改 BotSpec 并重新部署；部署前删除旧 backend 状态，失败后不恢复旧会话。 Native 的会话、工具执行、仓库任务和发布能力是长期保留的一等能力。

## 主会话与可选代码任务

旧 `agents.codex.owner_access/member_access`、`access.owner_only_project_access` 已删除，校验给出迁移错误。

Owner 主会话按宿主 ExecutionScope 直接读写项目；`start/get/cancel/resume_code_task` 保留为 Owner 可选独立任务。

独立 systemd code-worker 从远端默认分支创建任务私有 clone，在 bwrap 中使用固定 Codex 二进制与专用 worker 凭据，不能读取个人 MCP、个人 `CODEX_HOME`、AgentStrata Session Gateway 或 GitHub token。

验证通过后仅由受信宿主提交任务分支、非强制 push 并创建草稿 PR；不覆盖源仓、不修改运行副本、不重启、不部署、不 merge。

GitHub fine-grained PAT 必须在 clone 前和交付前解析为 `local.env` 明确配置的预期 actor；`delivery.json` 绑定 canonical actor，缺失、不匹配或漂移均失败关闭。

Git author/committer 使用独立的公开 AgentStrata AI Coding Bot 身份，commit 正文和 Draft PR 顶部保留 repository owner、AI generation 与 human-review-required provenance。

`CHATCOPILOT_CODEX_BOT_HOME` 的 main `auth.json` 与 `worker/auth.json` 必须分别 device auth 并独立 lease；GitHub token 只从 owner-only `0700` 配置目录内的 single-link mode `0600` worker 文件读取，交付进程用 `O_NOFOLLOW` + `fstat` 单次载入；Git askpass 只使用任务期内的临时 `0600` 快照，原始 token 不进入 Codex 沙箱、worker env 或 Git remote。

caller 摘要、角色、策略或 credential generation 变化必须使旧 resume ID 失效，不能只信任 `role_hint`。

## 主 Agent backend

`agents.backend` 默认 `native`，也可设为 `langgraph` 或 `codex`；三个 backend 必须共享 `AgentTask` / `AgentEvent` / `AgentResult` 协议和现有工具注册/权限 hook。选择只发生在实例配置，不按回合自动切换。

Native/LangGraph 复用 `agent/turn.py` 的 `TurnOps`；主 Codex 将 App Server 的公开事件投影为相同事件、工具结果、生命周期 intent 和最终 `AgentResult`，不得让 Console 解析 backend 私有日志。

## Subagent

主 Agent 是唯一对用户负责的交付者；subagent 只通过委托工具执行内部任务，并必须用 `submit_result` 返回 `{ok,summary,findings,evidence,changes,commands_run,outputs,risks,next_steps,confidence,cache_summary}`。

## Task pack

新委托使用 `objective/user_intent/deliverable/constraints/inputs/resources/acceptance_criteria/evidence_required/write_scope/excluded_context/cache_key_hint`；旧 `task` 只作为兼容别名。

## Codex mutation 与 PR 交付

Owner 可用宿主绑定的文件、命令和委托工具直接修改项目；独立代码任务保留为可选方式。

adapter_forge 仍消费一次性源码批准记录，其 selector 是该预设的任务范围。

code-worker 使用全局 FIFO、独立 transient cgroup、远端干净 clone 和 bwrap；changed paths 必须通过 `context.dev`，一次完整门禁通过后由沙箱外受信交付器生成中文 commit、非强制 push 并创建草稿 PR，`delivery.json` 记录分支、commit 与 PR 证据。

Native/LangGraph 保持不 commit/push 的 `RepositoryTaskService`；Codex PR 不自动 merge、部署或重启。
  - **先方案后确认**： Owner 明确要求先分析、设计、评审或给方案并等待后续确认时，当前 turn 只返回方案且不得调用 `start_code_task`；同一 session 后续明确确认时只调用一次，并完整重述已批准范围与可观测验收条件。直接要求立即实现时不增加确认轮，孤立且无明确待确认方案的“确认”必须澄清。提示投影测试须覆盖这三条模型契约；隔离的两轮产品能力 Case 只验证 plan→confirm 主路径，不能证明宿主侧一次性 proposal 门禁或真实 Draft PR E2E。
  - **验证工具链挂载**：bwrap 只把源仓 `.venv` 与经 manifest/父链校验的 `console/web/node_modules` 作为只读工具链映射到每条命令的临时候选树；前端构建仍在 `/workspace/console/web` 执行，任务不得改写宿主依赖。
  - **候选索引验证边界**：full validation 使用 job-private、只读挂载的权威 Git index 表示 `HEAD + exact task delta`，宿主 materialize/verify 只操作 disposable index copy；quick 前真实 index 必须等于 `HEAD`，pytest 等会创建临时仓库的检查不得继承候选 `GIT_INDEX_FILE`。

    每条 quick/full 使用独立 exact-materialized tree、`0700` HOME、无 profile/rc Bash 和独立网络 namespace；clone ignored 内容不进入验证。

    tree/home/index-copy/lock 必须在成功、失败和 resume 路径严格清理，遗留 symlink、foreign owner 或 inode 类型异常时失败关闭且不跟随。

    Console 依赖只在 source/task 的 `package.json` 与 `package-lock.json` 完全一致、父链无 symlink 且 source `console/web/node_modules` 存在时挂载。
  - **实例隔离与恢复**： `start_code_task` request 必须在任务目录可见前持久化非空 `instance_id`；每个 systemd worker 使用 BotSpec 派生的实例专属 workspace，只恢复与当前实例完全匹配的 request，missing/foreign identity 一律 fail closed。
  - **取消与交付边界**： cancel 与进入 `delivering` 必须共享状态锁；进入交付后不可取消，普通后台任务不得依赖该 POSIX 锁。 GitHub 返回的 PR `head.sha` 必须精确等于已验证 commit；远端分支恢复不得 force-push、改写 commit 或静默创建重复 PR。
  - **context.dev 接线**：BotSpec 只声明 `root_env` 与 `shell`，Application 捕获实际配置后生成执行资源；旧 allowed_paths/denied_paths 校验时报迁移错误。code-worker 继续复用现有配置解析与单次任务写入范围。
  - **路径解析与交付**：宿主根据可信角色绑定工作区和项目资源。Owner 文件工具默认相对已配置项目，成员相对当前会话工作区；绝对路径仍检查同一资源范围。命令通过 bwrap 限定可见与可写目录。运行副本只能通过显式部署或 self-update publisher 更新，代码任务的发布流程独立。

## Tool call 完整性修复

`AgentSession._repair_orphan_tool_calls` 扫描 messages，为缺失 tool result 的 `tool_calls` 补全合成 error result（`ok: false, error: aborted`）。三处调用：`_timeout_result`（超时截断后）、tool_call_cap 返回前、每次 `llm.chat()` 前的防御性校验。确保跨 turn 累积的 messages 不会因 orphan `tool_calls` 导致 OpenAI API 400。
