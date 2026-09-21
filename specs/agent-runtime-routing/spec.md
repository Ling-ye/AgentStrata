---
id: agent-runtime-routing
type: architecture
status: accepted
created: 2026-09-21
---

# Agent runtime、模型与宿主交互分离

## Summary

主 Agent 按实例选择 Native、LangGraph 或 Codex。模型、认证与执行循环分别声明；
Native 直接调用模型，Codex 使用原生 App Server。宿主继续拥有身份、权限、业务工具和交付。

## Design

遵守[四层运行时](../runtime-four-layer-definition/spec.md)与
[六层源码依赖](../domain-layered-dependencies/spec.md)。Contracts 定义不可变 DTO 与端口；
Core 负责模型传输和凭据；Application 绑定 actor、资源和权限；Agent 选择执行实现；
Gateway 持有交互决定、取消和交付事实。Console 与 Evaluation 只使用公开控制和查询入口。

权威路由链固定为：

```text
BotSpec agents.runtime
  -> BotRuntimeContext.runtime_id
  -> Application ResolvedRuntimeRoute
  -> AgentRuntime.route
  -> RuntimeAdapterRegistry
  -> NativeRuntimeAdapter | LangGraphRuntimeAdapter | CodexRuntimeAdapter
```

`build_agent_runtime()` 只接受冻结的 `ResolvedRuntimeRoute`，Agent 层不再读取 BotSpec、环境或
第二个 runtime 参数。`RuntimeOpenRequest` 复用同一个 route 并由 adapter 校验。Native 与
LangGraph 使用 `main_model_client` 执行自有 loop；Codex 的该字段必须为空，主 turn 只通过
App Server，宿主辅助能力改用显式的 research/search/subagent model client。

`agents.runtime` 是唯一实例执行选择。`llm.chat` 声明主模型与认证引用；研究模型独立解析；
`llm.code` 仅供独立 worker。旧 `agents.backend` 必须显式迁移，不提供运行时回退。
主会话模型选择不修改共享客户端或 worker。请求冻结模型、trace 和 actor 绑定，任意 metadata
不得决定授权、路由或恢复。配置来源、展示文本与 token 值不进入行为指纹。

Native/LangGraph 保留自身循环，通过单请求模型端口使用 Chat Completions、Platform Responses
或 ChatGPT Responses。模型端口不执行工具。Responses 私有 continuation 不进入可观测正文。
Codex 动态工具调用经同一个 ToolRegistry/ToolExecutor；原生工具与宿主工具显式确定执行归属。
人格、记忆和交付不能用普通文件修改替代。主 Agent 专属工具不授予原生子代理。

主 lane 认证由宿主刷新并向 Codex handoff access token，刷新锁不覆盖 turn；worker 的独立
refresh lineage 保留。actor 连接可复用，conversation lane 仍串行。恢复记录绑定 actor、
认证身份代际、权限和能力摘要。群会话仍只在 live actor 内恢复，未知执行不自动重放。

交互请求区分审批、澄清与 MCP elicitation；决定绑定请求及已认证 responder。操作员不是 QQ
actor。Gateway 原子记录决定后回应当前连接，重启关闭遗留等待者而不重放 RPC。审批沿用
现有 approvals，普通输入单独持久化。QQ 回复在准入后分流，不能排在等待它的 turn 之后。

迁移先固定辅助模型的有效配置，归档旧 runtime binding，保留业务历史、人格、记忆与 worker
记录。数据库迁移要求停机及独占 lease，并通过 SQLite backup API 保留备份；启动不隐式迁移。

结构化版本固定为 PromptPlan 2、Observation 2、Evaluation result 3 和
RuntimeSessionBinding 4。AgentEvent、Console wire、Evaluation Target/Trial、transcript
session ref 与活动数据库只使用 `runtime_id`。独立 code-worker 的冻结选择使用
`WorkerModelSelection` 和 `worker_model_selection`；公开 `llm.code` 与 `*_CODE_*` 环境变量
保持 worker 语义。

阶段 A 仅由 `runtime-cutover check/apply/verify --inventory ...` 读取旧格式。check 绑定
BotSpec/env/路径摘要和 plan digest；apply 在实例 lease 内用 SQLite backup API 备份 Gateway，
把旧 Observation、Evaluation、transcript、worker 与 session state 原字节移入私有归档并
写 mode 0600 manifest/receipt；verify 复核摘要、strict-load 新 schema，并通过生产 Gateway
装配执行 admission-denied 受控回放，固定 `production_delivery: false`。在线程序没有旧格式
fallback。阶段 B 只有在所有 inventory 实例 receipt 成功后才能删除 cutover reader；归档永远
没有在线 reader。

## Acceptance

- Native/Codex 与订阅/API Key 四种组合显式选路，兼容模型与 LangGraph 不退化。
- Codex 主会话不创建无用 API 客户端，不运行 Native loop；Native 不启动 Codex agent。
- 宿主工具、能力投影、PromptPlan 和执行授权一致，秘密不进入 DTO、日志或浏览器响应。
- 独立 conversation 可并行刷新与执行，同群 journal 顺序保留。
- QQ/Console 可处理绑定的交互，重复、冒领、取消、超时和重启不能产生第二次决定。
- 执行、业务提交和 provider acknowledgement 分别记录；历史缺字段不回填当前配置。
- 静态 vocabulary gate 拒绝退役 Agent Backend 符号与目录；通用 HTTP/database backend 不受限。

## Verification

执行 DTO/codec、模型 SSE、认证、Runtime、Gateway 交互、cutover 与 Console 定向回归，再运行
仓库 full 检查。复用生产 Gateway 装配回放，受控证据保留 `production_delivery: false`。
真实模型四组合与真实 QQ 验收独立记录，不以 mock 或局部测试替代。
