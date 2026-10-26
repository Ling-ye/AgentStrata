# 源码交付、实例版本与归档

本页维护 code-worker 与 Harness 的发布、对账和清理契约。交互式 AI 协作者未经逐项明确授权，
不得暂存、提交、推送、创建 PR、合并或发布；以下受控 worker 流程不授予交互式 AI 同等权限。

## 已验收版本启用与撤回

M5 使用 `HarnessController` 的准备、启用、查询、撤回、取消和对账入口。来源必须是当前协议下
已签发完整 `AcceptedCandidate` 的应用需求任务；实际候选快照、独立 Review、基线/候选/确认的
版本证据均重新核验。修改、取消、缺少版本事实或运行条件不一致的候选不能启用。
旧验收记录不会补造版本事实，需要重新验收后使用此入口。

目标是已登记的仓库部署实例，或显式选择的 `plugin:<plugin-id>:<agent-id>` 安装。
目标代码及应用配置必须与评测基线匹配，实际模型绑定和 Gateway 的 Python/依赖/native runtime
身份必须对应验收条件。不能借同名实例、模型或进程退出码复用旧接受结果。
插件目标仍要求与仓库评测应用配对；安装目录不会因此成为 M4 的任意可写配置来源。

每个实例在私有 `.agentstrata-versions/` 中保存基线和候选源码、内容清单、编译结果、切换记录与历史。
使用已安装解释器编译检查，然后由现有启动脚本或显式 `versioned` 服务选择源码版本和 Agent 配置。
`current.json` 只指向本安装的已准备版本，替换是原子的；程序依赖、凭据和可写实例数据保持原位置。
该目录及 Gateway 管理状态从 Agent 的文件资源视图中隐藏，不随源码同步或发布。

一次启用按以下顺序执行：

1. 记录绑定任务、候选和目标实例的意图；准备不停止服务。
2. 取得实例操作锁及准入锁，等待已运行任务、未确定执行和投递收尾。新的渠道输入继续进入已有持久队列，暂停认领；新的 RPC 回合返回 `gateway_updating`。
3. 等待桥接动作调用结束，停止已登记服务，并确认进程停止后替换版本指针。
4. 启动并核对实际进程、启动代际、回环监听端口、加载路径、代码/配置摘要及 runtime 环境，再记录启用成功。

启动后的进程记录实际加载事实，控制端另核对 PID、进程命令、监听归属和文件内容。
模型的最终文本、单次启动退出码和旧进程回执均不能替代这些事实。此核验说明版本已加载，
不代表真实模型能力或外部用户交付已经通过 M7 验收。

启用失败会尝试恢复原指针并重启上一版本；恢复失败记为 `recovery_required`，不能报告成功。
进程中断后先对账：已实际加载的目标版本可确认完成，已有撤回意图则继续撤回，不反向改判为启用成功。
取消请求和源任务失去启用资格会阻止继续切换。显式撤回只作用于当前版本，保留原操作及失败记录。
查询只读取记录与进程事实，不推进操作；`running_matches` 为未知时保留未知。

首版支持自有 Python 代码、桥接代码和冻结的公开 Agent 配置。依赖、上游补丁/构建资源及持久层变更
不通过普通切换入口，需要单独的构建或迁移/恢复验收。撤回程序版本不会撤销已发生的外部动作，
也不会覆盖后续业务数据。普通实例更新和插件重装须先撤回受管版本，避免两个更新路径同时生效。

源码入口：[事务](../../src/chatcopilot/plugins/activation_service.py)、[安装适配](../../src/chatcopilot/plugins/activation_runtime.py)、
[启动与身份](../../src/chatcopilot/core/release_runtime.py)、[准入](../../src/chatcopilot/core/release_gate.py)。
操作步骤见[启用与撤回](../guides/instances.md#启用与撤回已验收版本)。

凭据值不复制进版本，也不参与模型路由身份；保持同一认证引用的凭据轮换不改变代码版本。
一次切换仍冻结环境文件，期间出现并发修改时停止并保留诊断。已启用版本的显式撤回保留当前凭据，
不因为原需求任务后来取消而失去撤回能力。

## 两条受控交付路径

| 路径 | 授权与结果 |
| --- | --- |
| code-worker | Owner 明确调用独立代码任务，受信 worker 交付草稿 PR |
| Harness | 操作者启动正式修复或代码熵任务，冻结宿主在独立验收后交付正式 PR，并启用 GitHub 原生 squash 自动合并 |

## Harness

### 候选与发布资格

任务冻结远端 main、仓库、actor、任务分支与源码身份，在 `feat/harness-<task-id>` worktree 生成候选。
编程与审核 Agent 没有 Git 写权限或 GitHub 凭据。只有宿主签发的完整验收候选可以交付，正式提交
绑定审核过的精确文件、树和回归依据，不能用模型文本或进程退出码代替验收。

宿主执行独立审核、公开边界与秘密检查后，提交、普通推送并创建目标为 main 的正式 PR。
必需检查与审查不能绕过；不执行 Git hooks、强制推送或改写远端历史，不更新操作者本地 main，也不部署。
历史记录保持只读，不因重试获得新的发布权限；新请求不接受 `review_and_commit`。

### 对账与主干复验

重试先核对既有远端身份与交付意图，不重复创建提交或 PR。main 前进时先停止自动合并，恢复任务
worktree、整合 main，调用与首次修复相同的验收图重新验证和审核，通过后普通推送。
冲突、外部 head 变化或人工关闭自动合并会阻断自动进展，不能自动扩大修复范围。

明确取消在发布前阻止交付；已有 PR 时关闭自动合并并保留 PR，与合并竞争时以 GitHub 回执为准。
交付复验取消须确认外部测评收尾，不能改写已成立的修复结论或提前释放占用。
修复、交付、合并与清理分别记录，对账独立于 Console 页面。

### 归档与清理

PR 创建并核实提交后，本地任务工作区与分支可在归档验证后清理；失败任务也先归档再清理。
归档保留可恢复源码、Git bundle、补丁、验证证据和日志，不自动过期。活动进程、身份漂移、归档缺失
或损坏、现场外部修改及未归档内容均阻止删除。远端分支仅在 PR 已关闭或合并且 head 匹配登记提交时删除。

代码熵批次须等当前 PR 合并，或处理失败且 worker、验证与自动合并安全收尾后，再开始下一项。
实际交付与复验消耗批次预算，纯查询与等待不计时；预算耗尽仍可核对已发布 PR 并收尾，不能新发起复验。
配置、重试与定时器命令见[操作指南](../guides/harness.md#harness-pr-交付配置与恢复)。

## code-worker

Owner 明确调用 `start_code_task` 后，受信 worker 在独立的 `codex/<instance-id>/<task-id>` 分支
提交、非强制推送并创建草稿 PR。该授权不包含 merge、force-push、部署或修改操作者工作区。
执行隔离与认证边界见 [Agent](agent.md)，凭据准备见[外部服务指南](../guides/services.md#代码任务草稿-pr-凭据)。

## 源码入口

- [src/chatcopilot/harness/delivery.py](../../src/chatcopilot/harness/delivery.py)
- [src/chatcopilot/harness/delivery_archive.py](../../src/chatcopilot/harness/delivery_archive.py)
- [src/chatcopilot/harness/github_delivery.py](../../src/chatcopilot/harness/github_delivery.py)
- [src/chatcopilot/external_tools/repository_tasks](../../src/chatcopilot/external_tools/repository_tasks)
