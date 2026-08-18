---
id: evaluation-plugin-capabilities
type: architecture
status: accepted
created: 2026-08-17
---

# Evaluation 插件化与产品能力套件

## Summary

AgentStrata 保留现有 `comparison | suite`、Evaluation application service、UDS、managed worker、取消/恢复与 canonical local artifacts，只把 Suite/Case 的声明、执行和判分改造成仓库内受信插件与版本化 manifest。该规格修订 `evaluation-center-unification` 中“不形成可注册插件框架”的内部实现限制，但不改变其生命周期、artifact owner、幂等 mutation、单 Bot claim 或 Console 仅作为 UI/BFF 的边界。

用户只能从 Console 按钮或 CLI 命令手动启动评测；不得加入 Git hook、CI 自动门禁、文件监听、部署回调、定时器或 Bot 重启后的自动触发。第一阶段新增 `agentstrata-capabilities-v1`，正式运行面向所选 Bot 和其商用 LLM 配置，固定提供 26 个产品能力 Case，默认每 Case 只运行 1 次。图片理解的 3 个 Case 与合成 fixture 已配置；图片生成保持 `capability_not_configured`，不得按失败计数。真实 QQ 连通性改由 `qq-external-platform-check` 规格定义的平台外部检查承担，不进入 Agent Case、Trial 或 verdict。GAIA、BFCL、IFEval 保留为独立公开 Suite，其中 BFCL 必须继续标记为 `direct_llm/function_call_protocol` 函数调用协议校准，不能并入产品 Agent 能力结论。SWE-bench Verified、WebArena 和 `agentstrata-canary-self-update-v1` 只登记为 `planned/unavailable`，不能启动或暗示已经完成 Canary 自更新能力。

本规格区分“实现并经过仓库级自动化验证”与“使用真实外部配置完成端到端运行”。单元/集成测试可以证明 manifest、插件、预检、进程隔离、预算、取消和 artifact 契约，但不能据此宣称真实商用 LLM、真实 QQ 或 Canary 自更新 E2E 已经通过；这些结论只能来自维护者手动发起并保留相应 Trial 证据的实际 Evaluation。

## Design

Suite 定义从当前硬编码 catalog 迁入安装包内 `chatcopilot.evals/suites/<suite-id>/manifest.yaml`。只扫描该固定一层目录；manifest、Case、fixture 必须为 UTF-8 普通文件并通过大小、ID、duplicate key、未知字段、相对路径 containment、symlink、MIME 与 SHA-256 校验。YAML 只能引用受信 `plugin_id`、Core-owned `driver_id`、fixture 和 verifier ID，禁止 Python 模块、shell、任意 URL、secret、机器绝对路径、模板代码、cleanup 命令和任意表达式。

Python 插件只从静态 binding catalog 加载，模块必须位于 `chatcopilot.evals.plugins.*`，API version、plugin ID、允许 driver 和 hook 形状必须完全匹配。不支持 setuptools entry point、用户插件目录、环境变量 Python 路径、网络 registry 或自动安装。插件可实现 Case loading、受限 preflight、prepare、task/turn 构造、trial execution、judge 和 cleanup，但不能写 `request.json`、`state.json`、`result.json`、`summary.md`、`progress.jsonl`、claim 或 cancel marker。Core 创建 workspace/runtime，执行取消与预算，校验并脱敏 `TrialObservation`，再生成 Trial 和权威 artifact。

Core-owned driver 初始为 `agent_isolated`、`agent_configured`、`acp_scenario`、`direct_llm` 和 `dry_run`。Agent Evaluation Case 不允许 `external_write`；真实平台写入必须位于 Evaluation 生命周期之外的受信外部检查或运维动作中。

正式 Trial 不在 Evaluation Core 进程内直接执行。Core 使用独立 `spawn` supervisor 与有界 canonical JSON IPC 运行每个 Trial，插件 API 不提供权威 artifact writer；父进程在 Trial 前后用固定目录描述符、inode/owner/mode/link/time/content digest 校验权威 artifact、claim 与 Trial 证据，发现持久化漂移时将整次 Evaluation 标为 `error/indeterminate`、保留隔离 workspace 且禁止 resume。该完整进程树回收实现当前只支持 Linux/WSL；其他平台在执行前失败关闭。由于仓库内受信插件仍与 Core 使用同一 OS 用户，此完整性 guard 是持久化篡改的检测与拒绝机制，不是抵抗恶意插件“短暂写入后原样恢复”的 mount-level 隔离；若未来开放第三方或不再信任静态插件实现，必须先增加只读 authority mount（例如受控 bubblewrap worker）再扩大插件边界。

每次执行期限取受信 Case `timeout_seconds` 与当前 Evaluation 剩余 `max_wall_seconds` 的最小值；Case 期限耗尽形成基础设施错误 Trial，Evaluation 总预算耗尽则停止本次运行并保持 `partial`。取消、期限耗尽或启动失败会终止并回收该 Trial 的进程组及其模型/工具子进程；Linux/WSL 还使用父死保护，使 Core 意外退出时 Trial session leader 终止整个进程组。只有同一 Case/attempt 的完整 Target 组才能加入 checkpoint；取消或总预算在组内触发时，已产生但未成组的 Trial 与 workspace 都必须丢弃。

`SuiteEvaluationRequest` 增加 `preset`、`repetitions`、`max_wall_seconds`、`seed` 和严格校验的 `options`。默认 `repetitions=1`。现有 `dry_run` 与 `llm_judge` 保持兼容并归一化到 Suite option。Suite manifest、所选 Case、fixture digest、插件 binding/API/实现模块、driver/scorer 协议和 Bot runtime/Target 一同形成 definition fingerprint；任一漂移在写入前拒绝 resume，不同定义拒绝 compare。旧 artifact 可读和导出，但不得伪装成可恢复或可比较的新定义。

创建仍先执行无副作用预检。预检按所选 Bot 和 Preset 验证 BotSpec、backend/model、必要 env、白名单、Owner/Admin 与 feature/tool/service；不得探测或发送真实 QQ 消息。原始值不得进入 prompt、日志或 artifact。required 配置缺失返回结构化 `preflight_failed/configuration_invalid`，不创建 Evaluation、目录或进程，不调用模型。通过后只产生一条 Evaluation 和一份报告。required Case 失败或 critical invariant 违规使结果失败；基础设施、插件或证据完整性问题单独记为 error/indeterminate；不得把安全、产品能力、外部平台检查和公开 benchmark 平均为一个总分。

`agentstrata-capabilities-v1` 提供 `quick`、`full`、`security` 和 `custom` 四种手动选择方式。首批 26 个 Case 覆盖对话约束 2、工具编排 4、搜索 3、文件/Workspace 3、图片理解 3、会话/记忆/Subagent 3、代码/恢复 3、白名单/角色/注入 5。`quick` 固定选择 10 个代表性 Case，`full` 选择全部 26 个，`security` 选择 5 个白名单/角色/注入 Case，`custom` 要求显式列出 Case ID。MVP 默认 `repetitions=1`，26 Case × 1 只表示这一次运行的结果，不测量重复可靠性。拒绝用户、冒充、伪造身份和无 @ 继续使用隔离 ACP/OneBot 场景验证，不依赖真实 QQ 发送账号。

能力目录将图片理解标为已配置，并把图片生成单独显示为 `image_generation:not_configured`。公开基准保持各自语义：GAIA 与 IFEval 走 Agent runtime，BFCL 走 direct-LLM；SWE-bench Verified、WebArena 和 Canary 自更新 Suite 均保持 `planned/unavailable`，预检不得让它们进入正式 Trial。

真实 QQ 连通性不使用 Evaluation 配置。平台外部检查只消费 QQ Bot 已有的 `QQ_ACCOUNT`、`QQ_WS_URL`、`QQ_ACCESS_TOKEN` 和可选的 `CHATCOPILOT_EXTERNAL_CHECK_QQ_GROUP_ID`，其结果不进入 Evaluation artifact。

## Acceptance

- Console/CLI 只手动启动；代码修改、CI、部署和重启不会自动创建 Evaluation。
- Catalog 由 manifest 与静态受信插件单一事实源生成；GAIA、BFCL、IFEval 迁移后 Case 选择与 judge 保持，BFCL 明确为 direct-LLM，SWE-bench/WebArena/Canary 明确 planned/unavailable。
- 配置化普通 Case 无需改 runner；动态数据、ACP、QQ、Git、子进程和复杂 judge 只能由受信插件实现。
- 配置预检失败无目录、进程、模型费用或外部消息；通过后一个请求只生成一份报告。
- 26 个能力 Case 可按 preset/custom 选择；MVP 每 Case 一次并明确不声称重复可靠性。
- 图片理解 Case 已配置并要求 fixture 真正进入 Native/Codex backend；图片生成显示未配置而不伪装成失败。
- allowed tool 可执行，disabled/hidden/forbidden tool 即使被构造调用也不能产生副作用；白名单和角色负例以真实后置状态判定。
- 真实 QQ 连通性不注册 Evaluation Case、plugin、driver 或 preset；外部检查结果不影响 Agent verdict。
- Definition、Case、fixture、plugin、driver、scorer 或 Target 漂移拒绝 resume/compare；旧报告仍可安全读取和导出。
- 插件异常、基础设施失败和 Agent 失败分开；critical 安全违规不能被能力分数抵消。
- 正式 Trial 使用独立 spawn 子进程；有效期限是 Case timeout 与 Evaluation 剩余 max-wall 的最小值，取消和预算终止进程组，Linux/WSL 的 Core 父进程死亡不会遗留 Trial 后代。
- Trial 前后权威 artifact、claim 和已有 Trial 证据均通过父进程完整性 guard；持久化修改导致整次 Evaluation `error/indeterminate`、不 checkpoint 且拒绝 resume。静态受信插件的同 UID 短暂写入不是第一阶段已经证明的隔离属性。
- 只有完整 Target 组进入 checkpoint；取消或 Evaluation 预算耗尽时，不完整组的 Trial 与 workspace 不参与恢复、比较或通过率。
- 仓库级自动化结果不得描述为真实商用 LLM、真实 QQ 或 Canary 自更新 E2E 通过。

## Verification

以下命令验证仓库实现与契约；其中使用 fixture、mock、dry-run 或隔离 transport 的结果不构成真实商用 LLM、真实 QQ 或 Canary E2E 证据：

- `python3 scripts/check_sdd_specs.py`
- `.venv/bin/python -m pytest tests/unit -q -k "eval or evaluation or access_gate or qq"`
- `.venv/bin/python -m pytest tests/integration -q -k "evaluation or acp or attachment"`
- `.venv/bin/python scripts/check_component_catalog.py`
- `.venv/bin/python -m chatcopilot botspec validate bots/lingye-copilot-qq/bot.yaml`
- `cd console/web && npm test`
- `cd console/web && npm run build`
- `.venv/bin/python scripts/check_repo.py fast`
- `git diff --check`
