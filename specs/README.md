# 规格索引

先读现行领域正文；这里只保留长期规则与不能从源码确认已完成的设计条件。

## Agent 运行系统与自有能力优化主计划

[可定制 Agent 运行系统与自有能力优化](application-harness-framework/spec.md)定义四层产品主体、自有 LangGraph Agent、可选 Codex 执行器与开发 worker 的边界，并维护开源复用与公开契约。
[开发阶段](application-harness-framework/spec.md#分阶段开发)保留 M0—M6 的完成事实；后续 [A—D 路线](application-harness-framework/spec.md#自有-agent-能力优化路线)推进能力基线、能力与上下文案例、真实闭环以及按需求深入循环策略。
M7 尚需 Harness 实际生成并正式接受自有 Agent 实现候选，以及隔离实例启用/撤回证据；已有 `apply_pose` 桥接案例不能替代这一条件。
`accepted` 表示设计可实施；阶段状态与未完成条件只在主计划维护，fork 不作为通用前置条件。

## 长期基线

- [四层运行时职责](runtime-four-layer-definition/spec.md)
- [领域内部六层依赖](domain-layered-dependencies/spec.md)
- [工具按需披露](tool-progressive-disclosure/spec.md)
- [会话长期记忆条目与召回](conversation-memory-records/spec.md)
- [文档结构与治理](documentation-governance/spec.md)
- [Harness LangGraph 编排](harness-langgraph/spec.md) / [编译图](harness-langgraph/graphs.md)
- [Harness 控制与生命周期边界](harness-control-lifecycle/spec.md)
- [许可、署名与对应源码](agpl-license/spec.md)
- [持续集成与本地验证](ci-validation/spec.md)

## 待确认的设计条件

- [独立 Live2D 生成插件](live2d-generation/spec.md)：多模态输入、模型资产交付及独立于 AgentRuntime 的生成流程。
- [外部软件插件与受管部署](external-software-plugins/spec.md)：固定版本自动安装、独立发行与部署验收。

- [独立交互客户端与角色适配](interactive-client/spec.md)：独立客户端宿主及可选 Live2D Bridge；真实语音与角色交互需独立验收。

- [Harness 多角色协作](harness-multi-agent/spec.md)：保留固定失败样本、相同模型与预算下的工作流对照验收条件。
- [代码熵批次](harness-code-health/spec.md)：保留连续治理与真实远端交付、失败收尾的独立验收条件。
- [Harness Skill 学习](harness-skill-learning/spec.md)：保留真实 worker 的 Skill 发现、显式调用及学习交付验收条件。

- [微信 ClawBot 渠道](weixin-clawbot-channel/spec.md)：代码与本地验证已完成；真实微信扫码和媒体往返需独立验收。

- [独立 Codex 认证通道](codex-independent-auth-lanes/spec.md)：实现已在代码中存在；独立通道授权和实际部署验收无法由静态源码确认，保留原 accepted 状态。
- [Canary 自更新与恢复](canary-self-update-e2e/spec.md)：受控端到端验收仍需独立运行，不把可用的测试骨架当作完整外部能力。

Harness 的真实模型、真实 GitHub 交付、QQ 送达与运行库切换分别验证；某条隔离修复链通过，
不能替代其他场景验收。运行结果留在交付或 CI，不在规格中累计测试计数。

这些规格不作为已部署能力声明。更新时按 [维护工作流](../docs/maintenance.md#sdd-生命周期) 判断完成条件，再提炼或删除。
