# Evaluation 与评分

修改题目、执行环境、评分或结果展示时阅读。运行步骤见 [测评指南](../guides/evaluation.md)，服务协议见 [测评生命周期](evaluation-service.md)。

## 评分对象与证据

工具组织执行，题库规定输入与条件，评分方法负责判定。模型回答、工具轨迹、执行事实、真实状态与 LLM Judge 是不同证据。必要的语义判分不能代替实际副作用或资源验证。

题库审查以 [逐题约束](evaluation-cases.md) 为依据；术语见 [测评词汇](evaluation-glossary.md)。固定题库 README 和许可随源资源维护，不手工复制计数到操作页。

## 源码入口

- [src/chatcopilot/evals/agent_tasks](../../src/chatcopilot/evals/agent_tasks)
- [src/chatcopilot/evals/deepeval_engine.py](../../src/chatcopilot/evals/deepeval_engine.py)
- [src/chatcopilot/evals/suites](../../src/chatcopilot/evals/suites)
- [src/chatcopilot/evals/models.py](../../src/chatcopilot/evals/models.py)

## Evaluation 对象与执行边界

Console 按LLM测评、Agent测评、系统测试组织，Agent 任务题库见下一条；旧两个项目套件只保留历史与工程合同验证。现有 `agentstrata-qq-message-flow-v1` 的 7 个 Case 是 legacy Relay/attestation/ACP 合成链，只用于防止旧能力回归，不是新 Gateway 验收或当前推荐部署证据；迁移到 fake OneBot → real Channel → Gateway 前不得称为当前 QQ message-flow 证明。测评只允许 Console 按钮或 CLI 手动启动，默认每 Case 1 次，不接 Git hook、CI、文件/部署/重启回调，不发送真实 QQ。Console 按LLM测评、Agent测评、系统测试三类对象组织，默认 Agent测评；入口内按项目测评/公开基准分组，待接入默认折叠。BFCL 当前模型直测；IFEval 使用独立 PromptPlan 直接调用模型，GAIA/AgentBench 等按实际 Agent 执行归属，QQ 保留 Legacy 合成链路说明；Comparison 保留 CLI/服务入口。基准支持范围、实际执行对象与准备状态必须如实显示；仓库自动化不得描述成真实商用 LLM、真实 QQ、真实 cc-connect/NapCat 或 Canary E2E 通过。

## Agent 任务题库与目录

`agentstrata-agent-tasks-v1` 为唯一当前项目 Agent 套件：64 题，quick/full/security/live/skills/red-team=12/60/11/2/2/12。`agentstrata-capabilities-v1` 与 `project-business-v1` 已退役，原资源仅用于工程合同测试，正式创建和重跑拒绝，历史快照不改写。Case 通过静态 scenario_id 和参数提供隔离环境，不按新 Case ID 分派；输入不携带参考答案或操作脚本。事实验证结构、数值、依赖、真实产物与保护集，必要语义采用 strict GEval，不允许用 native 关闭。多主体评分必须保留全部回合与会话绑定，评分输入只供判分和复核；自然数量允许单位，拒绝操作不要求固定内部术语。版本 6 的模糊题显式允许合理澄清；结果导向题接受可核验的原生读源和宿主文件读回，协议与交付要求保持。逐题审查见 `docs/reference/evaluation-cases.md`。未记录证据、执行或评分异常不能算通过。IFEval 模型直测复用固定官方检查器，约束不丢弃、未知或参数异常失败关闭，旧 Agent 运行不自动转换。源码、许可、数据和修改说明一并打包。红队通过 Case metadata 的 test_category/red_team_surface 分类，不能用普通异常处理题冒充对抗覆盖；新增题必须同时检查攻击效果与正常功能。GAIA/SWE-bench 使用固定官方版本与私有缓存，缺附件或镜像不得标可运行；准备数据不自动下载全部镜像或调用模型。下载凭据使用私有 CHATCOPILOT_HF_TOKEN 或本机登录，不进入题目和产物。规格见 `docs/reference/evaluation.md` 与 `docs/reference/evaluation.md`。

## LLM 官方数据与输出

BFCL 固定 V4 单轮 13 类 3,641 题与官方 AST/相关性核心；IFEval 固定 541 道官方原题、25 种约束及分句资源。默认 full，balanced-100/smoke 显式选择，缺数据不回退。独立 PromptPlan 保存真实 model_request/model_response；模型函数调用不代表工具执行，历史调用仅只读投影。评分异常保留响应，未采集、空响应、截断分别显示。维护更新先取得 Evaluation idle lease，再同步依赖、构建与重启两项服务；不启动付费试跑。规格见 `docs/reference/evaluation.md`。

## AgentBench FC 本地环境

`scripts/prepare_agentbench.py` 仅显式准备固定版本 DB/OS Controller、worker、Redis 与容器环境，不运行模型或训练器。Controller 只发布回环端口；Docker socket 仅供受信 worker 分配隔离环境，任务容器不得挂载宿主数据或凭据。低内存资源配置、AgentRL exec timeout DTO 适配及实际镜像身份必须保留记录，不改上游评分与题目。目录和启动前核验活跃 worker 与准确索引；start_sample 按 messages/tools 契约解析，并核对会话仍存在，interact 必须有真实终态/评分。取消只容忍上游明确的 session not found，不忽略其他错误。规格见 `docs/reference/evaluation.md`。

## Evaluation 引擎与趋势

直接 Agent 轨道统一使用 DeepEval 4.2.2 的用例与指标执行，确定性事实加 Case 声明的默认质量评分；独立评分模型从 Evaluation 配置捕获，不继承被测 Bot 的评分设置。SDK 只在 Trial 内运行，禁用 dotenv、遥测、云上报与缓存，不能接管生命周期。Core 的 observation 仅保存可读执行证据，不进入完整组 checkpoint/resume。公开基准经 DeepEval 分别保存原生评分和 GEval，不以语义分覆盖原生结果；GAIA 使用官方数字、列表、字符串匹配。工作台冻结题单、框架和评分配置；趋势默认按题集、环境及指标协议区分可比条件，探索视图保留自由拆线，Git 为变化元数据；完整性、通过率分母和严格 compare/resume 保持。契约见 `docs/reference/evaluation.md`、`docs/reference/evaluation.md` 和 `docs/reference/evaluation.md`。

## 评测 backend override 例外

只有 Evaluation 执行层可在评测子进程内把同一 Bot 投影为 Codex/Native Target；不得写回 BotSpec、部署实例或复用线上 session。 Profile Case 使用稳定版本化定义，Suite 继续使用官方动态数据和数据准备流程。 Target 必须记录 executor、backend、model、reasoning effort 与包含 Bot runtime 行为摘要的稳定 fingerprint；Case coverage 按 Bot + Case + Target fingerprint 聚合。 Resume 必须在任何写入前核对完整请求、Case 快照、Target fingerprint 和已有 Trial 结构，任一漂移都拒绝；已完成 Evaluation 不可 Resume，未 checkpoint 的 workspace 必须清理后再执行，不能修改请求后复用旧 Trial。 非 Resume 禁止复用已有 Evaluation 证据目录；外部 Case ID 只作领域标识，不得直接形成 workspace 或 artifact 路径，但包含 `/` 时仍须可查询；Evaluation 持久化前必须统一脱敏，禁止落盘原始事件、凭据字段、通用 token、已知 secret 和机器绝对路径；不完整 Target 组不得计入胜负。
