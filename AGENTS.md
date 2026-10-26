# AGENTS.md — AgentStrata

本页是仓库 AI 协作者的入口。先确认当前任务，再按下表进入相关领域章节。默认使用 WSL/Linux。
面向人类的项目介绍见 [README](README.md)，操作与开发导航见[文档中心](docs/README.md)。

## 项目定位

AgentStrata 让自研 Agent 连接外部应用，并以 Harness 与 Eval 驱动持续改进。
Live2D、QQ/NapCat 等通过 Channel、Gateway 客户端、工具接口或 MCP 接入；应用日常运行与改进任务各自拥有生命周期。

统一宿主管理身份、权限、上下文、工具、会话与交付。LangGraph 与 Codex 按实例配置选择，
一次正常回合由一个 runtime 执行。Harness 的开发 worker 与被优化应用分别绑定模型。
Harness 编排直接复用 LangGraph；项目定义来源冻结、角色权限、候选、证据、独立验收及版本启用规则。
Eval 负责评测条件、执行与评分，候选接受、源码交付和运行版本分别记录。

改进对象包括 Agent 执行逻辑、提示词、上下文、工具、Skill 与桥接。
接入优先复用协议和成熟组件，受管源码、fork 与构建按实际需求展开。
项目定义与长期设计见[主计划](specs/application-harness-framework/spec.md)，本次范围以用户任务为准，事实沿源码与领域正文核对。

应用 `bot.yaml` 引用独立 Agent profile，运行层复用原类型化装配结果；旧内联配置须显式迁移，
不在加载时回退或改写文件。字段归属与原有共享引用的含义见[配置](docs/reference/configuration.md#配置归属与文件引用)。

执行方式与模型按配置路由，框架能力由公共契约定义；某个执行入口的默认适配不代表框架唯一执行方式。
项目定位、执行路由、设计参考与实际依赖分别说明，具体事实以[架构总览](docs/reference/architecture.md)及领域正文为准。
需求改进、故障修复、代码熵治理和受限 Skill 学习共用候选与证据边界；当前任务使用冻结规则，有效改动由后续任务加载。
Native 独立执行路径已退役，旧配置按运行时正文处理；业务历史、人格、记忆与凭据保留。

## 协作与授权

- 目标、范围和权限清楚时自主完成，避免过度设计和无关改动；结论以可复核证据为准。
- 修改前核对分支、工作区、暂存区和 worktree，保护用户已有改动。
- 交互式 AI 未获逐项明确授权不 add、commit、amend、push、PR、merge、rebase、tag 或发布。
  受控 worker 的例外见 [交付契约](docs/reference/delivery.md)，不授权交互式 AI 提交。
- 架构、权限、费用、公开契约和不可逆变更按需检查具体失败路径；兼容影响不明时先说明。
- 实际越过秘密、权限或数据完整性边界时立即披露。私有值放被忽略的环境文件，不写入源码、文档和公开日志。

## 必须保留的边界

- [四层运行时](specs/runtime-four-layer-definition/spec.md) 与 [六层源码依赖](specs/domain-layered-dependencies/spec.md) 同时生效。
- 身份、准入和权限先于模型、工具、附件和持久化副作用。Owner/member 由宿主采信，不从文本推断。
- PromptPlanBuilder 保持四个信任分区；按需资料不能提升为宿主规则。工具发现与执行复用同一 Registry 和 provider catalog。
- 执行结果、provider acknowledgement 和用户可见交付分别记录；静态、mock 和局部检查不等于真实模型或 QQ 端到端证据。

## 按任务渐进读取

1. 先从下面的任务选择首读章节，读到足以确定职责和修改边界。
2. 仅在涉及相邻领域或需要解释具体行为时展开对应正文；沿领域页的源码入口核对实现、配置与测试。
3. 架构、公共契约、部署或迁移先引用或创建 SDD；普通修复不需要通读历史规格。
4. 相关事实与验证方法明确后开始工作，跨领域任务同时保留关联边界，不批量加载整个文档树。

| 当前任务 | 首读 | 何时继续展开 |
| --- | --- | --- |
| 分阶段开发、双 runtime、外部桥接与需求改进 | [主计划与阶段](specs/application-harness-framework/spec.md#分阶段开发) | 先确认本阶段及前置条件，再进入相关领域；公开契约、复用源码与迁移方案见同一主计划 |
| 外部应用插件、适配器与 Live2D | [桥接基线](docs/reference/runtime.md#外部桥接基线)、[独立客户端](docs/reference/runtime.md#独立交互客户端)、[软件插件](docs/reference/runtime.md#外部软件插件管理) | 当前操作读 [Live2D 指南](docs/guides/live2d.md)；fork、自动接入与版本启用按主计划推进 |
| Harness 修复、验收与返工 | [角色](docs/reference/harness.md#按需角色协作)、[验收](docs/reference/harness.md#对照验证与独立验收) | 执行器或恢复问题读同页相应章节；图设计变化读 [Harness 编排规格](specs/harness-langgraph/spec.md) |
| Eval、评分、Case 或对照结果 | [Evaluation](docs/reference/evaluation.md) | 执行归属与生命周期读[服务协议](docs/reference/evaluation-service.md)；题目变化读[用例约束](docs/reference/evaluation-cases.md) |
| 自身治理、Skill 学习或交付 | [代码熵](docs/reference/code-health.md) | 检查规则读[黄金原则](docs/reference/harness-principles.md)；远端操作与清理读[交付契约](docs/reference/delivery.md) |
| 消息、身份、准入和资源 | [运行链](docs/reference/runtime.md) | 权限与文件边界读[身份与资源](docs/reference/identity-resources.md) |
| Agent、PromptPlan、上下文或工具 | [Agent](docs/reference/agent.md) | 上下文与记忆读[上下文](docs/reference/context.md)；工具发现执行读[工具](docs/reference/tools.md) |
| 执行或模型路由 | [执行契约](docs/reference/agent.md#一条执行契约)、[模型配置](docs/reference/configuration.md#llm) | 路由机制变化读 [runtime 路由规格](specs/agent-runtime-routing/spec.md)；修复 worker 另读 [Harness 执行适配](docs/reference/harness.md#执行适配与隔离) |
| 模型、BotSpec 或部署 | [配置](docs/reference/configuration.md)中的相关字段 | 操作读对应指南；公开或部署边界读[公开契约](docs/reference/publication.md) |
| 已验收版本启用、撤回或恢复 | [实例版本契约](docs/reference/delivery.md#已验收版本启用与撤回) | 操作读[启用与撤回](docs/guides/instances.md#启用与撤回已验收版本)；先核对任务回执、目标实例和原切换记录 |
| Console、证据与观测 | [Console](docs/reference/console.md) | 数据采集与结果语义读[观测](docs/reference/observability.md) |
| 文档与项目介绍 | [文档维护](docs/maintenance.md) | 导航与唯一正文规则读[文档治理基线](specs/documentation-governance/spec.md) |
| 开发验证或故障诊断 | [检查范围](docs/guides/development.md#选择检查范围) | 运行失败读[诊断](docs/guides/debugging.md) |

人和 AI 共用 reference 的当前事实，不创建平行的 AI 契约副本；运行日志和单次测试结果留在交付或 CI。

## 完成与维护

普通说明使用 docs 检查；代码修改按 [开发指南](docs/guides/development.md#选择检查范围) 执行定向及 fast/full 验证。检查通过后停止，不为形式反复运行。

报告实际结果、跳过项和剩余风险；运行流水放在交付或 CI，不追加到文档。文档改动与代码联动，现行事实只维护一处；旧内容提炼后删除，无法确认则报告待判断。

完整任务导航见 [文档中心](docs/README.md)。
