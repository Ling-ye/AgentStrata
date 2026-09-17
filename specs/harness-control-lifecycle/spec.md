---
id: harness-control-lifecycle
type: architecture
status: implemented
created: 2026-09-17
---

# Harness 控制与 worker 生命周期

## Summary

Harness 的调度结果 unknown 不代表 worker 已停止。控制入口不能据此完成取消或释放任务占用，
查询也不能隐式写入中断状态。本次只整理任务控制闭环，不迁移数据库或扩展自动交付权限。

## Design

遵循[六层依赖基线](../domain-layered-dependencies/spec.md)与
[四层运行时基线](../runtime-four-layer-definition/spec.md)。Harness 是配套宿主，不增加消息运行层。
纯 Types 定义 worker 三态和调度结果；Service 通过端口决定生命周期状态；Repo 提供事务与条件更新；
Runtime 持有配置快照、systemd 与执行锁探测、冻结运行目录及进程启动；CLI/Console 只调用公开入口。

启动、取消、恢复、接续和核对共用每任务控制锁，并保留全局维护锁和 worker 执行锁。
只有无待执行 systemd job、执行锁空闲且进程已停止时，才能完成外部取消；未知状态保留取消请求与占用。
worker 收尾可以自行确认取消。迟到写入不能复活已取消任务，较早的探测不能覆盖新的完成结果。
测评与交付保留独立事实，外部取消失败或尚未结束时保留关联和重试资格。

get/list/progress 只读；装配负责存储初始化。现有每分钟定时入口先核对活动任务再处理待交付，
单项失败不阻止其他任务。公开 reconcile 与同名 CLI 命令用于主动核对；取消、恢复和接续即时核对。
现有 HTTP 路径与状态格式不变。异常退出在定时器正常工作时由下一轮核对反映。

现有架构检查器只为本次控制模块及直接 UI 入口增加依赖门禁，不宣称整个 Harness 已完成六层迁移。
不覆盖已冻结运行目录；部署切换通过现有 maintenance 入口确认执行空闲，不自动部署或迁移历史。

## Acceptance

- unknown 调度结果下取消不提前结束；活动或未知 worker 不允许恢复、接续或清理。
- Case 修复和代码治理识别同一取消状态；并发启动、取消、完成与恢复不重复执行或覆盖终态。
- 查询不探测进程或写生命周期状态；没有 Console 访问时后台核对仍可识别失联任务。
- 外部测评取消失败保留关联，交付状态不由本地 worker 状态推断。
- 控制 Service 不依赖具体运行实现，Types 保持纯契约，UI 不绕过公开入口操作存储。

## Verification

使用隔离存储、可控 worker 端口、进程锁及并发夹具验证状态机与故障路径；验证 CLI、Console、
定时入口及导入门禁。运行定向回归后执行 check_repo.py full。夹具不证明真实 systemd、模型或 GitHub 端到端行为。
