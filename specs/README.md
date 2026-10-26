# 规格索引

先读现行领域正文；这里只保留长期规则与不能从源码确认已完成的设计条件。

## Agent、外部桥接与持续改进主计划

[自研 Agent、外部桥接与持续改进](application-harness-framework/spec.md)定义定位、开源复用与公开契约。
[开发阶段](application-harness-framework/spec.md#分阶段开发)保留 M0/M1，新增独立 M1.1 双 runtime 收敛；
M2/M3 的桥接基线、评测与版本对照沿用现有接口；M4 复用 Harness 图完成需求候选，M5 提供版本启用与撤回；M6/M7 继续实现工作台与真实闭环。
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
