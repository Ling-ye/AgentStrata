# 运行 Harness 需求任务、修复与代码熵治理

先配置执行环境，再选择问题来源。故障修复与代码熵任务冻结远端 main，在独立 worktree 修复；
完整验收后由宿主提交、推送、创建正式 PR 并启用 squash 自动合并。未提交修改不进入基线，
应用需求通过 `integrate`/`improve` 冻结当前源码并生成本地候选，不依赖 GitHub 交付。
任务不会自动部署机器人。机制与限制见[修复契约](../reference/harness.md)。

## 前置条件

| 条件 | 准备方式 |
| --- | --- |
| 源码与隔离环境 | 带 Git 元数据的源码仓库、Linux/WSL user systemd、bubblewrap；开发依赖见[开发指南](development.md) |
| 编程执行器 | 原生 Linux Codex 二进制、已有专用凭据根及已登录的 worker lane |
| Evaluation | 同 UID 的 Evaluation 服务可用，按[测评指南](evaluation.md)检查健康状态 |
| 模型方案 | 在 Console「模型配置」保存 worker Codex 方案，绑定 `harness` 和 `code_health` 用途 |
| GitHub 交付 | 故障修复与代码熵任务需完成下文[交付配置](#harness-pr-交付配置与恢复)；本地需求候选不需要 |

Harness 按需创建 systemd transient worker，没有常驻 Harness 服务。关闭 Console 不取消任务；
CLI 可直接操作同一用户的 Harness。前端写操作只接受同源、本机连接，远程维护使用 SSH 隧道。
模型、工具与第三方服务调用可能产生费用。

### 配置 worker 与模型方案

将 [harness.env.example](../../deploy/wsl/harness.env.example)复制到操作者私有的
`~/.config/agentstrata/harness.env`，目录权限为 `0700`，文件为 `0600`。填写原生二进制路径
`CHATCOPILOT_CODEX_BIN` 与专用凭据根 `CHATCOPILOT_CODEX_BOT_HOME`；worker 登录与查询沿用
[Codex 认证指南](services.md#codex-main--worker-认证)，选择 `worker` lane，不借用个人桌面认证目录。

模型、推理强度及认证引用统一在[模型配置](../reference/configuration.md#llm)维护。
CLI 的 `--profile` 选择已保存方案；省略时，普通修复使用 `harness` 绑定，代码熵任务与定时使用
`code_health` 绑定。模型参数不再通过旧环境变量或独立 CLI 参数覆盖。

Harness 配置文件不执行 shell，仅展开值开头的 `~`、`$HOME`、`${HOME}`，进程环境优先。
`CHATCOPILOT_HARNESS_ENV` 可指定另一份私有配置文件。worker 不自动继承机器人的 `local.env`；
Console 启动时读取配置快照，修改后重启 Console，已有 worker 继续使用自己的冻结配置。

默认状态根位于用户状态目录的 `agentstrata/harness/<repository-hash>/`，可用
`CHATCOPILOT_HARNESS_ROOT` 指定私有持久目录。根目录、`jobs` 与已有 `archives` 会收紧为 `0700`；
符号链接或其他用户拥有的路径会被拒绝。Codex 的 home 不应置于系统临时目录。
worker 按冻结依赖创建专用环境；uv 不在 PATH 且存在多个安装版本时，用
`CHATCOPILOT_HARNESS_UV_BIN` 指定已安装的绝对路径。

## 发起任务

以下命令在已准备环境的源码仓库运行，占位标识须替换为实际值。可先查看：

```bash
python -m chatcopilot.harness --help
```

### 提交接入或改进需求

选择 Evaluation 服务已登记的仓库应用（`bots/<id>/bot.yaml`）；其 Agent profile 决定被测 runtime 和模型。
已安装插件实例的直接选择与配置编辑留待 M6 工作台对接。以下示例使用既有桥接 Case
作为目标，`--allow-path` 是允许修改的目录或现有文件，可重复传入。应按需求缩小范围：

```bash
.venv/bin/python -m chatcopilot.harness improve \
  --bot lingye-copilot-qq \
  --goal '改进 Live2D 多轮回复传递，保持 QQ 的现有行为' \
  --allow-path integrations/open_llm_vtuber/agentstrata_vtuber \
  --goal-case live2d-dialogue
```

首次接入使用 `integrate`。没有现成目标 Case 时省略 `--goal-case`，由独立 Test 角色建立新需求检查；
应用 Suite 仍作为回归保护。仓库线索可通过 `--source-envelope <json-file>` 提供，字段为
`resource_name`、`source_url`、`approved_ref`、`license_evidence`、`integration_intent`，
沿用 adapter_forge 的来源批准约束。它不会自动 fork、发布或扩大可写范围。

`--profile` 选择开发 worker 方案，`--request-id` 支持同请求重试，预算沿用 `--max-attempts` 与
`--timeout-seconds`。预检和源码冻结失败时保留明确诊断；再次提交应使用新请求 ID，不能改写原任务。
成功时通过任务详情读取候选、对照、独立审核与接受回执。`get/cancel/resume` 继续操作同一任务，
未知执行须先按原 ID 对账。完整验收不等于实例已更新，启用另行处理。

### 修复测评失败 Case

1. 在 Console「测评中心」打开运行记录，找到失败测试点，复制该行的「Case 实例 ID」。
2. 在「AI Harness 修复」选择「测评 Case 实例」，粘贴 ID 并加载来源，核对测评、Case、Target、重复次数及失败结果。
3. 选择修复方案、预算与可选修复提示，启动任务。测评原答案不能被人工提示替换。

Case 实例 ID 以 `case-` 开头，同题不同测评、Target 和重复执行分别定位。不要填测评 ID、题库
Case ID 或旧 Trial ID。页面复制失败时可选择文本复制；显示「Case 实例 ID 未记录」时先检查原始执行材料。
通过、跳过、已删除或不满足[来源条件](../reference/harness.md#问题冻结与来源)的实例不能启动修复。

CLI 继续显式指定测评、Case 与 Target：

```bash
python -m chatcopilot.harness start --evaluation EVALUATION_ID --case CASE_REF --target TARGET_ID --profile HARNESS_PROFILE
```

两种修复入口均支持 `--max-attempts`、`--timeout-seconds`、`--request-id` 与 `--repair-hint`。
预算含所有角色返工，恢复不重置；具体默认值与停止规则见[修复预算](../reference/harness.md#预算取消与生命周期)。

### 修复机器人任务

在页面选择机器人实例与 Gateway `run_id`，加载观测后可填写「修复提示」和「参考答案／预期行为」。
两项均可空；提示提供待验证线索，预期说明目标行为，不能补造已丢失的观测。切换来源会清空反馈，
提交失败会保留。启动后来源与反馈冻结，更正时重新发起。

CLI 可以直接指定操作者已确认的观测目录：

```bash
python -m chatcopilot.harness start-task --bot BOT_ID --run RUN_ID --gateway-state-root INSTANCE_STATE_ROOT --profile HARNESS_PROFILE --repair-hint '调查输入处理步骤' --expected-behavior '保留输入中的换行'
```

输入图片缺失时，详情显示「等待原图」；选择与原任务对应的图片后继续当前任务。
要求机器人发送图片不需要为此额外上传输入原图。填写预期不代表已修复，也不会写入机器人知识或记忆。

### 代码熵回收与可选定时

在「代码熵回收」页选择按执行时间或问题发现数停止，再选择已保存的回收方案和每项修复尝试上限。
方案中的模型与推理强度随任务冻结；不可用时先修正模型配置或 worker 环境。每次发现一项后立即修复、
验证与交付，安全收尾后才调查下一项。没有额外的人工选题提示入口。

```bash
python -m chatcopilot.harness start-gc --profile CODE_HEALTH_PROFILE --timeout-seconds 3600
python -m chatcopilot.harness start-gc --profile CODE_HEALTH_PROFILE --findings 3 --max-attempts 3
python -m chatcopilot.harness get-gc GC_RUN_ID
python -m chatcopilot.harness cancel-gc GC_RUN_ID
python -m chatcopilot.harness resume-gc GC_RUN_ID
python -m chatcopilot.harness gc-schedule
python -m chatcopilot.harness gc-schedule --enable --profile CODE_HEALTH_PROFILE --interval-hours 24 --findings 2
python -m chatcopilot.harness gc-schedule --disable
```

定时默认关闭。启用后有活动批次或未完成交付时跳过，不排队；关闭不取消已开始批次。
旧参数与旧定时设置不转换，需重新保存。计时、发现数、失败收尾和 Skill 学习规则见[代码熵正文](../reference/code-health.md)。

## 查看修复结果与证据

```bash
python -m chatcopilot.harness list
python -m chatcopilot.harness list --page 2 --search SOURCE_ID --status blocked
python -m chatcopilot.harness get REPAIR_ID
```

任务详情先看当前阶段、轮次、剩余预算、目标覆盖与缺口，再展开角色输入、候选补丁、对照结果、审核及命令日志。
修复、交付和清理分别显示；「已修复」只证明精确候选在指定条件下通过验收，不表示已经合并或部署。
其他原失败项可保持失败，仓库结果应同时查看新增回归和已有失败。

进行中的公开进度每两秒更新，终态补取最后结果后停止轮询。命令输出默认折叠，刷新会同时读取状态与动态。
长命令执行中可能只有心跳，超过 30 秒没有新心跳也不能单独判定卡死。

页面展示最近 20 条事件及有界正文，截断会提示，完整归档在执行单元结束后读取。
「其他阶段的最近记录」不是当前动作；日志更新时间不是单条动作时间。「尚无公开执行输出」不表示未执行，
读取失败可保留现有内容并手动重试。数据与 API 语义见[修复流程记录](../reference/harness.md#修复流程记录)。

### 再次执行已收录回归

审核后收录的 pytest 回归会随仓库检查运行；Agent Case 需要显式执行，可能调用真实模型。
运行已收录的 Agent 回归 Suite，或执行包含 `case_snapshot` 的冻结请求文件：

```bash
python -m chatcopilot evals run --suite agentstrata-regression-v1 --bot BOT_ID
python -m chatcopilot evals run --request REQUEST_JSON
```

这些命令使用 standalone Evaluation 入口，结果查看与评分解释见[测评指南](evaluation.md)。

## 处理失败、取消与恢复

| 现象 | 判断与处理 |
| --- | --- |
| 当前未复现 | 冻结的最新远端 main 已满足目标；检查来源与执行条件，不将其计为一次成功修复 |
| 候选待审阅或证据不足 | 查看具体缺口与原始证据；局部检查通过不足以获得交付资格 |
| 评分、导入或验证环境错误 | 修正评分、依赖、fixture 或权限条件，不要求 Coding 用产品改动掩盖环境失败 |
| 执行状态未知 | 先核对原 worker 与外部执行，不重复启动相同操作 |
| 取消中 | 取消意图已登记，仍需等待角色、外部评测与交付收尾；不能手工清除占用 |
| 中断或预算耗尽 | 查看剩余预算、恢复资格及候选；恢复不会获得新预算，重新发起才创建新任务 |

```bash
python -m chatcopilot.harness reconcile REPAIR_ID
python -m chatcopilot.harness cancel REPAIR_ID
python -m chatcopilot.harness resume REPAIR_ID
```

`get/list` 只读；定时任务核对异常退出，没有运行定时器时可主动 `reconcile`。
恢复和页面的「接续修复（累计预算）」都先确认原执行停止，并核对会话、源码、环境与外部执行身份。
交付复验期间取消保留原修复结论，外部评测未结束前不能释放占用或清理工作区。

systemd 调度失败时先查详情中的 worker unit，再看该 unit 的 journal；不会自动降级到后台进程。
任务活动期间不要手工改动工作区。准备阶段中断且无草案时，可通过「重新发起修复」带入原来源、反馈和选项；
保留旧记录与标记，不删除 `reproducer/started` 强行继续。

若旧 worker 报 SQLite journal 文件不存在，先核对服务版本与冻结环境；journal 由 SQLite 事务管理，
不要手工创建或删除。更新服务不会替换已冻结 worker，旧协议任务须按维护流程保留历史后重新发起。

## Harness PR 交付配置与恢复

私有 `harness.env` 需要配置仓库、actor、token 文件与 Git 作者信息：
`CHATCOPILOT_HARNESS_GITHUB_REPOSITORY`、`CHATCOPILOT_HARNESS_GITHUB_ACTOR`、
`CHATCOPILOT_HARNESS_GITHUB_TOKEN_FILE`、`CHATCOPILOT_HARNESS_GIT_AUTHOR_NAME`、
`CHATCOPILOT_HARNESS_GIT_AUTHOR_EMAIL`。无秘密示例见 [harness.env.example](../../deploy/wsl/harness.env.example)。

token 单独保存为当前用户拥有、非符号链接、单链接的 `0600` 文件，需要目标仓库 Contents/Pull requests
读写及检查结果、分支保护读取权限。宿主核对实际 actor，不继承 Bot 凭据。仓库需启用 squash、
auto-merge 并配置 main 必需检查；交付身份不能绕过保护。

先预览再安装每分钟短时对账任务：

```bash
.venv/bin/python scripts/install_harness_delivery_timer.py --dry-run
.venv/bin/python scripts/install_harness_delivery_timer.py
systemctl --user status agentstrata-harness-delivery.timer --no-pager
journalctl --user -u agentstrata-harness-delivery.timer -u agentstrata-harness-delivery.service -n 80 --no-pager
```

Console 安装在定时器启动成功后才显示完成；仅定时器失败时可单独重跑安装命令。
处理交付或清理受阻时，先查看记录和证据，再按实际原因重试：

```bash
python -m chatcopilot.harness get REPAIR_ID
python -m chatcopilot.harness retry-delivery REPAIR_ID
python -m chatcopilot.harness retry-cleanup REPAIR_ID
```

正式 PR、主干前进复验、取消自动合并、归档与资源清理的完整规则见[交付契约](../reference/delivery.md#harness)。
CI 失败不能绕过；对账可在 Console 关闭后工作，不更新操作者本地 main 或部署机器人。

## 一次性维护切换

先安装新代码，停止旧任务并完成旧 PR 的合并或关闭；切换命令不会自动代做这些操作。
先检查执行与资源清单，再显式导出历史并启用新活动库：

```bash
python -m chatcopilot.harness cutover
python -m chatcopilot.harness cutover --apply
```

成功结果包含 `history_export.path` 与清单摘要。旧任务、候选、工作区和退役数据库保留，旧任务不续跑。
worker、外部评测、PR 或来源身份仍未知时先处理阻断；导出校验失败不能切换，已切换或已有新任务时拒绝再次切换。
归档内容、维护锁与隔离边界以[协议切换契约](../reference/harness.md#协议切换)为准。
