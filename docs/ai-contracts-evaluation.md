# Evaluation 与 Harness 契约

本文由根协作入口按任务引用。先定位与本次变更有关的章节，再读取对应规格；跨领域变更需同时读取有关契约。

返回 [AI 协作入口](../AGENTS.md)；用户操作见 [文档中心](README.md)。

本地 DeepEval trace 归档与现有 Trial/checkpoint 分开，执行单元结束后由所属宿主写入；
完整上下文在 Evaluation 摘要投影前采集，分块经受监督 IPC 交给 Core。新 Harness 来源
冻结归档及正文，旧无归档记录只供历史导出，不补造内容。契约见
[本地执行记录规格](../specs/local-deepeval-tracing/spec.md)，操作见 [本地执行记录](local-traces.md)。

## 章节索引

- [单 Case Harness 独立性](#rule-1)
- [Evaluation v2 结果链路](#rule-2)
- [Evaluation 独立生命周期](#rule-3)
- [Evaluation artifact 所有权](#rule-4)
- [Evaluation mutation 交付](#rule-5)
- [Standalone Evaluation 隔离](#rule-6)
- [Evaluation 对象与执行边界](#rule-7)
- [Agent 任务题库与目录](#rule-8)
- [LLM 官方数据与输出](#rule-9)
- [AgentBench FC 本地环境](#rule-10)
- [Evaluation 引擎与趋势](#rule-11)
- [Evaluation Trial 监督](#rule-12)
- [评测 backend override 例外](#rule-13)

<a id="rule-1"></a>

## 单 Case Harness 独立性

- **单 Case Harness 独立性**：`chatcopilot.harness` 拥有按需 worker、任务与尝试数据库；
  通过来源、验证、编程和 Git 交付适配器协作，Evaluation 不反向依赖 Harness。测评数据库由
  Evaluation 服务校验并保存新增结果，历史文件不批量迁移；Console 组合查询两个模块，
  不跨库写表。修复入口、进度和历史只在并列的 AI Harness 页面展示，测评页和机器人
  任务流不管理修复状态。Evaluation 为每条 Case 执行记录提供稳定的 `case_instance_id`，
  绑定测评、Case、Target 与重复执行序号并维护定位索引；测评页可复制测评及 Case 实例
  ID，不能发起修复或轮询 Harness。Console Harness 只提交 Case 实例 ID，由服务端
  经 Evaluation 公开查询获取来源并复检失败状态；不恢复手工拼接引用或客户端指定
  测评/Target。日常任务只读绑定实例的 Gateway 观测，冻结来源、根因假设与验证计划；
  确定性缺陷用 pytest，Agent 行为用 Evaluation 登记的声明式 Case 实际运行模型与隔离
  产品工具。准备阶段产品只读，修复阶段测试只读；环境、评分和证据错误不能冒充产品失败。
  两种来源共用仓库单元与架构回归；模型每组至少三次且在验收前独立确认，预算不因阶段拆分重置。
  Case 来源允许 repair_hint，但不得覆盖原预期；原始观测与评分不改写。
  机器人任务可携带可选 `feedback`（修复提示、参考答案／预期行为），与原始观测分开
  保存并随任务冻结，贯穿准备、修复和可选审核，参与请求幂等及候选复用身份。
  参考答案是用户期望，不是已验证事实或宿主策略，不能替代真实本地复现；修改后发起新任务。
  候选测评必须加载实际冻结源码与获准 BotSpec／提示词变更；模型/backend、身份和资源授权
  固定，候选配置身份与比较不变量分开。Case、评分与控制实现保持受信版本，评分预期不进入
  被测 Agent。Agent 回归收录至 tests/agent_regressions/<digest>/case.json，经显式测评执行，
  单元测试不得隐式调用商用模型。
  worktree 目标及保护集验收通过才可接受候选，原始成绩不改写。新任务显式启用
  review_and_commit 后，一次只读 AI 审核批准且宿主检查通过才允许将修复与冻结测试
  一并提交至本地任务分支；拒绝或无法确认时保留产物并停止，旧记录不补审或补提交。
  规格见 `specs/evaluation-case-harness/spec.md`。

<a id="rule-2"></a>

## Evaluation v2 结果链路

- **Evaluation v2 结果链路**：单题统一通过 `trial_runner` 获取执行观测并调用评分器，插件不能组装最终 Trial。
  `models` 与 `result_codec` 是结果和编解码的唯一契约；无错误为 null，未评分分数为 null。
  执行和评分快照经既有 observation 通道保存，结果校验异常不能丢失先前已验证证据或冒充 Judge 异常。
  单题失败/异常继续；结果契约、协议、权威持久化和清理失败停止整批，完整 Target 组检查点规则保持。
  预期只从受信题目/fixture 投影，运行前冻结，不注入被测模型；结果页只读当时快照。
  旧记录只归档导出，不迁移、补判、恢复或作为 Harness 来源；新来源排除测评系统故障。
  部署更新同时保护 Harness 创建/恢复和 Evaluation 维护窗口。规格见 `specs/evaluation-result-pipeline/spec.md`。

<a id="rule-3"></a>

## Evaluation 独立生命周期

- **Evaluation 独立生命周期**：Agent Profile 对比和 BFCL / GAIA / IFEval Suite 只使用 `Evaluation`，以 `kind: comparison | suite` 区分；`chatcopilot.evals.application` 与本机 `chatcopilot.evals.service` 是活动 claim、受管 worker、lifecycle state 和更新 maintenance lease 的唯一 owner。Console 只是通过同 UID Unix socket 调用服务的 UI/BFF，禁止在 `console.*` 中恢复 Evaluation manager、worker supervision、进程内 fallback 或旧 import facade。Console 启停和重启不得发送 worker 信号或改写 Evaluation 终态；运行代码更新必须在与创建相同的跨进程锁内原子证明 idle 并持久化 maintenance marker，整个构建、Evaluation 重启、UDS health 和 Console 重启窗口都拒绝新 Evaluation，结束后才释放；服务不可达、状态不明或已安装 unit 未运行时 fail closed。Console 页面触发自身更新时只允许 `systemd-run --user` 创建独立 transient unit；`setsid` / `nohup` 仍属于 Console service cgroup，禁止作为降级路径，transient unit 无法创建时必须在运行更新脚本和获取 maintenance lease 前失败。服务不可用时 BFF 明确返回 `503`，不得降级为本地 manager。

<a id="rule-4"></a>

## Evaluation artifact 所有权

- **Evaluation artifact 所有权**：创建必须先完成无副作用预检，阻断时返回结构化 `code/message/checks`，不创建报告目录或子进程。Application 唯一写 `request.json`、`state.json`、活动 claim 和取消标记；Evaluation Core 唯一写 `result.json`、`summary.md`、`progress.jsonl` 和逐 Trial 证据；managed worker 只写脱敏 `run.log`。Worker 必须等待父子启动握手，只有 PID 同时持久化到 state 与 claim 后才能执行 Core；握手前 service 退出时 worker 必须自行退出。受管进程退出前禁止删除、重跑或为同 Bot 创建下一条；worker PID 只有在 argv 精确包含内部 managed-worker 模块、且唯一 `--output` 与 Evaluation 目录规范路径匹配时才可发送信号，身份不明时 fail closed。Evaluation 根的既存祖先、目录、claim、取消标记和权威 artifact 必须拒绝符号链接，并校验 owner、inode 类型、`0700` / `0600`、单硬链接、记录 ID 与 containment。评测数据统一位于 `reports/evals/evaluations/<evaluation-id>/`，禁止恢复 `/api/evals/experiments`、`/api/evals/runs` 或第二套报告根。

<a id="rule-5"></a>

## Evaluation mutation 交付

- **Evaluation mutation 交付**：`start` / `rerun` / `cancel` / `delete` 必须在任何 mutation 前由 UDS server 返回绑定 request ID、operation 和 Evaluation ID 的 accepted 帧；未成功发送 accepted 时不得 dispatch。`start` / `rerun` 使用 client 生成的稳定 Evaluation ID 和规范请求指纹实现同请求幂等恢复，同 ID 请求漂移必须 conflict；accepted 后断线只能用同一 ID 有界查询或重试，禁止产生身份未知的重复 Evaluation。Suite 官方数据准备在显式子进程内使用私有环境快照，不得在下载期间修改全局 `os.environ` 或长期持有进程级环境锁。

<a id="rule-6"></a>

## Standalone Evaluation 隔离

- **Standalone Evaluation 隔离**：`evals run` 默认写入 `reports/evals/manual/<evaluation-id>`，允许显式 `--output`，并拒绝写入 `CHATCOPILOT_EVALUATION_ROOT` 或默认 `reports/evals/evaluations/` 受管根；standalone/CI 记录使用 `reports/evals/manual/` 等独立目录，不能绕过 service claim 写受管 artifact。

<a id="rule-7"></a>

## Evaluation 对象与执行边界

- **Evaluation 对象与执行边界**：Console 按LLM测评、Agent测评、系统测试组织，Agent 任务题库见下一条；旧两个项目套件只保留历史与工程合同验证。现有 `agentstrata-qq-message-flow-v1` 的 7 个 Case 是 legacy Relay/attestation/ACP 合成链，只用于防止旧能力回归，不是新 Gateway 验收或当前推荐部署证据；迁移到 fake OneBot → real Channel → Gateway 前不得称为当前 QQ message-flow 证明。测评只允许 Console 按钮或 CLI 手动启动，默认每 Case 1 次，不接 Git hook、CI、文件/部署/重启回调，不发送真实 QQ。Console 按LLM测评、Agent测评、系统测试三类对象组织，默认 Agent测评；入口内按项目测评/公开基准分组，待接入默认折叠。BFCL 当前模型直测；IFEval 使用独立 PromptPlan 直接调用模型，GAIA/AgentBench 等按实际 Agent 执行归属，QQ 保留 Legacy 合成链路说明；Comparison 保留 CLI/服务入口。基准支持范围、实际执行对象与准备状态必须如实显示；仓库自动化不得描述成真实商用 LLM、真实 QQ、真实 cc-connect/NapCat 或 Canary E2E 通过。

<a id="rule-8"></a>

## Agent 任务题库与目录

- **Agent 任务题库与目录**：`agentstrata-agent-tasks-v1` 为唯一当前项目 Agent 套件：64 题，quick/full/security/live/skills/red-team=12/60/11/2/2/12。`agentstrata-capabilities-v1` 与 `project-business-v1` 已退役，原资源仅用于工程合同测试，正式创建和重跑拒绝，历史快照不改写。Case 通过静态 scenario_id 和参数提供隔离环境，不按新 Case ID 分派；输入不携带参考答案或操作脚本。事实验证结构、数值、依赖、真实产物与保护集，必要语义采用 strict GEval，不允许用 native 关闭。多主体评分必须保留全部回合与会话绑定，评分输入只供判分和复核；自然数量允许单位，拒绝操作不要求固定内部术语。逐题审查见 `docs/evaluation-agent-task-audit.md`。未记录证据、执行或评分异常不能算通过。IFEval 模型直测复用固定官方检查器，约束不丢弃、未知或参数异常失败关闭，旧 Agent 运行不自动转换。源码、许可、数据和修改说明一并打包。红队通过 Case metadata 的 test_category/red_team_surface 分类，不能用普通异常处理题冒充对抗覆盖；新增题必须同时检查攻击效果与正常功能。GAIA/SWE-bench 使用固定官方版本与私有缓存，缺附件或镜像不得标可运行；准备数据不自动下载全部镜像或调用模型。下载凭据使用私有 CHATCOPILOT_HF_TOKEN 或本机登录，不进入题目和产物。规格见 `specs/evaluation-agent-task-unification/spec.md` 与 `specs/evaluation-red-team-data-preparation/spec.md`。

<a id="rule-9"></a>

## LLM 官方数据与输出

- **LLM 官方数据与输出**：BFCL 固定 V4 单轮 13 类 3,641 题与官方 AST/相关性核心；IFEval 固定 541 道官方原题、25 种约束及分句资源。默认 full，balanced-100/smoke 显式选择，缺数据不回退。独立 PromptPlan 保存真实 model_request/model_response；模型函数调用不代表工具执行，历史调用仅只读投影。评分异常保留响应，未采集、空响应、截断分别显示。维护更新先取得 Evaluation idle lease，再同步依赖、构建与重启两项服务；不启动付费试跑。规格见 `specs/evaluation-llm-official-output/spec.md`。

<a id="rule-10"></a>

## AgentBench FC 本地环境

- **AgentBench FC 本地环境**：`scripts/prepare_agentbench.py` 仅显式准备固定版本 DB/OS Controller、worker、Redis 与容器环境，不运行模型或训练器。Controller 只发布回环端口；Docker socket 仅供受信 worker 分配隔离环境，任务容器不得挂载宿主数据或凭据。低内存资源配置、AgentRL exec timeout DTO 适配及实际镜像身份必须保留记录，不改上游评分与题目。目录和启动前核验活跃 worker 与准确索引；start_sample 按 messages/tools 契约解析，并核对会话仍存在，interact 必须有真实终态/评分。取消只容忍上游明确的 session not found，不忽略其他错误。规格见 `specs/evaluation-agentbench-local-environment/spec.md`。

<a id="rule-11"></a>

## Evaluation 引擎与趋势

- **Evaluation 引擎与趋势**：直接 Agent 轨道统一使用 DeepEval 4.2.2 的用例与指标执行，确定性事实加 Case 声明的默认质量评分；独立评分模型从 Evaluation 配置捕获，不继承被测 Bot 的评分设置。SDK 只在 Trial 内运行，禁用 dotenv、遥测、云上报与缓存，不能接管生命周期。Core 的 observation 仅保存可读执行证据，不进入完整组 checkpoint/resume。公开基准经 DeepEval 分别保存原生评分和 GEval，不以语义分覆盖原生结果；GAIA 使用官方数字、列表、字符串匹配。工作台冻结题单、框架和评分配置；趋势默认按题集、环境及指标协议区分可比条件，探索视图保留自由拆线，Git 为变化元数据；完整性、通过率分母和严格 compare/resume 保持。契约见 `specs/evaluation-benchmark-workbench/spec.md`、`specs/evaluation-deepeval/spec.md` 和 `specs/evaluation-progress-history/spec.md`。

<a id="rule-12"></a>

## Evaluation Trial 监督

- **Evaluation Trial 监督**：正式 Trial 必须在独立 `spawn` 子进程执行，期限取 Case timeout 与 Evaluation 剩余 max-wall 的最小值；取消、期限或预算终止并回收 Trial 进程组，Linux/WSL 必须绑定父死保护。只有同一 Case/attempt 的完整 Target 组可写 checkpoint；中断的不完整组及 workspace 必须丢弃，不能参与 resume、compare 或通过率。

<a id="rule-13"></a>

## 评测 backend override 例外

- **评测 backend override 例外**： 只有 Evaluation 执行层可在评测子进程内把同一 Bot 投影为 Codex/Native Target；不得写回 BotSpec、部署实例或复用线上 session。 Profile Case 使用稳定版本化定义，Suite 继续使用官方动态数据和数据准备流程。 Target 必须记录 executor、backend、model、reasoning effort 与包含 Bot runtime 行为摘要的稳定 fingerprint；Case coverage 按 Bot + Case + Target fingerprint 聚合。 Resume 必须在任何写入前核对完整请求、Case 快照、Target fingerprint 和已有 Trial 结构，任一漂移都拒绝；已完成 Evaluation 不可 Resume，未 checkpoint 的 workspace 必须清理后再执行，不能修改请求后复用旧 Trial。 非 Resume 禁止复用已有 Evaluation 证据目录；外部 Case ID 只作领域标识，不得直接形成 workspace 或 artifact 路径，但包含 `/` 时仍须可查询；Evaluation 持久化前必须统一脱敏，禁止落盘原始事件、凭据字段、通用 token、已知 secret 和机器绝对路径；不完整 Target 组不得计入胜负。
