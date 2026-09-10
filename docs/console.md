# 运维控制台

运维控制台是 AgentStrata 在 WSL 中管理多机器人实例的 Web 入口。默认地址是 `http://localhost:8910`，后端由 `chatcopilot-console.service` 托管，前端产物位于 `console/web/dist` 并由 FastAPI 同源挂载。Evaluation 生命周期不在 Console 进程中，而是由独立的 `chatcopilot-evaluation.service` 管理。日常启停、更新、日志和诊断命令统一见 [`operations.md`](operations.md)。

机器人运行时按渠道适配、Gateway、Application、Agent 四个消息处理职责层组织，定义见 [`architecture.md`](architecture.md)。Console 是运行时之外的控制与观测入口，通过现有接口读取状态和发起运维操作。

## 技术栈

- 前端：React 18 + Rsbuild/Rspack + Arco Design + TanStack Query。
- 后端：FastAPI UI/BFF + `console/control/**` 通用控制层；Evaluation 调用通过同 UID Unix socket 进入独立 service。
- 长任务和日志：后端 SSE 流，前端用专用 hook 展示到任务/日志抽屉。
- 构建命令：`cd console/web; npm run build`。

## 主要页面

- **运维总览**：调用 `/api/overview` 汇总机器人、基础设施服务和后台任务健康状态，展示摘要指标和「需要关注」问题队列。
- **服务管理**：调用 `/api/infra` 展示 BotSpec 所需的共享 Docker 服务、平台网关等
  外部依赖，按 Channel 接入与工具外部能力筛选，展示实例或共享服务关系，支持启停、重启、Pull、日志、登录和诊断。无参数“全部启动”委托
  `services.sh start` 做 desired-state reconcile，不会启动已禁用服务。
- **机器人实例**：展示每个 BotSpec 实例的部署、注册、Gateway MainPID、Channel 连接证据、日志、任务和更新入口。
- **组件目录**：按 `tools` / `prompts` / `agents` / `context` 四个 surface 以及 application / Agent / 外部能力的组件分组筛选，展示实例声明使用关系与工具角色要求，只读浏览工具包、运行特性、MCP 服务、提示词、Agent preset、workflow DTO 和上下文来源；数据只来自 `chatcopilot.component_catalog` 的精确 pack/tool 投影，不直接读取 Agent/BotSpec 内部 registry 或自行 import 工具模块。
- **评测中心**：提供「开始测试 / 运行记录 / 进步趋势」。单次详情展示通过、失败、异常、跳过和测试点记录；历史曲线按测试条件分组，保留时间、Git 版本及配置变化。两条主测试方向为 Agent 评测与 QQ 链路；Agent 评测卡片注明「测评框架：DeepEval」，继续使用唯一 Evaluation 资源。

Console 后端的进程执行、YAML 投影和 job/task/log 可观测读取分别位于 `process_executor.py`、`yaml_io.py` 和 `observability.py`，`operations.py` 只保留控制面编排与兼容导出。前端路由按页面懒加载；Evals 的详情组件/展示函数位于 `features/evals/`，BotToolEditor 的模型与状态 hook 位于 `features/bots/tool-editor/`。
- **设置**：控制台自身更新、控制台后端日志等全局维护入口。

Gateway 实例的“运行中”必须同时满足 systemd active、非零 MainPID，以及 `/proc` 中解释器、
完整 `-m chatcopilot run --bot <exact deployed BotSpec>` 参数和实例环境绑定一致。Gateway 日志
来自精确 `chatcopilot@<id>.service` 的 journald；页面中的 Channel 状态只是日志或 provider
探针证据，不等于真实 QQ 入站、模型成功、客户端展示或用户已读。NapCat 单独显示为外部
OneBot provider，不与 AgentStrata Gateway 混称，也不由 Bot start/stop 隐式启动或停止。

## 任务工作台

机器人实例默认进入“任务”，与“分层配置”“运行状态”“能力与工具”组成四个同级入口。顶部选择实例并查看服务状态，日志统一使用一个“服务日志”入口；页面不展示运行统计或实例 MCP、工具包计数行。工作台可用宽度达到 960px 时，左侧为 300px 任务列表，右侧从任务输入开始，沿页面逐步展示执行过程，末尾展示任务结果与消息交付。左侧随页面滚动保持可见，列表自身滚动；右侧不设置固定高度。宽度不足 960px 时只显示列表或详情，通过“任务列表”按钮返回，选中任务后显示流程，不使用辅助抽屉或拖动分隔条。

“分层配置”展示当前实例的基础设置，按以下分组直接阅读。配置与观测分组用于组织运维信息，其数量和内容独立于四个消息处理职责层：

| 分组 | 内容 |
| --- | --- |
| 部署与服务 | 运行目录、配置文件来源、部署设置和版本 |
| Gateway 与协议 | 监听、认证、状态存储及网关配置 |
| Channel 与平台 | 通道、Provider、账号、连接参数和群聊 @ 策略 |
| 身份与权限 | 用户与群白名单、Owner/Admin、访问策略和角色限制 |
| 会话与工作区 | 工作区目录、会话隔离和基础访问边界 |
| Agent 与模型 | 主 Backend、三槽模型、Profiles、推理强度和超时 |
| 子 Agent 与委托 | 已选择的子 Agent、委托预算、覆盖配置和 Workflow |
| 记忆与 RAG | 记忆 Provider、命名空间、RAG 数据源和包含/排除规则 |
| MCP | 服务器命令或地址、参数、环境引用、访问限制和连接状态 |
| 提示词与实例信息 | 身份与风格提示词、BotSpec 信息、校验和数据版本 |

“能力与工具”使用独立功能与插件、Skills、搜索 Provider、工具包、具体工具、运行特性六个平铺分组。Wiki、职业情报、代码开发和仓库读取的专属配置在此查看；记忆、RAG、MCP 连接设置与子 Agent 预算留在分层配置。工具页显示 MCP/委托提供的具体工具及参数，通过“服务器配置”等入口定位对应配置，服务器也可以定位已记录的工具。搜索、分类和分组锚点均在当前页操作，不再使用“工具 / 提示词 / Agent / 上下文”内部页签。

配置正文按实例的保存设置与部署规则解析，包含默认值、路径展开和实例环境覆盖，不读取 Console 自身的模型或权限环境。字段直接显示有效值；“配置来源”在原处展开保存值和环境引用。未配置与显式留空按具体字段的启动语义处理。私有控制台继续显示凭据、端点与目录原值，不增加掩码开关。已配置、已加载和已连接来自各自来源；Skills 没有连接或启动状态，过期快照不显示为当前健康。

MCP 的添加、删除与开关，以及子 Agent/Workflow 选择放在分层配置；工具包、特性和隐藏工具编辑放在能力与工具。两个页签共用当前实例的一份内存草稿，切换页签和轮询不覆盖未保存编辑。其他字段只读，不提供通用 YAML 编辑器。未保存配置正文不写入浏览器持久存储。

- **保存配置**：只写配置，保持 MCP 专属参数和实例指定的配置文件，不重启服务。
- **保存并重启**：沿用现有更新任务，在任务完成后重新读取配置、inventory 和运行快照；保存之后仍可单独应用尚未加载的配置。
- **已应用**：当前有效配置、BotSpec 和引用文件与新鲜运行快照一致。
- **待应用**：确认存在尚未加载的有效差异，显示“最新配置尚未应用到服务”。
- **暂无法确认**：服务停止、快照缺失/过期、读取失败，或旧运行版本没有可比较的环境及引用文件摘要。不会据此宣称已应用或待应用；运行实例更新到具备摘要上报的版本后会自动重新判断。

组件目录按归属打开 `tab=configuration` 或 `tab=capabilities` 并定位内容。旧 `entity` 链接、原分层配置中的能力链接仍可使用，保留实例与任务参数。任务页不再展示“实例准入审计 · 最近 100 条”；后端继续记录准入拒绝，任务内权限决定仍在对应步骤展示，准入前拒绝不会创建任务。

“任务”左侧列表直接选择历史任务，右侧在原工作台显示对应流程。列表显示开始时间、执行状态、短任务 ID、耗时和模型，悬停 ID 可查看完整值，也可点击复制。时间范围、状态和任务 ID 搜索始终显示；“更多筛选”原处展开配置版本、Backend、模型、组件、错误码和最短耗时。默认最近 24 小时，也可选择 7 天、30 天或自定义范围，全部在服务端筛选，每页 50 条。

首次进入恢复该实例上次选择，没有历史选择时使用列表第一条。新任务、自动刷新、修改筛选或翻页都不会替换当前任务；当前任务不在列表时，详情头部显示“当前任务不在此列表中”。任务选择、筛选和列表位置按实例保留，各任务的步骤展开状态与阅读位置分别保留，首次查看从详情顶部开始。列表和详情分别显示读取错误，可以独立重试；查看其他页签时停止任务轮询。浏览器会话只持久保存这些界面状态，不保存任务正文和配置内容。旧 `tab=observation`、`tab=history` 链接保留实例与任务参数，统一进入 `tab=tasks`。

任务流沿用纵向连接线、状态点与步骤卡片的阅读方式：左侧显示名称、状态、流转关系及摘要，右侧显示时间、耗时和已记录 Token，窄屏将时间信息排列在摘要下方。点击步骤后，参数和结果在本步下方展开，可以同时展开多步；当前执行及异常步骤默认展开，也可“展开全部 / 收起全部”。用户调整后的展开状态不会被轮询重置。上下文、日志和原始记录一次展开即读取；会话历史与模型可见输入按角色消息分组，桌面并列展示，窄屏上下排列。长正文先显示预览，再按需展开全文。正文按可见区域请求；收起的 Agent 步骤显示有界预览，展开后继续阅读同一份已采集正文。

任务运行按真实交接分段：渠道接收、网关受理、应用准备、Agent 执行及返回、回复调度、实际投递和交换处理。每段常显所属层、动作、状态、时间和输入输出预览，同一层再次参与时在对应位置显示。段落不提供新的折叠导航；模型、工具与子 Agent 保留调用树、并行关系和原处详情。运行层采用独立标识，不改变基础配置与能力的分组。

新任务由运行后端持续采集边界输入输出，资源准备失败也保留已受理输入；Agent 最终输出在投递前独立记录，投递失败不会抹掉已生成内容。模型输入关联本次上下文，Native/LangGraph 可查看实际逐轮公开响应和工具调用建议，Codex 标注适配器可见范围，不把一次执行当成完整内部模型轮次。输入资源、Provider 回执和交换结果各自按真实 ID 关联。任务完成、消息已送达、群 journal 已提交是不同事实。

平台输入只保存受控消息字段，采集范围明确，不归档认证 Header、连接凭据、完整下载 URL 或原始帧全集；准入前拒绝仍不创建任务或归档正文。任务流只使用当前四层观测版本的事件，不再根据旧事件名称推断层级或补造调用标识，也不回退为旧式步骤列表。缺少当前结构时显示未采集；还有事件页时先显示尚未取得，有确切入口证据才能说明未经过某层。正文到期或截断后只能查看实际保留部分。

步骤、上下文、日志和原始记录的展开状态按任务恢复。轮询补充结束事件或父调用、加载下一页后保持当前阅读锚点；用户滚动和点击优先。无真实步骤关联的日志保留在任务末尾，不按时间或 logger 名称分配到某层。

历史配置在任务流程内以只读字段和文本展示：展开任务输入下的“执行时配置”可看完整快照，展开步骤可直接看该步骤关联组件当时的配置。没有配置来源选择、编辑按钮或跳转；缺失快照显示未记录，不使用最新配置补充历史。新记录保留原值；旧版已省略或替换的内容无法还原，页面会说明历史采集限制。分层配置和能力与工具独立展示当前实例；历史任务只使用执行时快照，不用当前值回填。进入配置编辑再返回任务流时，保留任务、筛选条件、展开状态和阅读位置。模型与工具调用按真实 trace/span 合并开始和结束，Gateway 主调用关联到本任务的“Agent 执行”，子步骤始终可见。上下文按快照 ID 关联，权限与日志按真实调用标识归属；仅有任务关联的记录留在任务末尾，不按工具名或时间猜测。历史缺失的父调用在步骤展开区说明，结束事件缺失仍明确显示。后台代码任务通过工具返回的真实任务 ID 保留关联及调用时状态；独立 code-worker 的完整执行过程尚未接入此索引，界面显示此缺口。

### 持续记录与留存

Agent 执行内部使用统一过程卡片展示 Native、LangGraph 和 Codex。模型卡直接提供“本轮发送的消息”和响应；工具卡提供参数、执行结果与“实际交给模型的结果”，后者保留宿主处理后的内容。子 Agent 的委托输入、配置、返回及内部步骤就地查看，收起父卡不会隐藏子步骤。缓存返回和提前失败也有委托记录。

Codex 过程适配器识别命令、文件变更、MCP、搜索、计划、思考活动及逐条公开消息。已提供的活动正文和错误可展开；思考没有公开摘要时显示未提供正文。一次 Codex exec 标为“Agent 后端执行”，只有实际模型调用边界才标号。宿主工具执行与 Provider 活动标明来源，没有共享 ID 时不按名称合并；不采集隐藏推理或推断未公开轮次。

卡片收起时仍提供可见区域的有界输入输出预览；展开直接显示已采集正文。流式消息按同一消息 ID 更新，结束或补页不会产生重复卡片；新增观测消息不用于聊天交付。三种 Backend 的格式在运行端适配，Console 统一读取现有事件和正文接口，历史未采集的信息不回填。

Gateway 运行进程持续写入独立的 `observability/index.sqlite3` 和按任务保存的详情文件，Console 关闭不影响记录、恢复校对或定期清理。任务生命周期、审批及消息交付由原有业务状态持有者决定，观测只保存投影；诊断失败不改写权限、任务结果或交付事实。成功、失败、取消和等待恢复的任务均记录，准入前拒绝单独进入实例审计。

任务摘要、结构化阶段指标、错误分类和私有配置原值快照长期保留。任务输入输出、工具参数与结果、上下文与提示词正文、关联日志按任务结束时间保留 30 天；运行中与等待恢复的任务不进入到期清理。单条正文上限 64 KiB、上下文每份 8 MiB、每任务详情合计 64 MiB，同时服从有界复制的结构和聚合字符串预算。到期、未采集、已截断、采集失败分别显示。清理只操作观测详情，保留业务任务状态、交付回执原件、会话记忆、人格和工作区文件。

任务日志只采集带真实任务关联的运行日志；“服务日志”仍为独立参考，不并入任务证据。Channel 投递的开始与返回合并为一个步骤，按该消息的出站记录和回执自动更新为 Provider 已确认、失败或交付结果未知；即使返回事件尚未加载，已有回执也可更新状态。历史投递使用本任务既有的固定出站消息标识关联，不改写旧记录。任务完成本身不能证明投递成功，也不推断平台显示或用户已读。隐藏推理及 Provider 未提供的内部状态不记录。

### 只读接口

- `GET /api/bots/{instance_id}/inspection?run_id=...&event_seq=...`：当前、已加载与任务/阶段执行时配置；阶段必须属于选中任务。当前条目补充 `effective_config/effective_environment`，比较结果通过 `configuration_status`（`applied/pending/unknown`）及原因返回；原始字段和 `pending_changes` 保持兼容。
- `GET /api/bots/{instance_id}/gateway-observation`：分页历史和最多 100 条实例准入审计。筛选参数为 `since/until/state/config_id/backend/model/component/error_code/search/min_ms/page/limit`；时间使用 Unix 秒，单页最多 100 条。
- `GET /api/bots/{instance_id}/gateway-observation/metrics`：同一筛选范围的聚合指标、组件分组和趋势。
- `GET /api/bots/{instance_id}/gateway-observation/runs/{run_id}`：任务摘要、首批阶段、审批、出站状态和交付回执。
- `GET /api/bots/{instance_id}/gateway-observation/runs/{run_id}/events?after=...&limit=...`：按序号增量读取，默认 200 条、最多 500 条。
- `GET /api/bots/{instance_id}/gateway-observation/runs/{run_id}/details/{body_id}`：按需读取当前实例、当前任务范围内的不透明正文引用。

Console 只读查询观测索引，不复制整份 Gateway 业务状态库，也不受旧 64 MiB 状态快照上限约束。目录、数据库及正文校验 owner、私有权限、普通文件与链接边界；读取使用有界查询和 no-follow descriptor。响应带来源、时间、配置版本、完整性或正文留存状态，并使用 `Cache-Control: no-store`。无法安全读取时返回脱敏错误。Console 不创建 Agent、连接插件、调用模型或推进 writer generation。

接口中的 `layer` 继续使用既有配置与观测分组标识；实体 ID、配置指纹和历史快照保持原有含义。任务事件的 `data.runtime_layer/operation/flow_version` 表达四层职责和交接阶段，`source/target` 随 metadata 持久化，开始/结束正文仍经既有不透明引用按需读取。新增字段不触发数据库或历史记录迁移。

尚未生成观测索引的实例明确显示运行观测不可用。Console 不再回退读取或复制 Gateway 业务状态库；底层独立诊断读取能力仍可用于运维，不作为旧版任务流展示入口。不会从当前配置补造历史快照，也不会将缺失过程填成成功。

### Legacy 任务记录

Legacy `/tasks` 接口继续只服务 ACP/adapter artifact；Gateway 访问它时保持原有 unavailable 契约，前端使用上述原生接口，不回退旧 Relay/cc-connect/ACP 记录。以下任务列表、八层转换、上下文和删除说明仅适用于 legacy edge。

Legacy 实例仍在“任务”中使用原有任务记录组件，不在前端扫描任务推导服务状态。可选择任务并
查看外部渠道、adapter、ACP、中间件、主 Agent、模型、工具/子 Agent/流程和回复交付证据。
统一的“任务”入口复用原有 Legacy 任务组件；分层配置、运行状态与能力配置使用同级页签。
启动、停止、注册、更新和服务日志集中在详情头部。

任务流中的每次转换均来自后端稳定投影，并标记为 `observed`、`correlated`、`declared`、
`provider_opaque` 或 `missing`。连续工具/子 Agent/流程调用可在前端折叠，但展开后仍显示
每个脱敏事件。旧任务不迁移、不补造历史网关证据，而是显示缺口。Agent 形成结果、ACP
发出 `session_update` 和外部客户端实际显示/阅读是不同边界；没有外部回执时，页面只声明
已经观测到的最强边界。QQ 客户端没有可用的显示/已读回执，因此 `channel` 层可以保持
`missing`，但不重复加入“证据缺口”警告和顶部缺口计数；交付声明仍明确提示未观察到外部
客户端回执。隐藏 chain-of-thought、provider 内部 instructions 和原始平台身份
不会被采集、重建或展示。

Legacy 完整任务信息由原有任务组件直接展示，不使用“完整任务证据”按钮或大型弹窗。
左侧只加载最近 50 个 `schema_version=2` 任务，按
“运行中 / 需要关注 / 最近完成”分组并在浏览器内搜索；旧任务不会进入该列表。右侧在
八层跨层链路之后继续展示分类耗时、Token/费用、每次模型调用的上下文快照，以及按 Span
层级组织的路由、模型、工具、Codex activity、subagent 和后台 Job 阶段。任务列表每 5 秒刷新，
选中且仍在运行的 task flow 每 2.5 秒刷新；运行中的墙钟耗时由浏览器每秒刷新。选中 task
从运行态进入终态时，任务流 query 会强制失效并重新获取一次最终尾部，然后停止周期轮询，
避免终态摘要继续配对倒数第二帧
任务流。此链路不使用 SSE。

每条任务记录都显示“删除”按钮。`succeeded`、`failed`、`error`、`cancelled` 终态记录经
二次确认后可以删除；运行中的按钮保持可见但禁用，因为删除记录不等于取消 Agent 执行。
删除只覆盖该任务目录内的 `task.json`、`turn.json`、`events.jsonl` 和上下文 artifacts，
不会删除独立的后台 Job、会话 journal、memory、persona、executor state 或相邻任务。
普通会话任务位于 workspace 的 `tasks/`；已接受 QQ shared-group 回合位于受保护的
`.conversation-state/task-actors/<actor-digest>/tasks/`，Console 统一发现。后者不位于成员可写
shared root，群内任务与 workspace 工具均不能读取。已准入回合会显示经过大小限制和可观测性
脱敏的当前 canonical 消息正文，但不保存模型历史、subagent transcript/result 或 delegated-job
自由文本。准入拒绝的消息仍按已认证 actor 留下通用
终态记录，但不会激活 actor 执行 session；身份校验失败的消息写入受保护的
`.conversation-state/task-intake/tasks/`，只显示“未验证来源”和通用失败原因，不保存原始正文、
sender envelope、发送者账号或上一轮残留 actor reference。任务记录无法安全创建时，入站管线失败关闭且不调用 Agent。

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

Legacy 共享事件和上下文 artifact 在首次落盘前统一过滤 secret-bearing 字段和动态 key、当前环境
secret、Authorization/Cookie、URI userinfo、Bearer/inline credential、私钥/JWT 与机器
根路径；原始事件仍可保留脱敏后的工具参数、精简结果
和错误，但不保存文本流增量或供应商私有 `reasoning_content`。后台 Job 阶段事件同样在
写入前脱敏并限制为 64 KiB；Console 原样读取已有 task/job 事件与状态，不再次替换字段。读取器
限制 node/item/聚合字符串总量，JSON reader 在 materialize 前检查结构预算；触发
上限时 API 返回显式 truncated/integrity 状态，不把剩余内容标成完整。它沿用每实例 30 天 /
1 GiB 的诊断清理策略。安装的 systemd unit 默认只监听 `127.0.0.1:8910`，避免新增上下文
正文被匿名暴露到全部网卡；Console 仍没有 HTTP operator 认证，本机可达进程仍可读取
完整配置、凭据和可读取的事件/上下文。所有 `/api/` 响应均返回 `Cache-Control: no-store`。显式改成非回环监听时，部署方必须另行提供可信代理认证和网络
边界。HTTP operator 认证仍属于独立的控制面安全变更。

Legacy QQ 合成 artifact 中的 Relay、sender envelope、transport attestation 与
`middleware.access_decision` 只解释旧 ACP 链，不能成为 Gateway 准入或身份依据。Gateway
实例的准入由 Gateway 调用授权模块判定；Console 只投影已持久化的决定和回执。

## NapCat WebUI 登录

- Console 只读取并投影 NapCat 登录状态，不通过 HTTP 返回 WebUI 管理 token，也不编排 bootstrap、扫码、Docker 或 systemd。
- 首次登录或恢复登录统一在仓库目录运行 `bash deploy/wsl/quickstart.sh --bot-id <id> --resume`。向导会在可信交互式终端显示一次本地 WebUI 链接，扫码完成后继续同步 OneBot token 与部署实例。
- WebUI 管理 token 只用于 NapCat 本地面板，不是正向 OneBot WebSocket 的 `QQ_ACCESS_TOKEN`；两种凭据都不进入 Console HTTP 响应。
- NapCat 的正式启动或重启继续要求合法 `QQ_ACCESS_TOKEN` 并通过双向 OneBot 探针；Console 状态检查不会降低该门禁。

## Evaluation 评测中心

统一资源名与状态口径见 [`evaluation-glossary.md`](evaluation-glossary.md)。

`Evaluation` 是唯一运行资源，使用 `evaluation_id` 标识，并以 `kind: comparison | suite` 区分执行方式。生命周期状态固定为 `queued / running / completed / partial / cancelled / interrupted / error`；通过/失败和 Codex/Native/平局只属于结果，不混入生命周期。

页面保留开始测试、运行记录和进步趋势。顶部机器人选择用于启动与运行记录；趋势有独立的多选范围。

- **开始测试**：Agent 评测与 QQ 链路分别选择快速、完整或安全范围，手动启动，不增加模型或 Agent 启动参数面板。Comparison Profile、BFCL、GAIA 和 IFEval 保留原有入口。
- **运行记录**：详情顶部显示通过率、通过/失败/异常/跳过和质量分覆盖情况。Case 表格常显实际输入与最终输出预览，原处展开全文、多轮交互、附件信息、工具证据和每项判分理由。多个测试点可以同时展开；完整正文按需读取，不要求逐层展开 JSON。Agent 执行失败与判分异常分别展示。判分前 Core 保存 `observation.json` 中的实际交互，判分取消或超时仍可阅读；这份记录不计入完成样本，也不能用于 resume。
- **进步趋势**：默认最近 30 天、当前机器人，支持 7 / 30 / 90 天、全部和自定义时间。机器人、模型、测试规模与测试点支持多选；勾选 Agent、模型、测试规模决定拆线，全部取消时合为一条时间曲线。每点代表一次 Evaluation 的一个 Target。Git 与严格比较指纹不自动拆线。通过率、质量分、Agent 执行耗时分别绘制，可用鼠标或键盘打开记录；关闭详情保留趋势条件。每页读取 50 条记录，通过“加载更多历史记录”继续查看，没有静默的 200 点截断。

Agent 题库包含 63 个测试点：原有 25 个、新增 30 个业务能力题与 8 个固定 IFEval 原题。快速测试保持 10 题，完整测试为 61 题，安全测试保持 3 题；原有 2 个来源专用题通过自定义选择。实例缺少所选题需要的 Skill 等能力时，在创建前明确提示，不自动变更题集。业务题重点检查自主选择工具、多轮与长期记忆、检索证据、文件图片、子 Agent 和已配置 Skills；运行资源均隔离。

Case 列表和详情显示“业务能力”或“IFEval 固定子集”。IFEval 详情保留原题 key、固定版本及每条约束的参数和结果，不以这个子集分数宣称完整官方基准成绩。格式检查不调用质量评分模型；语言识别或其他检查器异常显示判分异常。题集扩充不会修改历史记录，旧记录缺少来源时不补造。

Agent 评测由 DeepEval 4.2.2 执行指标。工具、权限、隔离、文件和回执由确定性事实指标验证，适用 Case 默认追加 GEval 或 ConversationalGEval 质量判分，阈值默认 0.7；两类必要条件均通过才算通过。固定步骤、参考标准与阈值随 Case 定义版本保存。质量分不能覆盖事实失败，评分异常不能算通过。

进步趋势分开提供三组操作：上方选择数据范围和刷新，中间勾选拆线维度，图表标题旁切换通过率、质量分和累计执行耗时。切换指标不改变分组，勾选维度不改变指标。

Agent 耗时累计所选 Target 下各测试点及重复执行的执行阶段时间，不包含 DeepEval 判分，也不是整次评测的墙钟时长。数据完整时显示累计值；缺项时保留已采集小计，显示“至少”和完整记录数，曲线上使用空心点，不连入完整耗时曲线。强制中断仅保留子进程已采样的时间，注明部分耗时；未采集的旧记录不从总耗时反推，也不填零。QQ 链路显示自身的累计执行耗时，无模型的合成 Target 不伪称模型执行。

通过率分母包含失败、异常和跳过，重复执行分别计数。质量均分只统计适用且成功判分的样本，并显示已评分/应评分数；旧记录未采集质量分时不补零。完整有效、非 dry-run 的记录才进入趋势，部分记录仍可查看。选中特定 Case 后，点指标只汇总这些 Case；规模标签仍说明原评测规模。不同测试条件可按用户选择连线，点详情保留版本、模型、规模和样本量；不能据此自动归因于一次代码修改。CLI compare/resume 的严格指纹校验保持。

独立评分模型使用 Evaluation 服务的 `~/.config/agentstrata/evaluation.env`，示例见 [`evaluation.env.example`](../deploy/wsl/evaluation.env.example)，配置步骤见 [Evaluation 运维](operations.md#evaluation)。该模型不随被测机器人切换，不使用机器人本地环境补齐。可通过 `CHATCOPILOT_EVALUATION_JUDGE_REASONING_EFFORT=medium` 设置评分推理强度；显式值随评分配置快照保存，历史记录不回填。缺少依赖或评分配置时创建前预检失败，不自动关闭判分。SDK 只在 Trial 内运行，禁用 dotenv、遥测、Confident Cloud 上报、评测缓存和交互提示；Token 与评分耗时独立记录。模型调用仍使用所配置服务，会产生其相应费用。

新记录保存实际执行时替换参数后的请求。历史仅展示已采集输入，或明确标注“当时用例定义；实际发送未记录”，不查当前定义补写历史。预览截短可以展开，采集上限导致的丢失显示“已截断”；继续遵守单 Trial 2 MiB 等保护边界。

新评测在创建时由 Evaluation application 将 Git commit、工作区是否有未提交改动和采集时间写入 `request.json`，不采集分支、remote、作者、文件名或 diff。缺少 Git 的安装环境显示版本未知，历史记录不补写当前版本；只读取历史不会调用 Git。幂等重试保留原版本，重跑重新采集。该快照标识创建时代码，不保证运行期间外部代码未变化。`insights` 和 `source_revision` 是既有列表、详情接口的附加只读字段，Console 不写评测 artifact。列表不加载每条历史记录的完整正文；详情按所选 ID 查询并轮询活动评测。

实现与验收依据见 [`evaluation-progress-history`](../specs/evaluation-progress-history/spec.md)。

`POST /api/evals/evaluations` 由 Console BFF 完成 HTTP 校验后，通过同 UID Unix socket 调用 Evaluation service。Service 在落盘和启动 worker 前原子执行 fail-closed 预检；阻断响应使用 `code/message/checks`，前端展开具体检查项。同一 Bot 的活动 Evaluation 通过 service 拥有的持久化 claim 跨线程和进程互斥，受管 worker 真正退出前禁止删除、重跑或为同 Bot 创建下一条。

创建、重跑、取消和删除在任何状态修改前先由 service 返回绑定操作与 Evaluation ID 的 accepted 帧。创建与重跑使用稳定 Evaluation ID 和请求指纹；accepted 后连接中断时，client 只查询或重放同一个 ID，不会因为普通读取超时让页面显示失败、后台却又生成一条身份未知的 Evaluation。

Console 不创建 Evaluation manager、不持有 worker，也不在 lifespan 结束时改写评测状态。重启、更新或暂时停止 Console 不影响已运行的 Evaluation；Console 恢复后可继续查询、读取 SSE 或取消同一记录。Evaluation service 不可用时，`/api/evals/**` 返回明确的 `503`，不降级为 Console 进程内 manager。`GET /api/evals/health` 返回 service ready、活动记录数、`idle_proven` 和 maintenance 状态。运行代码更新持有 service-owned maintenance lease 时，新建 Evaluation 返回 `409`，读取、导出和取消等既有记录操作仍可用。

Target 记录 executor、backend、model、reasoning effort 和包含已解析 Bot runtime 行为摘要的稳定 fingerprint；逐 Trial checkpoint 必须完成整个 Target 组后才参与胜负聚合。Resume 在任何写入前校验完整请求、Case 快照、Target fingerprint 和已有 Trial 结构，不能修改请求后混用旧 Trial；已完成 Evaluation 不可 Resume，未 checkpoint 的 workspace 残留会在重跑前清理。受管 worker 只接受严格匹配的 service bootstrap，直接 CLI 则保留 standalone resume 语义。评测只在 worker 进程内覆盖 backend，不修改 BotSpec 或线上会话；Case 工具默认拒绝，代码写入只发生在 Evaluation 的隔离 workspace。外部 Case ID 不直接形成 workspace 或 artifact 路径，包含 `/` 时仍可作为原始领域标识查询。Case coverage 按 Bot + Case + Target fingerprint 聚合。

CLI 的 prepare、validate 和 run 命令统一见 [`operations.md#evaluation`](operations.md#evaluation)；本页只维护 Console 与 API 契约。

Evaluation 目录的写入权按文件固定分配：application service 写 `request.json`、`state.json`、activity claim、maintenance lease 和合作式取消标记；Core 写 `result.json`、`summary.md`、`progress.jsonl` 和逐 Trial 证据；managed worker 自行写脱敏 `run.log`；Console 不写任何 Evaluation artifact。Evaluation 目录、activity claim、maintenance marker、取消标记和权威 artifact 不接受符号链接，并校验 owner、类型、权限、单硬链接与 `evaluation_id`；遗留 worker 只有在 argv 精确包含 managed-worker 模块、且唯一 `--output` 与记录目录规范路径相等时才可发送信号。JSON/Markdown 导出从 UDS 到 HTTP 均按块传输，不在 Console 中完整缓冲报告。事件、回答、工具参数、启动错误和报告在写 checkpoint 前过滤凭据字段、通用 token、已知 secret 和机器绝对路径。

该边界只实现 AgentStrata 同仓库、同版本的本机 Evaluation 服务，通过本地 DeepEval SDK 执行 Agent 指标，不引入实验追踪平台、远程调度器或第二套报告存储。

评测 API：

- `GET /api/evals/profiles`
- `GET /api/evals/health`
- `GET /api/evals/suites`
- `POST /api/evals/suites/{suite_id}/prepare`
- `GET /api/evals/cases/coverage`
- `POST/GET /api/evals/evaluations`
- `GET /api/evals/evaluations/{evaluation_id}`（`include_bodies=false` 返回 Case 预览）
- `GET /api/evals/evaluations/{evaluation_id}/cases/{case_ref}`（可按 `trial_id/target_id/attempt` 精确读取）
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
| Gateway 运行观测 | `/api/bots/{id}/inspection` 与 `gateway-observation` 的历史、阶段、正文和指标接口 |
| Legacy 任务流 | `/api/bots/{id}/tasks` 与 `/api/bots/{id}/tasks/{task_id}/flow`；Gateway 返回明确 unavailable |
| NapCat 登录状态 | Console 只读检查；登录与恢复交给 `bash deploy/wsl/quickstart.sh --bot-id <id> --resume` |

## 前端协作规则

- 修改控制台前端先读 `docs/ai-frontend.md` 和 `.cursor/rules/70-frontend-design.mdc`。
- 优先使用 Arco 原生组件；旧 UI 语义兼容层已移除，不允许新增 Semi 风格 prop 适配接口。
- 服务端读取、轮询、刷新优先走 TanStack Query；SSE 流仍用专用 hook。
- 修改 `console/web/**` 后至少运行 `npm run build`，并尽量用浏览器检查桌面和窄屏布局。

### 权限显示与工具接入证据

能力与工具使用“Owner”或“成员可用”标记。Owner 的业务授权不再按 Backend、群私聊或旧只读模式分档；
当前可用工具仍由实例实际装配决定。分层配置不再展示旧 owner_access/member_access 或项目权限开关。
任务内保留真实授权决定与拒绝原因，历史记录的原始角色和配置不改写。

Agent 过程区分别记录 Session Gateway 服务初始化、工具列表请求处理与实际工具调用。
这些记录说明各自的宿主或 Backend 接入事实，不将静态 inventory 表示成模型已收到或已调用工具。
人格更新必须查看 persona_manage 的 committed 回执；没有调用时不能把聊天草稿当作保存结果。
