---
id: harness-multi-agent
type: architecture
status: accepted
created: 2026-09-18
---

# 按需协作的 Harness 修复工作流

## Summary

Harness 只接受测评 Case 与机器人任务。移除主动巡检，保留黄金原则和固定验收。
Main、Plan、Coding、Test、Review 共用运行器，按需协作；只有宿主能验收与交付。
本规格取代隔离修复 v2 的作者职责分配，保留其隔离、取消、证据和 PR 交付边界。

## Design

遵循[四层运行时](../runtime-four-layer-definition/spec.md)、
[六层依赖](../domain-layered-dependencies/spec.md)和
[生命周期契约](../harness-control-lifecycle/spec.md)。Harness 是消息链外的独立 worker。
Types 定义角色和交接；Repo 保存不可变产物；Service 决定调度和验收；Runtime 注入
角色、工作区、验证与交付端口；Console 只使用公开入口。来源、执行适配不反向决定权限。

Main 只在开始、根因变化或分歧时调度；Plan 调查根因与最小改动；Coding 只写产品；
Test 只写测试草案；Review 独立只读。每任务一套工作区与依赖环境，各角色独立会话。
现成 Case 可复用，允许先形成候选后补验证。宿主执行相同冻结测试验证基线和候选，
环境、评分、证据故障不算产品失败；缺口候选不可发布。只有独立审核通过且验收完整
才能生成绑定候选、目标、测试与证据的 AcceptedCandidate。PR 交付继续遵循
[交付契约](../../docs/reference/delivery.md)，模型没有 Git 写权限或发布凭据。

任务预算默认三轮、累计 3600 秒，所有角色返工共用轮次。普通步骤由宿主推进，
角色不能创建嵌套委派或自己执行无界返工。连续两轮无新增有效证据时停止。
交接使用摘要和不可变产物引用，完整正文按需读取；任务不复制所有角色历史。

原始观测、人工线索和预期分别冻结。测评禁止人工答案；机器人反馈可空。黄金原则
以[统一索引](../../docs/reference/harness-principles.md)及冻结源码为准，不提升不可信材料。
新协议只运行新任务；旧数据在维护锁内归档后切换，不迁移、不续跑。

## Acceptance

- 正常路径不反复调用 Main；明确代码或测试错误只返工相关角色。
- Coding 不能写测试，Test 不能写产品；角色输出不能跳过宿主验证或发布。
- 先编码后补验证可完成，缺失、过期、跳过和错误分类不能制造成功。
- 重启不重复已确认角色，外部执行未知先对账；取消不能被迟到结果复活。
- 验收和交付绑定精确产物；主干更新需要重新验证与独立审核。
- 主动巡检入口、调度、预算和页面移除，原始测评与机器人数据不受影响。

## Verification

角色权限、调度、返工、引用完整性、来源、取消与交付定向测试；架构、SDD 与 full
检查；Console 测试、构建与界面检查。对固定失败样本在相同模型和预算下比较作者加审核
与新工作流，默认禁用发布，分别报告真实模型、GitHub 和 QQ 证据。
