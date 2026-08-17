---
id: canary-self-update-e2e
type: architecture
status: accepted
created: 2026-08-17
---

# 独立 Canary 自更新端到端评测

## Summary

AgentStrata 第二阶段新增手动 Suite `agentstrata-canary-self-update-v1`，在系统创建的一次性 Canary 中验证 Agent 修改代码、验证 candidate、受控重启、重启后真实行为、恢复 baseline 与生产目标不变。该 Suite 不改变生产 Codex “验证后 Draft PR、不自动 merge/deploy/restart”的契约，也不得直接把当前原位 `update_instance.sh` 当作 Canary 安全边界。

Canary Evaluation 继续使用现有 application service 和 canonical artifact，但部署、重启、回滚和黑盒探针由独立于被重启 Bot、Console 和普通 Eval worker 的外部 observer 拥有。所有入口只允许 Console 或 CLI 人工确认触发；不得加入 hook、CI、timer 或自动 cadence。

## Design

公共请求只允许选择受信 `template_id`、`smoke|full` mode、模型预算与人工确认，禁止传入 instance、source/destination/runtime 路径、unit、BotSpec、平台账号或 production credential。Application accepted-before-dispatch 后创建绑定 Evaluation/Trial 的 opaque `CanaryTargetHandle`。Target factory 在私有 Canary root 内创建不可变 source/base、Agent 可写 source/work、generation releases、workspace、socket、observer-only control/receipt 与 quarantine；所有路径逐级校验 owner、mode、inode、single-link、symlink 和与生产 root/unit/socket 的非重叠。

Canary Bot 从受信模板生成，使用本地测试 ingress、独立 session/memory/workspace/job/socket，禁止 QQ、飞书、GitHub、生产 MCP 与生产 local.env，只注入最小 Evaluation LLM credential。Agent 看不到 observer token、lease、receipt root、activation descriptor 或任意生产目标。Canary-aware lifecycle intent 不接受 target/path 参数，只能消费当前运行上下文预绑定、一次性的 TargetHandle。

Observer 通过固定 `systemd-run --user` transient unit 启动；禁止 Agent 生成命令、`setsid` 或 `nohup`。Observer 单独拥有 fixture provision、Target 级 deployment lease、baseline 启动与黑盒探针、candidate staging/validation、generation activation、重启、candidate 探针、baseline restore、cleanup/quarantine 和结构化 receipt。Eval worker 只发起真实 Agent turn、收集事件并等待 observer；普通 cancel 只写合作式标记，activation 后必须先恢复或 quarantine 才能进入终态。

部署采用不可变 generation 与原子 activation descriptor。Candidate 在未改变当前 generation 前完成路径白名单、source manifest、编译/单测、BotSpec 和 fixture-specific 验证。激活后必须证明旧 PID/start-time 退出、新 generation/PID 启动，并从新 session 的真实 ingress 观察 `canary_version` 工具事件返回 `candidate-v2`；最终文本包含该值不能作为证据。无论 candidate 成败，MVP 都恢复 baseline 并再次观察真实工具事件返回 `baseline-v1`，成功后销毁一次性目标。

`CanaryDeploymentLease` 绑定 Evaluation、Trial、Target、observer unit/PID/start-time、baseline/candidate generation、candidate digest 和状态；跨重启、回滚、cleanup 持有。TTL 只发现异常，不能自动抢锁；身份、unit 或 generation 不确定时 quarantine。现有要求 active_count 为零的全局 Evaluation maintenance lease 不用于活动 Canary Trial。

Observer 写单调序号、前向 digest chain 并由启动期临时密钥签名的 receipts，application 握手保存公钥，Core 验证后导入 Trial evidence。Receipt 至少覆盖 prepared、baseline_verified、agent_turn_completed、lifecycle_requested、candidate_validated、activated、restart_observed、candidate_behavior_verified、baseline_restored、cleanup_completed。Observer、Application、Core 和 worker 不得共同写同一权威 JSON。

## Acceptance

- 请求和模型不能选择或构造现有 instance、production unit、source/runtime path、platform account 或 credential。
- 生产 fingerprint、unit、PID/start-time、runtime digest、socket 与平台连接在 Canary 前后不变；伪造 TargetHandle 在任何目录、进程或 capability 消费前被拒绝。
- Agent 只修改允许路径，candidate 在激活前通过确定性验证，lifecycle intent 恰好一次且发生在 final flush 后。
- Observer receipt 证明旧 generation 停止、新 generation 启动，并通过真实工具事件验证 `candidate-v2`，随后恢复并验证 `baseline-v1`。
- Candidate 构建、激活、readiness 或行为探针失败自动回滚；回滚无法证明时为 error/indeterminate 并 quarantine，不能继续下一次 Canary。
- Activation 后取消不打断回滚；未知 observer 身份不发送信号、不释放 lease、不猜测终态。
- Receipt 身份、顺序、digest、签名和 target binding 完整；Agent 无法读取或伪造 observer-only evidence。
- 成功和失败都清理或隔离 unit、socket、credential snapshot、generation 与 fixture；生产部署能力不会因此向普通 Agent 开放。
- Console/CLI 只手动触发，现有 Draft PR 产品流程不变。

## Verification

- `python3 scripts/check_sdd_specs.py`
- `.venv/bin/python -m pytest tests/unit -q -k "canary or lifecycle or evaluation"`
- `.venv/bin/python -m pytest tests/integration -q -k "canary or evaluation_service"`
- 故障注入覆盖 candidate 验证失败、readiness 失败、activation 中 cancel、observer 重启、stale lease 和 rollback 失败。
- 手动 smoke 证明 `baseline-v1 -> candidate-v2 -> baseline-v1`、receipt 验证、cleanup 和 production fingerprint 不变。
- `.venv/bin/python scripts/check_repo.py fast`
- `git diff --check`
