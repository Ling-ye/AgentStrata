---
name: debug-chatcopilot-bot
description: 使用 task_* 或 job_* 拉取 ChatCopilot 定向诊断证据，排查机器人卡住、任务失败、回答不对、超时或 token 异常。Use when 用户提供任务 ID、要求拉日志/快照，或询问某次机器人任务为何失败。
---

# Debug ChatCopilot Bot

严格遵循 [`docs/ai-debugging.md`](../../../docs/ai-debugging.md)。

收到 `task_*` 或 `job_*` 后，第一步运行：

```powershell
.\deploy\wsl\win\diagnose-task.ps1 -Id <task_or_job_id>
```

随后只按 `summary.md -> index.json -> 必要证据` 的顺序读取。单任务问题不得先拉 full dump，不得删除任务目录或重启服务破坏现场。最终结论必须引用证据包内的具体文件；证据不足时明确列出缺口。
