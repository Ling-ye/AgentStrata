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
标记绑定实际候选内容与验证记录，不修改原始成绩。显式启用审核提交的新任务，
在复测及只读 AI 审核通过后，由宿主将修复和回归测试一并提交到本地任务分支。

## Design

遵循 [四层运行时基线](../runtime-four-layer-definition/spec.md) 与
[Evaluation 服务边界](../evaluation-service-boundary/spec.md)。Harness 是四层之外的可选
配套模块，独立命令与按需 systemd worker 拥有修复生命周期；Evaluation 只拥有测评与
结果，Console 只组合公开查询和操作。Evaluation 不依赖 Harness，Harness 核心通过
来源、验证、编程和 Git 交付端口协作，适配器不读取 Evaluation 私有状态或直接写它的数据库。

来源适配与执行分开：Evaluation 来源通过公开服务加载失败 Case/Target；机器人任务
来源通过绑定实例的 Gateway 观测 reader 读取 run、事件、有界正文和执行配置。Console
只负责可信实例解析与端口装配，不把任意客户端日志、路径或 Console 运维任务 ID
当成机器人证据。Harness 不回写来源记录，观测缺失、过期、截断或运行未结束均明确
记录自检受阻。历史和来源关联从 Harness 自己的数据库查询，不要求来源服务在线。

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
Agent 行为问题生成声明式 Case，经 Evaluation 公开登记接口校验和内容寻址保存。
Evaluation 受信执行器实际运行 Agent、模型与获准产品工具，在隔离工作区使用冻结
文件；当前提供文本文件 fixture 和工作区／已打包 Skill 工具，其他外部服务需要对应
隔离 fixture，尚未提供时明确受阻。不替换待修逻辑、模型回答或评分结果，不重放生产消息。
评分预期不进入被测 Agent 输入。需要未捕获外部状态时受阻，不伪造复现。

Harness 模块内以 ProblemEvidence、RepairHypothesis、VerificationPlan、VerificationResult、
CandidateRef 交接。worker 装配来源、验证、编程和 Git 交付适配器，核心状态机不选择
具体 Evaluation/pytest 实现；Evaluation 不反向依赖 Harness。验证异常分产品、环境、
评分和证据类别；仅产品行为失败启动或重试修复。机器人和 Case 来源共用仓库单元
回归及架构、配置检查，保留原通过项和历史 Harness 回归。真实模型验证每组默认
至少三次，原 Case 重复数更高时沿用；候选通过后独立确认，条件不变。预算包含准备、
执行、评分、确认、审核和提交，不以耗尽预算为由减少验收。

两种来源均可携带 repair_hint；仅机器人来源可声明 expected_behavior，Case 不能改变
原题及评分。feedback 独立于原始观测保存，参与幂等及候选身份，三阶段作为不可信
任务材料使用。修改后发起新任务。计划、比较结果和有界脱敏检查日志在 Harness 展示。

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

### 复测后审核与本地提交

本轮扩展仅服务新任务：API 的 `review_and_commit` 缺省为 false，控制台明确展示并默认
启用，CLI 显式选择。开关进入任务身份及幂等条件；旧请求、已有记录和运行时快照不
补审、不补提交。所有阶段复用现有 worker、锁、取消和时间预算，不增加服务或历史补录入口。

可公开收录的测试第一次生成即要求合成数据和可移植性。离线 pytest 冻结后在隔离验证副本
按 `tests/unit/harness_regressions/` 中的最终相对路径执行，审核后原样收录，不生成
第二份测试。已有 Evaluation Case 只关联定义身份；正式 pytest 回归随完整测试和
后续 Harness 验收执行。产品修改范围不扩展到测试或 Harness；测试与 Git 写入只由宿主执行。

宿主复用已有来源、前后验证和保护集记录作为证据。候选通过后，编程适配器开启一次
新的只读审核会话，复用修复模型和推理配置；输出 approved/rejected/inconclusive、
问题、理由和证据引用。拒绝或无法确认时停止，不收录、不提交、不自动修订或重审，
保留候选、补丁与草案；AI 判断与机械通过分别展示，不能宣称真实平台已经恢复。
大体积证据保存在任务私有的只读 JSON 文件中，模型通过路径及摘要按需查询完整记录，
不把全量通过清单直接塞入上下文，也不丢弃任何验收项。

审核结果绑定代码、测试、验证记录及范围摘要。提交前执行受信版本的公开边界、敏感
信息和相关代码检查，核对精确文件清单与内容；任何漂移或已有暂存内容均阻断。
宿主只在 `feat/harness-<task-id>` 本地分支创建一个提交，使用受信 Git 身份，提交说明
以 `[AI Harness] 自动修复：` 开头并标记 Generated-by 与公开 Regression-Id。
原始任务 ID、运行日志、账号和机器路径只保留在私有数据库，不进入提交及回归文件。
Git hooks 不参与自动提交；不推送、建 PR、合入 main 或部署。

检查索引及 Git 对象先保存在任务私有目录，只有检查通过才将批准对象导入仓库。
提交前保存意图及预期父提交、文件树、文件清单和说明；恢复时核对已存在的匹配提交，
只补记回执，不能再次提交。任务成功与 accepted attempt 在回执保存后一起落盘；
提交状态、工作区可用性与本地 main 包含状态按实际 Git 事实投影，历史缺失字段不补造。

## Acceptance

- 机器人任务的补充内容可选，提交、持久化、刷新查询及准备／修复／可选审核使用同一快照；
  测评来源允许修复线索并拒绝覆盖参考答案，原始观测不被改写。
- 同请求 ID 修改补充内容报冲突，修改答案不复用旧候选；来源缺失或无法本地复现仍受阻。
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
- 操作者工作区与暂存状态保持；未启用的任务不提交，启用任务仅受控本地提交。
- 审核拒绝或未知时记录原因并保留产物；批准后提交内容与受验收内容一致。
- 重复请求、取消与提交后数据库故障不产生重复提交，正式回归能检测缺陷再引入。

## Verification

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
