# 规格索引

先读现行领域正文；这里只保留长期规则与不能从源码确认已完成的设计条件。

## 长期基线

- [四层运行时职责](runtime-four-layer-definition/spec.md)
- [领域内部六层依赖](domain-layered-dependencies/spec.md)
- [工具按需披露](tool-progressive-disclosure/spec.md)
- [会话长期记忆条目与召回](conversation-memory-records/spec.md)
- [文档结构与治理](documentation-governance/spec.md)
- [Harness 控制与生命周期边界](harness-control-lifecycle/spec.md)

## 待确认的设计条件

- [隔离修复会话与宿主验收](harness-repair-v2/spec.md)：修复内核、独立沙箱和图片交付验证的实施与真实模型验收。
- [独立 Codex 认证通道](codex-independent-auth-lanes/spec.md)：实现已在代码中存在；独立通道授权和实际部署验收无法由静态源码确认，保留原 accepted 状态。
- [Canary 自更新与恢复](canary-self-update-e2e/spec.md)：受控端到端验收仍需独立运行，不把可用的测试骨架当作完整外部能力。

这些规格不作为已部署能力声明。更新时按 [维护工作流](../docs/maintenance.md#sdd-生命周期) 判断完成条件，再提炼或删除。
