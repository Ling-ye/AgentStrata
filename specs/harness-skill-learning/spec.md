---
id: harness-skill-learning
type: architecture
status: accepted
created: 2026-09-25
---

# Code Health 中的 Harness Skill

## Summary

Harness Skill 将已合入治理证据中的可复用方法提供给后续任务，不参与权限、评分和验收决策。
当前加载、学习、预算与写入范围由[代码熵正文](../../docs/reference/code-health.md#code-health-skill)维护。
本 accepted 规格保留真实 worker 中 Skill 发现、显式调用及学习交付的验收条件。

## Design

选题与经验加载分开：初始 Plan 自主调查，只有已冻结目标涉及 Harness 源码时才向后续角色提供冻结 Skill。
当前候选不能改写当前任务使用的经验或验收规则。

学习必须从已合入的治理证据提炼；没有可推广方法时不制造修改。有新方法时使用独立受限子任务、审核及
既有 PR 交付，失败不撤销产品修复。新版本只在后续任务从主干加载，不热替换当前宿主材料。
路径、摘要、收录及预算规则见[现行契约](../../docs/reference/code-health.md#code-health-skill)。
不改变 BotSpec Skill、黄金原则、测试、权限、治理选题与发布授权。

## Acceptance

- 目标内的后续角色收到真实冻结正文及摘要，非目标任务和初始 Plan 不加载。
- 当前候选不能修改 Skill；Skill 正文不能改变宿主验收或交付。
- 教训提炼只使用已合入 Code Health 证据；无新教训无变更，重试不重复 PR。
- Skill 更新仅在新主干启动的任务生效。

## Verification

真实 worker 与嵌套沙箱中的 Codex Skill 发现及显式调用须单独验证，不能用已有角色接线证明完成。
已合入治理证据到受限学习任务及交付的链路同样需要独立证据。
定向测试覆盖冻结、触发、权限、幂等、取消和学习失败；之后运行项目规定的 full 检查。
