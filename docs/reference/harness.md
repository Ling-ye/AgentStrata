# Harness 修复与验收

Harness 是项目自研的修复与验收体系，在消息链之外组织调查、修复、验证与返工，
接受测评 Case 实例、机器人任务、代码熵治理及应用需求来源。来源冻结、角色权限、失败分类、预算、
候选绑定与最终接受由项目定义，LangGraph 管理图执行与恢复。
[操作指南](../guides/harness.md)说明如何运行；本页维护当前行为与修改边界，
[LangGraph 规格](../../specs/harness-langgraph/spec.md)维护编排架构。

## 核心、项目集成与 Eval

| 部分 | 职责与边界 |
| --- | --- |
| `engine` | 定义状态、修复与验收节点、路由和接受条件；节点通过 `Runtime[HarnessContext]` 使用注入能力 |
| `integrations` | 解释 AgentStrata 来源、角色材料、执行条件和治理要求，不自行签发验收回执 |
| 运行装配 | 创建具体执行器、Verifier、工作区与仓储；`HarnessController` 保持公开集成入口 |
| Evaluation | 拥有题目、原评分及测评结果，通过同 UID 服务客户端读取来源和提交复测 |
| Console / CLI | 使用控制与查询接口，不直接执行图或修改内部存储 |

核心不能直接或经帮助模块、重导出加载 Codex、Gateway、Evaluation client、systemd 或 GitHub 实现。
现有[六层依赖检查](../../specs/domain-layered-dependencies/spec.md)约束领域内部依赖：图和路由属于 Service，
checkpoint 与历史导出属于 Repo，具体执行及装配属于 Runtime。静态检查不代替执行沙箱或恢复验证。

四类图分别使用修复、验收、批次与交付状态。修复结束后由宿主驱动交付；首次验收与主干复验复用
同一验收图，子图每次独立初始化并继承父图检查点。批次和交付等待使用 `interrupt`，由现有定时入口唤醒。
完整拓扑见[编译图](../../specs/harness-langgraph/graphs.md)。

## 问题冻结与来源

故障修复与代码熵任务冻结远端 main、来源、目标、规则及执行条件，未提交修改不进入这些任务的基线。
应用需求冻结操作者提交时的当前源码（包含公开的未提交改动），在独立 worktree 生成本地候选。
原始观测、人工线索与预期分别保存；缺失、截断和过期证据保留缺口，不能缩小原目标或制造标准答案。
[黄金原则](harness-principles.md)及其引用随任务冻结，不受候选修改影响。

| 来源 | 输入与限制 | 验收用途 |
| --- | --- | --- |
| 测评 Case 实例 | 绑定原测评、Case、Target 和重复执行；允许修复提示，不允许替换原答案 | 保留原题与评分，要求有效失败对照 |
| 机器人任务 | 读取 Gateway `run_id` 对应观测；修复提示与预期可空，不能从业务库补造观测 | 沿原请求和产品契约调查，要求有效失败对照 |
| 代码熵治理 | 冻结全仓源码索引与现行 SDD，由 Plan 选择有证据的问题 | 允许全绿基线，要求实际改善及行为保持 |
| 应用需求 | `integrate`/`improve`、实例、明确目标、可写路径及可选固定上游来源 | 冻结目标 Case 或独立新需求测试，证明变化并保护原通过行为 |

### 应用接入与改进

`HarnessController.start_improvement` 接受 `ImprovementRequest`，CLI 的 `integrate` 与 `improve`
使用同一控制入口。实例 runtime 与模型由 Evaluation 服务的只读预检解析并冻结，编程 worker 使用自己的
`harness` 模型方案。平台对接仍由 Channel、Gateway 客户端和工具承担，图核心不分派平台。
当前实例解析沿用 Evaluation 的仓库 Bot 注册范围；插件安装目录不自动成为可写配置来源。

源码写入限用户显式选择的产品路径；权限、凭据、评分与已有测试仍保持受保护。
`source_envelope` 复用 adapter_forge 的仓库、固定 ref、许可与接入目标契约，作为调查材料绑定到任务和回执。
宿主按批准 commit 读取公共 Git 源码，核对实际 commit，使用现有安全归档提取器生成只读材料，
保存归档和文件摘要；模型不自行选择远端版本。源码读取失败或材料变化时阻断，不能声称已完成调查。
来源声明不授予执行任意上游代码或修改外部仓库的权限；需要 fork 的工程工作另行明确范围。

两个需求类型共享原任务图与验收子图。已有目标 Case 可以选择 `existing`；新增功能由 Test 角色提交
独立 pytest 草案。宿主在相同冻结定义上运行基线、候选及独立确认，并执行应用 Suite 和仓库回归。
不要求已有历史故障记录；新需求检查可以揭示旧版本缺少的能力。原目标已满足且没有可测量差异时，
不签发改善回执。错误、缺证据与原通过行为退步不能由角色说明放行。

需求任务的 `delivery.mode=local`：保存精确候选、补丁和 `AcceptedCandidate`，不启动 GitHub 发布。
冻结源码与当前工作区分离，修改操作者文件或暂存区不是完成任务的一部分。
恢复继续使用现有 LangGraph checkpoint 和操作回执，按原 Evaluation ID 对账；未确认的外部执行不重放。
验证回执保留各次 Evaluation 的版本事实与不含明文凭据的模型身份。基线、候选和独立确认的
runtime、评分实现或模型绑定变化时，不能继续接受。M5 的启用与恢复是独立操作，
不会改写已成立的需求验收结论；见[版本交付](delivery.md#已验收版本启用与撤回)。

Case 修复只接受定义快照完整、实际执行 AgentStrata 的隔离 Suite；通过、跳过的实例、
Profile comparison、dry-run 和 direct-LLM 不进入修复。缺少旧定义时重新执行测评。
其他原失败 Case 不要求一并修好，原通过项仍受保护。

故障修复的基本作用域为 Gateway、Application、Agent 及与目标调用链直接相关的工具、契约与配置。
正式验收复用生产 Gateway 装配，经过身份与准入、Application 会话与策略、Agent 模型与工具及交付协调。
仅外部资源和 Provider 连接使用冻结 fixture；合法准入拒绝可提前结束，同时检查未执行后续层。
原 Case 保留，直接 Agent 或局部工具测试不能替代目标相关的产品回放。

## 按需角色协作

| 角色 | 进入时机与职责 | 写入范围 |
| --- | --- | --- |
| Plan | 首轮直接进入，追踪调用链、提出根因假设和最小方案 | 结构化计划 |
| Main | 根因变化或分歧需要重新判断时进入 | 调查结论与路由建议 |
| Coding | 按绑定计划生成候选 | 获准产品文件 |
| Test | 按需编写或修订验证 | 独立测试草案 |
| Review | 独立审查精确候选、证据和缺口 | 审核产物，候选只读 |

所有角色共用 AgentRunner，使用独立会话与权限，不自行创建嵌套委派。宿主按 `test_first`、
`code_first` 或 `existing` 推进；现成 Case 可复用，完整复现不是开始编码的前置条件。
明确测试错误返回 Test，产品错误返回 Coding，需要重规划时进入 Plan 或 Main。

Plan 用 `proceed/blocked` 区分待验证假设与必要条件缺失。Coding/Test 的局部执行说明和待宿主检查项
放 `notes`，`gaps` 只记录绑定验收目标的材料、fixture 或权限缺失。实际是否完成验证由宿主决定；
检查通过也不能抹去明确的证据或外部交付缺口。Verifier 产出验证定义与结果，不替 Plan 提出根因。

宿主收录草案前核对普通文件、当前用户归属和单链接，并收紧为 `0600`。新回归测试在最终收录路径
先执行 Ruff，再运行 pytest；静态失败不能作为基线债务放行。草案收集或试运行失败保留具体诊断，供 Test 修订。

### 角色上下文

- Plan 首轮读取绑定冻结源码的 SourceIndex：显式路径、失败标识、代码式提示、实现、测试与规则入口按稳定优先级排列，正文最多 16 KiB，标明遗漏数量。索引不限制继续只读调查全仓。
- 人工反馈作为独立证据提供，索引优先提取其中的路径与标识；机器人来源标明 Gateway 装配入口。
- 选定目标后，Coding、Test、Review 只接收相关源码与规则片段；新 attempt 为 Main、Plan、Coding、Test 建立新会话。
- 返工使用最多 8 KiB 的 FailureBrief，列出实际失败阶段、检查、断言、工具与交付事实、代表性输出及基线/上轮/本轮通过数。角色和 Console 使用同一摘要，完整证据按引用读取，摘要不参与验收。
- 接续提供已有草案与原始报告的只读引用，不复制进心跳载荷，也不继承旧验收结论。

## 对照验证与独立验收

### 验证条件

基线与候选在独立冻结快照运行相同验证定义。模型验证每组至少三次，原重复数更高时沿用；
原 Case 与 Agent 回放使用同一冻结快照时只执行一次，保留原条件并采用较高的重复次数要求。
源码、验证定义和执行条件一致才复用基线，Test 修改验证定义后重新执行。

确定性缺陷使用 pytest；模型行为使用由 Evaluation 登记并冻结的声明式 Case。实际 Agent 执行使用
冻结输入、相关上下文与合成文件，不注入参考答案。当前声明式环境支持 `read_text_head`、
`write_workspace_file`、`list_workspace`、`unzip_attachment`、`read_bot_skill`；图片工具需要声明
HTTP/OneBot 依赖 fixture。联网查询和全局状态需要相应受信能力，缺失时报告缺口，不能伪造答案。

宿主在编码前提供能力清单。图片输入物化、后端传图和输出交付分别验证；输入原图不足时等待补图，
要求机器人发送图片不等于必须提供输入原图。真实模型、模拟外部回执和真实 QQ 送达分别记录。

### 接受条件与失败分类

只有宿主能够签发 `AcceptedCandidate`。接受时要求：

- 检查结果绑定当前候选、冻结目标与执行条件，执行 ID、检查 ID 和重复次数矩阵完整一致。
- 故障修复具有有效失败对照；治理满足[实际改善与行为保持](code-health.md#验收与写入范围)要求。
- 原目标、保护集与所需独立确认通过；仓库回归保护基线中的每个通过项，已有失败与新增回归分开记录。
- Review 通过，不存在未解决的必需证据缺口，审核后候选未发生变化。

回执绑定候选摘要、目标、验收证据与审核；完整通过才记为 `fixed`。局部候选及缺口可保留为
`needs_review`，不进入自动交付。修复结果与[交付结果](delivery.md#harness)分别保存。

pytest 标准断言失败与普通 `assert` 一样分类；资源、导入、fixture setup、评分和材料错误不能转成产品失败。
冻结仓库基线无法收集测试时阻断验证环境，不让 Coding 修改产品掩盖问题。跳过或未运行不能记为成功。
原生会话启动或执行异常保留脱敏原因并停止，不归为角色输出错误或无进展返工。

### 回归产物与评分归属

可公开的机器人 pytest 回归使用离线合成材料，经审核后原样收录到 `tests/unit/harness_regressions/`；
后续仓库检查执行该集合。可公开 Agent Case 收录到 `tests/agent_regressions/<digest>/case.json`，
包含私有图片的 Case 留在 Evaluation 私有登记中，已有打包 Case 仍关联原定义。
普通单元测试不隐式调用模型，Agent 回归按[操作指南](../guides/harness.md#再次执行已收录回归)显式执行。

Evaluation 的 `register_case(case)`、`frozen_case(snapshot_id)` 提供不可变定义，Suite 请求用
`case_snapshot_id` 引用，不能由客户端替换。原始测评、机器人标识、日志和账号留在私有存储，
Git 只用修复 ID 与可公开回归标识关联。评分规则以 [Evaluation](evaluation.md)为准。

## 执行适配与隔离

AgentRunner 只接收已绑定角色、工作区、配置和取消检查。宿主统一记录执行轨迹与结构化产物，
会话绑定任务、worktree、模型、凭据代际和环境身份；工作区、验证与交付不属于厂商适配器。
当前 Codex 适配器负责原生二进制、凭据、权限投影和 App Server 启动，会话驱动投影有限角色事件。
扩展执行器须实现该契约，不能从原生会话推断宿主验收、权限或交付成功。

每任务拥有独立 worktree、分支、依赖环境与临时目录。宿主使用冻结依赖声明准备
`agent`、`acp`、`dev`、`evaluation`、`weixin` 依赖，角色只读。隔离 pytest 在无网络、无实例状态
或凭据的副本中使用独立磁盘临时目录，结束或取消后清理，避免耗尽小型 tmpfs。
角色局部 pytest 禁用会初始化网络 socket 的 `pytest-rerunfailures` 插件，正式验证仍由宿主执行。

候选可修改获准产品源码、Bot 提示词及受约束声明；模型/backend、凭据位置、资源授权、既有测试、
评分与当前控制程序固定。候选 Harness 代码不能接管本次验收。Harness 自身治理与受限 Skill 学习
遵循[代码熵正文](code-health.md)，当前任务使用冻结版本，新版本只影响后续任务。

仓库配置拆分后，现有修复开放 `bots/<id>/bot.yaml`、同目录 `agent.yaml` 和 `prompts/`，
两份 YAML 均核对冻结的不变项，不能创建或删除运行包络、改绑 profile 或更换模型及资源授权。
其他命名或目录的 profile 可由配置加载器读取，但不因此进入现有修复的可写范围；外部目标的通用修改范围由后续接入阶段定义。

外层 bubblewrap 按 `ExecutionScope` 挂载：候选根只读，获准目录及精确文件可写，Git 元数据与
受保护文件只读。Codex 的目录级可写根只投影到外层已隔离的命名空间，不开放宿主父目录，也不预建
厂商保护目录；Git 预检与角色执行使用同样处理。沙箱不可用时不能启动模型。

角色原生子命令只读角色运行目录，禁止改写 `config.toml`，禁止读取 `auth.json`；App Server 父进程
可维护自己的状态。角色命令网络关闭，编程禁用联网搜索，编程与 Codex 验证禁用账号连接器和原生图像生成。
模型的 developer/user 信任分区继续遵循[上下文契约](context.md)，不得提升 Bot 指令或来源材料的信任等级。

## 状态、回执与恢复

| 事实来源 | 权威范围 |
| --- | --- |
| LangGraph checkpoint | 图执行位置、轮次、当前状态及不可变产物引用；恢复未完成节点与 pending writes |
| 宿主执行回执 | 角色、验证、交付复验是否发生，及其外部 ID、输入绑定与结果引用 |
| 不可变产物 | 问题、原则、计划、角色正文、候选、逐项验证与审核依据 |

checkpoint 使用私有根下独立 SQLite 文件、稳定 thread_id 和同步写入；依赖对象、凭据、进程与完整日志
不进入图状态。业务库保存任务身份、取消、worker 事实与可查询投影，不维护另一套下一阶段调度逻辑。
Harness 与 Evaluation 各自保存数据库，不跨库写表；数据库错误不能替代执行成功证据。

统一 `operations` 回执有 `started/completed/failed/unknown` 四态，绑定任务、节点、轮次、输入摘要、
外部 ID 及结果引用。操作前登记，结果可靠保存后完成；已完成结果不能覆盖，也不能改写任务生命周期终态。

恢复时，已完成且绑定一致的结果可在 checkpoint 丢失时复用；角色或本地验证终态未知时阻断，
外部 Evaluation 按原 ID 对账。输入或候选变化后不得复用旧结果。checkpoint 不证明外部操作只执行一次，
GitHub 操作继续核对远端身份与交付意图，规则见[交付正文](delivery.md)。

## 修复流程记录

问题、原则、计划与角色输出按不可变产物保存，任务数据库保留可搜索身份和引用。
Console 展示角色、输入、结论与证据；大正文按需读取，命令日志独立分页，不嵌套复制完整历史。
步骤详情提供 prompt、证据、源码索引、失败摘要和命令输出大小计数；上下文告警不降低验收标准，
也不限制必要的只读调查。

`graph` 与 `delivery_graph` 是可重建的公开位置投影，包含检查点身份、下一节点和等待标记。
查询不执行图、探测 worker 或推进任务；原始私有状态与执行回执不直接暴露给前端。
公开字段与证据读取遵循[Console](console.md)及[观测](observability.md)契约。

### 来源与控制接口

| 入口 | 当前含义 |
| --- | --- |
| `GET /api/evals/case-instances/<case-instance-id>` | 读取真实 Case 实例定位；完整旧结果可补定位索引，不改写原成绩或证据 |
| `POST /api/harness/tasks` | 测评来源提交 `case_instance_id` 与修复参数，不另传测评或 Target；CLI 仍可显式提供三者 |
| 机器人任务反馈 | `feedback.repair_hint`、`feedback.expected_behavior` 分别保存线索与预期；测评只允许前者 |
| `POST /api/harness/tasks/{task_id}/image` | 上传原始图片字节以补齐原目标输入 |
| `POST /api/harness/tasks/{task_id}/continue` | 接续前核对冻结身份、剩余预算与外部执行状态，不复用旧验收结论 |
| `GET /api/harness/tasks/<repair-id>/progress` | 只读公开进度，不创建或恢复任务，不改写日志 |

来源在启动后固定，更正反馈须重新发起；旧测评 ID、题库 Case ID、Trial ID 和手工 `evalcase:`
引用不作为 Console 的 Case 实例 ID。缺失定位身份显示未记录。写操作限同源、本机连接，CLI 操作同一用户的 Harness。

## 预算、取消与生命周期

故障修复默认三个完整轮次、累计 3600 秒，全部角色返工共用预算，恢复不重置。
连续两轮没有新增有效证据时停止；尚有预算时允许 Plan 重诊断一次，引用具体失败并提出不同修正行动才继续。
机会、时间和轮次记入持久化反馈。无进展按实际检查项比较，目标内部分改善可以继续；草案由执行错误修正为
有效行为断言属于新证据，只改摘要或随机通过数变化不算进展。

Codex 会话中连续三次相同的已完成命令得到相同退出码与输出时停止本轮；明确标识的状态轮询不计入，
实际文件变更会重置该计数。该保护与跨轮次的无进展判定分别生效。

代码熵批次按[停止条件](code-health.md#停止条件与预算)跨问题累计，每项独立计算尝试上限。
未给 App Server 传入累计时限时，仍保留宿主取消轮询、中断确认与进程退出的有限等待。

### 控制与生命周期

`HarnessController` 提供公开入口，控制 Service 通过端口统一取消、恢复、接续与失联核对；
worker Runtime 持有配置快照、systemd、冻结目录与进程事实。存活判断检查执行锁、修复/交付 unit
及待执行 systemd job。调度 `unknown` 不等于停止，未知时保留取消请求与占用。

修复与交付使用同一 worker 入口和任务 unit，按维护共享锁、任务控制锁、执行锁取得资格。
worker 等待控制锁并在锁内复读资格，只有实际执行者持有执行锁才按重复 worker 退出。
宿主以 systemd `Type=exec` 确认启动，不持控制锁等待 worker 初始化。
取得资格后释放控制锁，执行结束才释放执行锁和维护锁。`queued` 且 `dispatch_state=creating`
表示尚未完成派发，核对不能误判中断；显式取消仍可终止准备。

取消只有在 worker 自行收尾或宿主确认停止后才能完成。Evaluation 的取消请求回执不证明外部执行结束；
失败或未知保留关联与重试资格。事务条件更新防止迟到结果复活已取消任务。

`current_evaluation_id` 属于修复，`delivery_evaluation` 属于交付复验，不双写。
交付取消保留原修复终态，外部终态确认后才清除交付引用；blocked 交付仍可取消，未完成保留
`cancel_pending`。任一测评引用存在时保留占用，禁止清理；交付复验未收尾时禁止恢复或接续修复。

`get/list/progress` 只读。每分钟定时入口先核对活动任务，再处理交付，单项失败不阻止其他任务。
`reconcile` 主动核对；取消、恢复与接续即时检查事实。定时器不可用时不承诺自动更新时间。
Console 初始化失败只影响 Harness 入口；systemd 调度失败不降级为 nohup 或进程内后台任务。
并发不变量见[控制生命周期规格](../../specs/harness-control-lifecycle/spec.md)。

## 协议切换

当前 pipeline 12、图版本 2。旧记录阻止创建新任务；旧数据不迁移、不续跑，使用明确的维护切换。
维护锁与 Evaluation 租约内确认旧 worker、外部测评及 PR 收尾，再用 SQLite backup API 导出数据库，
复制并校验证据、源码、补丁、日志和检查点，最后替换活动库。导出失败不得切换。

退役数据库、旧任务目录、归档、checkpoint、worktree 和分支均保留；临时环境与凭据不进入导出。
原始机器人观测、独立 Evaluation、人工工作区和远端资源保持独立。导出路径与清单摘要随结果返回；
已切换或已有新任务时拒绝再次切换。操作步骤见[维护指南](../guides/harness.md#一次性维护切换)。

## 源码入口

- [任务图](../../src/chatcopilot/harness/engine/repair_graph.py)、[任务节点](../../src/chatcopilot/harness/engine/repair.py)
- [应用需求契约](../../src/chatcopilot/harness/improvement_types.py)、[需求验证](../../src/chatcopilot/harness/improvement_verification.py)
- [验收图](../../src/chatcopilot/harness/engine/acceptance_graph.py)、[交付图](../../src/chatcopilot/harness/delivery_graph.py)、[批次图](../../src/chatcopilot/harness/batch_graph.py)
- [图恢复](../../src/chatcopilot/harness/graph_service.py)、[检查点](../../src/chatcopilot/harness/graph_repository.py)、[历史导出](../../src/chatcopilot/harness/history_repository.py)
- [角色调度](../../src/chatcopilot/harness/integrations/roles.py)
- [源码索引与失败摘要](../../src/chatcopilot/harness/integrations/briefs.py)
- [场景规则](../../src/chatcopilot/harness/integrations/scenarios.py)、[运行依赖](../../src/chatcopilot/harness/engine/context.py)、[执行回执](../../src/chatcopilot/harness/operation_repository.py)
- [修复服务](../../src/chatcopilot/harness/workflow.py)
- [运行装配](../../src/chatcopilot/harness/assembly.py)
- [维护切换](../../src/chatcopilot/harness/cutover_runtime.py)
- [执行契约](../../src/chatcopilot/harness/agent_types.py)、[验证契约](../../src/chatcopilot/harness/models.py)
- [Codex 适配](../../src/chatcopilot/harness/codex_adapter.py)、[会话驱动](../../src/chatcopilot/harness/repair_session.py)、[会话绑定](../../src/chatcopilot/harness/agent_session_state.py)
- [宿主执行包装](../../src/chatcopilot/harness/agent_execution.py)、[验证实现](../../src/chatcopilot/harness/verification.py)
- [生命周期控制](../../src/chatcopilot/harness/control_service.py)、[worker 装配](../../src/chatcopilot/harness/worker_runtime.py)
