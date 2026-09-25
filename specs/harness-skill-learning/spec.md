---
id: harness-skill-learning
type: architecture
status: accepted
created: 2026-09-25
---

# Code Health 中的 Harness Skill

## Summary

仅当 Code Health 已选定 Harness 源码为本项治理目标时，后续角色读取冻结主干中的
`harness-code-health` Skill。它提供流程经验，不参与权限、评分和验收决策。

## Design

Skill 存放在仓库 `.agents/skills/harness-code-health/`，禁止隐式触发。宿主从任务冻结源码
保存内容和 SHA-256，并在相关角色输入中按需提供正文及加载回执；Plan 首次全仓发现不加载。
普通候选无权修改 Skill。同一任务始终使用其冻结版本，候选文件不能覆盖它。

仅在 Harness 相关的 Code Health 任务经完整验收且 PR 合入后，宿主检查最终 finding 与审核
证据是否形成可复用的流程教训。无教训记录 `no_change`。有教训时启动独立、幂等的受限
学习任务，只修改该 Skill 的参考文件，经独立审核、相关检查和现有 PR 交付契约合入。
学习子任务最多一次修订、累计执行 1800 秒，计入批次执行时长但不计入问题发现数。
学习失败保留记录，不撤销已合并的产品修复；新任务只从新主干读取新 Skill。

不改变机器人 BotSpec Skill、黄金原则、测试、权限、代码治理选题及发布授权。

## Acceptance

- 目标内的后续角色收到真实冻结正文及摘要，非目标任务和初始 Plan 不加载。
- 当前候选不能修改 Skill；Skill 正文不能改变宿主验收或交付。
- 教训提炼只使用已合入 Code Health 证据；无新教训无变更，重试不重复 PR。
- Skill 更新仅在新主干启动的任务生效。

## Verification

先以实际 worker 与嵌套沙箱验证 Codex Skill 发现及显式调用，再启用角色接线。
定向测试覆盖冻结、触发、权限、幂等、取消和学习失败；之后运行项目规定的 full 检查。
