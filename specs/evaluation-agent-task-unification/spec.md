---
id: evaluation-agent-task-unification
type: architecture
status: implemented
created: 2026-09-12
---

# Agent 任务能力题库与 IFEval 模型直测

## Summary

按已批准审计新建 `agentstrata-agent-tasks-v1`，合并项目业务与 Agent 回归为 58 道任务。66 道旧题中保留/改写 44、并入 11、移出 IFEval 8、退役 2、拆分 1 为 2，再新增 12。默认执行事实主判与必要语义判定。旧套件退出可选目录，历史只读，不自动迁移、重跑或拼接成绩。

## Design

版本 4 的局部修订见 [逐题审查](../../docs/evaluation-agent-task-audit.md)：保留 64 题，修复跨主体评分前文丢失与题意歧义，删除重复的 Skill 缺参题；不改变本规格分层和生命周期，以下首版数量保留为历史事实。

后续 [红队与数据准备规格](../evaluation-red-team-data-preparation/spec.md) 将版本 2 扩为 61 题、完整离线 56 题，并增加独立 8 题红队专项；本规格的 66→58 审计及验证计数保留为首版事实。

遵循 [运行时四层基线](../runtime-four-layer-definition/spec.md)。Console 仍为 UI/BFF，Evaluation Core/service 拥有 Trial、隔离、预算、取消、恢复与产物；受信场景只提供配置、工具、环境与观察，不拥有第二套生命周期。不改 Channel/Gateway/Application/Agent 依赖方向。

- Case 沿用声明式契约，增加 `scenario_id` 与参数。静态场景负责隔离文件、工具、真实记忆/人格、子 Agent 与示例代码环境；禁止按新 Case ID 分派，数据与评分侧期望不得进入 Agent 输入。安全角色、群身份与工作区由宿主绑定。
- 题单为 quick 12、full 53、security 7、live 2、skills 3，默认每题一次。缺必要图片、代码环境、实际 Skill 或 Judge 时预检阻断，不缩减题单。默认必要语义不可通过选 native 模式关闭。
- Console 保留题单中未满足条件的选择，列明 Case 与缺项并禁止启动；预检错误显示在题目区，不改变目录、题目、运行配置的排版。
- 判定验证精确结构、数值、来源、调用依赖、真实产物与保护集；不要求唯一补丁或无关调用顺序。无证据或评分异常记 error；语义高分不覆盖事实失败。冻结场景、题目、seed 与实现指纹，同 Case/attempt 的 Target 使用相同条件。
- 两个旧套件标 retired，目录不展示，正式创建、resume 与重跑拒绝；原资源保留用于工程合同验证。历史只消费原有快照。新 IFEval 的 direct_llm 路径不从旧 Agent 运行自动转换。
- IFEval 固定官方 revision `26d8ccdab6fec61b5c83ad6327ea8bda9e580288`，保留源码、许可与修改说明。直接模型经唯一 PromptPlan 使用独立测评配置，不加载 Bot 风格/人格、记忆、RAG 或工具；记录实际请求与上游条件差异。固定 8 题移入 IFEval；外部数据完整保留每道题的约束，不支持/参数错/资源缺失明确失败。严格为主，宽松与指令指标单列；语言识别异常不得按上游的宽容分支算通过。
- IFEval 官方评分依赖随 evaluation 安装；运行快照保留 Python 与评分依赖版本。独立中性 PromptPlan 满足项目输入契约，与官方空系统提示条件的差异明确记录，不宣称完整官方榜单等价。
- 代码场景为小型纯算术项目，保护用户文件、测试与索引；可接受等价函数，禁止任意导入、反射和环境访问，独立解释器限制 CPU 与内存。服务题使用宿主管理的真实独立示例进程。二者不扩大为任意仓库执行沙箱、AgentStrata 自更新或生产部署能力。
- 迁移清单与题目可读指南记录所有旧题去向和 14 个候选题的输入、场景、证据与验收。新结果不作为真实 QQ、生产部署或完整官方榜单成绩。

## Acceptance

- 新套件恰有 58 道；题单 12/53/7/2/3；66 道旧题均有去向，旧目录不可选，历史不回填。
- 新场景覆盖分页去重、消歧、幂等超时、并发冲突、空/错、交付未知、多跳、群记忆隔离、人格追加、委托冲突、跨模块修复与用户修改保护。
- 无关回答、错误数值子串、错引用、虚假保存与失败冒充零均不得通过；每场景验证正确、错误、缺证据三类轨迹。
- IFEval 不启动 Agent、不提供工具；不忽略约束，严格/宽松分离；迁移前后边界样本与官方检查器比对。
- 相关 Python、前端、架构、公开边界、SDD 与打包检查通过。没有商业模型试跑授权时仅报告本地和受控验证。

## Verification

2026-09-12，本地 Python 3.13.15 与生产前端构建验证：

- `.cache/agent-task-venv/bin/python -m pytest tests/unit -q -k 'eval or agent_task' --basetemp=/tmp/as-evals-complete`：878 passed、2597 deselected、8 warnings、2 subtests passed。覆盖退役与重跑、运行快照、服务生命周期、官方约束、模型路径及场景判定；58 道场景都有正确、错误与缺证据样本。
- `.cache/agent-task-venv/bin/python scripts/check_repo.py fast`：1113 passed、34 subtests passed、2 warnings；SDD、公开边界、架构依赖、配置与组件目录、requirements 同步、Ruff、类型化契约等门禁通过。与上述测评测试存在重叠，不累加测试数。
- `npm --prefix console/web test`：18 files、172 passed；`npm --prefix console/web run build` 通过。浏览器发现缺项过滤和预检错误排版问题后修正，并重新执行前端测试与构建。
- Chromium 使用实际生产构建，API 目录取自实际 Python 目录；覆盖 58 题、五份题单、精确提交、IFEval 8 题及模型入口、选择重置、缺项保留与阻断、历史冻结归属、退役趋势、严格/宽松独立、QQ Legacy 说明与 Canary 不可运行、390px 无横向溢出。没有 pageerror。准备状态和历史分数为明确的受控夹具，POST 被截获，不启动测评。
- 使用 `scripts/build_smoke.py` 验证 wheel、sdist 精确成员、从 sdist 重建及隔离安装运行时。未跟踪候选源码通过独立临时索引和独立对象目录参与验证，真实 Git 索引不变。固定原题、官方评分源码、许可和场景资源均随安装包交付。
- 为新增评分依赖与检查工具建立隔离验证环境，原 `.venv` 与运行服务不改动。运行日志、浏览器脚本、模拟目录和截图保存在 `.cache/agent-task-unification/`。

模型响应与语义判分为受控替身；记忆/人格状态、Native ToolRegistry 执行、算术解释器和示例进程使用真实本地实现。没有进行商业模型试跑、完整官方题库、真实 QQ 或部署验证。新题目的模型区分度、难度和稳定性仍需另行授权的真实试跑。不暂存、提交、部署或重启服务。
