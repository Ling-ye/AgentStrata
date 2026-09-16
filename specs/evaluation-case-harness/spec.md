---
id: evaluation-case-harness
type: architecture
status: implemented
created: 2026-09-11
---

# 独立 AI Harness 修复工作台与测评结果存储

## Summary

控制台提供与测评中心并列的 AI Harness 修复页面，接收 Case 实例 ID 或绑定实例的机器人
任务 ID，每次只处理一个问题。测评中心负责测评，机器人任务流负责运行观测；来源
加载、自检、修复、复测和修复历史全部归 Harness，不在来源页面标记修复进度。
从已完成测评的单个失败 Case 手动发起修复。先确认当前代码仍有问题，再在专属本地
worktree 中调用 Codex，使用原题单复测。目标通过且原有通过项不退化才记录已修复；
标记绑定实际候选内容与验证记录，不修改原始成绩。新任务在复测及只读 AI 审核通过后，由宿主将修复和回归测试一并交付正式 PR，等待 GitHub 自动合并。

## Design

本规格的 Case/机器人失败修复与[代码治理](../code-health-gc/spec.md)共用 Harness 任务设施。
`code_health` 是独立来源和执行流程，同样冻结远端 main，不要求失败 Case；两类任务按
[PR 交付契约](../harness-pr-delivery/spec.md) 统一发布和清理；两类任务共享维护锁、worker、公开进度和 artifact 读取，历史按类型筛选。

遵循 [四层运行时基线](../runtime-four-layer-definition/spec.md) 与
[Evaluation 服务边界](../evaluation-service-boundary/spec.md)。Harness 是四层之外的可选
配套模块，独立命令与按需 systemd worker 拥有修复生命周期；Evaluation 只拥有测评与
结果，Console 只组合公开查询和操作。Evaluation 不依赖 Harness，Harness 核心通过
来源、验证、编程和 Git 交付端口协作，适配器不读取 Evaluation 私有状态或直接写它的数据库。

Console 修复模型下拉复用实例 inspection 的当前有效 Codex 模型和模型档案（含环境覆盖），
加载新来源后默认选择对应机器人当前模型。推理强度默认 xhigh（极高），总时间预算
默认 2 小时，提交接口保持秒单位（7200）；模型配置不可读取时允许重试，不使用历史模型。
机器人来源缺归档时保留结构化 warnings（code/message），允许使用已有观测和反馈开始诊断。

修复详情顶部默认展示「修复进度」：当前阶段、准备修订版本或修复次数、累计用时、最近心跳，
以及最近公开消息和已完成命令。活动任务每两秒刷新；心跳超过 30 秒未更新只提示观测延迟，
不推断卡死或改写任务终态。结束时补取一次最终输出并停止轮询，读取失败保留已有展示并允许手动刷新。

`HarnessController.progress(task_id)` 提供只读 `GET /api/harness/tasks/{task_id}/progress`，
返回 state（ready/empty/partial）、source（id/kind/number/current）、updated_at、events、
truncated 和 message；成功与失败响应均禁止缓存。Console 不直接读取任务目录。数据取自
任务所属准备、准备修订、候选及审核目录的既有 `public-events.jsonl`，优先当前阶段日志，
否则明确标注最近可用日志所属阶段。每次仅读文件尾部 256 KiB，保留最后 20 条完整公开事件，
每条正文合计最多 8 KiB；截断和异常记录明确显示，末尾未完成行等待下一次读取。
目录和文件沿任务归属校验权限、owner、链接及读取身份，允许正常追加；不读取私有 Codex
会话或隐藏推理，不修改任务数据库、worker 冻结代码或源日志。动态不是验收证据，不能替代
冻结复测、独立确认或交付回执；单条事件没有时间戳时不补造，日志更新时间和 worker 心跳分开显示。

来源适配与执行分开：Evaluation 来源通过公开服务加载失败 Case/Target；机器人任务
来源通过绑定实例的 Gateway 观测 reader 读取 run、事件、有界正文和执行配置。Console
只负责可信实例解析与端口装配，不把任意客户端日志、路径或 Console 运维任务 ID
当成机器人证据。Harness 不回写来源记录。运行未结束、来源不匹配、读取失败或证据摘要校验失败在创建前拒绝；
归档及观测缺失、过期、截断作为 warnings 随来源冻结，由准备阶段判断复现实际需要的材料。
界面统一使用「开始修复」，不提供只保存受阻记录的操作；真正无法建立复现时说明缺失材料及影响。历史和来源关联从 Harness 自己的数据库查询，不要求来源服务在线。

每次测评沿用唯一 `evaluation_id`。每条 Case 执行记录新增服务端生成的
`case_instance_id`（`case-` 加 32 位十六进制摘要），稳定绑定 Evaluation、Case、Target
及重复次数序号；重读和服务重启不改变 ID，不同测评、目标和重复执行各有独立 ID。
Evaluation 在自己的 SQLite 中维护轻量定位索引，详情读取时幂等补齐已有记录的索引，
不改写原始结果、成绩、证据或冻结条件，也不批量导入历史文件。缺失执行身份的记录
不能生成 ID；索引失败不能展示无法查回的 ID。删除测评同时删除索引。

Evaluation 提供按实例 ID 查询的公开服务及 Console 只读 API，查询先解析索引，再
核验原始来源和精确 Trial 绑定，索引不能代替当前来源证据。测评详情直接展示可复制
的测评实例 ID，结果表与 Case 详情展示可复制的 Case 实例 ID，不增加修复控制或
Harness 轮询。Console Harness 只输入并提交 Case 实例 ID；来源加载和启动都由后端
经公开服务解析，前端不能指定所属测评、Case 或 Target。未知 ID、通过/跳过实例及
受阻来源不启动。修复来源记录原始实例 ID 与 Trial，沿用同 Case/Target 的原重复
次数和回归保护集合，其他失败 Case 不要求一并修好。修改输入或来源类型立即清除
旧预览，通过请求代次校验忽略迟到响应。旧 `evalcase:` 输入与前端拆解逻辑删除，
机器人任务来源及现有内部按三字段调用的修复端口保持。

日常任务在只读产品的准备阶段生成问题假设与验证草案。来源证据、操作者反馈、
根因假设和验证计划分别冻结。确定性缺陷可生成一个包含多个相关断言的 pytest 文件；
Agent 行为问题生成声明式 Case，可与确定性测试组成同一计划，经 Evaluation 公开登记接口校验和内容寻址保存。
Evaluation 受信执行器实际运行 Agent、模型与获准产品工具，在隔离工作区使用冻结
文件；提供文本 fixture、来源作用域绑定的私有图片引用和工作区／已打包 Skill 工具，其他外部服务需要对应
隔离 fixture，尚未提供时明确受阻。不替换待修逻辑、模型回答或评分结果，不重放生产消息。
评分预期不进入被测 Agent 输入。原图缺失时等待补图，补充后继续同一任务，不伪造复现。
草案先试运行并自动审查、修订，再冻结有效计划；异常记录阶段、类型、稳定代码和因果链。
每轮版本及失败证据保留，相同草案不重复执行；技术失败不要求用户判断技术方案。
验收项固定完整参考答案，图片还需物化、实际视觉发送回执及语义评分全部通过。
候选产生后修订草案需恢复原基线重验，再恢复精确候选重验，不重放编程调用。

Harness 模块内以 ProblemEvidence、RepairHypothesis、VerificationPlan、VerificationResult、
CandidateRef 交接。worker 装配来源、验证、编程和 Git 交付适配器，核心状态机不选择
具体 Evaluation/pytest 实现；Evaluation 不反向依赖 Harness。验证异常分产品、环境、
评分和证据类别；仅产品行为失败启动或重试修复。机器人和 Case 来源共用仓库单元
回归及架构、配置检查，保留原通过项和历史 Harness 回归。真实模型验证每组默认
至少三次，原 Case 重复数更高时沿用；候选通过后独立确认，条件不变。预算包含准备、
执行、评分、确认、审核和提交，不以耗尽预算为由减少验收。

两种来源均可携带 repair_hint；仅机器人来源可声明 expected_behavior，Case 不能改变
原题及评分。「接续修复」为旧记录保留原来源、反馈、选项及诊断，创建幂等的新 worker 并累计用时；
旧记录不改写。补图在本任务内原子领取一次继续执行，私有原图不进入公开回归或 Git。
feedback 独立于原始观测保存，参与幂等及候选身份，三阶段作为不可信
任务材料使用。修改后发起新任务。计划、比较结果和有界脱敏检查日志在 Harness 展示。

新任务使用 pipeline_version 5（独立存储锁协议），旧冻结 worker 不原地恢复；SQLite
表结构与 user_version 保持不变。自动修订与组合验收仍遵循 `specs/harness-autonomous-validation/spec.md`。确定性来源的目标检查只包含冻结复现断言；普通仓库单元与
静态检查走与 Agent 来源相同的 repository_baseline / repository-verify 通道。目标跳过或
执行错误仍阻断；仓库基线的 skipped 保留原状态，不计入通过或保护集合，原通过项在候选中
失败或跳过都算退化。仓库执行环境错误仍阻断。旧 worker 快照、任务和候选不迁移或复用。

候选包含产品代码和受约束的 BotSpec 四面声明及提示词资源，模型/backend、凭据、
资源授权和运行包络固定。Evaluation 将实验不变量与候选产品配置摘要分开，加载
实际候选配置；测试、评分、执行中的 Harness 及宿主控制保持受信版本。
离线回归保留 tests/unit/harness_regressions；声明式 Agent 回归收录到
 tests/agent_regressions/<case-digest>/case.json。公开收录仅含经过检查的合成材料，
真实对话和运行证据留在私有存储。真实模型回归通过显式 Evaluation 入口执行，
普通单元测试不调用商业模型。

本次只改变新建任务；已有记录与冻结 worker 不迁移、不补判、不补提交。仍一次处理
一条 run 或一个 Case，不增加持续巡检、跨任务聚类、生产投递或控制器自修改。

新增测评的结构化请求、Trial、结果和采集详情由 Evaluation 服务校验后幂等写入自己的
SQLite 数据库；文件仍承载 Core 的原始证据与恢复检查点。历史文件只读，不批量导入。
结果入库失败不改变原始执行结果，重启可对已登记的新记录补写。Harness 的独立 SQLite
只保存任务和尝试，保存证据引用和必要快照，不建通用问题库，不做跨库事务。

共享 Core 的 PrivateDatabase 按 SQLite 的文件生命周期检查辅助文件：`-journal`、
`-wal`、`-shm` 使用一次不跟随符号链接的属性读取，只有该读取的 FileNotFoundError
按“当前不存在”处理。其他连接提交或关闭时可能正常删除辅助文件，不能因此中断 worker。
存在的辅助文件仍须满足普通文件、当前用户所有权、私有权限和单硬链接；主库的存在及
打开前后身份检查保持严格。事务中的其他文件错误、权限/磁盘错误、数据库损坏继续失败，
不通过重放事务、模型或工具调用重试；不修改日志模式或数据库结构。

通用候选测评接口接收同仓库受管 worktree 的内容身份与调用方稳定请求 ID；服务冻结
源码后启动隔离 worker。Case、评分及测评实现使用服务的受信版本，不能由候选覆盖；
每轮创建独立 Evaluation，保留代码、定义、配置与产物身份。定义不兼容或来源漂移时
明确阻断，不将修改代码后的运行当作原 Evaluation 的 resume。

修复任务绑定来源 Evaluation/Case/Target，默认从当前仓库 HEAD 创建分支。保护集合是
来源批次已通过项与当前基线新增通过项的并集；其他原失败项不要求修好。模型验证至少三次并保持各阶段条件，默认最多三个候选，预算在启动时固定。重试恢复本任务基线，保留失败补丁。
验收期间工作区冻结，结果绑定候选摘要。未复现、受阻、取消和失败与已修复分别记录。

同一来源条件、失败特征及代码基线的活动任务幂等返回；已有已验证补丁提供历史关联，
新失败不能被旧修复标记覆盖。已修复仅表示 worktree 验收通过，前端展示未合入状态。
取消和继续核验任务进程与候选身份，已提交的子测评用稳定 ID 查回，不盲目重放。
后台 worker 脱离 Console cgroup，运行时源码使用私有冻结副本；Agent 只可写候选产品
源码，凭据由租约提供，验收、Harness、测试定义和 Git 控制目录不可被它修改。

### 复测后审核与 PR 交付

新任务必须完成独立只读审核。公开回归首次生成即要求合成数据和可移植性，冻结后按最终路径执行，
审核后原样收录；带私有图片的 Case 留在私有资源库。拒绝或无法确认时不发布，先保全证据再清理。

交付完全遵循 [Harness PR 交付契约](../harness-pr-delivery/spec.md)：从远端 main 创建分支，
宿主提交精确验收内容并普通推送，创建正式 PR、启用 squash 自动合并；主干前进时先复验，
保留取消、人工干预及失败边界。编程 Agent 不获得 GitHub 凭据或 Git 写权限。
修复结果、远端交付、可恢复归档和资源清理分别保存，旧任务只读，不提供升级或补交付入口。

## Acceptance

- 未结束的编程调用追加完整公开事件后，在下一次成功轮询可见；准备版本、修复与审核来源准确。
  验证阶段显示上一阶段日志时明确标注，终态补取最后一次输出；空日志、半行、截断和读取失败不误报进展。
- 进度读取不创建目录、不改变任务或尝试状态；越界、符号链接、硬链接及非私有文件被拒绝，
  隐藏推理与额外私有字段不进入接口。桌面和窄屏可查看消息与折叠命令输出。

- 机器人任务的补充内容可选，提交、持久化、刷新查询及准备／修复／可选审核使用同一快照；
  测评来源允许修复线索并拒绝覆盖参考答案，原始观测不被改写。
- 同请求 ID 修改补充内容报冲突，修改答案不复用旧候选；来源无效或无法本地复现仍受阻；仅缺归档不能提前拒绝诊断。
- 页面切换来源清空补充内容、提交时禁用、失败时保留，桌面与窄屏可输入并查看完整内容。
- 不启动 Harness 时普通测评、入库和查询正常；关闭 Console 不终止 Harness。
- 新测评入库保留原始成绩、工具轨迹、错误和证据，历史记录仍可查询。
- 当前版本未复现时不启动编程执行器；目标和保护集通过才标记已修复。
- 复测实际加载冻结候选源码，评分与 Case 不能被候选更改。
- 重复请求、断线与恢复不产生重复活动任务或子测评，失败入库可幂等补写。
- 不同失败特征不误合并，旧修复与新失败同时可见，失效补丁不自动复用。
- CLI、Console、数据库和 worker 共享相同公开控制入口与状态语义。
- 测评页直接复制测评实例 ID 和每条 Case 实例 ID，不导入或轮询 Harness；Harness 只凭 Case 实例 ID 查回来源并启动单 Case 修复。
- 相同 Case 的不同测评、Target 和重复执行有不同实例 ID；刷新、重启和查询模式不改变 ID，原始结果不改写。
- 未知、非法、已删除或非失败实例不能启动，也不自动替换目标；服务端拒绝客户端额外指定测评或 Target。
- 输入修改与迟到响应交错时，预览和提交只对应当前实例 ID；机器人任务来源继续可用。
- 机器人任务的测试准备与产品修复拥有不同写入范围，测试冻结后不得改变验收标准。
- 操作者工作区与暂存状态保持；新任务仅由受信宿主执行获准的 PR 交付。
- 审核拒绝或未知时记录原因并保留产物；批准后提交内容与受验收内容一致。
- 重复请求、取消与提交后数据库故障不产生重复提交，正式回归能检测缺陷再引入。

## Verification

修复进度最小优化验证（2026-09-14）：

- 进度读取与 Console 路由定向测试 48 passed，覆盖运行中进程持续追加、半行写入、阶段归属、
  截断、异常文件、未知任务、禁止缓存及任务状态保持；前端 Harness 单元测试 29 passed。
- 受控 API 的真实浏览器检查 9 组通过，覆盖轮询、展开输出、读取失败保留内容、心跳延迟、
  验证阶段来源标注、终态最终读取与迟到响应、手动刷新、任务切换及 1360px/390px 布局。
- 本机三条既有任务以只读方式成功取得最近公开日志；未修改 worker 冻结代码或启动真实模型修复。
- 最终 `PYTHONPATH=src .venv/bin/python scripts/check_repo.py full` 为 12/12 检查通过，
  Python 4000 passed、1 skipped、154 subtests passed，生产构建和变更秘密扫描通过。
  初次 full 因开发环境缺少 pip 停止；使用 ensurepip 离线补齐后重新通过，项目依赖版本未调整。
  未提交文件通过临时索引参与打包验证，真实索引摘要保持不变。完整记录位于
  `.cache/harness-progress/full-verified/`，浏览器记录位于 `.cache/harness-progress/browser-result.json`。
  本轮没有部署 Console，也不代表真实模型或 QQ 端到端修复验收。

SQLite 辅助文件竞态修复验证（2026-09-13）：

- 新增真实 SQLite 提交/关闭连接与属性检查的确定性交错、文件安全边界、事务回滚、三进程状态更新/查询，以及 Harness 取消检查、心跳、Console 轮询和 Evaluation 结果保存回归；定向集合 59 passed。
- 同一组交错测试的旧实现负向对照为 6 failed、6 passed；修复后全部通过，证明覆盖了原始竞态。
- `check_repo.py full` 全部 12 项通过；Python 为 3909 passed、1 skipped、10 warnings、154 subtests passed，561.35 秒。
- 正式 Console/Evaluation 在空闲维护窗口更新并恢复，原任务记录、日志、冻结 worker、worktree 和暂存区经摘要核对保持不变；生产日志模式仍为 delete，quick_check=ok。
- 正式环境解释器的三类辅助文件交错探针全部通过，业务回调各执行一次。模拟编程流程不等于真实模型修复；本轮没有调用模型或重跑原任务。

本轮启动与回归通道修复验证（2026-09-13）：

- 最终 `check_repo.py full` 全部 12 项通过；Python 为 3871 passed、1 skipped、10 warnings、154 subtests passed，584.96 秒。前端为 21 files、190 tests passed，生产构建通过。
- 新增来源缺口启动、硬阻断不保存伪任务、目标与仓库回归分离、基线跳过项不冒充通过、原通过项变成 skipped/error 阻止验收等定向覆盖。
- 真实 Codex 在独立合成任务中自动生成并冻结 3 个 pytest 目标检查，复现缺陷并修改一个产品文件，目标复测 3/3 通过。仓库基线 3735 项中 3730 通过、1 失败、4 跳过；候选为 3731 通过、4 跳过，regressions=[]，最终 fixed，候选摘要复核一致。未启用提交，没有提交或合入。
- 桌面与窄屏受控浏览器验证重新发起、反馈保留、同请求重试、活动来源拒绝；部署后的真实 Console 也验证了旧记录信息带入和开始按钮可用，没有提交原记录给模型。
- 现有实例、Evaluation 与 Console 已在维护窗口更新并恢复；旧记录、用户原有文件改动及暂存区保持。真实模型验证仅使用合成来源，不代表真实 QQ/NapCat 消息端到端验证。

证据驱动闭环实施验证（2026-09-13）：

- `PYTHONPATH=src .venv/bin/python scripts/check_repo.py full` 的 12 项检查全部通过；
  Python 为 3803 passed、1 skipped、10 warnings、154 subtests passed，545.34 秒。
  包含公开边界、架构、SDD、Ruff、类型契约、组件目录、依赖、wheel/sdist 隔离验证和前端构建。
- `npm --prefix console/web test`：19 files、183 tests passed；生产构建通过。
  生产前端与受控 API 的桌面及 390px 交互无 pageerror 或横向溢出。
- 最后补充大体积证据文件读取后，Harness 质量、来源和 Agent 验证定向集合为
  61 passed，126.21 秒；Ruff、Harness mypy 和 SDD 通过。
  真实 Codex 读取约 3.95 MB 的只读证据并正确拒绝目标仍失败的记录。
- 异常路径覆盖 Judge 错误不触发产品修复、普通单元回归阻止接受、多个冻结断言、
  声明与输入漂移、预期隔离、独立确认失败、审核拒绝，以及取消／恢复和提交后数据库故障。
- 独立提示词候选只改变合成身份名，经真实 Evaluation 装配前后各三次；基线三次返回
  原名字、候选三次返回新名字，输入没有提供新名字，模型及权限等固定条件一致。
- 隔离仓库注入文本预览大小写缺陷，真实 Agent 使用 GPT-5.5 调用实际产品工具。
  来源与 Harness 基线各三次出现有工具证据的目标失败；AI 生成一行通用修复，
  候选和独立确认各三次通过，只读 AI 审核批准，六项宿主提交检查通过。
  基线保护集 3696 项全部保持，候选通过项增至 3698；一个预先选定模型与硬编码默认名
  的断言冲突，以及四个环境跳过项单独记录，不计作通过。
  宿主创建一个本地任务提交，包含实际验收的产品修复与冻结 Agent Case；提交树、
  精确文件集合、单提交数量和干净工作区均已核验，任务最终为 fixed。
- 本机 Codex alpha 与 GPT-5.6 Terra 曾出现 MCP 目录已登记但模型没有调用工具的现象；
  对应任务最终失败且无提交，不用这些记录证明大小写修复。成功闭环从新任务启动前
  固定 GPT-5.5，未在同一任务中更换模型或降低验收次数。

报告、日志和浏览器产物位于 `.cache/harness-agent-implementation/`，真实执行原始记录
保存在任务私有存储。该证据证明合成问题的真实模型、产品工具和本地修复交付闭环，
不代表生产 QQ 投递、部署或普遍模型质量提升。操作者工作区没有暂存或提交，真实
index 与已有无关脚本内容哈希保持不变。以下为历史验证记录。

2026-09-11，在独立 worktree 使用 CPython 3.13.15 与本地 Node.js 20.20.2 验证：

- 私有临时索引下的 `python scripts/check_repo.py full`：12 项全部通过；
  全量 Python 为 3433 passed、1 skipped、10 warnings、121 subtests passed。
  包含 wheel/sdist 精确成员及隔离安装验证、架构、SDD、类型、Ruff、公开边界和前端构建。
  主工作区及开发分支真实 index 的 SHA-256 前后相同；未执行提交或推送。
- Harness、Console BFF、Evaluation 协议/读取与架构定向集合：72 passed。
  包含真实隔离 Git worktree、候选源码导入、受管 dry-run 子进程与 SQLite 存储，
  并使用受控编程/测评端口验证修复、回归拒绝、中断恢复、幂等与取消。
- `npm --prefix console/web test`：16 files、154 tests passed。
- `bash scripts/check_secrets.sh changes`：私有候选索引、工作区及未忽略候选变更扫描通过。
- 新模块 mypy 检查通过；架构检查确认 Evaluation 不反向依赖 Harness，核心不直连私有执行器。
- 实际生产前端配合受控 API 完成 1360px 与 390px 浏览器交互检查：正确选择 Case/Target、
  提交修复、展示验证状态与报告、再次发起，未出现 pageerror。截图和结果位于
  工作区 `.cache/harness-verification/`。

这些结果不包含真实 Codex/商用模型修复、systemd worker 的实际部署运行或真实 QQ
端到端。

后续修复 QQ 合成预检的分流错误：通用 Suite 预检原先将 QQ 确定性 driver 也送入
DeepEval，导致出现 `judge.quality.enabled must be explicitly declared`。现在与执行器
保持一致，仅为 `agent_isolated` / `agent_configured` 执行 Agent 评分预检；未更改 QQ
Case、断言或质量评分标准。QQ quick/full/security 预检及非 dry-run 合成 full 7/7
已通过，受管候选源码与结果入库回归一并覆盖。

本次跟进验证：QQ/Harness 定向回归 33 passed；仓库 `fast` 9 项检查全部通过，
其中核心测试 1097 passed、2 warnings、34 subtests passed。真实 index 前后哈希不变。
非 dry-run QQ full 的 7/7 报告保存在工作区 `.cache/qq-preflight-fix/qq-full/`，
仍只证明本地合成链路，不代表真实 QQ 或商业模型端到端。


独立工作台跟进验证（2026-09-11）：

- 仓库 `full` 的 12 项检查全部通过；Python 全量为 3455 passed、1 skipped、
  10 warnings、121 subtests passed。真实主工作区与 worktree 的 index 哈希前后相同。
- 前端独立模块 16 files、156 tests passed，生产构建通过。
- 实际 bubblewrap + pytest 子进程验证了：环境变量隔离、产品只读、取消终止子进程、
  基线断言失败、冻结测试在候选代码上通过，以及原有单元测试结果逐项保留。
  Gateway 真实观测数据库读取前后文件内容相同。编程准备/修复使用受控执行器，
  不将这些检查描述成真实 Codex 模型修复。
- 受控 API 下的浏览器检查覆盖 1360px / 390px、两种来源、Case/Target 选择、
  自检与复测记录、刷新恢复选中任务、明确结果后的新请求，以及测评页无 Harness 请求。
- 验证报告及浏览器截图位于工作区 `.cache/harness-independent/`；没有部署或真实 QQ
  发信，没有调用商用模型。原 QQ 合成预检修复保留并由完整回归覆盖。


复测后审核与本地提交验证（2026-09-11）：

- 仓库 `full` 12 项检查全部通过；Python 为 3482 passed、1 skipped、10 warnings、
  154 subtests passed。私有临时索引承载候选检查，开发分支及主工作区真实 index 哈希未变化。
- Harness 审核、提交、来源、Console、QQ 和架构定向回归 91 passed；新增验证包括只读
  审核、拒绝/未知后停止并保留产物、正式路径上的冻结测试、产品与测试同一提交、
  已有暂存/外部修改阻断、Git 成功后数据库中断恢复且不重复审核或提交。
- 前端 16 files、157 tests passed，生产构建通过；受控 API 浏览器验证默认开关、审核
  问题/理由/证据、本地提交与 main 状态、两种来源及 1360px/390px 布局，未出现 pageerror。
- 使用受控编程、审核和测评执行器，实际运行公开边界、Gitleaks 与 Ruff 检查，并在
  临时克隆仓库创建单个本地 Git 提交。该验证不调用真实商用模型，不提交开发分支，
  不推送、创建 PR、合入或部署。结果及截图位于 `.cache/harness-quality/`。

最后收紧了提交检查边界：提交说明使用公开 Case 描述或测试文档说明；索引与对象
先在任务私有目录构建，提交元数据也进入同一敏感扫描。批准后只导入最终提交所需
对象。新增断言证明被拒绝的候选不会写入仓库对象库；最后的 12 项质量回归及 mypy
通过，真实扫描的正常提交与合成敏感提交说明的拒绝场景均通过。

单 Case 引用输入验证（2026-09-12）：

- `npm --prefix console/web test`：17 files、174 tests passed；新增引用解析与精确匹配
  覆盖编码、裸 ID、空字段、不同测评/Target、不存在目标和无失败结果。
- `npm --prefix console/web run build`：TypeScript 与生产构建通过。
- `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_harness_sources.py tests/unit/test_harness_console.py tests/unit/test_case_harness.py -q --basetemp=/tmp/agentstrata-harness-case-reference-pytest`：42 passed。
- `PYTHONPATH=src .venv/bin/python scripts/check_repo.py fast --report-dir .cache/harness-case-reference/fast-complete`：
  9 项检查通过，核心测试为 1111 passed、2 warnings、34 subtests passed。首次检查因
  本地虚拟环境缺少 Ruff 停止；补齐 Ruff 0.16.4 与 mypy 1.20.2 后完整通过，依赖清单未改动。
- 实际生产前端配合受控 API 在 1360px / 390px 下完成 12 组浏览器检查，无 pageerror
  或页面横向溢出。覆盖引用拒绝、精确目标提交、受阻来源、迟到响应、来源切换、机器人
  任务提交和测评页无 Harness 请求；结果与截图位于 `.cache/harness-case-reference/`。

这些结果只证明本地代码与受控接口交互，未启动真实修复 worker、调用商业模型或部署。

Case 实例 ID 与复制入口验证（2026-09-12，替代上述组合引用输入）：

- `npm --prefix console/web test`：17 files、166 tests passed；生产构建通过。
- `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_evaluation_read_models.py tests/unit/test_harness_sources.py tests/unit/test_harness_console.py tests/unit/test_case_harness.py tests/unit/test_evaluation_service_protocol.py -q --basetemp=/tmp/agentstrata-case-instance-final`：78 passed。
  覆盖旧结果索引、原始字节不变、不同测评/Target/重复次数、重启、并发、索引故障、删除、
  精确来源复检，以及真实本地 UDS/HTTP 查询和 Harness 证据快照、请求幂等、通过项拒绝。
- `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_evaluation_console.py tests/unit/test_evaluation_execution_capture.py tests/integration/test_evaluation_service.py -q --basetemp=/tmp/agentstrata-case-instance-evaluation`：84 passed、2 warnings。
- `PYTHONPATH=src .venv/bin/python scripts/check_repo.py fast --report-dir .cache/harness-case-instance/fast-final`：9 项通过；核心测试 1113 passed、2 warnings、34 subtests passed。
- 新增身份、结果索引和 Harness 来源适配器的 mypy 定向检查通过。架构检查保持
  Harness 仅依赖 Evaluation 公开服务，不读取其私有身份实现或数据库。
- 生产前端配合本地 Evaluation 生成的受控数据，在 1360px / 390px 完成 12 组浏览器检查：
  测评 ID 及 Case ID 实际剪贴板复制、拒绝剪贴板时手动复制、只提交 ID、原始引用与
  通过项拒绝、迟到响应及机器人来源。无 pageerror、无页面横向溢出。
  报告及截图保存在 `.cache/harness-case-instance/`。

验证不调用商业模型，不运行真实修复 worker 或部署；实际创建的 Harness 记录位于
测试临时目录，使用 `launch=False`。未暂存、提交或推送操作者工作区。

机器人任务修复提示与参考答案验证（2026-09-12）：

- `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_harness_console.py tests/unit/test_harness_sources.py tests/unit/test_harness_quality.py tests/unit/test_case_harness.py -q --basetemp=/tmp/agentstrata-harness-feedback-pytest`：79 passed。
  覆盖可选字段、非法输入与来源拒绝、CLI 参数、持久化与重读、原始证据不变、请求冲突、
  活动任务及已验收候选的复用身份、三阶段传递，以及来源缺失／无法本地复现时受阻。
- 随后将编程适配器检查扩展为准备、修复、审核三个阶段；
  `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_harness_quality.py -k coding_stages -q --basetemp=/tmp/harness-feedback-prompts-final`：3 passed、11 deselected。
  实际渲染的 PromptPlan 只在不可信任务材料中包含补充文本，三个阶段保持原写入范围。
- `npm --prefix console/web test`：18 files、179 tests passed；
  `npm --prefix console/web run build`：TypeScript 与生产构建通过。
- `PYTHONPATH=src .venv/bin/python -m mypy src/chatcopilot/harness`：16 个源码文件通过。
- `PYTHONPATH=src .venv/bin/python scripts/check_repo.py fast --report-dir .cache/harness-feedback/fast`：
  9 项全部通过，核心测试 1113 passed、2 warnings、34 subtests passed。
- 既有 Playwright 驱动生产前端与受控 API，在 1360px / 390px 完成 12 组交互检查：
  多行输入、可选提交、提交时禁用、失败保留及重试幂等、修改答案发起新请求、旧记录
  刷新回显、证据接口展示，以及切换任务／机器人／来源类型时清空。无 pageerror 或页面
  横向溢出，桌面表单及窄屏表单、详情截图已逐项查看。产物位于 `.cache/harness-feedback/`。

本次补齐本地虚拟环境缺失的已声明开发检查工具，未更改依赖清单。操作者的暂存区和
已有无关脚本改动哈希前后相同；测试中的 Git 写入只发生在临时测试仓库。
这些检查不包含真实模型回答质量、真实修复 worker 或 QQ 端到端验证，也没有部署。
