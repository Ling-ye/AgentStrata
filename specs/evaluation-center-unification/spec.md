---
id: evaluation-center-unification
type: architecture
status: accepted
created: 2026-07-26
---

# 评测中心 Evaluation 统一

## Summary

AgentStrata 将原先分离的单 Suite Run 与 Agent Comparison Experiment 收敛为统一的评测记录（Evaluation）。控制台、API、CLI、生命周期、持久化与报告只保留一套公共契约，同时完整保留 Profile 配对评测、BFCL/GAIA/IFEval Suite 评测、数据准备、任意 Case 和覆盖追踪能力。

旧 `/api/evals/runs`、`/api/evals/experiments`、`compare-agents`、双 manager 与 Legacy 页面直接删除，不提供兼容别名或旧报告读取器。已有旧报告目录不自动删除。

## Design

Evaluation 使用 `evaluation_id` 和 `kind: comparison | suite`。顶层 `status` 只表示 `queued/running/completed/partial/cancelled/interrupted/error` 生命周期；Trial 保存 `passed/failed/skipped/error` 结果；只有完整双 Target 组生成 Case Comparison。

Comparison 选择版本化 Profile。Quick/Standard 使用 Profile 的服务端默认策略；Custom 显式声明 Target、Case、重复次数、墙钟预算和 seed。Suite 选择一个 benchmark Suite 与 Case 集，并只接受该 Suite 声明的 dry-run 或 Judge 选项。Target 是包含 executor、backend、model、reasoning effort 与稳定 fingerprint 的不可变受测配置快照。

创建 API 原子执行无副作用校验；失败返回结构化阻断项，通过后才创建目录和进程，不保留独立 preflight API。统一编排器负责 Target 组顺序、预算、取消、恢复、完整组 checkpoint、脱敏和报告；BFCL direct-LLM、Agent 与 dry-run 是明确的内部执行路径，不形成可注册插件框架。

统一持久化位于 `reports/evals/evaluations/<evaluation-id>/`，权威文件为 `request.json`、`state.json`、`result.json`、`summary.md`、`progress.jsonl` 与逐 Trial 证据。Case 覆盖按 Bot、Case 和 Target fingerprint 聚合。

恢复是同一 Evaluation 的续跑，不是修改请求后复用旧 Trial。恢复前必须在任何写入之前同时核对完整请求、Case 快照、已有 Trial 和包含 Bot runtime 行为摘要的 Target fingerprint；任一 fingerprint 或 checkpoint 结构漂移都拒绝恢复。已完成 Evaluation 不再恢复，未形成完整 Target 组的 workspace 必须在重跑前清理，不能把中断残留带入 Judge。非 Resume 只能写入新目录或严格匹配、无证据文件的 Evaluation service bootstrap 目录，不能覆盖旧 Evaluation。外部 Suite 的原始 Case ID 只作为领域标识，workspace 和 Trial artifact 文件名必须使用确定性的安全编码，不能参与路径解析；包含 `/` 的 Case ID 仍须可通过查询与详情 API 访问。

持久化边界统一识别 credential、secret、password、API key 以及通用 token 字段和环境变量，并清除未声明的机器绝对路径。Core 是 `progress.jsonl` 的唯一结构化进度写入者，Console monitor 只写脱敏日志。比较报告只接受 lifecycle 已完成、同 kind、同 Profile/Suite、同 Case/Trial 样本、同 Judge 且 Target executor/backend 语义可比的 Evaluation，禁止跨任务集计算伪差值；所有数值参数必须是有限数。`evaluation_id` 在 CLI、Core 和 Console 统一为 1–128 位 ASCII 字母、数字、下划线或连字符。

同一 Bot 的活动 Evaluation 约束跨 Evaluation application 线程和 service 进程生效。创建使用持久化根级原子 claim，进程真正退出后才释放；只要 service 仍观察到活进程，记录就不能删除、重跑或让同 Bot 启动下一条。Worker 身份只能通过解析 argv 中唯一的 `--output` 并与 Evaluation 目录规范路径精确匹配，不能使用路径子串；遗留 PID 存在但身份暂时无法验证时必须 fail closed，不能发送信号、定态或释放 claim。Evaluation 目录、claim 和权威 artifact 均拒绝符号链接，读取、流式传输、导出和删除前必须核对记录内的 `evaluation_id`。进程与部署所有权的后续边界以 `evaluation-service-boundary` 规格为准。

控制台产品面按后续 `evaluation-two-track-center` 规格收敛为“开始测试 / 运行记录”，开始页只显示直接 Agent 与 QQ 消息链路两个入口。Profile、公开 benchmark、数据准备与任意 Suite 仍由同一 service/CLI 管理，但不进入 Console 主测评面。

## Acceptance

- Comparison 与 Suite 都从同一 API、Evaluation application、CLI 和持久化根创建、观察、取消、重跑、删除与导出。
- 一键 Quick/Standard 不再产生额外字段 422；所有结构化错误均显示字段或阻断原因，不出现 `[object Object]`。
- BFCL direct-LLM、GAIA/IFEval Agent、dry-run、Profile 隔离、完整配对、seed、预算和脱敏语义保持。
- Evaluation status 与 Trial outcome 分离；单 Target 不生成胜负，多 Case 同 Dimension 正确累计。
- 同一 Bot 跨进程同时最多一个活动 Evaluation；服务重启与取消保留完整 Target 组 checkpoint。
- Worker PID 必须精确绑定 Evaluation 输出目录；目录别名、符号链接、artifact 外链或记录 ID 不一致时，读取、导出、取消与删除均 fail closed。
- Resume 只接受完整请求、Case 快照、Bot runtime Target fingerprint 和 Trial 结构均一致的完整 checkpoint，拒绝已完成记录、请求漂移与残留 workspace 污染，失败前不改写任何权威文件。
- 任意外部 Case ID 都不能逃逸 Evaluation workspace 或 Trial artifact 根；通用 token、已知 secret 和机器绝对路径不能落盘。
- CLI 保持严格布尔、列表、有限数、统一 ID 和新目录语义，并直接生成 `progress.jsonl`；报告比较拒绝 incomplete lifecycle、不同 kind、Profile、Suite、Case/Trial 样本、Judge 或 Target executor/backend 语义。
- CLI/服务仍可准备公开 Suite 数据并按 Bot、Case、Target fingerprint 查询覆盖历史。
- 旧 API、旧 CLI 分支、旧 manager 和 Legacy 页面不存在；旧报告目录不会被自动删除。
- 桌面和窄屏下只出现两轨创建与统一记录信息架构；切换 Bot、轨道或记录后，旧异步响应不得覆盖当前上下文。

## Verification

- `python3 scripts/check_sdd_specs.py`
- `.venv/bin/python -m pytest tests/unit -q -k "eval or evaluation"`
- `cd console/web && npm test`
- `cd console/web && npm run build`
- `.venv/bin/python -m chatcopilot botspec validate bots/lingye-copilot-qq/bot.yaml`
- `.venv/bin/python scripts/check_repo.py fast`
- `.venv/bin/python scripts/check_repo.py full`
- `git diff --check`
- 使用本地浏览器检查桌面与窄屏的两轨卡片、阻断、活动进度和记录抽屉。
