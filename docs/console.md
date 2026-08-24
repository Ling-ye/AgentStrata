# 运维控制台

运维控制台是 AgentStrata 在 WSL 中管理多机器人实例的 Web 入口。默认地址是 `http://localhost:8910`，后端由 `chatcopilot-console.service` 托管，前端产物位于 `console/web/dist` 并由 FastAPI 同源挂载。Evaluation 生命周期不在 Console 进程中，而是由独立的 `chatcopilot-evaluation.service` 管理。日常启停、更新、日志和诊断命令统一见 [`operations.md`](operations.md)。

## 技术栈

- 前端：React 18 + Rsbuild/Rspack + Arco Design + TanStack Query。
- 后端：FastAPI UI/BFF + `console/control/**` 通用控制层；Evaluation 调用通过同 UID Unix socket 进入独立 service。
- 长任务和日志：后端 SSE 流，前端用专用 hook 展示到任务/日志抽屉。
- 构建命令：`cd console/web; npm run build`。

## 主要页面

- **运维总览**：调用 `/api/overview` 汇总机器人、基础设施服务和后台任务健康状态，展示摘要指标和「需要关注」问题队列。
- **服务管理**：调用 `/api/infra` 展示 BotSpec 所需的共享 Docker 服务、平台网关等
  外部依赖，支持启停、重启、Pull、日志、登录和诊断。无参数“全部启动”委托
  `services.sh start` 做 desired-state reconcile，不会启动已禁用服务。
- **机器人实例**：展示每个 BotSpec 实例的部署、注册、运行、平台连接、日志、任务、更新和诊断入口。
- **组件目录**：按 `tools` / `prompts` / `agents` / `context` 四个 surface 只读浏览工具包、运行特性、MCP 服务、提示词、Agent preset、workflow DTO 和上下文来源；数据只来自 `chatcopilot.component_catalog` 的精确 pack/tool 投影，不直接读取 Agent/BotSpec 内部 registry 或自行 import 工具模块。
- **评测中心**：固定为「新建评测 / 评测记录 / 任务集」三个页签。Agent Profile 对比和 BFCL / GAIA / IFEval Suite 运行统一为 `Evaluation` 资源；记录页负责筛选、查看详情、取消、删除、重跑和导出，任务集页统一展示 Profile、Suite 数据准备状态与 Case coverage。报告统一保存在 `reports/evals/evaluations/<evaluation-id>/`。

Console 后端的进程执行、YAML 投影和 job/task/log 可观测读取分别位于 `process_executor.py`、`yaml_io.py` 和 `observability.py`，`operations.py` 只保留控制面编排与兼容导出。前端路由按页面懒加载；Evals 的详情组件/展示函数位于 `features/evals/`，BotToolEditor 的模型与状态 hook 位于 `features/bots/tool-editor/`。
- **设置**：控制台自身更新、控制台后端日志等全局维护入口。

## 任务可观测工作台

机器人实例页使用“实例列表 + 实例详情”的主从工作台。实例列表消费后端提供的活动任务数、
最近 24 小时失败数和最后活动时间，不在前端扫描任务推导运行状态。实例详情默认打开
“任务流”，可直接选择任务并查看外部渠道、NapCat/OneBot/cc-connect、接入网关、ACP
中间件、主 Agent、模型、工具/子 Agent/流程和回复交付八层证据。详情使用“任务流 / 运行状态 /
能力与工具”三个同级页签；启动、停止、注册、更新、日志和诊断只集中在详情头部，运行状态页
展示只读服务与实例信息，能力与工具页继续按 BotSpec surface 管理工具、Prompt、Agent 与上下文来源。

任务流中的每次转换均来自后端稳定投影，并标记为 `observed`、`correlated`、`declared`、
`provider_opaque` 或 `missing`。连续工具/子 Agent/流程调用可在前端折叠，但展开后仍显示
每个脱敏事件。旧任务不迁移、不补造历史网关证据，而是显示缺口。Agent 形成结果、ACP
发出 `session_update` 和外部客户端实际显示/阅读是不同边界；没有外部回执时，页面只声明
已经观测到的最强边界。隐藏 chain-of-thought、provider 内部 instructions 和原始平台身份
不会被采集、重建或展示。

完整任务信息直接位于“任务流”页签，不再使用“完整任务证据”按钮、大型弹窗或
其他页签中的重复任务入口。左侧只加载最近 50 个 `schema_version=2` 任务，按
“运行中 / 需要关注 / 最近完成”分组并在浏览器内搜索；旧任务不会进入该列表。右侧在
八层跨层链路之后继续展示分类耗时、Token/费用、每次模型调用的上下文快照，以及按 Span
层级组织的路由、模型、工具、Codex activity、subagent 和后台 Job 阶段。任务详情每 3 秒
轮询，运行中的墙钟耗时由浏览器每秒刷新；此链路不使用 SSE。

每条任务记录都显示“删除”按钮。`succeeded`、`failed`、`error`、`cancelled` 终态记录经
二次确认后可以删除；运行中的按钮保持可见但禁用，因为删除记录不等于取消 Agent 执行。
删除只覆盖该任务目录内的 `task.json`、`turn.json`、`events.jsonl` 和上下文 artifacts，
不会删除独立的后台 Job、会话 journal、memory、persona、executor state 或相邻任务。
普通会话任务位于 workspace 的 `tasks/`；已接受 QQ shared-group 回合位于受保护的
`.conversation-state/task-actors/<actor-digest>/tasks/`，Console 统一发现。后者不位于成员可写
shared root，群内任务与 workspace 工具均不能读取。准入拒绝的消息仍按已认证 actor 留下
终态记录，但不会激活 actor 执行 session；身份校验失败的消息写入受保护的
`.conversation-state/task-intake/tasks/`，只显示“未验证来源”和通用失败原因，不保存原始正文、
sender envelope 或发送者账号。任务记录无法安全创建时，入站管线失败关闭且不调用 Agent。

任务可观测 API：

- `GET /api/bots/{instance_id}/tasks?limit=50`：v2 任务摘要，服务端硬限制最多 50 条；
  同时返回服务端计算的活动数、最近 24 小时失败数和最后活动时间。
- `GET /api/bots/{instance_id}/tasks/{task_id}/flow`：版本化、最多 300 条转换的八层任务流
  投影，包含证据等级、结构化决策、覆盖情况、明确缺口和最强回复交付声明。前端不解析
  runtime 私有事件名，也不从原始 OneBot 帧重新推导准入或身份。
- `GET /api/bots/{instance_id}/tasks/{task_id}`：步骤树、分类耗时、固定预测、实际累计
  Token、Job 状态和本地价格表计算的实际用量费用估算。
- `GET /api/bots/{instance_id}/tasks/{task_id}/events`：按需读取任务执行事件与关联
  Job 阶段事件。每次最多返回 1000 条、每个 JSONL 文件最多读取 512 KiB 尾部；较早
  事件被裁剪时响应和页面都会明确标记。只有展开运行中步骤后才持续刷新；请求串行化，
  任务由运行态进入终态后会再取一次权威尾部，避免漏掉最终事件。损坏的 JSON/JSONL
  记录被跳过，非法或越界 ID 被拒绝。事件文件可被 group/other 写入、尾行半写或损坏、
  或同一 source 的 sequence 不连续时，响应另返回 `integrity_gap=true`，页面不会把剩余
  尾部误称为完整记录。
- `GET /api/bots/{instance_id}/tasks/{task_id}/contexts/{snapshot_id}`：按需读取一份
  已脱敏的模型上下文 artifact；snapshot ID、task identity、containment、owner、普通
  文件、非符号链接、单硬链接和 8 MiB 上限均在返回前校验。Context 与 event tail 都从
  已验证并持续持有的 task/job directory descriptor 通过 `openat` 读取，祖先目录不能在
  检查与读取之间通过 symlink 竞态重定向正文。
- `DELETE /api/bots/{instance_id}/tasks/{task_id}`：删除一个终态 v2 任务记录。控制层在
  mutation 前重新校验实例 containment、唯一任务身份、`0700` 目录、owner、inode、
  `task.json` 普通文件/单硬链接/大小/终态状态，并独占任务事件写锁；递归删除使用
  descriptor-relative `openat` / `unlinkat` / `rmdir` 且不跟随符号链接。活动任务、仍有关联
  活跃 Job 的记录、未知状态、畸形、跨实例、重复 ID 或不安全记录返回冲突且不删除目标，
  关联 Job 记录保持不变。

上下文卡片分开显示“AgentStrata 会话历史”和“实际模型输入”。
`exact_model_input` 表示 AgentStrata 能证明 Native/LangGraph 的纯文本最终请求；
`partial` 表示文本与工具上下文已确认，但图片二进制或私有推理等受限字段只保留安全
回执和明确 omission；
`adapter_visible` 表示 Codex adapter 能证明 stdin prompt、工具投影和资源，但 provider
原生 resume 历史或内部 instructions 仍不可见。页面会显式显示 redacted、truncated、
partial 与 provider-opaque 状态，不展示或声称捕获隐藏 chain-of-thought。正文只在展开
对应卡片时加载，任务摘要和轮询接口不复制大块 prompt。
如果安全持久化失败，卡片保留与模型 span 关联的 `unavailable` 摘要，不请求不存在的
正文，也不会把观测失败误显示成“没有上下文”。
上下文摘要达到上限时保留最新模型调用，并在区块顶部显示 retained/total；如果
`task.json` 为满足总量上限只保留了最小索引，页面会明确提示正文仍按 snapshot ID
懒加载，不能把保留子集误解为完整历史。
模型跨度与 Codex activity span 允许嵌套或并行，分类耗时用于解释时间线，不能相加后
当作墙钟耗时。
密集 provider activity 在 `task.json` 中最多保留 500 条结构化摘要；工具/步骤序列化
视图另有 1000 条硬上限，页面会显示总数、保留数和裁剪状态。每条脱敏 raw event 最多
64 KiB，超限参数/结果替换为包含关联 ID、原始字节数和 digest 的 manifest，避免单条大
payload 挤掉整个 512 KiB 事件尾部。`task.json` / `turn.json` 也有 8 MiB 总上限；超大
后台子任务结果按字段、条数和 digest 生成显式裁剪摘要，不能阻断后续终态写入。任务、
Job 及其祖先目录不接受 symlink 重定向；request/status/result/notification JSON 使用
私有、无符号链接、8 MiB 有界读写。任务列表不再重复传输 tool arguments/results
或完整 LLM call 数组。

Token 口径：

- `prompt_tokens` 是总输入，`cached_tokens` / `cache_read_tokens` 是输入子集；
  `non_cached_input_tokens = prompt_tokens - cached_tokens`，Cache 不再加进
  `total_tokens`。
- 任务实际累计只汇总叶子 LLM 调用一次。父 Span 的 `inclusive_usage` 用于解释
  分支成本，不能再与任务总量相加。
- 输入粗估包含消息、system prompt 和工具 Schema。步骤输出/Cache 与任务总基线
  都要求同 Bot、模型、上下文（步骤另隔离 main/subagent）至少 20 个有效样本，
  最多读取最近 200 个样本并取中位数。任务基线首次可计算后固定，运行中只更新
  实际累计；冷启动显示“样本不足”或“粗估”。
- 费用是基于已发生调用和本地模型价格表的估算，不是供应商账单；没有价格的模型
  明确显示未配置，不推导预计费用。

事件和上下文 artifact 在首次落盘前统一过滤 secret-bearing 字段和动态 key、当前环境
secret、Authorization/Cookie、URI userinfo、Bearer/inline credential、私钥/JWT 与机器
根路径；原始事件仍可保留脱敏后的工具参数、精简结果
和错误，但不保存文本流增量或供应商私有 `reasoning_content`。后台 Job 阶段事件同样在
写入前脱敏并限制为 64 KiB，Console 对历史 task/job 事件与状态再次做读取侧脱敏。共享
脱敏器限制 node/item/聚合字符串总量，JSON reader 在 materialize 前检查结构预算；触发
上限时 API 返回显式 truncated/integrity 状态，不把剩余内容标成完整。它沿用每实例 30 天 /
1 GiB 的诊断清理策略。安装的 systemd unit 默认只监听 `127.0.0.1:8910`，避免新增上下文
正文被匿名暴露到全部网卡；Console 仍没有 HTTP operator 认证，本机可达进程仍可读取
脱敏后的事件和上下文。显式改成非回环监听时，部署方必须另行提供可信代理认证和网络
边界。HTTP operator 认证仍属于独立的控制面安全变更。

QQ 回环接入代理可为无损纯文本转发写入短期私有 ingress receipt，内容仅含会话、actor、
正文的摘要和安全决策码。ACP 只有在既有 sender envelope 与 transport attestation 已经
完成权威身份校验后，才会精确消费一条匹配 receipt 作为 `correlated` 可观测证据；歧义、
过期、非文本或持久化失败只降低任务流覆盖率，不改变准入、角色、授权或消息处理结果。

## NapCat WebUI 登录

-  服务管理中的“WebUI 登录”调用 `POST /api/infra/napcat:<instance>/webui-session`；后端通过 `qq_gateway.sh bootstrap` 幂等启动或修正回环容器，等待 `localhost:6099` 就绪后返回含 WebUI 管理 token 的登录链接。
-  WebUI 管理 token 来自 NapCat 容器日志，只用于进入管理面板，不是正向 OneBot WebSocket 的 `QQ_ACCESS_TOKEN`；相关响应带 `Cache-Control: no-store`。
-  已停止容器仍可通过 `GET /api/infra/napcat:<instance>/webui-token` 恢复历史 WebUI token；容器不存在或日志尚未产生 token 时返回明确错误。
-  NapCat 的正式“启动/重启”继续要求合法 `QQ_ACCESS_TOKEN` 并通过双向 OneBot 探针；WebUI bootstrap 不启动 QQ Bot service，也不降低该门禁。
-  缺失或错配 OneBot token 时先在 WSL 执行 `bash deploy/wsl/qq_gateway.sh sync-token --instance <id>`，再运行实例更新；控制台的 gateway 输出会移除 ANSI 控制序列，启动等待期的临时探针错误不会混入成功响应。

## Evaluation 评测中心

统一资源名与状态口径见 [`evaluation-glossary.md`](evaluation-glossary.md)。

`Evaluation` 是唯一运行资源，使用 `evaluation_id` 标识，并以 `kind: comparison | suite` 区分执行方式。生命周期状态固定为 `queued / running / completed / partial / cancelled / interrupted / error`；通过/失败和 Codex/Native/平局只属于结果，不混入生命周期。

- `comparison`：选择 Bot、Profile 与 `quick / standard / custom` preset。Quick 和 Standard 使用服务端固定默认值，不接受执行参数覆盖；Custom 必须显式提供 Targets、Case refs、重复次数、预算和 seed。MVP Profile `agent-comparison-mvp` 仍覆盖 IFEval 指令遵循、GAIA smoke、确定性工具调用和隔离代码修复，不生成“智能总分”。
- `suite`：选择 BFCL、GAIA 或 IFEval，可指定任意 Case、dry-run 和 GAIA judge。官方 Suite 数据仍按需准备；Profile Case 使用稳定版本化定义，不依赖官方数据缓存。

新建评测只保留一个 Bot 选择器和一个「开始评测」动作。`POST /api/evals/evaluations` 由 Console BFF 完成 HTTP 校验后，通过同 UID Unix socket 调用 Evaluation service。Service 在落盘和启动 worker 前原子执行 fail-closed 预检；阻断响应使用 `code/message/checks`，前端展开具体检查项。同一 Bot 的活动 Evaluation 通过 service 拥有的持久化 claim 跨线程和进程互斥，受管 worker 真正退出前禁止删除、重跑或为同 Bot 创建下一条。

创建、重跑、取消和删除在任何状态修改前先由 service 返回绑定操作与 Evaluation ID 的 accepted 帧。创建与重跑使用稳定 Evaluation ID 和请求指纹；accepted 后连接中断时，client 只查询或重放同一个 ID，不会因为普通读取超时让页面显示失败、后台却又生成一条身份未知的 Evaluation。

Console 不创建 Evaluation manager、不持有 worker，也不在 lifespan 结束时改写评测状态。重启、更新或暂时停止 Console 不影响已运行的 Evaluation；Console 恢复后可继续查询、读取 SSE 或取消同一记录。Evaluation service 不可用时，`/api/evals/**` 返回明确的 `503`，不降级为 Console 进程内 manager。`GET /api/evals/health` 返回 service ready、活动记录数、`idle_proven` 和 maintenance 状态。运行代码更新持有 service-owned maintenance lease 时，新建 Evaluation 返回 `409`，读取、导出和取消等既有记录操作仍可用。

Target 记录 executor、backend、model、reasoning effort 和包含已解析 Bot runtime 行为摘要的稳定 fingerprint；逐 Trial checkpoint 必须完成整个 Target 组后才参与胜负聚合。Resume 在任何写入前校验完整请求、Case 快照、Target fingerprint 和已有 Trial 结构，不能修改请求后混用旧 Trial；已完成 Evaluation 不可 Resume，未 checkpoint 的 workspace 残留会在重跑前清理。受管 worker 只接受严格匹配的 service bootstrap，直接 CLI 则保留 standalone resume 语义。评测只在 worker 进程内覆盖 backend，不修改 BotSpec 或线上会话；Case 工具默认拒绝，代码写入只发生在 Evaluation 的隔离 workspace。外部 Case ID 不直接形成 workspace 或 artifact 路径，包含 `/` 时仍可作为原始领域标识查询。Case coverage 按 Bot + Case + Target fingerprint 聚合。

CLI 的 prepare、validate 和 run 命令统一见 [`operations.md#evaluation`](operations.md#evaluation)；本页只维护 Console 与 API 契约。

Evaluation 目录的写入权按文件固定分配：application service 写 `request.json`、`state.json`、activity claim、maintenance lease 和合作式取消标记；Core 写 `result.json`、`summary.md`、`progress.jsonl` 和逐 Trial 证据；managed worker 自行写脱敏 `run.log`；Console 不写任何 Evaluation artifact。Evaluation 目录、activity claim、maintenance marker、取消标记和权威 artifact 不接受符号链接，并校验 owner、类型、权限、单硬链接与 `evaluation_id`；遗留 worker 只有在 argv 精确包含 managed-worker 模块、且唯一 `--output` 与记录目录规范路径相等时才可发送信号。JSON/Markdown 导出从 UDS 到 HTTP 均按块传输，不在 Console 中完整缓冲报告。事件、回答、工具参数、启动错误和报告在写 checkpoint 前过滤凭据字段、通用 token、已知 secret 和机器绝对路径。

该边界只实现 AgentStrata 同仓库、同版本的本机 Evaluation 服务，不引入外部评测引擎、实验追踪平台、远程 evaluator 或第二套报告存储。

评测 API：

- `GET /api/evals/profiles`
- `GET /api/evals/health`
- `GET /api/evals/suites`
- `POST /api/evals/suites/{suite_id}/prepare`
- `GET /api/evals/cases/coverage`
- `POST/GET /api/evals/evaluations`
- `GET /api/evals/evaluations/{evaluation_id}`
- `GET /api/evals/evaluations/{evaluation_id}/cases/{case_ref}`
- `GET /api/evals/evaluations/{evaluation_id}/stream`
- `POST /api/evals/evaluations/{evaluation_id}/cancel`
- `POST /api/evals/evaluations/{evaluation_id}/rerun`
- `DELETE /api/evals/evaluations/{evaluation_id}`
- `GET /api/evals/evaluations/{evaluation_id}/export/{json|markdown}`

## 运维入口与配置更新

实例更新、Console 更新、状态、重启和日志的完整命令集中在
[`operations.md`](operations.md)。控制台中的“更新并重启”和“更新控制台”分别调用
`update_instance.sh` 与 `deploy_console.sh --update-only`，不维护第二套运维流程。
“更新控制台”只允许通过 `systemd-run --user` 的独立 transient unit 启动；无法
创建该 unit 时明确失败，不使用仍留在 Console service cgroup 内的
`setsid` / `nohup` fallback，也不会先获取 Evaluation maintenance lease。

WSL 终端直接运行不带参数的 `bash deploy/wsl/deploy_console.sh` 是全量机器更新入口：
先安装/修复 Console，再发现全部 `bots/*/bot.yaml` 并依次执行实例更新。单实例失败不会
阻断后续实例，脚本最终汇总失败并返回非零；`--skip-bots` 仅用于显式的 Console-only
安装/修复，`--update-only` 仍只更新 Console 与 Evaluation。

 「能力与工具」Tab 以 `tools` / `prompts` / `agents` / `context`
四面展示当前配置。可编辑项写回 WSL 源仓中的 `bots/<id>/bot.yaml` 和
`bots/<id>/mcp/servers.yaml`；“保存并重启”复用统一实例更新入口，通常走不重复安装
依赖的快速路径，Git 提交仍由操作者在源仓完成。

 工具配置“保存并重启”先取得同实例 TaskManager 串行资格，再在任务内写配置和调用统一更新；已有活动任务时返回 409 且不得修改配置。机器人更新 SSE 只有收到服务端 `end` 事件才读取最终 Task：成功后才清除编辑器未保存状态、刷新配置并关闭任务抽屉，失败时保留当前草稿、标红并显示最后错误；传输断线只显示重连提示并由 EventSource 自动重连，不得伪装成任务终止。更新脚本只即时检查主 systemd 服务 active，不把 QQ、飞书等平台通道连接作为任务成功条件。

机器人实例的运行操作区只在状态明确为“未注册”时显示“注册服务”；已注册实例不提供“重注册”按钮。需要修复 systemd 注册配置时，使用下表对应的底层脚本。

### systemd 不可用

控制台依赖 `systemctl --user` 管理实例。WSL 的 PID 1 为 systemd 并不代表用户总线
可用；`user@<uid>.service` 活着但 `/run/user/<uid>/bus` 缺失时，面板仍会正确显示
“systemd 不可用”。WSL 引导必须安装 `dbus-user-session`。修复命令与
`219/CGROUP` 的一次性重试步骤见 `deploy/wsl/README_WSL.md`。

### code-worker 启动失败

`chatcopilot-code-worker@<id>` 若以 `218/CAPABILITIES` 循环退出，说明安装的
用户 unit 仍包含 WSL 不支持的内核 capability 加固项。使用当前源码重新运行
`bash console/systemd/register.sh <id>`，再重启实例。注册会保留兼容的 systemd
加固，并从 `local.env` 的 `export KEY=value` 形式提取允许进入 worker 的 Codex
配置；QQ、LLM 等平台凭据不会进入 worker 环境。

主 unit 的实例 ID 与部署后 BotSpec 路径由注册配置显式固定。同一 `wsl_home`
包含多个 `bots/*/bot.yaml` 时，不得退回选择任意首个 BotSpec。

### Codex 独立 lane 登录

控制台不提供 Codex 登录 UI 或 API。main / worker 的独立 device auth 与安全状态检查
统一使用 [`operations.md#codex-main--worker-认证`](operations.md#codex-main--worker-认证)
中的 CLI；凭据布局、lease 和 resume 失效契约见 [`runtime.md`](runtime.md) 与
[`bot-spec.md`](bot-spec.md)。

## 工具配置 DTO

`GET /api/bots/{id}/tools` 和 `PUT /api/bots/{id}/tools` 使用以下四面 BotSpec DTO：

```json
{
  "tools": {
    "packs": ["workspace.read_write"],
    "features": ["chat.file_uploads"],
    "hide": ["dangerous_tool"]
  },
  "agents": {
    "presets": ["mcp_query"],
    "workflows": [],
    "unified_search": {
      "enabled": true,
      "providers": [{ "id": "searxng", "kind": "searxng", "enabled": true }]
    }
  }
}
```

机器人 inventory 使用展示字段 `tool_packs`、`tool_features`、`hidden_tools`、`agent_presets`、`workflows` 和 `config`；`config` 展示 `prompts`、`context.rag`、`context.memory_store`、`context.codebases`、`context.playbooks` 等只读配置状态。当前内置 workflow registry 可为空，控制台仍保留 DTO 字段以兼容后续注册。

## 按钮与底层入口

| 控制台动作 | 底层入口 |
| --- | --- |
| 首次部署 | 写 `bots/<id>/local.env`，再执行平台准备、同步、重建、注册、启动 |
| 注册服务（仅未注册实例显示） | `bash console/systemd/register.sh <id>` |
| 启动 / 停止 / 重启 | `bash console/scripts/ctl.sh <verb> <id>` |
| 更新并重启 / 工具配置“保存并重启” |  `bash deploy/wsl/update_instance.sh --instance <id>`；默认快路径，依赖或安装脚本变化、实例 venv 缺失时完整 bootstrap |
| 更新 Console 与全部机器人 | `bash deploy/wsl/deploy_console.sh`；失败实例汇总后返回非零 |
| 更新控制台 | `bash deploy/wsl/deploy_console.sh --update-only` |
| 实例日志 | `/api/bots/{id}/logs/stream` SSE |
| 控制台日志 | `/api/console/logs/stream` SSE |
| 任务流 | `/api/tasks/{task_id}/stream` SSE |
| 实例诊断 | `bash deploy/wsl/dump.sh --instance <id>` |
| NapCat WebUI 登录 | `POST /api/infra/napcat:<id>/webui-session` → `qq_gateway.sh bootstrap` |

## 前端协作规则

- 修改控制台前端先读 `docs/ai-frontend.md` 和 `.cursor/rules/70-frontend-design.mdc`。
- 优先使用 Arco 原生组件；旧 UI 语义兼容层已移除，不允许新增 Semi 风格 prop 适配接口。
- 服务端读取、轮询、刷新优先走 TanStack Query；SSE 流仍用专用 hook。
- 修改 `console/web/**` 后至少运行 `npm run build`，并尽量用浏览器检查桌面和窄屏布局。
