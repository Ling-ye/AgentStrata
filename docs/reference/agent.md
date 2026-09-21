# Agent 与执行 Runtime

修改模型、Runtime、PromptPlan 或委托执行时阅读。BotSpec 选择能力，Application 完成实例到 Agent 的受控投影。

AgentRuntime 重构的目标、结构化交接与迁移验收见
[runtime 路由规格](../../specs/agent-runtime-routing/spec.md)。

## 一条执行契约

Native、LangGraph 和 Codex 共用任务、事件、结果与 PromptPlan 契约。Runtime 私有协议由 adapter 解释，平台身份与交付事实留在外层。能力不足时明确报不可用，不通过文本隐式切换 Runtime。

## 源码入口

- [src/chatcopilot/agent/runtimes](../../src/chatcopilot/agent/runtimes)
- [src/chatcopilot/agent/context/prompt_plan.py](../../src/chatcopilot/agent/context/prompt_plan.py)
- [src/chatcopilot/contracts/agent.py](../../src/chatcopilot/contracts/agent.py)
- [src/chatcopilot/contracts/prompt.py](../../src/chatcopilot/contracts/prompt.py)
- [tests/unit/test_prompt_plan_architecture.py](../../tests/unit/test_prompt_plan_architecture.py)

## Agent 流式观测

主 Codex 使用 actor 级懒启动、可复用的 App Server stdio；一个原生 thread 只有一个活动写入者。同 conversation 继续串行，独立 conversation 可以并发。Application 绑定 PromptPlan、ExecutionScope 和 HostRuntimePolicy，主 lane 在宿主刷新凭据，锁只覆盖读取、刷新和原子写回，不覆盖 turn；只 handoff access token，不复制 refresh token 到 actor home。

公开消息、摘要和命令输出通过 AgentContentDelta 进入既有观测索引，Console SSE 只读续传；过程不进入渠道最终回复，不采集 raw/encrypted reasoning，不重放已开始但结果未知的 turn。独立 worker 保持单独认证 lineage。取消发出原生 interrupt，再关闭连接。

## Agent 层禁止 import

`chatcopilot.botspec.*` / `chatcopilot.platforms.*` / `chatcopilot.middleware.*` / middleware `Workspace` 实现 / `BotRuntimeContext` / ACP 帧。共享 DTO/ports 只能从 `chatcopilot.contracts` 取；策略通过 hook 注入，如 `tool_payload_filter`、`background_submitter`、`file_sender`。

## Contracts 层禁止 import

`chatcopilot.agent.*` / `chatcopilot.middleware.*` / `chatcopilot.platforms.*` / `chatcopilot.botspec.*` / `chatcopilot.external_tools.*`。

## 唯一 PromptPlan 契约

BotSpec `prompts.schema_version` 只接受 `2`，Bot 文件只声明 `identity/response_style/refusal_style/role_styles/mode_styles`，不得声明安全、授权、记忆、人格持久化、搜索触发或工具规则。

middleware 只提供可信结构化输入，所有 main Agent、subagent、runtime 和 Evaluation 模型入口都经唯一 `PromptPlanBuilder`；Native/LangGraph/Codex renderer 只渲染不可变 plan，禁止追加第二份规则。

Prompt trust 必须保持 `host policy / runtime facts / bot instructions / untrusted data` 四分区：只有宿主策略和可信运行时事实进入 Native system envelope，Bot identity/style/Skills 使用独立 user-context envelope；Codex 使用 schema v2 的独立字段。

Bot 文本只能形成 identity/style，persona、memory、journal、网页和用户正文始终是不可信数据。

禁止恢复旧 prompt assembler、旧导出、旧字段转换、自由文本 capability fragments 或 runtime appendix。

## LLM 三槽配置

`llm.chat` 是所有 runtime 的主模型；`llm.research` 是辅助模型；`llm.code` 只供独立 worker。`agents.runtime` 只选择谁执行 loop，模型名、API Key 和历史记录不能改变它。Native/LangGraph 的模型传输可选 Chat Completions、Platform Responses 或 ChatGPT Responses；这些传输只做一次推理，不启动 Codex Agent 或执行工具。

认证使用互斥的 `auth: {mode: chatgpt, profile: main}` 或 `auth: {mode: api_key, key_env: ENV_NAME}`。BotSpec、配置快照和 DTO 只保存引用；订阅 token 不会发送到自定义端点，也不会被环境 API Key 覆盖。请求序列化完成后才生成 Responses 模型输入观察，私有 continuation 只用于协议回传。

`llm.chat.profiles` 形成主模型选择白名单。Owner 使用 `/model <profile> [once]` 或 `/model default`；不会改变认证、runtime 或 worker。启用 `dev.code_tasks` 时，仍须用 `llm.code.code_task_profile` 引用 worker profile。worker 使用 `llm.code.env_prefix`，与主会话超时、命令和选模隔离。

## 能力与结构化交接

`AgentTask.execution` 显式携带执行 ID、session、actor、来源、turn_index、trace 与冻结的模型选择；metadata 不参与身份、授权或路由。`HostRuntimePolicy` 是权限权威，PromptPlan 仅引用 capability digest 与 policy revision，仍保留四个信任分区。

ToolRegistry 是宿主工具唯一注册来源，CapabilitySnapshot 只是当前会话的有效投影。Codex 使用原生文件、命令、图片和普通子代理；人格、记忆、Wiki、业务服务与渠道交付继续经过 `agentstrata` dynamicTools → ToolExecutor。配置了宿主管理搜索时关闭重复原生搜索。main-only 工具在执行时拒绝原生子代理调用。

可选原生扩展文件由 `agents.runtime_options.codex.extensions` 声明，只接受 `mcp_servers/plugins/skills/apps` 表；不能覆盖身份、模型或权限。MCP 秘密只能通过显式环境引用传入，当前只授予 Owner，成员不可访问扩展。连接应用必须声明 `apps._default.enabled=false` 并逐个启用，只有 MCP 声明不会顺带开放整个账号的应用。自动推荐安装和 Skill 自动安装 MCP 关闭，未声明的个人配置不导入。

Application 只通过 `AgentRuntime.open_session()` 取得显式的公开 session 与宿主工具执行端口；session 仅暴露 `run_task/update_context/record_exchange/snapshot_transcript/cancel/close/discard`。transcript 明确标注 host_history 或 adapter_visible，不充当原生恢复状态。RuntimeSessionBinding 记录 actor、认证身份代际、能力和策略摘要；普通 token 刷新不失效，重新登录或不兼容权限变化失效。群 actor 只支持进程内恢复。

## 非交互执行

所有 Agent Runtime 固定使用非交互模式。宿主权限和沙箱已授权的操作直接执行；权限扩张、越界访问、MCP elicitation 和原生用户输入请求立即拒绝，不创建等待中的 QQ 或 Console 请求。信息不足时采用安全默认值，无法可靠完成时直接说明限制，不向用户索取补充材料。Adapter 导入、Harness 交付和发布部署等独立业务授权不受此规则影响。

## 大模块保留 facade

`agent/mcp/client.py`、`agent/tools/builtin/workspace_tools.py`、`agent/subagents/registry.py`、`agent/search/coordinator.py` 是稳定入口；新增职责放到同层子模块，不把 runner/stateless/serialization/workspace handler/subagent definition/delegate/workflow/search factory/circuit/result helper 逻辑塞回 facade。

## 当前导出与 Legacy 退出

内部代码使用 canonical imports：`core.config` / `core.llm_client` / `core.concurrency`、`core.mcp_catalog`、`core.workspace_runtime`、`component_catalog`、`contracts.agent` 和 `agent.search`。旧主 Codex TCP/MCP session relay 已由 dynamicTools 替代，不能恢复旧模块、空存根或动态回退。worker 协议仍独立维护。

## Subagent 是 Agent 层基础能力

BotSpec 只通过 `agents` 声明 preset、workflow 和预算；主 Agent 通过委托工具调用；subagent 禁止 import middleware、platforms、Workspace。

## Lingye 固定 Codex runtime

`lingye-copilot-qq` 使用 `agents.runtime: codex`，主模型位于 chat 槽。选择作用于整个实例，不按角色、命令或单回合切换。旧配置和 Gateway schema 2 须在停机、独占 lease 下显式迁移，归档旧 runtime binding，保留 journal、人格、记忆、worker 和历史证据；部署不会自动删除历史。迁移入口见[配置](configuration.md)。Native 仍是一等执行实现。

## 主会话与可选代码任务

旧 `agents.codex.owner_access/member_access`、`access.owner_only_project_access` 已删除，校验给出迁移错误。

Owner 主会话按宿主 ExecutionScope 直接读写项目；`start/get/cancel/resume_code_task` 保留为 Owner 可选独立任务。

独立 systemd code-worker 从远端默认分支创建任务私有 clone，在 bwrap 中使用固定 Codex 二进制与专用 worker 凭据，不能读取个人 MCP、个人 `CODEX_HOME`、AgentStrata Session Gateway 或 GitHub token。

验证通过后仅由受信宿主提交任务分支、非强制 push 并创建草稿 PR；不覆盖源仓、不修改运行副本、不重启、不部署、不 merge。

GitHub fine-grained PAT 必须在 clone 前和交付前解析为 `local.env` 明确配置的预期 actor；`delivery.json` 绑定 canonical actor，缺失、不匹配或漂移均失败关闭。

Git author/committer 使用独立的公开 AgentStrata AI Coding Bot 身份，commit 正文和 Draft PR 顶部保留 repository owner、AI generation 与 human-review-required provenance。

`CHATCOPILOT_CODEX_BOT_HOME` 的 main `auth.json` 与 `worker/auth.json` 必须分别 device auth 并独立 lease；GitHub token 只从 owner-only `0700` 配置目录内的 single-link mode `0600` worker 文件读取，交付进程用 `O_NOFOLLOW` + `fstat` 单次载入；Git askpass 只使用任务期内的临时 `0600` 快照，原始 token 不进入 Codex 沙箱、worker env 或 Git remote。

caller 摘要、角色、策略或 credential generation 变化必须使旧 resume ID 失效，不能只信任 `role_hint`。

## 主 Agent runtime

`agents.runtime` 默认 `native`，也可设为 `langgraph` 或 `codex`；三个 runtime 必须共享 `AgentTask` / `AgentEvent` / `AgentResult` 协议和现有工具注册/权限 hook。选择只发生在实例配置，不按回合自动切换。

Native/LangGraph 复用 `agent/turn.py` 的 `TurnOps`；主 Codex 将 App Server 的公开事件投影为相同事件、工具结果、生命周期 intent 和最终 `AgentResult`，不得让 Console 解析 runtime 私有日志。

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
