# 项目沿革与架构演进

AgentStrata 的开发始于 2025 年 11 月，先后经历早期原型仓库和后期私有开发仓库，
并于 2026 年 8 月建立公开基线。后期私有仓库包含 196 次提交；更早仓库未计入
这个数字。

公开仓库从审计后的源码树建立，因此公开根提交只表示开源基线，不表示项目起点。
具体规则见
[`fresh-public-repository-bootstrap`](../specs/fresh-public-repository-bootstrap/spec.md)。
下文集中说明项目最初如何组织、实践中暴露了什么问题，以及架构如何逐步调整。

## 演进总览

```mermaid
flowchart LR
    A["端到端原型<br/>打通消息、Agent、工具与部署"]
    B["声明式实例<br/>BotSpec · adapters · tool packs"]
    C["契约内核<br/>contracts · core · architecture gates"]
    D["统一 Agent runtime<br/>Native · LangGraph · Codex"]
    E["隔离开发任务<br/>read-only session · worker · Draft PR"]
    F["统一搜索<br/>direct providers · deadlines · circuits"]
    G["统一 Evaluation<br/>lifecycle · claims · artifacts"]
    H["公开基线<br/>public boundary · capability parity"]
    I["插件化能力评测<br/>trusted cases · supervised trials"]
    J["确认式开发请求<br/>plan first · explicit confirm · isolated evidence"]
    K["QQ 群级共享会话<br/>conversation scope · turn identity · actor-bound execution"]
    L["统一上下文可观测性<br/>effective input · provider boundary · safe artifacts"]
    M["架构边界加固<br/>trust partitions · static DAG · zero multi-module SCCs"]
    N["QQ Gateway 收敛<br/>direct OneBot channel · optional ACP edge"]

    A --> B --> C --> D --> E --> F --> G --> H --> I --> J --> K --> L --> M --> N
```

这些阶段按主要架构变化划分，实际开发时间存在重叠。

## 1. 端到端原型：先打通完整运行链路

**初始设计**

项目最初以可运行的机器人为目标，在同一工程中串联平台消息、LLM turn、工具调用、
workspace、部署和基础运维。这个阶段优先验证端到端能力，而不是提前拆分大量抽象层。

**暴露的问题**

随着平台、工具和运行场景增加，实例策略、平台行为、Agent 逻辑和部署参数开始互相
影响。继续在共享 runtime 中增加机器人或平台条件，会让每次扩展都触及多层代码。

**优化**

保留单一代码库，但把实例差异从共享实现中提取出来：机器人负责声明自身能力，
共享层负责稳定运行机制，平台差异通过 adapter 接入。

**结果**

项目从“一个可运行的机器人实现”转向“多个机器人实例共享同一运行平台”。

相关文档：[`README.md`](../README.md)、
[`architecture.md`](architecture.md)。

## 2. 声明式实例：用 BotSpec 取代产品分支

**初始设计**

早期配置与实现仍然紧密相邻。机器人需要什么 prompt、工具、模型、上下文或平台能力，
部分由配置表达，部分由代码路径决定。

**暴露的问题**

新增机器人或平台时，如果继续在 Agent、middleware 和部署脚本中加入条件分支，
实例差异就会扩散到共享层；工具发现、权限和运行配置也容易形成多套事实源。

**优化**

BotSpec 被整理为四个核心面：

- `prompts`：角色与行为指令。
- `tools`：工具包、MCP binding 和能力开关。
- `agents`：主 backend、subagent、搜索策略和访问模式。
- `context`：RAG、Wiki、memory、playbooks、代码仓库和开发范围。

`platform`、`llm`、`workspace`、`deploy` 和 `access` 组成实例运行包络。
平台通过 registry 自动发现 adapter，工具通过 catalog 和 tool pack 装配。

**结果**

实例能力可以通过 BotSpec 组合；新增平台主要实现 adapter，新增工具能力主要进入
tool pack 或 MCP catalog，而不是修改 Agent 层的产品分支。

相关文档：[`bot-spec.md`](bot-spec.md)、
[`architecture.md#BotSpec 组合`](architecture.md#botspec-组合)。

## 3. 契约内核：从便利 import 转向可检查的依赖方向

**初始设计**

平台、middleware、Agent、BotSpec 和 external tools 已经形成模块分区，但部分共享
DTO、registry 和策略仍定义在具体实现包中。Prompt assembly、workspace 类型和组件
发现也存在跨层直接引用。

**暴露的问题**

便利 import 逐渐形成反向依赖：

- Contracts 之外的实现包拥有跨层 DTO。
- External tools 依赖 Agent 或 BotSpec 内部结构。
- Console 直接读取内部 registry。
- Search 新旧实现互相引用，兼容入口和实际实现边界不清。
- 大型 facade 持续吸收新职责。

这些依赖让局部修改更容易越过层级边界，也允许相似行为在不同位置并行演化。

**优化**

项目引入 `chatcopilot.contracts` 作为共享类型内核，并进一步建立：

- `chatcopilot.core`：配置、LLM client、MCP catalog、workspace runtime 等中立入口。
- `chatcopilot.component_catalog`：控制面读取组件信息的稳定投影。
- Canonical imports 与只负责旧导出的 compatibility wrappers。
- 稳定 facade 与同层聚焦子模块的拆分规则。
- `scripts/check_architecture.py` 和 SDD-lite 规格门禁。

**结果**

依赖方向由约定变成可执行检查。Agent、external tools、contracts、platforms 和
control plane 的职责边界能够在 CI 与本地验证中持续约束。

相关规格：
[`architecture-contract-kernel`](../specs/architecture-contract-kernel/spec.md)、
[`architecture-boundary-consolidation`](../specs/architecture-boundary-consolidation/spec.md)、
[`architecture-decoupling-roadmap`](../specs/architecture-decoupling-roadmap/spec.md)。

## 4. 主 Agent 收敛：统一 Native、LangGraph 与 Codex 生命周期

**初始设计**

Native 和 LangGraph 已经可以作为主 Agent，Codex 随后加入。不同 backend 对
session、streaming、工具结果和 turn completion 的职责划分并不一致，ACP 还承担了
部分 backend 特有的路由和生命周期逻辑。

**暴露的问题**

Backend 差异向上泄漏后，同一条消息在附件、权限、session 恢复、错误处理和事件流
方面可能经过不同路径；新增 backend 也容易复制一套 turn orchestration。

**优化**

三个 backend 统一到 `AgentTask`、`AgentEvent`、`AgentResult` 和共享 turn
runtime：

- `BotSpec.agents.backend` 成为主 backend 的唯一选择点。
- Backend 按整个实例选择，不按角色、命令或单回合动态切换。
- ACP 只保存不透明的 backend session reference。
- 附件、访问检查、确定性回复、session materialization、执行和完成事件进入有序
  pipeline。
- Backend 能力与工具访问策略取交集，不支持的能力明确失败。

**结果**

Native、LangGraph 和 Codex 成为相同生命周期契约后的平级实现；平台层和控制面不再
需要理解某个 backend 的内部 session 模型。

相关规格：
[`main-agent-backend-unification`](../specs/main-agent-backend-unification/spec.md)、
[`deterministic-llm-boundaries`](../specs/deterministic-llm-boundaries/spec.md)、
[`lazy-acp-agent-runtime`](../specs/lazy-acp-agent-runtime/spec.md)。

## 5. 开发能力隔离：从交互式修改转向异步代码任务

**初始设计**

Owner 会话可以发起源码修改，早期流程更接近同步执行：对话、代码变更、验证和运行
环境之间的边界较弱。

**暴露的问题**

如果交互会话同时拥有源码写入、个人凭据、宿主 shell 和发布能力，就难以稳定保证：

- 一次任务只修改声明范围。
- 取消、恢复和失败不会留下不明状态。
- Worker 无法读取个人 MCP、平台凭据或宿主源码。
- 验证通过的 commit 与交付的 Pull Request 完全一致。
- 源码修改不会自动变成部署或服务重启。

**优化**

开发流程改为双 lane：

- 主 Codex 会话保持源码只读，只负责提交和查询任务。
- 独立 code worker 从远端默认分支创建任务专属 clone。
- Worker 在隔离边界中运行，使用独立认证 lane 和资源限制。
- `context.dev` 约束允许与禁止修改的路径。
- 受信宿主在验证后提交任务分支并创建 Draft Pull Request。
- 流程不自动 merge、deploy、restart，也不覆盖操作者工作区。

**结果**

对话能力与源码 mutation、验证、交付和部署被拆成不同信任边界；Native/LangGraph
原有的本地 RepositoryTaskService 仍作为独立能力保留。

相关规格：
[`codex-main-session-permissions`](../specs/codex-main-session-permissions/spec.md)、
[`qq-owner-isolated-code-tasks`](../specs/qq-owner-isolated-code-tasks/spec.md)、
[`codex-independent-auth-lanes`](../specs/codex-independent-auth-lanes/spec.md)、
[`codex-pull-request-delivery`](../specs/codex-pull-request-delivery/spec.md)。

## 6. 搜索收敛：从按来源编排转向统一执行

**初始设计**

不同搜索源主要通过各自的 subagent 和 MCP 工具执行；router、reranker、页面读取和
fallback 分散在 research/search 路径中。

**暴露的问题**

多层 LLM 编排增加延迟和 token 消耗；同源查询可能重复；大量结果会扩张 context；
远端 quota、超时、动态页面和显式来源要求缺少统一处理方式。

**优化**

搜索统一到 `search_information` 和 `SearchCoordinator`：

- 常见路由由确定性逻辑完成，只有复杂比较进入路由 LLM。
- `DirectSearchProvider` 跳过不必要的 subagent 会话；薄 Web API provider 在进程内
  调用，账号态和垂直来源保留 search-only MCP。
- 显式来源、canonical URL、去重、来源优先级和结果上限统一处理。
- Deadline、同源步骤上限、circuit breaker 和递增 quota TTL 进入协调层。
- 页面读取先走静态抓取，必要时升级到浏览器渲染。
- 同一 turn 的重复搜索由 runtime 拦截。
- BotSpec 成为搜索 provider 与共享服务启用状态的唯一事实源；服务管理器只启动
  SearXNG engine、Playwright、小红书等确实需要隔离的组件。
- 删除 Tavily / Brave / SearXNG MCP wrapper、Sequential Thinking 和未完成独立审阅
  的 Taoke 部署；保留容器固定 digest、回环端口和有界资源。

**结果**

搜索源仍可独立扩展，但路由、容错、时间预算和结果整理只维护一套行为。Native 与
LangGraph 共享这条路径，线上 Lingye 实例继续使用 Codex backend；Native 可用性通过
隔离 override 验证，不修改部署配置。

相关文档：[`runtime.md`](runtime.md)；规格：
[`mcp-runtime-placement-policy`](../specs/mcp-runtime-placement-policy/spec.md)。

## 7. Evaluation 统一：合并比较评测与 benchmark suite

**初始设计**

Agent Profile 对比使用 Experiment，BFCL、GAIA、IFEval 等 benchmark 使用 Suite
Run。两类评测分别拥有 API、manager、页面、持久化和报告概念。

**暴露的问题**

两套资源模型导致生命周期、取消、恢复、并发控制、结果状态和 artifact 布局逐渐
分叉。跨进程 claim、worker 身份、resume fingerprint 和报告可比性也难以在两套实现
中保持一致。

**优化**

评测收敛为一个 `Evaluation` 资源：

- `kind: comparison | suite` 区分评测类型。
- Lifecycle status 与 Trial outcome 分离。
- 同一 Bot 的活动 Evaluation 使用持久化 claim 互斥。
- Resume 在写入前核对请求、Case 快照、Target fingerprint 和 checkpoint。
- Core 独占结构化 progress 写入，Console 只读取脱敏状态。
- Comparison 与 Suite 共享目录、CLI、API、报告和控制台入口。

**结果**

Profile 比较和 benchmark suite 保留各自执行语义，但共享一套生命周期与数据模型，
旧 runs/experiments 双 manager 和 API 被移除。

相关规格：
[`evaluation-center-unification`](../specs/evaluation-center-unification/spec.md)；
相关界面说明见 [`console.md`](console.md)。

## 8. 公开基线：把私有工程转换为可持续维护的公共仓库

**初始设计**

项目长期在私有仓库中运行，源码历史同时包含机器配置、私有身份、组织集成和产品特定
能力。原有仓库适合内部迭代，但不适合作为公共分发边界。

**暴露的问题**

直接公开旧 Git 对象图可能暴露已经从当前文件中删除的信息；只复制部分源码又可能
遗漏通用运行能力或引入迁移回归。

**优化**

公开迁移以经过验证的 tracked-only 源码树建立新根：

- 通用 BotSpec runtime、QQ/Feishu adapter、Codex backend、Console、MCP、
  Evaluation、Wiki、search、memory 和开发任务基础设施继续保留。
- GamePerf 产品能力和私有运行值不进入公共范围。
- 当前树、历史、secret、身份和外部私有字面量使用独立门禁检查。
- `public-capability-parity` 定义迁移后的能力范围与验证方式。
- 公开仓库成为后续源码、规格和发布流程的唯一维护入口。

**结果**

公开版本保留通用平台能力，并将隐私、发布和能力等价纳入仓库级约束；后续功能演进
只在公开仓库继续。

相关规格：
[`fresh-public-repository-bootstrap`](../specs/fresh-public-repository-bootstrap/spec.md)、
[`public-capability-parity`](../specs/public-capability-parity/spec.md)、
[`releasing.md`](releasing.md)。

## 9. Component Catalog 收敛：从模块级发现到精确工具投影

**初始问题**

tool pack 只记录模块路径。多个 Feishu pack 复用同一 `spec` 模块时，选择任意一个
pack 都会加载模块导出的全部工具；builtin pack 又由 Agent 内的第二张表维护，导致
Console 无法展示其工具，静态 catalog 与运行时装配可能漂移。

**结构调整**

- `ToolModuleBinding` 同时声明模块和该 pack 精确拥有的工具名。
- builtin 与 external pack 统一进入 `tool_packs.catalog`，兼容入口只派生视图。
- Agent discovery 和 Console 读取同一个 binding resolver；共享工具必须显式列入每个
  需要它的 pack。
- `component_catalog.audit` 作为薄 facade，按 tool/module 与其他 catalog surface 拆分审计模块，
  检查 pack、feature、MCP、subagent、workflow、prompt manifest、ToolDef schema 和跨 surface
  名称冲突，并以稳定 JSON 接入仓库 fast gate。

**结果**

单个 pack 不再隐式获得同模块的其他领域工具，Console 与 Agent 的 pack 工具列表保持
一致；缺失、未分配、冲突或无效声明在测试前失败。

相关规格：
[`component-catalog-consistency-gate`](../specs/component-catalog-consistency-gate/spec.md)。

## 10. Evaluation 插件化：从少量 benchmark 路径到产品能力 Case

**初始问题**

统一 Evaluation 已解决生命周期、claim、artifact 和 Console 所有权，但 Suite 的 Case
覆盖仍以少量公开 benchmark 和 Runner 内的固定路径为主，不能回答白名单、工具副作用、
图片输入或代码恢复是否按产品契约工作。公开 benchmark 也不能替代这些产品能力的验收；
QQ/NapCat/OneBot 连通性则属于部署与平台检查，不应混入 Agent 能力结论。

**结构调整**

- Suite manifest、Case、fixture 和 verifier 使用仓库内版本化声明；Python 执行只允许
  静态 catalog 中的受信插件，不开放任意第三方模块加载。
- `agentstrata-capabilities-v1` 固定 26 个产品 Case，提供仅手动启动的
  `quick/full/security/custom`；MVP 默认每 Case 1 次，不把单次结果描述为重复
  可靠性。
- 图片理解的 3 个 Case 已配置；图片生成保持 `not_configured`。BFCL 明确保留为
  direct-LLM 协议校准；SWE-bench Verified、WebArena 和 Canary 自更新保持
  `planned/unavailable`。
- 创建前先做无副作用预检；配置阻断不创建 Evaluation、artifact 或进程，也不调用模型。
- 真实 QQ 连通性移到 `external-platform-check/v1`：默认只读验证 OneBot 认证、登录身份与
  可选群访问，并在随机回环端口用假 NapCat + 真实 QQ @ Relay 验证合成帧正例
  转发和负例丢弃，不创建 Evaluation 或调用模型。可选群消息动作要求双参数单次确认；
  没有独立发送 QQ 时，真实入站 Agent 往返仍明确为 `not_tested`。
- OneBot `get_status` 的动作成功与 QQ 在线状态分开判定；`online=false` 或 `good=false`
  会让外部检查失败，Console 也分别展示容器运行、账号离线和状态未知，避免把 Provider
  进程存活误报成 QQ 可收消息。
- 正式 Trial 在独立 spawn 子进程运行，期限取 Case timeout 与剩余 max-wall 的最小值；
  取消和预算终止进程组，Linux/WSL 使用父死保护。只有完整 Target 组进入 checkpoint，
  中断的不完整组不参与恢复和结果聚合。

**结果与证据边界**

产品能力、公开校准与生命周期继续使用同一个 Evaluation Core 和报告根，同时 Case 定义、
执行插件和环境条件可以独立演进。QQ 外部检查保持独立报告语义；hermetic 模拟 ingress
只证明本地 gateway relay，不冒充真实 QQ 或 Agent E2E，避免平台可用性污染 Agent
verdict。仓库自动化覆盖 manifest、插件 parity、预检、进程隔离、预算、取消和 artifact
契约；这些 fixture/mock/dry-run 结果不构成真实商用 LLM、真实 QQ 或 Canary 自更新 E2E
通过的声明，实际结论仍须由维护者手动执行对应检查并审阅证据。

相关规格：
[`evaluation-plugin-capabilities`](../specs/evaluation-plugin-capabilities/spec.md)、
[`qq-external-platform-check`](../specs/qq-external-platform-check/spec.md)。

## 11. 确认式开发请求：区分传输回执、方案与真实任务

**暴露的问题**

cc-connect 在 Agent 处理消息前发送固定即时回复，容易被理解成模型已经开始分析；同时
Owner 开发提示原先强调立即调用 `start_code_task`，与“先给方案、确认后开发”的请求
存在竞争。既有产品能力 Case 只覆盖单轮代码任务生命周期，不能区分两轮工具时序。

**结构调整**

- 生成配置显式关闭 `instant_reply` 并删除固定内容，最终回复和真实工具进度仍由 ACP
  事件链交付。
- Owner、tool pack、工具说明与 Codex policy 统一 plan-first 契约：首轮只给方案，后续
  明确确认后提交一次完整任务；提示投影同时保留直接实现请求不增加确认轮、孤立确认先
  澄清的规则。本次配置模型运行只实测前述双轮主路径。
- 现有代码恢复 Case 升级为同 session 双轮 Case，逐轮保存工具证据，并使用生产形状的
  `title/prompt/acceptance_criteria` 假工具；该工具不创建真实 job 或 PR。
- Codex 的 session MCP 只批准 AgentStrata 已筛选的精确工具白名单，不关闭全局沙箱；
  standalone Evaluation 在预检前冻结 BotSpec、bot-local 与机器环境，并复用部署侧的
  非执行式 `local.env` 和 home 路径语义。

**结果与边界**

配置模型的单次隔离两轮运行观察到它能够先给方案、等待明确确认，再提交完整代码任务；
该证据不等于重复可靠性、线上 QQ、真实 code-worker 或 GitHub Draft PR 通过。当前方案
仍是可评测的模型行为契约；若需要宿主层形式保证，应另行引入绑定 Owner/session/可信
turn、内容 digest、过期时间和一次性消费的 proposal envelope，不能让模型自行声明
`confirmed=true`。

相关规格：
[`code-plan-confirmation-flow`](../specs/code-plan-confirmation-flow/spec.md)。

## 12. QQ 群级共享会话：共享上下文，不共享权限会话

早期 QQ 群目录和 `SessionState` 同时按群成员切分，导致同一群里的不同用户无法延续群
对话或使用同一组普通文件；如果直接复用单一 session，又会把 Owner 权限、工具 hook 和
backend 状态带给下一位说话人。

运行时因此把稳定 `ConversationIdentity` 与逐轮 `TurnIdentity` 分开：QQ 群按群号共享
workspace 和有界 journal，当前发送者则由 cc-connect sender envelope 与同步 message hook
写入的实例私有、有界加锁 attestation 队列在每轮 prompt 边界双重绑定。Middleware 先交叉验证
transport actor 与正文摘要并精确消费一条随机 ID attestation，再选择 actor-bound 执行 session；
角色继续只按稳定发送者解析，Owner 在私聊和群聊都保持 Owner，其他成员保持各自普通角色；
QQ 私聊、不同群、旧成员目录和其它平台
保持隔离。

共享范围覆盖普通群文件和 conversation history；权限则按 actor 保持。journal、actor backend
state 与 transcript 收进受保护的 `.conversation-state/`；群 Codex 外层只读暴露 shared root，
文件 mutation 只经 actor-bound scoped MCP。Owner 后台 job 控制面也按 actor 放在保护目录；
已接受回合的 turn diagnostics 按 actor 写入保护目录供 Console 读取，但不进入 member-writable
shared root，普通成员不能发现或控制；该阶段的共享 memory 仍禁用。无法绑定 chat/message
的 cc-connect legacy attachment inbox import 在 shared-group 中失败关闭，shared attachments 中
的同名旧文件也不能作为本次上传证明。

Owner 群聊不再被特判降为 User：它复用完整的通用角色解析、prompt、工具和 Codex 路由；
User/Admin 的 member 投影保持不变。群人格也不再引入专项 manager、intent grant 或新文件格式，
而是复用当时的 Markdown persona provider `group` 层和 `persona_*` 工具。公开群场景仍统一
脱敏 Owner payload，不自动投影 private memory/Wiki/RAG，显式 `private_chat_only` 工具继续拒绝群聊。

journal 同时写入受保护的 epoch/sequence/内容摘要 metadata；删除、合法截断或恢复旧快照都会
群级逐出 actor backend 并失败关闭，不允许缓存 cursor 与新 journal 世代静默分叉。跨进程身份
handoff 也从公共 `/tmp` shell env 改为实例私有 `0700/0600` JSON，wrapper 只加载白名单字段。

部署配置同时启用 cc-connect 的群共享 session 与 sender injection；当前验证覆盖本地合成
ingress 和隔离边界，真实两账号 QQ 群 ingress E2E 仍属于部署验收，不能由单元测试代替。

随后的人格与会话记忆授权重构把权威状态移入 workspace 根的
`.conversation-state/persistent/` 保护域：persona 全部改为 Owner-only，群按
`global → group`、私聊按 `global → user` 逐轮加载；群 memory 改为按稳定群身份共享，准入成员
可 read/append、只有 Owner 可 clear，且不再投影任何 actor 的私聊 memory。旧 p2p memory 只做
一对一迁移，旧群 actor/shared memory 不合并，成员可写 persona 不提升为权威配置。

2026-08-20 的 QQ 失败任务先暴露出工具 schema 投影并不能证明 Codex provider 内部实际看见或
调用了人格工具；随后第一版宿主前置实现又把分类 JSON、URL/短摘录证据和研究草案全部设成保存前
硬门禁，结果真实消息继续分别失败在证据校验和意图判定，`/persona` 后不加空格也无法识别。

2026-08-21 的破坏式提示词重构进一步删除所有双轨入口：BotSpec 只接受 prompts schema v2，
main Agent、subagent、middleware、backend 与 Evaluation 统一使用不可变 PromptPlan；旧 assembler、
旧字段、subagent 平行 prompt、TaskPack 别名、自由文本 capability fragment 和两级质量门禁全部移除。
人格流程同步改为零模型候选 detector、仅歧义调用的严格 interpreter、研究完成后的单次原子写。
命名实体研究或草案失败时不再保留部分写入；中可信要求与 clear 使用 actor-bound 十分钟提案，只有
精确 `/persona confirm` 能落盘。纯人格回合不启动 Codex，复合回合仍只把经过连续子串校验的剩余
任务交给主 Agent。

随后真实 QQ 请求失败在 `enrichment_model_failed`。复核发现旧实现用完整 Owner 原文构造一次固定
搜索，再仅以 URL 数量判断来源充分性；它把日常 chat 模型用于研究合成，OpenAI SDK 默认隐藏重试
又把一次 30 秒调用放大为三次尝试，最终异常类型和实际模型调用没有进入 task artifact。修复删除
`PersonaEnricher` 和“原文 + 资料补充”的拼装路径，改为独立 `PersonaDraftAgent` 在研究模型槽中自行
选择最多三个搜索请求并输出整份严格 JSON Markdown。`set`、`append`、`research`、`refresh` 最终都只
执行一次宿主原子 `set`；SDK 重试关闭，由框架显式拥有预算，任务记录模型、调用、搜索、来源和稳定
错误类。人格控制不再维护歌词专用契约。

相关规格：
[`qq-group-shared-conversation-context`](../specs/qq-group-shared-conversation-context/spec.md)、
[`persona-and-conversation-memory-authorization`](../specs/persona-and-conversation-memory-authorization/spec.md)。

## 13. 上下文可观测性：从 Native 专属步骤到跨 backend 统一快照

**暴露的问题**

任务工作台已经能解释 Native/LangGraph 的模型、工具、Span 和 Token，但 Codex 主
backend 绕过 `TurnOps`，并把 `codex exec --json` 全部缓冲到进程结束，只提取 thread ID
和最终消息。线上 Codex 任务因此没有模型、步骤、usage 或上下文；即使 Native 有
`context_kind` 和 token 粗估，也没有留下该次调用实际收到的消息与工具定义。

**结构调整**

- 共享 contracts 增加每次模型请求的 `ContextSnapshotPrepared`，统一关联 backend、
  model、trace/span、完整 AgentStrata session ledger、effective messages、tool schemas、
  path-free resources、token estimate 与 capture coverage。
- Native/LangGraph 在最终 `LLMClient.chat` 边界记录纯文本 `exact_model_input`；含本地
  二进制资源或受限字段时降为 `partial` 并只保留回执。Codex 记录实际 stdin prompt、
  允许的 MCP 工具面与公开 JSONL activity/usage，并把 provider-native resume 历史和
  内部 instructions 标为 `provider_opaque`。
- 大块上下文不进入频繁轮询的 `task.json` 或 raw event。统一 recorder 在第一次落盘前
  脱敏，将正文写到 private、bounded、lazy-loaded context artifact；事件获得 task-local
  单调 sequence 与稳定 event ID，并通过已验证的 task-dir descriptor 安全追加；崩溃时
  以最后一条完整 JSONL 记录校准 sequence sidecar。Codex 的权威 MCP relay receipt 在进程运行期间按真实
  时间投影；relay 与并行搜索的 nested subagent 事件继承同一 trace、由主线程串行回放，
  不让 worker 并发写 recorder。subprocess 输出、task/turn 总体、后台结果、单事件和
  Console 读取尾部均有显式上限；JSON materialize 和脱敏遍历也有结构/字符串总预算。
  task/job 及祖先 symlink 不能把 artifact 写出 workspace，Console 用同一 descriptor 链
  读取 event/context，避免检查后的祖先替换竞态。
- Console 使用同一摘要和 artifact API 渲染所有 backend，分别展示 AgentStrata 会话历史
  和实际模型输入；不从 Codex 私有目录读取数据，也不保存隐藏 chain-of-thought。
- Recorder 与后台完成 watcher 共享 completion lock 和主 turn 注册边界：快速 child 不能
  提前结束仍可能继续注册 job 的任务，main failure 也不会被迟到的成功 child 覆盖；超大
  result 降级后，worker status 与退出码以实际持久化 manifest 为准。

**结果与边界**

选择 Codex 不再让任务工作台退化为空时间线；后续 backend 只需适配共享事件契约。
“完整”严格限定为 AgentStrata 可见且可证明的边界，provider 不公开的状态始终显式缺失。
安装的 Console unit 改为只监听回环地址，并且只返回脱敏落盘内容；它仍没有独立 HTTP
operator 认证，显式改成非回环地址前必须增加可信代理认证和网络边界。事件 cursor/SSE
和外部 OTLP/Langfuse/Phoenix exporter 不在本次变化内。事件尾部存在权限异常、半写、
损坏或 sequence 缺口时 Console 会显示 `integrity_gap`；Codex 超时后无法强制取消的在途
工具以原 trace 的 unknown late-completion receipt 收口，不会污染下一轮。

相关规格：
[`unified-agent-context-observability`](../specs/unified-agent-context-observability/spec.md)。

## 14. 入站追踪与机器级更新：消除控制台盲区和逐实例手工操作

**暴露的问题**

ACP 过去只在身份校验、白名单和 actor session 激活之后创建 `task_...`。因此身份见证失败、
群白名单拒绝以及处理管线早期异常都不会出现在 Console，操作者只能从聊天回复或 service
日志推断发生过什么。同时，`deploy_console.sh` 的默认模式只修复 Console；机器上有多个
BotSpec 时仍需手工逐个运行 `update_instance.sh`，容易漏更新某个运行副本。

**结构调整**

- 可信身份一旦解析完成，就在访问策略前创建 actor-scoped task；白名单拒绝只结束该 task，
  不激活 actor execution session。
- 身份无法可信绑定时，在群保护状态的 `task-intake` 分区写通用失败记录；不落盘原始正文、
  sender envelope 或发送者账号。任何任务存储初始化失败都在附件、模型与工具执行前失败关闭。
- Console 继续通过统一 task discovery 读取普通 workspace、`task-actors` 与 `task-intake`，
  不给群 workspace 工具新增读取面。
- 不带参数的 `deploy_console.sh` 在 Console 安装/修复后发现全部 BotSpec，并复用唯一的
  `update_instance.sh` 逐个更新运行副本。失败实例不会阻断后续实例，最终统一汇总并返回非零；
  `--skip-bots` 提供显式 Console-only 路径，页面自更新继续使用 `--update-only`。

**结果与边界**

Console 现在能区分已接受、准入拒绝和身份拒绝的入站消息，而不以观测需求削弱 QQ 身份
边界。机器级更新入口覆盖仓库中全部已声明机器人，并通过 canonical updater 修复 systemd
注册配置和重启服务；它不自动启动共享 Docker 服务、不启用新的开机自启，也不执行 Git 或
发布操作。首次部署仍使用专用首次部署流程。

相关规格：
[`task-observability-workbench`](../specs/task-observability-workbench/spec.md)、
[`qq-group-shared-conversation-context`](../specs/qq-group-shared-conversation-context/spec.md)、
[`all-bot-console-deploy-entrypoint`](../specs/all-bot-console-deploy-entrypoint/spec.md)。

## 15. 架构边界加固：从局部禁止规则到可执行静态依赖门禁

**暴露的问题**

原有架构门禁能阻止若干明显的跨层 import，但没有建立覆盖 `src/chatcopilot` 与 Console Python
源码的静态 import 图；当时 401 个受检 Python 模块和 1,149 条可静态解析内部边仍形成 5 个强连通
分量。Prompt DTO 虽区分宿主策略、运行时事实和不可信数据，
renderer 仍把 Bot identity/style 合入 system authority。显式启用的 tool pack 在 import 或导出异常时
还会静默消失，使部署错误表现成能力缺失。

**结构调整**

- Prompt layer kind 与 trust 改为封闭映射，renderer 固定输出 host policy、runtime facts、Bot
  instructions 和 untrusted data 四个分区。Native 的 user-context envelopes 作为不可裁剪前缀保留，
  topic routing 不把它们误识别成历史用户发言。
- 架构脚本解析 `src/chatcopilot` 与 Console 的绝对、相对 import，同时验证 area policy DAG、受检
  模块静态 SCC、canonical import、兼容 facade 白名单和跨 owner 私有符号。
- Workspace identity 改为依赖 contract view；QQ 的 token/loopback boundary 与 access relay、
  Evaluation execution support/runtime、ACP tool permission/workspace service/job host port 分别归属明确
  模块。facade 保留已有入口，但生产实现不再借兼容层反向调用。
- Tool catalog binding 改为显式物化契约；模块、`TOOLS`、类型、重复名称和缺失声明任一异常都返回
  `ToolMaterializationError`。Evaluation 预检可以结构化呈现该错误，真实运行不能降级为空列表。
- 唯一 PromptPlan 删除旧 builtin prompt 资源后，打包 allowlist 和安装后 runtime probe 同步改用
  canonical PromptPlan，避免源码架构已迁移而 sdist 契约仍指向已删除资源。

**结果与边界**

受检 Python 模块的静态 import 图不再包含多模块强连通分量；架构 CLI 与单元测试执行同一个检查
入口和规则集合。既有 BotSpec、工具名、平台协议、Evaluation artifact 和兼容 facade 保持不变。
自动化验证覆盖本地 renderer、真实 QQ @ Relay 上的合成 OneBot 消息，以及结构化证据中明确列出的
AgentStrata-owned ACP 准入、任务、确定性 Agent 和回复投影链；它不等于真实 QQ、NapCat、cc-connect、
商用模型或两账号外部往返，也不能证明 Codex provider 内部 instructions。

相关规格：
[`architecture-boundary-hardening`](../specs/architecture-boundary-hardening/spec.md)、
[`prompt-plan-architecture`](../specs/prompt-plan-architecture/spec.md)。

## 16. 测评中心两轨收敛：分开 Agent 表现与 QQ 后链路

**暴露的问题**

旧控制台把 Agent Profile 对比、公开 benchmark、产品能力、ACP 场景、数据准备和覆盖目录
放在同一创建面。产品能力 Suite 也同时包含真实 Agent Trial 与无模型 ACP 场景，因此一次
结果既不能纯粹回答“Agent 会不会”，也不能完整回答“QQ 消息进入后链路是否正确”。

**结构调整**

- Console 主测评面只保留“直接测试 Agent 能力”和“QQ 消息全链路”两张卡，以及统一运行记录。
- `agentstrata-capabilities-v1` 收敛为 25 个纯 Agent Case，加入人格行为和独立 ECB oracle
  判分的最新 USD/CNY Case；默认 `full` 只选择当前内置 Bot 可运行的 23 个，两个来源专用
  Case 保留给显式 `custom`，所有 Trial 明确记录未经过 ACP/transport。
- 新增 7 Case 的 `agentstrata-qq-message-flow-v1`，用随机回环端口上的假 NapCat、真实
  QQ @ Relay、Evaluation-owned cc-connect 等价交接、one-shot attestation、ACP 准入、身份/权限、临时保护
  persona 状态和 ACP 回复投影验证仓库自有链路。
- Comparison、GAIA、BFCL、IFEval 和数据准备继续复用现有 Evaluation service 与 CLI；旧记录
  保持可读，但不再占据 Console 产品入口。

**结果与边界**

两条轨道分别给出模型能力和系统链路证据，失败归因不再混在一个总分中。QQ 轨道使用确定性
Agent sentinel 隔离模型波动，并在 receipt 中列出 `qq_platform/napcat/cc_connect/agent_model`
替代层；其通过不证明真实 QQ 或外部用户端到端，真实连通性仍由基础设施检查单独报告。

相关规格：
[`evaluation-two-track-center`](../specs/evaluation-two-track-center/spec.md)。

## 17. 工具注册统一与人格工具化：从多条装配路径到 Provider 快照

**暴露的问题**

静态 builtin、external、MCP、搜索、委托和会话临时工具通过不同列表或适配路径进入 Agent，
中央 catalog 还复制精确工具名，新增能力时容易出现运行面与 Console 漂移。人格修改则在主 Agent
前依赖 detector、命令 parser 和解释器分流；“你来模仿清宵，作为你的人格”这类明确自然语言也
可能在分流阶段失败，主 Agent 没有机会理解和调用能力。

**结构调整**

- `ToolDef` 统一持有完整输入/输出 JSON schema，handler 固定接受结构化参数与可信
  `ToolContext`，并统一返回 `ToolResult`。
- 领域模块通过显式 `ToolProvider` 声明 pack 与工具；中央 catalog 只定位 provider 模块，不再
  维护第二份工具名清单。静态和会话动态 provider 均进入同一个 `ToolRegistry`，再生成用于模型
  schema、执行索引、来源定位和 Console 投影的快照。
- 注册阶段拒绝 provider、pack、工具名冲突、非法 schema 和旧 handler 签名；没有加入目录扫描、
  decorator import 副作用、第三方 entry point、依赖图或通用插件生命周期。
- 人格能力迁为 `persona.control` pack 中的 Owner-only `persona_manage`。自然语言与 `/persona`
  原样进入主 Agent，由模型选择是否调用；执行端继续以可信 actor、role、chat、scope、原始请求、
  受保护提案和原子 mutation receipt 失败关闭。

**结果与边界**

Bot 配置可独立选择 tool pack，新增或定位工具只需沿 catalog → provider → handler 查找，Agent 与
Console 不再各自维护工具成员。人格意图不再被宿主启发式分类器提前拒绝，但模型是否选择工具仍是
模型行为，不能承诺确定性；只有结构化结果中的真实 `committed` receipt 可以证明已持久化。该改造
不提供任意第三方 Python 插件 ABI，也不把 persona mutation 暴露给 subagent。

相关规格：
[`unified-tool-registry`](../specs/unified-tool-registry/spec.md)、
[`persona-and-conversation-memory-authorization`](../specs/persona-and-conversation-memory-authorization/spec.md)。

## 18. QQ 公网图片直发：从模型两步推断到显式组合能力

**暴露的问题**

QQ adapter、OneBot 图片 sender、`download_image_urls` 和 `send_files_to_user` 已经存在，但主 Codex
回合仍可能只输出 Markdown 图片，随后错误判断当前会话没有图片发送能力。工具可见并不等于模型会
稳定选择跨工具编排，而 Markdown 链接也没有平台发送回执。

**结构调整**

- `workspace.read_write` 新增 `send_image_urls_to_user`，复用同一公网图片下载器和平台无关
  `file_sender`，在一次主 Agent 工具调用中完成下载、校验、批量发送和结构化回执。
- workspace capability policy 明确区分公网 URL 直发、已有工作区文件发送和 Markdown 展示；只有
  成功发送回执才能声称已发出，用户可见的发送工具继续对 subagent 隐藏。
- 公网下载只保留无需人工审批的最低运行时边界：固定已验证公网地址连接、逐跳重定向复检、数量与
  字节上限、图片签名/MIME、一致的工作区边界和安全化错误。部分 URL 失败时发送有效项，发送结果
  不确定时不自动重试。
- WSL 透明代理可能把公网域名解析到 RFC 2544 benchmark Fake-IP 网段；下载器不放行该保留网段，而是经
  固定公网地址访问公共 DoH，重新取得并校验真实公网地址后再固定连接，解析不可确认时仍失败关闭。

**结果与边界**

搜索得到直接图片 URL 后，主 Agent 有一个与用户意图一致的外发动作，不再依赖模型自行拼接两次
工具调用，也不会把 Markdown 图片误记为 QQ 图片消息。自动化测试使用 fake downloader、actor-bound
Session Gateway relay 和 fake OneBot，不调用商用模型、不向真实 QQ 写入，也不证明客户端实际显示。

相关规格：[`multimodal-image-io`](../specs/multimodal-image-io/spec.md)。

## 19. QQ 入站正文诊断：区分 transport 包装、可信正文与失败 intake

**暴露的问题**

cc-connect 在 `message.received` hook 写入原始正文摘要后，才向 ACP prompt 追加本地文件或图片
路径尾缀。ACP 若先校验摘要再去掉尾缀，会把合法附件消息误判为正文不匹配。另一方面，已通过
身份与准入的群任务为了避免历史内容泄漏而统一隐藏当前正文，导致 Console 无法诊断真实请求；
身份失败记录还可能从共享 session shell 带出上一轮 actor reference。

**结构调整**

- 在身份校验前只识别并剥离 cc-connect 完整末尾文件/图片协议段，把 basename 合并为结构化
  resource；canonical 剩余正文继续与 one-shot attestation 严格比对，静态群附件 inbox 仍不可信。
- 已准入群 task 的 summary/turn 只放行当前 canonical 正文，并继续经过大小限制、secret/path 与
  workspace identity 脱敏；model context、subagent 和 delegated-job 自由文本仍按原契约省略。
- `task-intake` 明确覆盖共享 session shell 的 actor 投影，身份失败 task 固定为“未验证来源”，
  workspace payload 不再生成 actor reference。

**结果与边界**

合法的 cc-connect 图片/文件包装不再制造身份失败，Console 可以查看已准入任务的当前群消息；
未通过身份或准入的消息仍只保存通用文本。测试模拟了 ACP 入站、attestation、准入与后续任务链，
没有发送真实 QQ 消息，也不把合成链路描述成 NapCat/cc-connect/商用模型端到端证明。

相关规格：
[`qq-group-shared-conversation-context`](../specs/qq-group-shared-conversation-context/spec.md)。

## 20. 引导式首次部署：从脚本清单到可恢复用户流程

**暴露的问题**

早期 Quick start 只创建开发 venv 并校验内置 BotSpec，却容易被新用户理解为已经部署；首次安装、
Console、实例更新和 QQ gateway 的命令又分散在多份文档中。高级内置机器人需要搜索、Codex、MCP
和多组凭据，不适合作为不懂代码用户的第一台机器人。部分部署入口还在统一实例更新后重复注册和
启动，Console provisioning 则使用固定 LLM 环境变量名，不能忠实反映 BotSpec。

**结构调整**

- 新增唯一终端入口 `deploy/wsl/quickstart.sh`，把主机预检、最小运行时、通用 QQ starter、
  隐藏式配置、NapCat 本地扫码、认证探针、单次实例更新和证据摘要串成可恢复状态机。
- CLI 与可选 Console 消费同一个 BotSpec-derived provisioning plan 和原子 env writer；秘密不进入
  argv、JSON、日志或 receipt，失败后从机器实际状态 `--resume`，不维护平行流程记录。
- Console 的 NapCat 登录入口按 Bot 的 `QQ_WEBUI_PORT` 直接打开无凭据的本机 `/webui`，
  登录状态检查复用同一实例端口；常态状态 API 和浏览器 URL 均不携带 WebUI token。
- 用户显式点击后，Console 可经 loopback-only、`no-store` 的 POST 接口读取现有 WebUI token
  并直接写入剪贴板；前端不显示或持久化 token，旧 token/session 路由继续保持移除。
- Docker 与系统包在精确变更预览后才允许安装；WSL systemd、docker group 和扫码等无法在当前
  进程安全完成的动作返回 `needs_user_action`，不使用提权或权限降级技巧绕过。
- 文档按用户状态收敛：README 只给推荐入口，部署文档只讲首次安装，运维手册只讲安装后操作，
  WSL README 只保留异常排障。高级 `lingye-copilot-qq` 不再作为新手默认实例。

**结果与边界**

新手路径只启用 Native 对话、workspace、memory 和基础附件，Console、搜索、MCP、Persona、Codex
和 code-worker 都是后续显式选择。自动化可以证明配置、编排、回环认证和本地 service 边界，默认
不会消耗模型额度或向 QQ 写入；真实扫码、新鲜主机 systemd 和独立账号 QQ 入站往返在未实际执行
时继续记录为 `not_tested`。

相关规格：[`guided-first-deployment`](../specs/guided-first-deployment/spec.md)。

## 21. QQ Gateway 收敛：从外部桥接链转向实例运行宿主

QQ 的旧运行路径把 NapCat、Relay、cc-connect 与 ACP 串成一条外部桥接链，导致平台连接、
实例生命周期、权限与证据边界分散。当前 BotSpec 改为显式 `gateway` 与 `channels.qq`，每个
systemd Bot unit 直接以前台 Python 运行唯一 Gateway，Gateway 连接用户独立维护的回环
NapCat/OneBot provider，并在进入 Agent 前完成身份、准入、权限审核和任务持久化。

QQ 推荐部署不再安装或启动 Node、cc-connect 和 Relay；Feishu legacy edge 保持隔离可选。
ACP 降为本地 Gateway client edge，不拥有 Channel 或 Agent runtime。Console 和 quickstart
分别按精确 MainPID、Gateway/OneBot evidence 报告状态，并继续把真实 QQ 入站、模型行为、
客户端展示和用户已读标记为尚未由本地检查证明的独立边界。

相关规格：[`gateway-acp-runtime-boundary`](../specs/gateway-acp-runtime-boundary/spec.md)。

## 当前架构的收敛结果

| 关注点 | 当前做法 |
| --- | --- |
| 实例差异 | 由 BotSpec 声明，不进入共享 Agent 分支 |
| 平台差异 | 由 adapter 实现，通过 registry 发现 |
| 跨层类型 | 由 `chatcopilot.contracts` 统一拥有 |
| 运行与控制面读取 | 通过 `core` 和 `component_catalog` 提供稳定入口 |
| 主 Agent | Native、LangGraph、Codex 共享 task/event/result、模型上下文快照与 turn lifecycle |
| 会话身份 | Gateway 从结构化 OneBot 事件绑定 conversation 与 actor；群共享普通数据但不共享执行权限 |
| 源码修改 | 主会话只读，异步 worker 隔离执行，验证后交付 Draft PR |
| 搜索 | 统一入口、直接 provider、统一 deadline/circuit/result policy |
| 工具注册 | catalog 只定位显式 provider，静态与会话动态工具统一进入 Registry 快照 |
| 评测 | Console 只展示直接 Agent 与 QQ 后链路两轨；Comparison/benchmark 保留 CLI，全部复用统一 Evaluation 生命周期 |
| 首次部署 | 单一终端向导生成 Gateway QQ starter；NapCat 外置，QQ 路径无 Node/cc-connect/Relay |
| 公开维护 | 公开仓库是源码、规格和发布流程的唯一事实源 |

进一步的组件关系、依赖方向和运行时细节分别见
[`architecture.md`](architecture.md)、[`runtime.md`](runtime.md) 和
[`bot-spec.md`](bot-spec.md)。
