# 修复与代码治理

先选择失败来源或代码巡检范围，核对冻结基线、预算和交付条件，再启动受控任务。机制见 [Harness](../reference/harness.md)。

## 单 Case AI Harness

代码主动治理的使用方式见[代码治理](#代码治理)。两类任务共用后台设施和维护窗口。

Harness 是可选的独立模块，通过同 UID Evaluation 客户端读取结果和提交复测，
按需创建 systemd transient worker，没有常驻 Harness 服务。Console 关闭不取消任务。
需要带 Git 元数据的源码仓库、Linux/WSL user systemd、bubblewrap 和原生 Codex 二进制。

首次使用可把 `deploy/wsl/harness.env.example` 复制到操作者的
`~/.config/agentstrata/harness.env`，将目录设为 `0700`、文件设为 `0600`。配置
`CHATCOPILOT_CODEX_BIN` 和已有专用凭据根 `CHATCOPILOT_CODEX_BOT_HOME`；后者的
`worker` lane 必须已经登录。凭据操作沿用本文的 Codex 凭据命令，不使用个人桌面
认证目录。配置文件不执行 shell，仅展开值开头的 `~`、`$HOME`、`${HOME}`；
进程环境优先。`CHATCOPILOT_HARNESS_ENV` 可指定另一份私有配置文件。
Console 在启动时装载 Harness 配置快照；修改后重启 Console，现有 worker 仍使用其冻结运行环境。

默认修复数据库位于用户状态目录的 `agentstrata/harness/<repository-hash>/`，
可通过 `CHATCOPILOT_HARNESS_ROOT` 指定；任务、runtime 快照、补丁和工作区都属于
该目录。使用用户私有的持久目录；当前 Codex 不在系统临时目录下创建原生辅助程序。
可用 `CHATCOPILOT_HARNESS_MODEL` 设置 CLI 默认修复模型。

在源码仓库运行：

```bash
python -m chatcopilot.harness --help
python -m chatcopilot.harness start --evaluation <evaluation-id> --case <case-ref> --target <target-id> --model <codex-model>
python -m chatcopilot.harness list
python -m chatcopilot.harness get <repair-id>
python -m chatcopilot.harness reconcile <repair-id>
python -m chatcopilot.harness cancel <repair-id>
python -m chatcopilot.harness resume <repair-id>
```

`get/list` 只读取已记录的状态，不再顺带把失联任务改成中断。正常进度由 worker 持续写入，
异常退出由现有每分钟 Harness 定时任务核对；没有运行定时器时，执行 `reconcile` 主动核对。
取消显示「取消中」时，不代表进程已经停止；存活未知或外部测评尚未确认结束时保留任务占用，
排除运行环境故障后再次取消或核对即可。恢复与接续会先确认原执行停止。

创建时可设置 `--max-attempts`（默认 3）、`--timeout-seconds`（默认 7200）、
`--reasoning-effort` 和稳定 `--request-id`。继续使用原冻结代码和剩余预算；编程阶段
中断不重放同一 Agent 回合，保留尝试记录。模型与工具调用可能产生 Provider 费用。

在 Console 的「测评中心」打开一次测评，详情顶部可直接复制「测评实例 ID」。
在「测试点结果」中找到失败记录，点击该行的「复制 Case 实例 ID」；无需展开原始
JSON 或手工拼接标识。相同题目在不同测评、不同 Target、不同重复执行下各有独立
的 `case-` 开头实例 ID。复制不可用时，页面提供可选中文本供手动复制。

进入「AI Harness 修复」，选择「测评 Case 实例」，将复制的 ID 粘贴到「Case 实例 ID」
并点击「加载来源」。页面显示所属测评、Case、Target、第几次执行及结果，再设置
参数启动修复。Harness 后端凭此 ID 查询原始记录；前端不能额外指定测评或 Target。
测评 ID、题库 Case ID、旧 Trial ID 和手工 `evalcase:` 引用均不作为此输入。
不存在、已删除、通过或跳过的实例，以及不符合现有条件的来源不能启动修复。

已有完整结果在读取时补齐定位索引，ID 刷新或服务重启后保持；原始成绩、证据和
结果文件不改写，也不批量导入旧文件。缺失执行身份时显示「Case 实例 ID 未记录」。
HTTP API 可通过 `GET /api/evals/case-instances/<case-instance-id>` 查询单个实例；
`POST /api/harness/tasks` 的测评来源只提交 `case_instance_id` 及修复参数。
现有 CLI 仍可显式提供 Evaluation、Case 和 Target 参数。

查看进行中的修复时，打开「AI Harness 修复」中的任务详情，顶部「修复进度」会每两秒更新
当前阶段、准备版本或修复次数、累计用时、最近心跳和公开执行动态。命令输出默认折叠，点击
「命令输出」展开；「刷新进度」同时重新读取状态和动态。任务结束后保留最后一次输出并停止轮询。
心跳超过 30 秒未更新只表示暂未收到心跳，不代表任务已卡死；任务是否停止以其实际状态为准。

公开消息及命令在完成后写入日志，长命令等待期间可能只有心跳更新。动态来源标注准备、修复
或审核轮次；显示「其他阶段的最近记录」时，内容不是当前动作。日志更新时间不是单条动作时间。
页面最多展示最近 20 条事件及有界正文，截断会提示；完整执行归档在执行单元结束后查看。
「尚无公开执行输出」不代表没有执行，读取失败时保留已经展示的内容并可手动重试。
只读 API 为 `GET /api/harness/tasks/<repair-id>/progress`，不创建或恢复任务，也不修改既有日志。

修复绑定被选中的失败实例；模型验证每组至少三次，原重复数更高时沿用。其他已通过
Case 继续作为回归保护集，其他失败 Case 不要求一起修好。先按原条件确认最新远端 main 仍失败；未提交
修改不进入基线，当前通过则标记「当前未复现」。
仅支持具有完整定义快照、实际执行 AgentStrata 的隔离 Suite；Profile comparison、
dry-run 与 direct-LLM 测评不进入修复流程。旧定义缺失时重新运行测评。

机器人任务来源在页面选择实例并输入 Gateway `run_id`。自检读取只读观测索引及有界
详情，不能从业务状态库补造丢失证据。点击「开始修复」后自动保存材料并启动诊断；缺归档或
部分观测不可用只显示证据缺口，不再提供「保存自检受阻记录」。准备 Agent 在独立草案目录中生成
根因假设与验证草案；产品代码只读。确定性缺陷生成可含多个断言的 pytest 文件，
Agent 行为问题生成由 Evaluation 登记并冻结的声明式 Case。基线先取得有效行为失败，
随后修复；两种来源都运行仓库 `tests/unit` 和架构／配置检查，保护基线中每个通过项。
测试收集、导入、运行环境错误或跳过不能冒充目标失败或成功。需要已安装开发测试
依赖的 Python 环境；缺失依赖应按测试进程错误处理，不能绕过回归验证。
pytest 在无网络、无实例状态或凭据的隔离副本中执行。Agent Case 实际调用模型和产品
工具，冻结输入、相关上下文与合成文件，不注入评分预期。当前声明式环境支持
`read_text_head`、`write_workspace_file`、`list_workspace`、`unzip_attachment` 和
`read_bot_skill`；生产投递、联网查询和全局状态工具需要独立依赖 fixture，不能用假回答
替代。Codex 验证及编程会话禁用账号连接器，编程会话也禁用联网搜索。外部依赖无法本地复现时
保存具体失败证据。准备会自动试运行、审查和修订测试；每版草案及原因在详情中展示，
不需要操作者判断导入、fixture 或异常表达问题。重复失败或预算耗尽时给出技术失败原因。
图片任务先查找同一来源绑定的留存材料，没有可用原图时显示「等待原图」；在详情选择
原图后自动继续当前任务。图片在私有存储中保存，通过真实 Agent 资源入口执行，
下载成功、实际传图和参考答案语义全部通过才可验收。

页面加载机器人任务后，可填写「修复提示」和「参考答案／预期行为」。前者提供待验证
的调查线索，后者记录你希望得到的答案或行为，可附解释与来源线索，默认允许语义等价。
两项均可留空；切换来源会清空，提交失败保留。内容在修复详情及来源证据中可查，
启动后固定；更正时重新发起任务，不沿用旧参考答案下的候选验收结论。旧受阻记录的
「接续修复（累计预算）」带入来源、反馈、诊断和执行选项，创建一次新 worker 并累计旧任务用时；
重复点击返回同一接续任务，旧记录和冻结 worker 保留。需要更改参考答案才使用「重新发起修复」。
补图 API 为 `POST /api/harness/tasks/{task_id}/image`（原始图片字节），接续入口为
`POST /api/harness/tasks/{task_id}/continue`；均沿用本机同源操作边界。
API 的机器人任务请求可携带 `feedback.repair_hint`、`feedback.expected_behavior`，
测评来源允许 `feedback.repair_hint`，不允许覆盖原参考答案。CLI `start` 也支持 `--repair-hint`。CLI 可以直接指定操作者已确认的实例观测目录，不依赖
Console，并使用下面两个可选参数：

```bash
python -m chatcopilot.harness start-task --bot <instance-id> --run <run-id> --gateway-state-root <instance-state-root> --model <codex-model> --repair-hint '调查输入处理步骤' --expected-behavior '保留输入中的换行'
python -m chatcopilot.harness list --page 2 --search <source-id> --status blocked
```

补充信息用于准备复现、修复与已启用的 AI 审核，不修改原始观测或被测任务输入。
回答质量可使用真实 Agent 与独立语义评分；缺少必要外部状态时明确受阻。
填写参考答案不等于已修复，也不会自动写入机器人知识或记忆。

每个任务创建 `feat/harness-<id>` 分支和专属 worktree。允许修改运行时产品源码、Bot 提示词及受约束声明；
模型／backend、凭据位置、资源授权、测试、评分与控制实现固定。候选进行 Python 语法与 Git diff 检查，并
复测原题单；模型候选还需同条件独立确认。目标和保护集通过才记录「已修复」，其他原失败项可保持失败。此结论仅指
该 worktree 在指定条件下验证通过，远端交付进度另行展示。

新任务统一进行独立审核、公开边界与秘密检查，然后提交和普通推送任务分支、创建目标为 main 的
正式 PR，启用 squash 自动合并。旧 `--review-and-commit` / `review_and_commit` 不再接受。
历史任务、旧补丁和旧 worktree 只读保留，不自动续跑或发布。

机器人复现测试从生成时就采用离线合成数据，在隔离副本按最终回归路径执行，并在
审核批准后原样收录到 `tests/unit/harness_regressions/`；完整 pytest / CI 和后续
Harness 修复会执行其基线版本中的该集合。已有打包 Case 继续关联原定义；登记的
可公开的 Agent Case 原样收录为 `tests/agent_regressions/<digest>/case.json`；包含私有图片的
Case 留在 Evaluation 私有登记中，组合任务只收录合成的确定性回归。普通单元测试不隐式
调用模型。通过显式 `evals run --suite agentstrata-regression-v1 --bot <bot-id>` 运行已收录
回归，或用 `evals run --request <request.json>` 执行包含 `case_snapshot` 的冻结请求。
公开服务客户端提供 `register_case(case)`、`frozen_case(snapshot_id)`；启动 Suite 的请求
使用 `case_snapshot_id`，服务读取自己的不可变登记数据，不接受客户端替换冻结内容。
原始机器人/测评标识、日志和账号只保存在私有数据库；Git 只使用本次修复 ID 和可公开回归标识关联。

### Harness PR 交付配置与恢复

在操作者私有 `harness.env` 中配置 `CHATCOPILOT_HARNESS_GITHUB_REPOSITORY`、
`CHATCOPILOT_HARNESS_GITHUB_ACTOR`、`CHATCOPILOT_HARNESS_GITHUB_TOKEN_FILE`、
`CHATCOPILOT_HARNESS_GIT_AUTHOR_NAME` 和 `CHATCOPILOT_HARNESS_GIT_AUTHOR_EMAIL`。
无秘密示例见 `deploy/wsl/harness.env.example`。token 另存为当前用户拥有、非符号链接、单链接、
0600 文件；需要目标仓库 Contents/Pull requests 读写，以及读取检查结果和分支保护的权限。
宿主在启动前及交付前核对实际 GitHub actor，不继承 Bot 的凭据。

仓库应启用 squash、auto-merge，并配置 main 的必需检查。管理员只在设置阶段开启仓库 auto-merge，
交付身份不绕过保护。安装每分钟一次的短时对账任务（先查看渲染结果）：

```bash
.venv/bin/python scripts/install_harness_delivery_timer.py --dry-run
.venv/bin/python scripts/install_harness_delivery_timer.py
python -m chatcopilot.harness get <repair-id>
python -m chatcopilot.harness retry-delivery <repair-id>
python -m chatcopilot.harness retry-cleanup <repair-id>
```

Console 安装会在定时器启动成功后才显示完成。若仅对账定时器启动失败，可单独重跑上面的
`install_harness_delivery_timer.py` 安装命令，无需重启 Console 或 Evaluation。用
`systemctl --user status agentstrata-harness-delivery.timer --no-pager` 查看定时器状态，
用 `journalctl --user -u agentstrata-harness-delivery.timer -u agentstrata-harness-delivery.service -n 80 --no-pager` 查看加载和执行错误。

PR 创建并核实提交后，本地工作区与任务分支会清理；日志、源码快照、回归证据和 Git 恢复档案保留。
失败任务也先归档再清理，归档损坏、现场外部改动或仍在执行时停止删除。主干前进时自动恢复任务
工作区、合入最新 main、复验和审核，通过后普通推送；冲突时保留 PR 并报告受阻。
PR 合并或关闭后，只删除仍指向登记提交的任务远端分支。对账在 Console 关闭后仍可工作。

取消在发布前阻止外部交付，已有 PR 时关闭自动合并并保留 PR；与合并竞争时以 GitHub 回执为准。
界面分别显示修复、交付和清理结果。“修复成功”不等于“已合并”；CI 失败不能绕过，也不自动扩大
产品修复范围。新流程不会执行 Git hooks、改写远端历史、更新操作者本地 main 或部署机器人。

测评中心和机器人任务流只展示原始运行事实，修复状态统一显示在 AI Harness 页面。
新增平台测评由 Evaluation 保存到自己的 `results.sqlite3`；
Harness 的 `harness.sqlite3` 只保存任务和尝试，两者不跨库写表。原始日志与附件仍是
文件产物，历史测评不批量入库。数据库故障保留原始执行事实，不能据此确认修复。

前端修复写操作仅接受同源、本机连接；远程维护使用 SSH 隧道。CLI 可直接操作同一
用户的 Harness，不需要打开 Console。systemd 调度失败会记录受阻，不使用 nohup
或进程内后台任务降级。详情含 worker unit、工作区、候选摘要和逐轮验收引用；
先检查任务详情，再检查对应 unit 的 journal。不要在任务活动期间手工改动其工作区。

若旧任务报 `harness.sqlite3-journal` 不存在，应先更新修复了 SQLite 辅助文件检查竞态的
服务；这个文件由 SQLite 随事务创建和删除，不应手工创建或删除。更新不会替换旧任务的
冻结 worker。准备阶段已中断且没有生成草案时，用「重新发起修复」带入原来源、反馈和选项，
确认后启动新任务；保留旧记录、日志和 `reproducer/started`，不要删除标记强行继续。

独立 worker 从私有 `~/.config/agentstrata/harness.env` 读取执行器配置，示例为
`deploy/wsl/harness.env.example`；引用已配置的原生 Linux Codex 可执行文件和专用
worker 凭据目录，不自动继承机器人的 `local.env`。Console 安装包含现有 `dev` 依赖组，
供 Harness 的冻结测试和受信本地提交检查使用；机器人实例仍只安装 `agent + acp`。

## 代码治理

进入 Console「代码治理」，选择全部源码、运行时、控制台（前后端）或文档范围，填写可用的 Codex
模型，点击「开始治理并自动交付 PR」。启动时冻结最新远端 main，包含公开环境示例，排除本地未提交改动；
在预算内逐组优化，每组通过后保存检查点。主表单提供三种停止条件：

- **按修复数量**：默认验收通过 1 个问题组后停止，可调整数量。失败、待判断和重复尝试
  不占成功额度；一个根因组包含多条发现仍只计一组。
- **按发现问题数**：设为 1 时，机械扫描或首个有发现的审计批次返回后，立即停止后续
  巡检，只修最先发现的一个根因组。一批多出的发现完整保留，但不在本轮修复。选中问题
  失败或待判断时结束，不继续寻找替代问题；选中目标之间的依赖按顺序处理，目标之外的
  依赖不自动纳入。有限范围结束而发现不足时，修复实际已发现的问题并说明数量不足。
- **按总时间**：初值 2 小时，到达总时限后停止，不限制成功修复数量。

两种数量模式都不设任务或单次模型/验证执行时间上限。所有参数直接显示在主表单，
推理强度默认极高（xhigh），单个问题最多尝试默认 3 次，可调整。
快照、Harness、检查器的候选实现也可修复，实际宿主与验收标准保持冻结。

执行环境复用上一节的 `harness.env`、原生 Codex 和专用 worker 凭据。可用
`CHATCOPILOT_HARNESS_MODEL` 配置页面默认模型，未配置时在页面填写。需要 Linux/WSL 的
systemd 用户服务、bubblewrap 和项目开发依赖；修改前端时还需要已安装的 Console Node 依赖。
任务共用 Harness 存储及维护窗口，关闭 Console 不取消后台工作。

在详情查看问题依据、当前阶段、公开执行动态和检查日志。候选通过固定检查与独立审查后交付。
普通说明修改由宿主核对实际差异后采用轻量验收，记录定向检查和独立审查；代码、权限或
标准修改仍须标准验证。轻量验收不生成行为测试或运行两轮 fast。
验收结果与停止原因分别显示。「已验证累计补丁」相对于启动时的源码快照，组内补丁相对于上一检查点；不能把原工作区此前的修改
当作此次清理成果。应用补丁前检查当前源码是否仍匹配该快照；新任务通过上述 PR 流程交付。

取消或预算耗尽时保留已经验收的检查点和累计补丁；未验收改动恢复到最近检查点。
「已达修复数量目标」「已达总时限」和执行故障分别显示。达到数量目标只证明已经
交付指定数量的问题组，范围覆盖另行显示。新建接口要求 budget，
旧 timeout_seconds 和 step_timeout_seconds 请求不再接受；旧任务参数、结论和补丁保留，不迁移或续跑。
进程意外停止时保留已持久化检查点；重新启动将创建新快照。不要在任务运行中编辑其候选目录。
如果依赖、检查器或模型执行失败，先看相应阶段日志；失败或待判断项不会标记为已清理。
治理不操作测评结果、不调用机器人。已有验收成果在部分失败或时间耗尽后仍交付；主动取消停止发布。归档、PR 与清理规则同上。
