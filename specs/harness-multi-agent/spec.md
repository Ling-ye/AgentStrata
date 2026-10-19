---
id: harness-multi-agent
type: architecture
status: accepted
created: 2026-09-18
---

# 按需协作的 Harness 修复工作流

## Summary

修复按需使用 Plan、Coding、Test、Review 与 Main，角色共享运行器，宿主拥有最终验收与交付权。
现行行为由[Harness 正文](../../docs/reference/harness.md)维护；本 accepted 规格保留角色协作的验收条件，
不能仅凭文档整理或受控测试宣布真实模型对照验证完成。

## Design

分工以[角色契约](../../docs/reference/harness.md#按需角色协作)为准：首轮直接 Plan，Main 按需处理变化与分歧，
Coding 与 Test 分离产品和验证草案写权限，Review 独立只读。核心决定调度，角色不能嵌套委派或绕过验收。

来源冻结、三层产品回放、失败分类、预算与重诊断、SourceIndex/FailureBrief 及证据引用均以
[修复正文](../../docs/reference/harness.md)为唯一现行说明。治理复用同一闭环，额外要求由
[代码熵正文](../../docs/reference/code-health.md)维护。

设计遵循[LangGraph 编排](../harness-langgraph/spec.md)、[控制生命周期](../harness-control-lifecycle/spec.md)、
[四层运行时](../runtime-four-layer-definition/spec.md)与[六层依赖](../domain-layered-dependencies/spec.md)。
原始观测与人工材料不能提升为宿主规则；[交付](../../docs/reference/delivery.md)始终绑定独立验收的精确候选。

## Acceptance

- 正常路径不反复调用 Main；明确代码或测试错误只返工相关角色。
- Coding 不能写测试，Test 不能写产品；角色输出不能跳过宿主验证或发布。
- 先编码后补验证可完成，缺失、过期、跳过和错误分类不能制造成功。
- 重启不重复已确认角色，外部执行未知先对账；取消不能被迟到结果复活。
- 验收和交付绑定精确产物；主干更新需要重新验证与独立审核。
- 治理自主调查全仓，一个连贯主题一个 PR；不采用扫描切片或问题配额，原始测评与机器人数据不受影响。
- SourceIndex 与 FailureBrief 分别满足 16 KiB 和 8 KiB 上限；返修路由、失败分类与完整证据引用不因裁剪丢失。

## Verification

角色权限、调度、返工、引用完整性、来源、取消与交付定向测试；架构、SDD 与 full
检查；Console 测试、构建与界面检查。对固定失败样本在相同模型和预算下比较作者加审核
与新工作流，默认禁用发布，分别报告真实模型、GitHub 和 QQ 证据。
