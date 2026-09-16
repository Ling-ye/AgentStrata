# 运行测评

选择被测对象、题目与环境，再按预检执行。真实模型测评须显式启动；结果解释见 [Evaluation](../reference/evaluation.md)。

## Evaluation

Agent 轨道使用 DeepEval 4.2.2。Console 安装/更新流程对账 `evaluation` 可选依赖；开发环境可运行 `python -m pip install -e ".[agent,evaluation,dev]"`。评分模型独立配置，先从 [`evaluation.env.example`](../../deploy/wsl/evaluation.env.example) 复制非秘密模板至服务用户的 `~/.config/agentstrata/evaluation.env`，目录使用 `0700`、文件使用 `0600`，填写 `CHATCOPILOT_EVALUATION_JUDGE_MODEL`、`BASE_URL`、`API_KEY` 和可选 `TIMEOUT`（默认 60 秒）。可选 `CHATCOPILOT_EVALUATION_JUDGE_REASONING_EFFORT=medium` 将评分推理强度明确设置为中；留空则不发送该参数。模型可接受的档位由 Provider 决定，不支持时会报告判分错误。推理强度记录在评分快照中，独立于被测模型。模板不包含默认商业模型或凭据。

Evaluation systemd unit 读取该文件；配置更新按既有维护流程在服务 idle 时应用，不需要修改或重启机器人。命令行独立执行时显式提供同名环境变量。选择 GEval 且题目需要质量判分时，预检缺少评分配置会阻止创建评测；不会借用被测模型。每次判分也受 Case 和 Evaluation 剩余时间约束。DeepEval 只使用本地 SDK，结果留在现有 Evaluation 根；正常模型 Provider 调用费用与 Agent 测试调用分开记录。

控制台中的「开始测试 / 运行记录 / 进步趋势」通过
`chatcopilot-evaluation.service` 执行。服务是 activity claim、lifecycle state 和
managed worker 的唯一 owner；Console 只是 UI/BFF。先检查本机服务：

```bash
python -m chatcopilot.evals.service health --json
curl -fsS http://127.0.0.1:8910/api/evals/health
```

`ready=true` 表示 UDS 协议可用，`active_count` 是当前 `queued/running` 记录数。
Console API 返回 `503` 时，不要反复提交创建请求；按以下顺序检查：

```bash
systemctl --user status chatcopilot-evaluation.service --no-pager -l
journalctl --user -u chatcopilot-evaluation.service -n 120 --no-pager
python -m chatcopilot.evals.service health --json
```

日常的 Profile、Suite、Case、数据准备、coverage、SSE、导出、取消、重跑和
删除都走同一 service client。Suite 数据准备仍在 Console 任务抽屉中展示输出，
但实际准备操作由 Evaluation service 执行；关闭抽屉或重启 Console 不会成为
Evaluation worker 的取消信号。

CLI 的 `evals list/describe/prepare/run/compare` 保留为 standalone/CI 入口，不会
接管正在运行的 managed Evaluation。查看和准备 standalone 评测：

```bash
python -m chatcopilot evals list
python -m chatcopilot evals describe --suite gaia
python -m chatcopilot evals prepare --suite bfcl
```

执行 standalone Profile 对比或官方 Suite：

```bash
python -m chatcopilot evals run \
  --profile agent-comparison-mvp \
  --preset quick \
  --bot bots/lingye-copilot-qq/bot.yaml \
  --output reports/evals/manual/agent-quick

python -m chatcopilot evals run \
  --suite bfcl \
  --bot bots/lingye-copilot-qq/bot.yaml \
  --output reports/evals/manual/bfcl-smoke
```

### 基准工作台与手动测评

Console 的「开始测试」按LLM测评、Agent测评、系统测试组织，目录由 Suite metadata 提供。
自建 Agent 任务能力与专项题单以当前目录为准，支持独立红队分类；公开基准包含
SWE-bench Verified、BFCL、GAIA、AgentBench FC 和 IFEval，QQ 保留合成链路说明。
查看目录和题目不会下载数据、启动容器或调用模型。勾选题目后，运行计划使用
准确 Case ID，并冻结框架版本、题单摘要与评分配置。数据准备由独立「准备官方数据」动作触发，
完成后刷新目录。缺少环境或数据的基准保留可见并显示阻断原因。

「原生评分 + GEval」分别记录确定性结果和语义质量；「仅原生评分」不调用 Judge。
公开基准默认原生评分，只能另加独立 GEval 附评；历史自定义 GEval 记录保留可读并标为非原生成绩。附评的固定量表可选证据质量或
任务回应质量，阈值 0.7；项目回归使用每题自身的固定质量定义。GAIA 不再使用旧 LLM fallback
改判答案，按官方数字、顺序列表和字符串归一化规则检查。质量分范围为 0–1，不表示正确概率。

自建任务以执行事实和必要的严格语义共同判定，不允许关闭必要语义。字段、数据维护和异常说明见 [业务 Case 指南](evaluation-cases.md)。旧两个题库已退役，仅保留历史与工程合同验证。

运行记录显示框架、来源、用途、适配器、评分器来源和版本、题单和评分快照，并可展开逐题实际输入、输出、工具和评分理由。
缺少旧字段时显示未记录。趋势默认按精确题集、环境、预算和指标协议区分可比条件；GEval
额外区分 Judge 与量表。探索视图可看历史及不同条件，变化不能直接解释为能力进步。
质量覆盖不完整时不生成完整可比质量点。不同基准不平均为总分。

`agentstrata-agent-tasks-v1` 直接使用隔离 Agent runtime，不经过 ACP 或 QQ；
`quick/full/security/red-team/live/skills` 分别选择 12/60/11/12/2/2 题。
缺条件的题目保留在选择中并阻断启动，不静默减题。

`agentstrata-qq-message-flow-v1` 使用当前 OneBot Channel 探针，再进入隔离的 attestation/ACP 合成链，
`quick/full/security` 分别选择 3/7/4 个 Case。它只保留为 legacy regression suite，
不是新 Gateway 验收；迁移到 fake OneBot → real Channel/Gateway 前不得把名称解释成当前
推荐运行路径。两者只可手动启动；默认
`repetitions=1`，只说明本次执行结果，不能作为重复可靠性结论。

两条产品轨道均不接 Git hook、CI、文件监听、部署回调或 Bot 重启回调。

官方数据通过 Console“准备官方数据”或以下命令显式下载；CLI 读取调用进程环境，
Console 会从所选 Bot 的私有 `local.env` 读取配置。数据默认保存在
`~/.cache/agentstrata/evals`，也可使用 `CHATCOPILOT_EVALS_DATA_DIR` 指定位置。

```bash
.venv/bin/python -m chatcopilot.evals prepare --suite swe-bench-verified --json
.venv/bin/hf auth login
.venv/bin/python -m chatcopilot.evals prepare --suite gaia --json
```

GAIA 必须由同一账号在 [官方数据页](https://huggingface.co/datasets/gaia-benchmark/GAIA)
同意访问条件。可在 Bot 的 `local.env` 填写 `CHATCOPILOT_HF_TOKEN` 代替 CLI 登录，
示例见 [local.env.example](../../bots/lingye-copilot-qq/local.env.example)；只读权限即可。
下载固定 revision 的 validation 题目与全部关联附件，保存 JSONL 与来源/哈希回执。
缓存完整时自动发现，不必手工设置 `CHATCOPILOT_GAIA_DATA_PATH`；自备文件仍可显式
覆盖，并使用 `CHATCOPILOT_GAIA_FILES_DIR` 指定附件目录。`CHATCOPILOT_GAIA_CASE_PROFILE=full`
展示全部 validation 题目；默认 balanced-100 只选固定 100 题，不代表只下载了 100 题。
官方 gated 数据、答案与附件不得提交到公开仓库。

BFCL 默认载入固定 BFCL V4 单轮 13 类共 3,641 题，IFEval 默认载入固定官方全部 541 题。
在控制台选择对应套件后使用“准备官方数据”；准备过程校验版本、题目与答案 ID、数量、哈希及必要评分资源，最后发布准备回执。
可用 `CHATCOPILOT_BFCL_DATA_DIR` 和 `CHATCOPILOT_IFEVAL_DATA_PATH` 指定数据，留空时读取校验后的私有缓存。
两个套件的 `CHATCOPILOT_<BFCL|IFEVAL>_CASE_PROFILE` 默认 `full`；`balanced-100` 为显式 100 题子集，`smoke` 为调试题。
全量目录内也可直接选择“固定 100 题子集”。未准备成功不会自动回退到调试题。BFCL Live 表示题目来源类别，无需真实工具联网；结果不代表 BFCL V4 全榜总分。
IFEval 依赖随 Evaluation extra 安装，准备入口下载固定版本的 English `punkt`/`punkt_tab` 分句资源。

结果列表同时显示模型正文与函数调用摘要；展开题目可复制完整已采集 JSON、查看原始参数及模型结束原因。
“模型返回函数调用”只表示调用提议。旧记录仅显示已保存字段，不补写原始请求、文本或成绩。
`deploy_console.sh --update-only` 在维护锁内同步锁定 Python 依赖并更新 Evaluation/Console，不更新机器人实例。

SWE-bench 与 AgentBench 的执行资源仍需单独准备：

- SWE-bench 使用已固定的 `swebench==5.0.2` 评分库（包含在 `evaluation` extra）。
  “准备官方数据”下载固定版本的 500 题并自动发现缓存；可选的
  `CHATCOPILOT_SWEBENCH_DATA_PATH` 覆盖默认 JSONL，每行包含 `instance_id`、
  `problem_statement`、`base_commit`、`image`、`eval_script`、`repo`、`version`、
  `FAIL_TO_PASS`、`PASS_TO_PASS`、`log_parser`、`eval_type`。
  先按所选题详情声明的 image 执行 `docker pull <image>`，无需一次下载全部 500 个镜像。
  目录保留全部题目，并分别显示已准备/缺镜像；正式运行前再次核验，运行不自动拉取镜像。Agent 通过 `benchmark_shell`
  在无宿主挂载、断网、丢弃 capabilities、有限 CPU/内存/PID 的容器内修复，模型补丁在新容器中
  接受隐藏测试，再交给上游 log grader 判卷。官方镜像可能附带构建提交，宿主只在新建的
  临时容器内将仓库恢复到题目 base_commit，并记录原镜像 HEAD；求解与评分使用同一基线。
  镜像 ID 与补丁摘要
  随结果保存。该受限执行配置需与原始榜单条件区分；暂不支持 Multimodal 资源。
- AgentBench FC 可通过项目准备脚本安装固定版本的本地 DB/OS 环境：

  ```bash
  .venv/bin/python scripts/prepare_agentbench.py up
  .venv/bin/python scripts/prepare_agentbench.py status
  .venv/bin/python scripts/prepare_agentbench.py stop
  ```

  准备脚本使用独立 Compose 项目 `agentstrata-agentbench`，默认只将 Controller
  的 `15020` 端口绑定到本机回环；worker 与 Redis 不发布宿主端口。DB/OS 各一 worker、
  并发均为 1；DB 缓冲池 128 MiB、任务容器内存上限 1 GiB，OS 使用 Ubuntu 24.04。
  这些是明确的本地环境条件，不等同于上游高并发部署；题目和评分逻辑保持上游实现。
  KG/ALFWorld/WebShop 不随该准备命令启动。
  默认数据、Compose 配置及来源/镜像回执保存在 `~/.cache/agentstrata/evals/agentbench-fc`。
  首次准备完成后，将输出的数据路径及 Controller URL 填入 Bot 的私有 `local.env`：
  `CHATCOPILOT_AGENTBENCH_DATA_PATH`、`CHATCOPILOT_AGENTBENCH_CONTROLLER_URL`。
  `status`/`stop` 使用非默认端口时也需传同一个 `--port`。
  `CHATCOPILOT_AGENTBENCH_CONTROLLER_URL` 只接受明确的回环 IP HTTP `/api` 地址，
  禁止重定向、代理和带用户信息的 URL。`CHATCOPILOT_AGENTBENCH_DATA_PATH` 为从所部署
  固定版本导出的 JSONL 目录，每行包含 `task`、`index`、`input`（预览原题）、
  `source_revision`（40 位源码 commit）。支持的 task 为 `dbbench-std`、`os-std`、`kg-std`、
  `alfworld-std`、`webshop-std`；实际可用性由已部署 worker 和数据决定。运行时真实输入和
  工具来自 Controller，AgentStrata Agent 调用工具推进环境，终态按环境 reward 判定。
  本地准备脚本从实际 worker 的数据加载器导出题目预览，并与运行中的 Controller 索引核对；
  固定源码版本、低内存配置与实际镜像 ID 保存在 `source.json`。
  目录和运行前会只读核验 worker 是否存活、是否繁忙、题目索引是否存在；不凭 URL 格式判可用。
  Controller 的启动消息与后续交互终态分别校验，不把 start_sample 缺少 finish 当作执行失败。
  环境回收只针对当前会话；上游已经删除的终态会话不会被重复取消误报。完整成绩仍需真实 Agent 运行。
- 环境资源在 Agent 调用前向 Trial supervisor 登记，正常结束与取消后按精确资源身份回收。
  无法确认回收时返回清理异常，不当作完成。环境准备、真实模型运行和完整官方数据集成绩
  分别验收；本地模拟环境测试不替代这些结果。

环境配置模板见 [`evaluation.env.example`](../../deploy/wsl/evaluation.env.example)。新基准仍使用
现有 Evaluation service、单 Bot claim、预算与取消机制，不创建第二个生命周期或报告根。

直接 Agent 的实时汇率 Case 要求搜索最新可用业务日的 ECB USD/CNY 参考值，并由
Evaluation 独立读取 ECB Data Portal 作为 oracle。oracle 不可用时 Case 记为基础设施
错误，不会降级为格式或搜索调用通过。QQ 轨道只使用随机合成身份、回环端口、临时保护
状态和确定性 Agent sentinel，不连接或写入真实 QQ；其中 QQ suite 使用的是 legacy
ACP path。真实 NapCat/OneBot/外部用户
往返继续由基础设施检查报告，缺少独立发送账号时仍为 `not_tested`。

代码修改后可先用只读 Advisor 获取建议；它只做 changed-path 到 Preset/Case 的确定性
映射，不读取 Git diff、不创建 Evaluation，也不会自动启动模型或外部服务：

```bash
python -m chatcopilot evals advise \
  --changed-path src/chatcopilot/agent/tools/registry.py \
  --changed-path src/chatcopilot/middleware/acp/admission.py
```

quick/security/full standalone 示例：

```bash
python -m chatcopilot evals run \
  --suite agentstrata-agent-tasks-v1 \
  --preset quick \
  --repetitions 1 \
  --bot bots/lingye-copilot-qq/bot.yaml \
  --output reports/evals/manual/agent-tasks-quick

python -m chatcopilot evals run \
  --suite agentstrata-agent-tasks-v1 \
  --preset security \
  --repetitions 1 \
  --bot bots/lingye-copilot-qq/bot.yaml \
  --output reports/evals/manual/agent-tasks-security

python -m chatcopilot evals run \
  --suite agentstrata-qq-message-flow-v1 \
  --preset full \
  --repetitions 1 \
  --bot bots/lingye-copilot-qq/bot.yaml \
  --output reports/evals/manual/qq-message-flow-full

python -m chatcopilot evals run \
  --suite agentstrata-agent-tasks-v1 \
  --preset full \
  --repetitions 1 \
  --bot bots/lingye-copilot-qq/bot.yaml \
  --output reports/evals/manual/agent-tasks-full
```

图片理解已有 3 个配置化 Case 和合成图片 fixture；图片生成尚未配置，能力目录显示
`image_generation:not_configured`，它不属于失败 Case。GAIA 使用 Agent runtime；IFEval 与 BFCL 使用独立 PromptPlan 模型直测，结果不进入 Agent 任务通过率。SWE-bench Verified 需官方数据与隔离容器镜像；WebArena 和 `agentstrata-canary-self-update-v1` 仍待接入。

QQ 连通性与外部边界检查见 [QQ 指南](qq.md#qq-外部平台检查)。

## 阅读测评预期与异常

开始测评的题目列表直接展示预期摘要；展开后可查看参考答案、预期行为和具体校验要求。
结果表并排显示预期与实际回答，展开后查看本次冻结的完整预期、执行事实、评分及错误。
没有唯一文本答案的任务按行为或状态验证，不要求模仿某段示范话术。

v2 结果包含 `expectation`、`execution`、`assessment` 和结构化 `error`。
未评分显示“未评分”，未知耗时显示“未记录”；实际零分和零耗时保留。
单题执行/评分异常继续后续题目，共用结果契约、传输、权威写入或清理故障终止整批。
已完成的执行观测和有效评分保留，不能据此补判未完成的整题。

运行记录中的“归档记录”只提供旧格式摘要和原始 JSON/Markdown 导出。旧 Case 实例
ID 明确提示已归档，不能重跑、恢复、比较或进入 Harness；请从当前题库创建新测评。
原文件和数据库记录不自动改写。

Console 安装/更新入口先证明 Harness 空闲，并在整个更新过程中阻止新建和恢复修复任务，
再沿用 Evaluation maintenance lease 保护测评。存在活动任务或无法取得维护资格时停止更新。
完成或取消活动任务后重新执行既有更新命令；不要手工移除活动 claim 来绕过检查。
该保护不用于只读状态查询或仅重启 Console。手工更新可用
`python -m chatcopilot.harness maintenance -- <更新命令及参数>` 包裹既有维护流程。
