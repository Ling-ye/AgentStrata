# Evaluation 与评分

Eval 是项目自研的评测执行与证据体系，组织 Case、Target、Trial、隔离执行、条件绑定、
服务生命周期与结果比较。具体指标使用 DeepEval、相应基准的评分实现或项目确定性检查器，评测生命周期与原始证据由项目管理。

修改题目、执行环境、评分或结果展示时阅读。运行步骤见 [测评指南](../guides/evaluation.md)，服务协议见 [测评生命周期](evaluation-service.md)。

## 评分对象与证据

工具组织执行，题库规定输入与条件，评分方法负责判定。模型回答、工具轨迹、执行事实、真实状态与 LLM Judge 是不同证据。必要的语义判分不能代替实际副作用或资源验证。

题库审查以 [逐题约束](evaluation-cases.md) 为依据；术语见 [测评词汇](evaluation-glossary.md)。固定题库 README 和许可随源资源维护，不手工复制计数到操作页。

## 源码入口

- [src/chatcopilot/evals/agent_tasks](../../src/chatcopilot/evals/agent_tasks)
- [src/chatcopilot/evals/deepeval_engine.py](../../src/chatcopilot/evals/deepeval_engine.py)
- [src/chatcopilot/evals/suites](../../src/chatcopilot/evals/suites)
- [src/chatcopilot/evals/models.py](../../src/chatcopilot/evals/models.py)
- [桥接执行与独立事实评分](../../src/chatcopilot/evals/bridge_runtime.py) / [评分器](../../src/chatcopilot/evals/bridge_scoring.py)
- [版本快照](../../src/chatcopilot/evals/version_snapshot.py) / [结果对照](../../src/chatcopilot/evals/report.py)

## Evaluation 对象与执行边界

Harness 的正式故障验收使用生产 Gateway 装配的三层回放，规则见 [Harness](harness.md)。
回放仍使用本服务的 Trial 生命周期、评分与候选源码隔离；只替换外部 HTTP/Provider 响应，
声明或输入不能切换为直接 Agent 以绕过产品链路。独立测评的既有执行对象保持原用途。

Console 按LLM测评、Agent测评、系统测试组织，Agent 任务题库见下一条；旧两个项目套件只保留历史与工程合同验证。现有 `agentstrata-qq-message-flow-v1` 的 7 个 Case 是 legacy Relay/attestation/ACP 合成链，只用于防止旧能力回归，不是新 Gateway 验收或当前推荐部署证据；迁移到 fake OneBot → real Channel → Gateway 前不得称为当前 QQ message-flow 证明。测评只允许 Console 按钮或 CLI 手动启动，默认每 Case 1 次，不接 Git hook、CI、文件/部署/重启回调，不发送真实 QQ。Console 按LLM测评、Agent测评、系统测试三类对象组织，默认 Agent测评；入口内按项目测评/公开基准分组，待接入默认折叠。BFCL 当前模型直测；IFEval 使用独立 PromptPlan 直接调用模型，GAIA/AgentBench 等按实际 Agent 执行归属，QQ 保留 Legacy 合成链路说明；Comparison 保留 CLI/服务入口。基准支持范围、实际执行对象与准备状态必须如实显示；仓库自动化不得描述成真实商用 LLM、真实 QQ、真实 cc-connect/NapCat 或 Canary E2E 通过。

## Agent 任务题库与目录

`agentstrata-agent-tasks-v1` 是直接 Agent 任务套件；应用链路另由下述桥接 Suite 验证。本套件含 64 题，quick/full/security/live/skills/red-team=12/60/11/2/2/12。`agentstrata-capabilities-v1` 与 `project-business-v1` 已退役，原资源仅用于工程合同测试，正式创建和重跑拒绝，历史快照不改写。Case 通过静态 scenario_id 和参数提供隔离环境，不按新 Case ID 分派；输入不携带参考答案或操作脚本。事实验证结构、数值、依赖、真实产物与保护集，必要语义采用 strict GEval，不允许用 native 关闭。多主体评分必须保留全部回合与会话绑定，评分输入只供判分和复核；自然数量允许单位，拒绝操作不要求固定内部术语。版本 6 的模糊题显式允许合理澄清；结果导向题接受可核验的原生读源和宿主文件读回，协议与交付要求保持。逐题审查见 `docs/reference/evaluation-cases.md`。未记录证据、执行或评分异常不能算通过。IFEval 模型直测复用固定官方检查器，约束不丢弃、未知或参数异常失败关闭，旧 Agent 运行不自动转换。源码、许可、数据和修改说明一并打包。红队通过 Case metadata 的 test_category/red_team_surface 分类，不能用普通异常处理题冒充对抗覆盖；新增题必须同时检查攻击效果与正常功能。GAIA/SWE-bench 使用固定官方版本与私有缓存，缺附件或镜像不得标可运行；准备数据不自动下载全部镜像或调用模型。下载凭据使用私有 CHATCOPILOT_HF_TOKEN 或本机登录，不进入题目和产物。规格见 `docs/reference/evaluation.md` 与 `docs/reference/evaluation.md`。

## Agent 与外部桥接评测

`agentstrata-application-bridges-v1` 复用现有 Suite、Case、Target、Trial、进程监督与结果存储。
所选 Bot 的 Agent profile 决定 LangGraph 或 Codex 与模型，每个 runtime 分别建立基线。
平台细节由静态注册的 `application-bridge` 插件组织，不进入 Harness 核心，也不新增桥接注册中心。

执行链使用生产 Gateway 装配、实际 OneBot Channel、仓库薄 Live2D 客户端和 MCP 动作工具。
浏览器动作接收方与 OneBot 连接是隔离本地对端；对话默认调用所选真实模型。
测试可以替换模型响应，此时只能证明对应工程契约。Suite 只读取当前执行源码树内的受管适配模块，
缺少源码时预检阻断；不能转而加载操作者机器上另一套已安装插件。纯 wheel 安装尚不提供这套源码环境。

每题临时装配自己的工作区、会话和凭据，限制工具为题目所需能力，不使用生产 QQ 连接、记忆或语音设备。
直接 Agent 任务题库负责一般任务能力，桥接题库负责对话结果传递、动作使用及边界；
具体题目和预设由[套件资源](../../src/chatcopilot/evals/suites/agentstrata-application-bridges-v1/README.md)维护。

评分是独立的确定性事实检查，不调用 LLM Judge，也不宣称验证开放式回答质量：

| 判断层 | 实际依据 | 未满足时的语义 |
| --- | --- | --- |
| Agent 完成任务 | 冻结的目标文字、模型调用观测、动作工具名与参数 | 有充分执行证据的错误回答或动作选择记为 `agent_error` |
| 桥接遵守契约 | Gateway 回合/会话、客户端原文、权限拒绝、实际动作与回执绑定 | 输出损坏、越权或协议违背记为 `bridge_error` |
| 对端执行或交付 | OneBot 请求及 provider ack、浏览器动作请求及匹配回执 | 缺失或未知的必要执行证据为 `evidence_missing`，不判通过 |
| 环境与执行可用 | 启动、连接、模型协议与结构化异常 | 已识别环境故障为 `environment_error`；模型执行异常单列，不猜测产品是否改善 |

QQ 未知交付题专门验证“如实保留未知且不盲目重发”，该题通过不代表消息已交付。
模型回答、Gateway 完成、provider acknowledgement 与用户可见结果仍是不同证据。
真实浏览器渲染、语音和 QQ 接收方交付按主计划 M7 另行验收。

## 版本绑定与基线候选对照

服务以既有 `code_source` 接收同仓库工作区及其源码摘要，复制隔离源码，覆盖宿主受信评分实现。
Core 从服务拥有的请求和源码回执生成 `config_snapshot.version_snapshot`；环境变量或插件 metadata
不能授权版本身份。快照包含提交源码与实际执行源码摘要、验证实现摘要、Target fingerprint、
runtime 依赖版本和参与的桥接组件哈希。配置与权限沿既有 Target/conditions 绑定；Codex 执行还绑定原生二进制内容。
检查执行期间的源码和 runtime 漂移，发生漂移即停止，不能继续形成完整可比结果。
模型服务端未公开的精确版本仍为未知，保留既有 `model_version_note`，不由模型名称推定。

普通 standalone 结果保留原有严格对比。产品源码变化导致 Target fingerprint 变化时，只有两份有效版本快照、
相同 runtime 身份、模型、权限、环境、题目、评分条件和完整 Trial 样本才能做代码候选对照。
Trial 必须绑定自身结果中的 Target，不能将旧 Trial 搬入新版本；题目或评分变化需重建双方基线。
改变 runtime 或模型的实验单独记录，不能宣称是同条件代码优化。

对照输出 `improved`、`unchanged`、`regressed` 或 `inconclusive`。任何已观察到的用例退步都使整体为
`regressed`，不能由其他题增分抵消。错误、跳过或缺证据的样本记入 `inconclusive`，其恢复不计为改善；
存在此类样本时不提供整体 `score_delta`。CLI 对应退出码为退步 `1`、仅证据不足 `2`、改善或不变 `0`。
版本快照是内容绑定记录；对照结果不是 `AcceptedCandidate`，退出码 `0` 不授权启用。
Harness 独立验收和版本启用仍分别由 M4、M5 推进。

## LLM 官方数据与输出

BFCL 固定 V4 单轮 13 类 3,641 题与官方 AST/相关性核心；IFEval 固定 541 道官方原题、25 种约束及分句资源。默认 full，balanced-100/smoke 显式选择，缺数据不回退。独立 PromptPlan 保存真实 model_request/model_response；模型函数调用不代表工具执行，历史调用仅只读投影。评分异常保留响应，未采集、空响应、截断分别显示。维护更新先取得 Evaluation idle lease，再同步依赖、构建与重启两项服务；不启动付费试跑。规格见 `docs/reference/evaluation.md`。

## AgentBench FC 本地环境

`scripts/prepare_agentbench.py` 仅显式准备固定版本 DB/OS Controller、worker、Redis 与容器环境，不运行模型或训练器。Controller 只发布回环端口；Docker socket 仅供受信 worker 分配隔离环境，任务容器不得挂载宿主数据或凭据。低内存资源配置、AgentRL exec timeout DTO 适配及实际镜像身份必须保留记录，不改上游评分与题目。目录和启动前核验活跃 worker 与准确索引；start_sample 按 messages/tools 契约解析，并核对会话仍存在，interact 必须有真实终态/评分。取消只容忍上游明确的 session not found，不忽略其他错误。规格见 `docs/reference/evaluation.md`。

## Evaluation 引擎与趋势

直接 Agent 轨道统一使用 DeepEval 4.2.2 的用例与指标执行，确定性事实加 Case 声明的默认质量评分；独立评分模型从 Evaluation 配置捕获，不继承被测 Bot 的评分设置。SDK 只在 Trial 内运行，禁用 dotenv、遥测、云上报与缓存，不能接管生命周期。Core 的 observation 仅保存可读执行证据，不进入完整组 checkpoint/resume。公开基准经 DeepEval 分别保存原生评分和 GEval，不以语义分覆盖原生结果；GAIA 使用官方数字、列表、字符串匹配。工作台冻结题单、框架和评分配置；趋势默认按题集、环境及指标协议区分可比条件，探索视图保留自由拆线，Git 为变化元数据；完整性、通过率分母和严格 compare/resume 保持。契约见 `docs/reference/evaluation.md`、`docs/reference/evaluation.md` 和 `docs/reference/evaluation.md`。

## 评分连接恢复

Evaluation Judge 对同一评分 API 请求最多尝试三次，重试前分别等待 0.8 秒、1.6 秒。
仅连接异常、请求超时和 HTTP 408、429、5xx 可以重试；鉴权、参数、响应格式错误及正常低分不重试。
恢复只调用评分模型，不重跑被测 Agent、工具或 Trial；SDK 与 DeepEval 的额外自动重试关闭。
单次请求沿用 Judge timeout，全部尝试仍受已有 Trial/Evaluation 期限及取消监督约束。

评分证据中的 `judge_attempts` 记录请求序号、尝试序号、耗时、结果和受控错误字段
（异常类型、HTTP 状态、底层异常类型和系统错误码），不记录请求头、密钥、私有端点或响应正文。
既有成功调用计数和 token usage 含义不变。耗尽重试后保留 `judge_error`、未评分的语义结果和
已完成的确定性检查、执行证据；不会把评分不可用解释为产品通过，也不改写历史测评。

## 评测 runtime override 例外

只有 Evaluation 执行层可在评测子进程内把同一 Bot 投影为 Codex/LangGraph Target；不得写回 BotSpec、部署实例或复用线上 session。Profile Case 使用稳定版本化定义，Suite 继续使用官方动态数据和数据准备流程。Target 必须记录 executor、runtime_id、model、reasoning effort 与包含 Bot runtime 行为摘要的稳定 fingerprint；Case coverage 按 Bot + Case + Target fingerprint 聚合。Resume 必须在任何写入前核对完整请求、Case 快照、Target fingerprint 和已有 Trial 结构，任一漂移都拒绝；已完成 Evaluation 不可 Resume，未 checkpoint 的 workspace 必须清理后再执行，不能修改请求后复用旧 Trial。非 Resume 禁止复用已有 Evaluation 证据目录；外部 Case ID 只作领域标识，不得直接形成 workspace 或 artifact 路径，但包含 `/` 时仍须可查询；Evaluation 持久化前必须统一脱敏，禁止落盘原始事件、凭据字段、通用 token、已知 secret 和机器绝对路径；不完整 Target 组不得计入胜负。
