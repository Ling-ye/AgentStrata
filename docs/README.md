# 文档导航

AgentStrata 是面向外部应用的可定制 Agent 运行系统。自有四层架构、Agent 能力和 LangGraph 执行策略构成产品主体，Harness 与 Eval 支撑持续改进，Codex 作为可选业务执行器和当前开发 worker 接入。
初次了解项目先看[首页](../README.md)，理解模块关系看[框架示意图](../README.md#框架总览)，技术约束与源码入口见[架构总览](reference/architecture.md)。

本页供使用者与开发者选择操作路径；AI 协作者从 [AGENTS](../AGENTS.md)按任务逐层读取。
guides 解释如何操作，reference 维护当前机制与修改边界，specs 保留长期约束及待验收条件。

## 参与社区贡献

- [首次 PR、认领与审查](../CONTRIBUTING.md) / [English contribution guide](guides/contributing-en.md)
- [按贡献类型准备环境](guides/development.md#按贡献类型开始)
- [新手任务](https://github.com/Ling-ye/AgentStrata/issues?q=is%3Aissue%20is%3Aopen%20label%3A%22good%20first%20issue%22%20no%3Aassignee%20-linked%3Apr) / [接入与使用交流](https://github.com/Ling-ye/AgentStrata/discussions/categories/q-a)
- [作品展示](https://github.com/Ling-ye/AgentStrata/discussions/categories/show-and-tell) / [项目动态](https://github.com/Ling-ye/AgentStrata/discussions/categories/announcements)
- [任务整理与社区维护](guides/community.md)

## 理解新架构与按阶段开发

[Agent 运行系统与自有能力优化主计划](../specs/application-harness-framework/spec.md)是唯一实施计划，
包含固定版本的开源源码参考、最小公开契约和 [M0、M1、M1.1 与 M2—M7](../specs/application-harness-framework/spec.md#分阶段开发)的前置条件与验收标准。
M1 已提供独立应用与 Agent 配置，M1.1 收敛为 LangGraph/Codex；M2 的[桥接基线](reference/runtime.md#外部桥接基线)统一说明现有 Live2D 与 QQ/NapCat 的配置和证据边界。
M3 提供[Agent 与桥接评测](reference/evaluation.md#agent-与外部桥接评测)和版本对照；
M4 提供[需求任务](guides/harness.md#提交接入或改进需求)，M5 提供[已验收版本启用与撤回](guides/instances.md#启用与撤回已验收版本)；
M6 提供[Console 改进工作台](guides/harness.md#通过-console-提交需求)；真实闭环按主计划独立验收，受控验证不代替真实交付。
后续沿 [A—D 路线](../specs/application-harness-framework/spec.md#自有-agent-能力优化路线)建立自有 Agent 基线，完成能力与上下文案例，再收口真实 Harness 闭环；控制流程存在实际缺口时深入循环与协作策略。
参与开发时先选择阶段，再读取相关领域，不把设计目标当作已实现能力。

## 选择使用路径

| 目标 | 下一步 |
| --- | --- |
| 理解目标架构与现有差距 | [主计划](../specs/application-harness-framework/spec.md)、[当前架构](reference/architecture.md) |
| 按阶段开发应用接入与优化能力 | [阶段范围与完成条件](../specs/application-harness-framework/spec.md#分阶段开发) |
| 定制自有 Agent 能力、上下文或 LangGraph 策略 | [自有实现入口](reference/agent.md#自有实现与优化边界)、[能力优化路线](../specs/application-harness-framework/spec.md#自有-agent-能力优化路线) |
| 验证自有 Agent 改进及归因 | [自有 Agent 能力验收](reference/evaluation.md#自有-agent-能力验收) |
| 配置独立 Agent 或迁移旧内联文件 | [配置归属](reference/configuration.md#配置归属与文件引用)、[迁移步骤](guides/instances.md#迁移内联-agent-配置) |
| 准备源码开发环境 | [开发指南](guides/development.md) |
| 评测 Agent 或比较改动 | [Evaluation 指南](guides/evaluation.md) |
| 修复失败与启动自身治理 | [Harness 指南](guides/harness.md) |
| 核对 Live2D 与 QQ 桥接的配置、权限和回执 | [外部桥接基线](reference/runtime.md#外部桥接基线) |
| 建立桥接评测基线、对照代码候选 | [桥接测评](guides/evaluation.md#live2d-与-qq-桥接基线) / [版本对照](guides/evaluation.md#代码基线与候选对照) |
| 运行机器人应用 | [首次部署](guides/deployment.md) / [实例运维](guides/instances.md) |
| 使用已有 Live2D 客户端或生成器 | [Live2D 指南](guides/live2d.md) |

## Harness 与 Evaluation

| 想做什么 | 操作入口 | 需要了解机制时 |
| --- | --- | --- |
| 运行评测、比较改动效果 | [Evaluation 指南](guides/evaluation.md) | [评分与证据](reference/evaluation.md) / [服务协议](reference/evaluation-service.md) |
| 修复失败 Case 或机器人任务 | [Harness 指南](guides/harness.md#发起任务) | [冻结来源与独立验收](reference/harness.md) |
| 查看候选、失败原因与执行记录 | [结果与证据](guides/harness.md#查看修复结果与证据) | [流程记录](reference/harness.md#修复流程记录) / [观测](reference/observability.md) |
| 治理代码熵或改进 Harness | [治理与定时](guides/harness.md#代码熵回收与可选定时) | [代码熵与 Skill 学习](reference/code-health.md) / [黄金原则](reference/harness-principles.md) |
| 处理交付、取消或恢复 | [异常处理](guides/harness.md#处理失败取消与恢复) / [交付配置](guides/harness.md#harness-pr-交付配置与恢复) | [生命周期](reference/harness.md#控制与生命周期) / [交付契约](reference/delivery.md) |
| 修改修复核心与执行适配 | [Harness 职责](reference/harness.md#核心项目集成与-eval) | [Harness 编排架构](../specs/harness-langgraph/spec.md) / [编译图](../specs/harness-langgraph/graphs.md) |

## 其他开发与运维任务

| 本次任务 | 先读 | 需要机制与边界时 |
| --- | --- | --- |
| 准备开发环境、选择检查 | [开发](guides/development.md) | [架构总览](reference/architecture.md) |
| 首次部署或管理实例 | [首次部署](guides/deployment.md) / [实例运维](guides/instances.md) | [配置](reference/configuration.md) |
| QQ 登录、认证或外部服务 | [QQ](guides/qq.md) / [服务](guides/services.md) | [身份与资源](reference/identity-resources.md) / [服务边界](reference/services.md) |
| 配置机器人定时调查与群推送 | [定时任务](reference/schedules.md) | [运行链](reference/runtime.md) / [Console](reference/console.md) |
| 修改消息或平台接入 | [运行链](reference/runtime.md) | [身份与资源](reference/identity-resources.md) |
| 角色库、完整备份与私有预设分发 | [角色预设](guides/character-presets.md) | [角色库契约](../specs/character-presets/spec.md) |
| 生成角色与接入 Live2D 语音客户端 | [Live2D 接入](guides/live2d.md) | [独立客户端边界](reference/runtime.md#独立交互客户端) |
| 修改 Agent、提示词、记忆 | [Agent](reference/agent.md) | [上下文](reference/context.md) |
| 选择执行或模型路由 | [执行契约](reference/agent.md#一条执行契约) | [模型配置](reference/configuration.md#llm) / [路由规格](../specs/agent-runtime-routing/spec.md) |
| 新增或修改工具 | [工具](reference/tools.md) | [配置](reference/configuration.md) |
| 编写题目和验收样本 | [用例指南](guides/evaluation-cases.md) | [题目约束](reference/evaluation-cases.md) / [术语](reference/evaluation-glossary.md) |
| 修改 Console | [Console](reference/console.md) | [观测](reference/observability.md) / [归档](reference/local-traces.md) |
| 排查运行失败 | [诊断](guides/debugging.md) | [WSL 恢复](guides/wsl-troubleshooting.md) |
| 维护文档或准备发布 | [文档维护](maintenance.md) / [发布](guides/releasing.md) | [公开边界](reference/publication.md) / [规格](../specs/README.md) |

## 模块入口

- [Bot 模板](../bots/_template/README.md)
- [Docker 服务](../deploy/docker/README.md) / [跨平台部署公共脚本](../deploy/common/README.md)
- [当前 Agent 题库](../src/chatcopilot/evals/suites/agentstrata-agent-tasks-v1/README.md)
- [历史工程夹具说明](../src/chatcopilot/evals/suites/agentstrata-capabilities-v1/README.md)、[QQ 合成测试](../src/chatcopilot/evals/suites/agentstrata-qq-message-flow-v1/README.md)、[业务样例](../src/chatcopilot/evals/suites/project-business-v1/README.md)

机器人 Prompt、Skill、数据和第三方许可由各自运行或许可契约管理，不能为了文档精简改变它们的行为。
