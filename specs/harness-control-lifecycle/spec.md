---
id: harness-control-lifecycle
type: architecture
status: implemented
created: 2026-09-17
---

# Harness 控制与 worker 生命周期

## Summary

worker 的 unknown 状态不能当作停止，查询不能隐式推进生命周期。本规格保留并发控制与事实归属的
长期约束；操作和当前字段语义见[Harness 生命周期](../../docs/reference/harness.md#控制与生命周期)。

## Design

遵循[六层依赖](../domain-layered-dependencies/spec.md)与[四层运行时](../runtime-four-layer-definition/spec.md)。
Types 定义 worker 三态和调度结果；Service 通过端口决定控制动作；Repo 提供事务条件更新；
Runtime 持有配置、systemd、进程锁和冻结目录；CLI/Console 只使用公开入口。

启动、取消、恢复、接续与核对必须共享任务控制锁，并保留维护锁与 worker 执行锁。
锁顺序与字段归属由[当前控制协议](../../docs/reference/harness.md#控制与生命周期)维护。
控制锁串行化资格判断，执行锁证明真实执行占用，维护锁阻止活动执行期间切换数据。
不能因探测短暂持锁就拒绝正常 worker，也不能持控制锁等待 worker 完成初始化。

只有无待执行 systemd job、执行锁空闲且进程停止时，外部控制才可完成取消；worker 也可自行确认收尾。
事务条件更新防止旧探测覆盖新结果，以及迟到结果复活已取消任务。外部测评取消请求与执行终态分开确认。

修复与交付复验各自拥有测评引用、取消意图与结论。混合验证按实际 Agent 测评 ID 取消，纯本地验证
不发送外部请求。占用、恢复、接续及清理同时检查两类引用，不从本地 worker 状态推断外部执行结束。

查询与核对分开：get/list/progress 只读，装配负责存储初始化；后台对账与显式 reconcile 读取真实执行事实。
单项对账错误不能中止整个调度批次。HTTP 路径与业务语义保持稳定，初始化失败只隔离 Harness 入口。

执行位置由[LangGraph](../harness-langgraph/spec.md)管理；本控制层负责外部事实与操作资格。
不覆盖已冻结运行目录，协议切换与资源保留遵循[维护契约](../../docs/reference/harness.md#协议切换)。

## Acceptance

- unknown 调度结果下取消不提前结束；活动或未知 worker 不允许恢复、接续或清理。
- 两类修复来源识别同一取消状态；并发启动、取消、完成与恢复不重复执行或覆盖终态。
- 查询不探测进程或写生命周期状态；没有 Console 访问时后台核对仍可识别失联任务。
- 外部测评取消失败保留关联，交付状态不由本地 worker 状态推断。
- 控制 Service 不依赖具体运行实现，Types 保持纯契约，UI 不绕过公开入口操作存储。
- 探测与 worker 启动交错时正常执行者不会退出；两个实际 worker 仍只能有一个取得执行资格。
- 交付复测期间存储故障后取消不丢失测评引用；blocked 交付在无需 Console 访问的情况下完成取消。

## Verification

使用隔离存储、可控 worker 端口、进程锁及并发夹具验证状态机与故障路径；验证 CLI、Console、
定时入口及导入门禁。运行定向回归后执行 check_repo.py full。夹具不证明真实 systemd、模型或 GitHub 端到端行为。
