# Git 交付与归档

本页维护 code-worker 与 Harness 的发布、对账和清理契约。交互式 AI 协作者未经逐项明确授权，
不得暂存、提交、推送、创建 PR、合并或发布；以下受控 worker 流程不授予交互式 AI 同等权限。

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
