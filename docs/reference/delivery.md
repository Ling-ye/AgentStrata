# Git 交付与归档

修改 code-worker 或 Harness 的发布、重试和清理时阅读。交互式协作者未经逐项授权不暂存、提交、推送或发布。

## 两条受控交付路径

code-worker 由 Owner 明确调用独立代码任务后交付草稿 PR；新的 Harness 修复与代码治理任务由冻结宿主交付正式 PR，并交由 GitHub 原生 squash 自动合并。

### Harness

- 新任务冻结远端 main、仓库、actor、任务分支和源码身份。候选没有 GitHub 凭据或 Git 写权限；正式提交绑定已审阅的精确文件、树和回归依据。
- 只有完整验收候选可以交付；明确取消不继续发布，并关闭未完成的自动合并。重试先核对远端身份，不能重复创建提交/PR或强制推送。
- main 前进时先停自动合并，恢复任务 worktree、整合 main、重新执行冻结验证与审核，再普通推送。冲突、外部 head 变化或人工关闭自动合并会阻断自动进展。
- 清理前保存并验证可恢复的源码、Git bundle、补丁和验证证据。活动进程、身份漂移、缺失归档或未归档修改阻止删除。远端分支只在 PR 已关闭/合并且 head 匹配时删除。
- 交付对账独立于 Console，历史任务不获得新发布权限，日志与归档不自动过期；不更新操作者本地 main，不部署。

## code-worker

## 源码入口

- [src/chatcopilot/harness/delivery.py](../../src/chatcopilot/harness/delivery.py)
- [src/chatcopilot/harness/delivery_archive.py](../../src/chatcopilot/harness/delivery_archive.py)
- [src/chatcopilot/harness/github_delivery.py](../../src/chatcopilot/harness/github_delivery.py)
- [src/chatcopilot/external_tools/repository_tasks](../../src/chatcopilot/external_tools/repository_tasks)

## Git 写操作只接受当前请求的明确授权

当前交互式 AI 协作者不得自行执行 `git add` / `git commit` / `git push`；既有发布自动化例外是 Owner 明确调用 `start_code_task`，由受信 code-worker 在任务专属 `codex/<instance-id>/<task-id>` 分支上提交、非强制推送并创建草稿 PR。 该例外不授权 merge、force-push、部署或修改操作者工作区。新 Harness 修复任务按冻结远端 main 创建 `feat/harness-<task-id>` worktree；启动操作授权受信宿主在独立验收通过后提交精确文件、普通推送、创建正式 PR、启用 squash 自动合并，并在可恢复归档核验后清理任务自有资源。GitHub 检查与审查不得绕过；主干前进需重新验收。历史记录只读，新请求拒绝 `review_and_commit`，不扩大编程/审核 Agent 的 Git 权限；不更新操作者本地 main 或部署。完整契约见 `docs/reference/delivery.md`。
