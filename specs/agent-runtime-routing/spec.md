---
id: agent-runtime-routing
type: architecture
status: accepted
created: 2026-09-21
---

# Agent runtime、模型与宿主交互分离

## Summary

AgentStrata 的四层 Agent 运行系统是产品主体，AgentRuntime 保留 Agent 层统一执行入口与共享能力装配的含义。
主 Agent 按实例选择 LangGraph 或 Codex，模型、认证与执行循环分别声明。
项目实现的 LangGraph 图、上下文、工具与协作策略都是自有 Agent 能力及 Harness 的优化对象；
Codex 经公开 App Server 接入，宿主继续拥有身份、权限、业务工具和交付。

## Design

遵守[四层运行时](../runtime-four-layer-definition/spec.md)与
[六层源码依赖](../domain-layered-dependencies/spec.md)。Contracts 定义不可变 DTO 与端口；
Core 负责模型传输和凭据；Application 绑定 actor、资源和权限；Agent 选择执行实现；
Gateway 持有取消和交付事实。所有 Agent 固定非交互执行；Console 与 Evaluation 只使用公开控制和查询入口。

定位修订沿用现有类、配置、CLI 和持久化字段，不新增 runtime ID。
LangGraph 路径是深度策略定制主线；优先根据需求改进能力、上下文和应用策略，
问题位于控制流程时再修改图、TurnOps、停止或委托策略。循环始终可成为正式优化对象。
Agent 图与 Harness 编排图分别维护，候选改变下一版本的 Agent，不改变本次冻结的评分与接受规则。

Codex 是可选业务执行器，近期也是 Harness 唯一开发 worker；开发 worker 与被优化实例的 runtime、
模型和环境分别绑定，不开发第二套 LangGraph 编程 worker。Codex 路径可改进宿主提示词、工具、上下文和桥接，
其未开放内部行为不属于自有实现修改范围。后续优化顺序与验收由[主计划](../application-harness-framework/spec.md#自有-agent-能力优化路线)维护。

权威路由链固定为：

```text
AgentProfile agents.runtime → BotSpec 类型化装配
  -> BotRuntimeContext.runtime_id
  -> Application ResolvedRuntimeRoute
  -> AgentRuntime.route
  -> RuntimeAdapterRegistry
  -> LangGraphRuntimeAdapter | CodexRuntimeAdapter
```

`build_agent_runtime()` 只接受冻结的 `ResolvedRuntimeRoute`，Agent 层不再读取 BotSpec、环境或
第二个 runtime 参数。`RuntimeOpenRequest` 复用同一个 route 并由 adapter 校验。LangGraph 使用 `main_model_client` 执行自有 loop；Codex 的该字段必须为空，主 turn 只通过
App Server，宿主辅助能力改用显式的 research/search/subagent model client。

`agents.runtime` 是唯一实例执行选择。`llm.chat` 声明主模型与认证引用；研究模型独立解析；
`llm.code` 仅供独立 worker。旧 `agents.backend` 必须显式迁移，不提供运行时回退。
主会话模型选择不修改共享客户端或 worker。请求冻结模型、trace 和 actor 绑定，任意 metadata
不得决定授权、路由或恢复。配置来源、展示文本与 token 值不进入行为指纹。

LangGraph 保留自身循环，通过单请求模型端口使用 Chat Completions、Platform Responses
或 ChatGPT Responses。模型端口不执行工具。Responses 私有 continuation 不进入可观测正文。
Codex 动态工具调用经同一个 ToolRegistry/ToolExecutor；原生工具与宿主工具显式确定执行归属。
人格、记忆和交付不能用普通文件修改替代。主 Agent 专属工具不授予原生子代理。

主 lane 认证由宿主刷新并向 Codex handoff access token，刷新锁不覆盖 turn；worker 的独立
refresh lineage 保留。actor 连接可复用，conversation lane 仍串行。恢复记录绑定 actor、
认证身份代际、权限和能力摘要。群会话仍只在 live actor 内恢复，未知执行不自动重放。

Codex 固定使用 `approvalPolicy=never`，宿主策略内操作直接执行；权限扩张、越界访问、用户输入
请求和 MCP elicitation 立即拒绝。LangGraph、Codex、Evaluation 与后台 Agent 都不创建
等待中的审批或澄清。信息不足时采用安全默认值，无法可靠完成时直接说明限制，不向用户提问。

迁移先固定辅助模型的有效配置，归档旧 runtime binding，保留业务历史、人格、记忆与 worker
记录。数据库迁移要求停机及独占 lease，并通过 SQLite backup API 保留备份；启动不隐式迁移。

Native 独立执行路径退役，自有子 Agent 使用 LangGraph，公共会话状态与 TurnOps 继续复用。
有效 runtime 只有 langgraph/codex，通用默认值与模板选择 LangGraph，明确选择 Codex 的配置保持。
旧 Native 配置与配置历史废弃，不提供自动转换、兼容别名或专门迁移器；旧运行时恢复绑定不续跑。
`runtime_options.langgraph.env_prefix` 接替进程内参数声明，旧 native 块明确报错。
历史观测和评测保留原 runtime 标识，已归档/失效绑定可以只读审计；活跃 Native 绑定不接受。
业务历史、人格、记忆和凭据保持完整，切换 runtime 建立新运行时会话。

结构化版本固定为 PromptPlan 2、Observation 3、Evaluation result 3、Gateway 4 和
RuntimeSessionBinding 4。AgentEvent、Console wire、Evaluation Target/Trial、transcript
session ref 与活动数据库只使用 `runtime_id`。独立 code-worker 的冻结选择使用
`WorkerModelSelection` 和 `worker_model_selection`；公开 `llm.code` 与 `*_CODE_*` 环境变量
保持 worker 语义。

历史 runtime cutover 已离线归档并完成验证；在线程序和最终源码不包含旧 schema reader、迁移器
或 fallback。现行数据库只接受上述结构化版本，旧文件只能按既有私有 manifest 离线审计。

## Acceptance

- 四层运行系统、AgentRuntime、自有 LangGraph Agent 与 Codex 角色含义明确，既有公开字段和路由保持稳定。
- 自有循环与能力实现可以被优化；Agent 候选与冻结的 Harness/Eval 控制、权限和接受规则分离。
- LangGraph/Codex 与订阅/API Key 的兼容组合显式选路，模型配置不隐式改变 runtime。
- Codex 主会话只执行 app-server 回合；LangGraph 通过宿主模型端口执行 StateGraph 回合。
- 宿主工具、能力投影、PromptPlan 和执行授权一致，秘密不进入 DTO、日志或浏览器响应。
- 独立 conversation 可并行刷新与执行，同群 journal 顺序保留。
- QQ/Console 不展示或处理 Runtime 待办请求，所有 turn 在既有策略边界内自行完成或直接说明限制。
- 执行、业务提交和 provider acknowledgement 分别记录；历史缺字段不回填当前配置。
- 静态 vocabulary gate 拒绝退役 Agent Backend 符号与目录；通用 HTTP/database backend 不受限。

## Verification

执行 DTO/codec、模型 SSE、认证、Runtime、Gateway 协议与 Console 定向回归，再运行
仓库 full 检查。复用生产 Gateway 装配回放，受控证据保留 `production_delivery: false`。
真实模型四组合与真实 QQ 验收独立记录，不以 mock 或局部测试替代。
