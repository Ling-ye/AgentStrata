# 代码熵回收

代码熵回收依据现行 SDD 和[黄金原则](harness-principles.md)，从冻结的远端 main 自主调查全仓，
识别死代码、重复职责、失效配置、结构漂移与文档失真。两种停止条件都逐项执行：
发现首个有证据的问题后立即停止调查，修复、验证并交付一个 PR；确认合并且 worker 停止后，
从最新远端 main 开始下一项。规格见[熵回收 SDD](../../specs/harness-code-health/spec.md)。

## 停止条件与预算

Console 第一项选择停止条件，模型与推理强度共用于整次回收。

- 按时间：默认累计实际执行 1 小时，跨问题共用；准备、调查、修复、验证与交付复验计时，
  等待 GitHub 检查、审查和轮询间隔不计时。耗尽即停止主动执行，保留候选和证据；
  已发布 PR 可继续状态核对与收尾，需要新复验时转为待处理。
- 按问题发现数：默认 1 项，无隐含时间上限。宿主确认问题后计数，同一问题的返工与恢复
  不重复计数。第 N 项仍完成修复和交付，之后结束，不创建第 N+1 项。
- 每个问题的修复尝试上限含首轮，默认 3 次，只限制该问题的首次修复和返工。
  连续两轮无进展沿用 Harness 停止规则；恢复不重置次数或累计时间。

没有可执行发现时提前结束，并保留未覆盖范围。问题需要人工判断、修复失败或交付受阻时，
整次回收停止自动推进，不跳过问题凑数。取消先禁止创建下一项，再等待当前执行和交付取消
实际完成。恢复从批次入口操作；耗尽预算、终止失败或需人工判断的任务不能靠恢复重置限制。

## 共用 Harness

code_health 仍是 [Harness](harness.md) 的第三类任务来源。每个问题复用既有角色、工作区、
尝试记录、验收、取消、日志与 [PR 交付](delivery.md)。批次只保存停止条件、当前任务、累计
发现数、合并数、执行时间和停止原因，与任务共用 Harness 状态库。

Main 安排任务，Plan 读取规则、追踪调用者并提交原始片段；Coding 实现候选，Test 按需建立
验证，Review 独立审查。单次报告最多一个 finding；宿主冻结问题身份、规则、源码证据、
修改范围和验收目标，返工不得换题或扩展目标。人工提示与人工预期不作为代码熵回收输入。

宿主按文件与行引用提取原文并绑定冻结源码身份。Agent 报告的阅读路径须结合命令日志核对，
目录清单不构成全仓阅读或健康结论。首轮 SourceIndex 提供有界导航，后续可按需只读搜索全仓；
调查报告记录实际范围、未决项与未覆盖范围。没有可执行发现为 no_changes，敏感项为 needs_review。

## 验收与写入范围

熵回收可以从全绿基线开始，但不能仅凭测试全绿、删行数或模型自评交付。宿主检查候选文件
范围，执行冻结仓库 full 检查及必要行为保持验证；独立审核说明真实前后变化与改善依据。
证据不足时保留候选，不能自动交付。故障修复仍要求有效失败对照，角色不能切换验收用途。

产品源码、声明配置、Console 和普通指南可进入候选。既有测试、依赖、SDD、黄金原则、权限
和检查配置保持固定；需要调整时列为待判断。新增验证由 Test 在草案目录编写、宿主冻结收录。
正在运行的控制程序、选择规则和检查器不会被候选替换。

## 入口与可选定时

批次 API 位于 `/api/harness/code-health/runs`，支持创建、分页、详情及 `/{run_id}/cancel`、
`/{run_id}/resume`。停止条件为 `{mode: "time", seconds}` 或 `{mode: "findings", count}`，
手动、CLI 和定时共用。子任务证据、日志和 PR 详情继续使用普通任务入口。

定时默认关闭、间隔 24 小时；启用约一分钟后首次触发。systemd timer 只创建普通回收批次，
已有活动批次或待完成交付时跳过，不累积排队。关闭定时不取消当前批次。现有分钟级交付
对账入口推进批次，不依赖 Console 页面在线；创建下一项用稳定请求身份防止重复。

本功能沿用 pipeline 9，增量建立批次记录，不清空状态库。旧熵回收任务只读保留，旧参数不做
兼容解析；旧定时配置需要重新保存。每项仅冻结远端 main，不纳入操作者未提交内容，不修改
操作者工作区，不自动部署。操作示例见 [Harness 指南](../guides/harness.md#代码熵回收与可选定时)。

## 源码入口

- [批次契约](../../src/chatcopilot/harness/governance_types.py)、[批次推进](../../src/chatcopilot/harness/governance_run_service.py)
- [批次记录](../../src/chatcopilot/harness/governance_run_repository.py)、[执行预算](../../src/chatcopilot/harness/task_budget.py)
- [问题证据](../../src/chatcopilot/harness/governance_repository.py)、[验收](../../src/chatcopilot/harness/governance_verification.py)
- [定时触发](../../src/chatcopilot/harness/schedule_runtime.py)、[Console 页面](../../console/web/src/pages/CodeHealthPage.tsx)
