# 架构总览

AgentStrata 是具备自我优化能力的开源 Agent 框架。项目自行定义运行、权限、上下文和工具契约，
以自研 Harness 与 Eval 支撑故障修复、代码熵治理和受限 Skill 学习。实例由 BotSpec 声明能力与运行包络。
本页用于定位职责与修改范围，具体规则由对应领域正文维护。

模块关系见首页的[框架总览图](../../README.md#框架总览)。该图展示运行与改进体系，
四层基线与各领域规格继续维护具体契约。

## 自我优化与模块分工

[Harness](harness.md)拥有来源冻结、角色分权、候选、返工与最终验收；[Eval](evaluation.md)拥有
题目、执行对象、生命周期、条件绑定、评分组织与结果证据。运行证据或治理发现形成候选目标，
候选经过对照验证与独立审核后交付，新版本由后续任务加载。当前宿主、评分与检查规则保持冻结。

## 执行路由、设计参考与当前实现

执行方式与模型在项目契约内分别选择。[Agent 路由](agent.md#一条执行契约)支持 Native、LangGraph
和 Codex，Codex 是其中一种执行路由；[模型配置](configuration.md#llm)管理连接、模型方案与用途绑定。
具体入口的执行支持不能互相推定：当前 Harness 默认装配 Codex 适配器，运行条件见[操作指南](../guides/harness.md)。

Harness 的状态、节点、子图与恢复设计参考 LangGraph；当前实现也实际使用 StateGraph 与 SQLite
checkpointer，详见[编排规格](../../specs/harness-langgraph/spec.md)。这些是具体机制的参考与实现依赖，
框架的运行职责、公共契约、修复策略和验收规则仍由项目定义。

DeepEval 与官方评分器在项目 Eval 生命周期内执行具体评分，评分组织和证据归属见[Evaluation](evaluation.md)。

## 两个同时生效的视角

| 视角 | 回答的问题 | 权威定义 |
| --- | --- | --- |
| Channel → Gateway → Application → Agent | 消息在哪一层处理、如何交接 | [四层运行时基线](../../specs/runtime-four-layer-definition/spec.md) |
| Types → Config → Repo → Service → Runtime → UI | 领域内部的源码允许依赖谁 | [六层依赖基线](../../specs/domain-layered-dependencies/spec.md) |

四层描述运行职责；六层按基础到上层排列，使用方依赖矩阵允许的基础层。实例宿主负责装配与生命周期，Harness、Evaluation 和 Console 位于消息链之外。

## 按修改目标进入

| 目标 | 领域正文 |
| --- | --- |
| 平台接入、消息运行和取消/交付 | [运行链](runtime.md) |
| 身份、准入、文件与群隔离 | [身份与资源](identity-resources.md) |
| 模型、Backend、提示词和子 Agent | [Agent](agent.md) |
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
- [Harness 默认装配](../../src/chatcopilot/harness/assembly.py)
