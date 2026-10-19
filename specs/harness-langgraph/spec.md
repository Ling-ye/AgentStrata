---
id: harness-langgraph
type: architecture
status: implemented
created: 2026-10-16
---

# Harness LangGraph 编排

## Summary

以 LangGraph StateGraph 管理修复、验收、批次与交付的执行位置，分离可复用核心和 AgentStrata 集成。
本规格维护长期编排约束；当前来源、角色、验收、预算与协议版本由[Harness 正文](../../docs/reference/harness.md)维护。
修复与验收实现完成不代表真实 GitHub 交付、QQ 送达或运行库切换已经验收。

## Design

遵循[四层运行时](../runtime-four-layer-definition/spec.md)、[六层依赖](../domain-layered-dependencies/spec.md)
与[控制生命周期](../harness-control-lifecycle/spec.md)。Harness 位于消息链之外，不增加串行消息层。

### 核心与项目集成

`engine` 定义纯状态、执行端口、图、节点和路由；`integrations` 解释 AgentStrata 来源、角色材料、
验证绑定与场景附加要求。故障修复与代码熵治理是固定场景，不增加通用插件发现机制。
场景不能越过核心接受条件或自行签发 AcceptedCandidate。

节点采用 `node(state, runtime: Runtime[HarnessContext]) -> dict`，直接注册到 StateGraph 并返回状态增量。
Context 注入场景、角色协调、Verifier、候选工作区、仓储与预算；不进入 checkpoint。
具体 Codex、Eval、systemd 与 GitHub 实现由装配创建，核心不得经帮助模块或重导出间接依赖这些实现。
依赖约束纳入既有架构检查器，公开入口仍是项目集成的 HarnessController。

### 图与状态

RepairState、AcceptanceState、DeliveryState、BatchState 各有状态范围；修复与验收显式声明输入输出。
普通字段顺序覆盖，历史由不可变产物保存。状态只持有当前候选、计划、结果和审核引用，不复制完整历史。
首次修复与主干复验使用同一验收图；每次调用独立初始化，继承父图检查点，不能沿用上次通过状态。
修复结束后由宿主启动独立交付图，交付失败不改写修复结论。

图与条件边是唯一执行位置管理者。业务库存储身份、外部事实及可重建查询投影，不维护平行阶段调度器。
等待子任务或 GitHub 使用 `interrupt`，由既有后台对账唤醒；等待前后的副作用分开。
实际拓扑以[从 StateGraph 生成的编译图](graphs.md)为准，图变更后运行生成脚本，并用 `--check` 核对。

### 持久化与副作用

官方 SQLite checkpointer 使用独立私有文件、稳定 thread_id 与同步持久化。
未完成节点按 checkpoint 恢复；终态图重新启动时清除旧错误与结论，不能重置未完成图的位置。
图版本绑定冻结宿主，旧图不能执行新状态。

checkpoint、宿主操作回执、不可变产物分别拥有执行位置、外部操作与证据的权威范围，具体记录见
[状态、回执与恢复](../../docs/reference/harness.md#状态回执与恢复)。节点可能重入，检查点不能保证外部操作只发生一次。
已完成且输入一致的回执可复用；外部 ID 可查询时先对账；执行未知不能盲目重放。
交付复验的可重复只读准备与模型、评测等外部操作分开，查询只读公开投影。

预算、权限、取消与最终接受由宿主掌握，不以递归上限替代业务预算；迟到结果不能复活取消任务。
主干前进重新调用同一验收图，保留[交付契约](../../docs/reference/delivery.md)的远端身份与归档约束。
协议切换遵循[先校验归档、后替换活动库](../../docs/reference/harness.md#协议切换)，开发过程不自动切换运行实例。

## Acceptance

- 核心不解析 Bot、Case 或 Gateway 来源布局，使用受控注入即可运行完整修复与验收。
- 正常首轮直接进入 Plan，阶段与返工由 StateGraph 条件边控制。
- 已持久化成功节点可恢复复用；节点执行未知、外部响应丢失与候选漂移不会导致盲目重放。
- 角色权限、冻结验收、AcceptedCandidate、累计预算与取消事实保持独立且完整。
- 批次不重复创建子任务或计数，等待不消耗主动执行预算，未知状态保留占用。
- PR 身份核对、普通推送、主干复验、归档及安全清理均由明确图节点推进。
- Console 通过公开投影展示图执行位置，命令日志和完整证据仍独立按需读取。
- 历史导出经核验后才允许替换活动库，旧数据库与任务资源保留；新任务仅使用新协议，旧任务不迁移或续跑。

## Verification

验证节点边界恢复、角色执行不重复、测试定义变更后基线失效、预算与无进展、取消与迟到结果，
以及批次重入、PR 响应丢失和 main 更新后的复验。使用隔离数据库、工作区和可控执行端口，
覆盖进程终止与 checkpoint 写入失败、已完成操作不可改写、取消后迟到回执、不同执行 ID 的结果拒绝，以及核心的直接和间接依赖隔离。执行定向测试、架构/SDD/文档与 full 检查。
真实模型、真实 GitHub 交付、部署与 QQ 证据独立验证，不由夹具或检查点测试代替。
