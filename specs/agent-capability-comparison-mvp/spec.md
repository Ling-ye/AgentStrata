---
id: agent-capability-comparison-mvp
type: architecture
status: superseded
created: 2026-07-23
---

# Agent 能力对比与统一评测中心 MVP

 本规格已由 `evaluation-center-unification` 取代；以下内容仅保留为历史设计记录。

## Summary

AgentStrata 已能运行单个评测套件并持久化结果，但没有把同一 Bot 的两个 Agent backend 作为一场可复现的配对实验来执行、观察和解释。MVP 将单 Agent 评测与 Codex/Native 对比收敛到统一评测中心，以四个固定代表任务展示指令遵循、知识与检索、工具编排和代码任务四维结果。结果只描述该固定任务集上的差异，不生成通用“智能总分”。

非目标包括 CI 门禁、定时评测、长期趋势、排行榜、任意多目标对比、在线编辑评测任务、原始轨迹留存、生产外部写操作和 LangGraph UI。

## Design

评测任务（Case）属于评测套件，任务集（Profile）引用跨套件的固定 Case。评测实验（Experiment）包含一个 Bot、一个或两个候选目标（Target）、重复次数、固定随机种子和墙钟预算。每次候选目标执行一个 Case 称为单次尝试（Trial）；同一 Case、同一 attempt 下两个 Target 的完整结果构成一个配对。实验执行状态与能力结论分离：状态描述实验是否完成，配对结果描述 Codex、Native、平局或无法判断。

默认 Profile 包含一条 IFEval 指令任务、一条固定 GAIA 知识/检索任务、一条确定性工具任务和一条隔离仓库代码任务。快速档每个 Target 执行一次，标准档执行三次并限制为 2700 秒。Runner 按 Case 和 attempt 配对串行运行，使用固定 seed 交替 Target 先后顺序；预算只在完整配对之间检查，预算耗尽后保留已完成配对并把实验标记为 partial。

评测只在进程内覆盖 backend，不写回 BotSpec。Codex 使用当前 code lane，Native 使用当前 chat lane；公共提示词、声明工具和上下文仍来自同一个 BotSpec。每个 Trial 使用独立 workspace。Case 显式声明评测能力边界；只读任务不获得写工具，确定性工具任务只获得评测 fixture 工具，代码任务只能写隔离 fixture。消息、部署、真实记忆或 Wiki 写入以及 workspace 外 shell 均被拒绝。

评分确定性优先：IFEval 使用规则，GAIA 使用标准答案，工具任务核对工具调用，代码任务运行固定验证命令。只有无法确定语义等价时才使用独立 Judge；需要 Judge 而 Judge 未配置时预检失败。所有分数归一化到 0..1；完整配对两侧平均分差不超过 0.05 为平局，不完整或无有效裁判为 inconclusive。

请求、状态、结果和证据以文件形式持久化到 `reports/evals/experiments/<id>/`。写盘前统一脱敏，原始事件不落盘。结果记录 backend、模型、reasoning effort、BotSpec/提示词/工具/Profile/Case 哈希、Git 状态、seed 和 Judge 标识，但不记录凭据值。JSON 和 Markdown 使用同一脱敏数据源。

控制台保留一个“评测中心”入口，内部提供工作台、运行记录和任务集。工作台提供快速/标准一键对比和高级选项；启动前严格预检。运行详情通过结构化 SSE 展示配对进度和脱敏日志。结果页展示四维矩阵、胜平负、稳定性、耗时、token 与逐题并排证据。现有单 suite API 与 CLI 保持兼容，并复用新的运行与展示基础设施。

回滚时可停止使用 Experiment API 和新控制台视图，已有单 suite runner、报告与 `/api/evals/runs` 数据仍保持可读。

## Acceptance

- 同一 Bot 可从控制台或 CLI 启动 Codex/Native 快速或标准配对实验，且不会修改 BotSpec。
- 未准备的数据、backend、Judge、fixture 或工具策略会在启动前给出可操作的失败原因。
- 快速档创建 8 个 Trial，标准档最多创建 24 个 Trial；运行顺序可由 seed 复现。
- 取消、服务重启和预算耗尽会保留完整配对 checkpoint，并分别显示 cancelled、interrupted 或 partial。
- 结果页按四个维度展示样本量、两侧通过率、稳定性、耗时、token 和逐题胜平负，不显示总智能分。
- Case 详情可并排复核两侧输出、评分理由和脱敏工具证据。
- JSON/Markdown 报告不包含已知 secret、凭据字段、用户主目录或工作区绝对路径。
- 现有单 suite 列表、准备、运行、取消、详情和历史接口继续工作。
- 控制台在桌面与窄屏下具备可用的 loading、empty、error、partial 和长文本状态。

## Verification

- `python3 scripts/check_sdd_specs.py`
- `.venv/bin/python -m pytest tests/unit/test_evals.py tests/unit/test_eval_console.py -q --basetemp=/tmp/chatcopilot-pytest-eval-mvp`
- `.venv/bin/python scripts/check_repo.py fast`
- `.venv/bin/python -m chatcopilot botspec validate bots/lingye-copilot-qq/bot.yaml`
- `cd console/web && npm run build`
- `git diff --check`
- 使用本地浏览器检查评测中心的桌面与窄屏布局、预检、运行、结果和错误状态。
