# 架构总览

AgentStrata 是面向外部应用的可定制 Agent 运行系统，以自有四层架构承载应用接入、会话、上下文与 Agent 能力。
能力装配、上下文、工具使用及基于 LangGraph 的循环和协作策略由项目实现，都是 Harness 的主要优化对象。
Harness 根据需求开发适配、能力模块和运行策略，Eval 与独立验收验证改进；Codex 通过公开接口接入。
本页维护当前实现与目标架构的关系，具体运行规则由对应领域正文维护。

模块关系见首页的[框架总览图](../../README.md#框架总览)，具体实现与职责由本页及领域正文维护。
[Agent 运行系统与自有能力优化主计划](../../specs/application-harness-framework/spec.md)维护目标设计与阶段，
四层基线与各领域规格继续维护现行契约。

## 产品主体与执行边界

| 概念 | 当前含义 |
| --- | --- |
| Agent 运行系统 | Channel → Gateway → Application → Agent 四层整体，是唯一产品主体 |
| AgentRuntime | Agent 层的统一执行入口与共享能力装配，沿用现有类与执行契约，不代表四层整体 |
| 自有 LangGraph Agent | 项目编写的图、状态转换、上下文、工具与协作策略；LangGraph 库提供图执行基础设施 |
| Codex | 可选业务执行器及近期唯一 Harness 开发 worker，二者分别绑定模型和运行环境；未开放的内部行为不属于可承诺修改范围 |

LangGraph 是深度运行策略定制主线。优化先定位能力、上下文和应用策略问题，再按证据进入控制流程；
循环与委托始终是正式优化对象。平台协议和平台专用状态留在桥接/应用侧，Agent 核心消费通用契约。

## 当前实现与目标架构

| 领域 | 当前实现 | 目标扩展，由主计划分阶段落实 |
| --- | --- | --- |
| 自有 Agent | AgentRuntime、能力 provider、统一工具 Registry、PromptPlan、模型/工具图、TurnOps 与 LangGraph 子 Agent 均已有实现 | 按 [A—D 路线](../../specs/application-harness-framework/spec.md#自有-agent-能力优化路线)测量实际缺口，生成并正式验收自有实现候选 |
| 应用与配置 | M1 已分离应用描述和 Agent profile，复用 BotSpec 装配、实例状态及 LangGraph／Codex 执行契约 | 保持连接与 Agent 配置独立，按需扩展桥接信息 |
| 外部接入 | QQ 渠道、VTuber 独立客户端与[桥接基线](runtime.md#外部桥接基线)，沿用静态插件清单 | 沿现有协议接入能力，确需修改上游时使用受管 fork |
| Harness | LangGraph 编排修复、验收、批次和交付；接入与改进共享子图，Console 与 CLI 共用控制入口，Codex 执行开发角色 | 在冻结权限与验收规则下完成自有 Agent 的真实改进闭环 |
| Eval | 管理 Case、Target、Trial、条件、评分与证据；M3 提供[桥接 Suite 和版本对照](evaluation.md#agent-与外部桥接评测) | 按需求补充能力场景，真实外部交付与改进归因由 M7 验证 |
| 版本管理 | 已验收源码保存为独立版本，支持受管实例启用、实际身份核对和撤回；工作台展示操作与对照 | 对正式接受的自有 Agent 候选取得隔离实例启用/撤回及实际效果证据 |

配置拆分和显式迁移已实现，字段归属见[配置正文](configuration.md#配置归属与文件引用)。
改进工作台复用原需求与版本控制入口；仓库调查、适配开发与受管 fork 按具体授权范围展开。
各阶段完成后再更新相关领域正文、规格和操作指南。应用正常运行独立于 Harness 的优化任务。

## 自我优化与模块分工

[Harness](harness.md)拥有来源冻结、角色分权、候选、返工与最终验收；[Eval](evaluation.md)拥有
题目、执行对象、生命周期、条件绑定、评分组织与结果证据。运行证据或治理发现形成候选目标，
候选经过对照验证与独立审核后交付，新版本由后续任务加载。当前宿主、评分与检查规则保持冻结。

自有 Agent 代码、配置调整和桥接扩展分别评价。固定外部工具、任务、模型和环境后，才能将能力改善归因于
Agent 侧实现。桥接已有 `apply_pose` 案例不构成自有循环优化或完整闭环证据；具体完成条件只在主计划维护。

## 执行路由、设计参考与当前实现

执行方式与模型在项目契约内分别选择。[Agent 路由](agent.md#一条执行契约)支持 LangGraph
和 Codex，Codex 是其中一种执行路由；[模型配置](configuration.md#llm)管理连接、模型方案与用途绑定。
具体入口的执行支持不能互相推定：当前 Harness 开发 worker 只装配 Codex，近期沿用这一选择，运行条件见[操作指南](../guides/harness.md)。

Harness 直接使用 LangGraph 的 StateGraph、子图、SQLite checkpointer 和 interrupt／Command 管理执行与恢复，
详见[编排规格](../../specs/harness-langgraph/spec.md)。项目定义来源冻结、角色权限、候选、操作回执与接受条件。
检查点、外部操作是否发生及用户可见交付分别记录，不能互相替代。

后续扩展继续复用这些公共 API。Deep Agents／better-harness 等仅作为设计参考，尚未引入新执行体系；
固定源码 revision、版本差异与采用理由见[主计划复用清单](../../specs/application-harness-framework/spec.md#开源复用与版本依据)。

DeepEval 与官方评分器在项目 Eval 生命周期内执行具体评分，评分组织和证据归属见[Evaluation](evaluation.md)。

统一宿主负责身份、权限、工具、上下文接入和交付，一个回合由所选 runtime 执行。
Agent 的 LangGraph 图组织日常模型与工具回合；Harness 的 LangGraph 图组织开发和验收。
两类图复用同一库的基础设施，项目分别维护其设计，不意味着 Agent 图自动具备 Harness 的 checkpoint 恢复能力。
Codex 通过 app-server 管理自身循环、线程与压缩，开发 worker 与被优化实例独立。

Native 已退出有效执行路径，自有子 Agent 统一使用 LangGraph，共用会话状态和 TurnOps。
旧配置及其历史废弃，业务历史、人格、记忆和凭据保留，历史证据不重标；具体边界见[Agent](agent.md)。

## 两个同时生效的视角

| 视角 | 回答的问题 | 权威定义 |
| --- | --- | --- |
| Channel → Gateway → Application → Agent | 消息在哪一层处理、如何交接 | [四层运行时基线](../../specs/runtime-four-layer-definition/spec.md) |
| Types → Config → Repo → Service → Runtime → UI | 领域内部的源码允许依赖谁 | [六层依赖基线](../../specs/domain-layered-dependencies/spec.md) |

四层描述运行职责；六层按基础到上层排列，使用方依赖矩阵允许的基础层。实例宿主负责装配与生命周期，Harness、Evaluation 和 Console 位于消息链之外。

## 按修改目标进入

| 目标 | 领域正文 |
| --- | --- |
| 产品定义、应用接入与自有能力优化阶段 | [主计划](../../specs/application-harness-framework/spec.md) |
| 现有独立客户端与软件插件 | [运行链](runtime.md#独立交互客户端)、[插件管理](runtime.md#外部软件插件管理) |
| 平台接入、消息运行和取消/交付 | [运行链](runtime.md) |
| 身份、准入、文件与群隔离 | [身份与资源](identity-resources.md) |
| 自有 Agent 实现、Runtime、图与子 Agent | [Agent](agent.md#自有实现与优化边界) |
| 记忆、人格、搜索与按需资料 | [上下文](context.md) |
| 注册、权限和外部能力 | [工具](tools.md) |
| BotSpec、环境和配置投影 | [配置](configuration.md) |
| Case、评分、监督与结果 | [Evaluation](evaluation.md) |
| 修复与 PR 交付 | [Harness](harness.md) |
| 私有配置、过程展示和前端 | [Console](console.md) |

## 为什么按领域组织

运行方向不等于 import 方向；回调和端口连接职责，不让底层反向导入宿主。声明、运行状态、执行结果与投递回执各自有权威来源，不能用一项替代另一项。领域细则只在对应正文维护。

## 源码入口

- [scripts/check_architecture.py](../../scripts/check_architecture.py)
- [src/chatcopilot/contracts](../../src/chatcopilot/contracts)
- [src/chatcopilot/gateway/runtime.py](../../src/chatcopilot/gateway/runtime.py)
- [执行路由注册](../../src/chatcopilot/agent/runtimes/registry.py)、[模型路由契约](../../src/chatcopilot/contracts/model_runtime.py)
- [AgentRuntime](../../src/chatcopilot/agent/runtime.py)、[能力装配](../../src/chatcopilot/agent/capabilities/assembly.py)、[自有 LangGraph 图](../../src/chatcopilot/agent/langgraph_session.py)、[TurnOps](../../src/chatcopilot/agent/turn.py)
- [Harness 默认装配](../../src/chatcopilot/harness/assembly.py)
- [图执行](../../src/chatcopilot/harness/graph_service.py)、[官方检查点装配](../../src/chatcopilot/harness/graph_repository.py)
- [插件清单](../../src/chatcopilot/plugins/catalog.py)、[插件管理](../../src/chatcopilot/plugins/service.py)
- [应用与 Agent 声明](../../src/chatcopilot/contracts/application.py)、[公共配置解析](../../src/chatcopilot/core/application_configuration.py)
