# 任务、配置与过程观测

修改任务投影、字段读取或私有操作值时阅读。过程输出、执行结果和平台确认不能互相替代，缺失值不能按成功或零值补齐。

## 任务工作台

Codex 主会话使用 App Server stdio，Agent 执行段连续展示公开消息、推理摘要、工具及命令输出。
SSE 按已持久化事件序号续读，切换任务或隐藏页面时关闭订阅，断开不影响机器人执行。
位于底部时跟随更新，上翻阅读后使用“有新活动”返回；消息和摘要直接显示正文，工具可展开参数及结果。
Codex 回合不等于底层模型调用，未提供摘要、采集失败、截断和到期会分别说明；历史缺失不回填。
恢复旧 thread 后若没有可信的历史用量基线，本轮不把 thread 累计量冒充本轮用量，后续 live 回合使用真实差值。
实现与验收边界见 [流式观测规格](observability.md)。

机器人实例默认进入“任务”，与“分层配置”“运行状态”组成三个同级入口。顶部选择实例并查看服务状态，日志统一使用一个“服务日志”入口；页面不展示运行统计或实例 MCP、工具包计数行。工作台可用宽度达到 960px 时，左侧为 300px 任务列表，右侧从任务输入开始，沿页面逐步展示执行过程，末尾展示任务结果与消息交付。左侧随页面滚动保持可见，列表自身滚动；右侧不设置固定高度。宽度不足 960px 时只显示列表或详情，通过“任务列表”按钮返回，选中任务后显示流程，不使用辅助抽屉或拖动分隔条。

当前实例配置统一采用四层导航、分组列表与详情抽屉；分类、关联跳转和响应式交互只在
[四层配置工作台](console.md#四层配置工作台) 维护。配置的展示归属不改变观测实体 ID、后端分类或历史快照。

配置正文按实例的保存设置与部署规则解析，包含默认值、路径展开和实例环境覆盖，不读取 Console 自身的模型或权限环境。字段直接显示有效值；“配置来源”在原处展开保存值和环境引用。未配置与显式留空按具体字段的启动语义处理。私有控制台继续显示凭据、端点与目录原值，不增加掩码开关。已配置、已加载和已连接来自各自来源；Skills 没有连接或启动状态，过期快照不显示为当前健康。

配置编辑范围、草稿保护与保存操作见 [工具配置编辑机制](console.md#工具配置编辑机制)。
未保存配置正文不写入浏览器持久存储。配置应用状态继续由以下观测事实决定：

- **已应用**：当前有效配置、BotSpec 和引用文件与新鲜运行快照一致。
- **待应用**：确认存在尚未加载的有效差异，显示“最新配置尚未应用到服务”。
- **暂无法确认**：服务停止、快照缺失/过期、读取失败，或旧运行版本没有可比较的环境及引用文件摘要。不会据此宣称已应用或待应用；运行实例更新到具备摘要上报的版本后会自动重新判断。

当前会话收到源配置写入成功回执后可保留应用入口；这不改变上方运行观测的“暂无法确认”状态。
组件目录统一打开分层配置并定位对应条目，地址规则见 Console 正文。任务页不再展示“实例准入审计 · 最近 100 条”；后端继续记录准入拒绝，任务内权限决定仍在对应步骤展示，准入前拒绝不会创建任务。

“任务”左侧列表直接选择历史任务，右侧在原工作台显示对应流程。列表显示开始时间、执行状态、短任务 ID、耗时和模型，悬停 ID 可查看完整值，也可点击复制。时间范围、状态和任务 ID 搜索始终显示；“更多筛选”原处展开配置版本、Runtime、模型、组件、错误码和最短耗时。默认最近 24 小时，也可选择 7 天、30 天或自定义范围，全部在服务端筛选，每页 50 条。

首次进入恢复该实例上次选择，没有历史选择时使用列表第一条。新任务、自动刷新、修改筛选或翻页都不会替换当前任务；当前任务不在列表时，详情头部显示“当前任务不在此列表中”。任务选择、筛选和列表位置按实例保留，各任务的步骤展开状态与阅读位置分别保留，首次查看从详情顶部开始。列表和详情分别显示读取错误，可以独立重试；查看其他页签时停止任务轮询。浏览器会话只持久保存这些界面状态，不保存任务正文和配置内容。旧 `tab=observation`、`tab=history` 链接保留实例与任务参数，统一进入 `tab=tasks`。

任务详情顶部使用四列运行轨迹展示 Channel、Gateway、Application、Agent 的真实阶段。每个阶段按交接顺序占一行并落在所属职责列，返回、投递与交换处理再次显示对应层；列标题只汇总阶段和异常，不承担筛选或视图切换。窄屏改为保持原顺序的单列时间线。缺少所属阶段的真实记录单独显示为观测缺口，不补造阶段或调用关系。

选择真实 Agent 阶段后，阶段下方提供局部“执行列表 / 调用关系图”；非 Agent 阶段不显示该选择。执行列表按明确父子关系缩进，关系图按每次调用展开，同名工具不会合并；搜索、只看异常、收起子调用、聚焦子 Agent、定位步骤和放大画布均限制在当前 Agent 阶段。虚线表示父子归属，蓝色箭头表示触发调用，绿色箭头表示工具消息进入模型上下文。图只连接明确标识，不将时间相邻当作因果；历史没有 `input_tool_refs` 时不补造输入来源。布局在 Worker 内计算，正文与 Token 更新不会重排图；图失败时仍可使用执行列表。

阶段、Agent 执行列表和关系图共享结构化检查器：选择非 Agent 阶段时检查器位于运行轨迹下方；选择 Agent 阶段时宽屏与局部执行面板并列，空间不足时上下排列。可固定另一节点对照，输入、执行结果、实际交给模型的工具结果、上下文、配置、权限和日志分别保留。对象与数组统一使用可展开的树，仅提供树和 JSON 两种视图；支持搜索当前已加载载荷，树和搜索结果不附加逐字段的“路径／复制”按钮。切换到 JSON 后仅提供一个“复制 JSON”，复制当前已加载数据块，长文本预览省略不影响复制；采集截断时仍只复制实际保留部分。单个字段可选中文本使用浏览器原生复制，搜索结果继续显示定位路径。null、空字符串、空数组、空对象与缺失字段分别显示。模型消息继续按角色组织，长数组和长文本按需展开。只读取选中或固定步骤的可见正文，原始 trace 放在“原始执行记录”中。

任务输入、请求参数和配置等折叠区域只保留外层标题，内部仍显示采集状态。工具接入正文直接展示“提供工具”列表，状态、来源和错误在步骤摘要中各出现一次。同一步骤的多个面板共享正文引用时，完整“原始记录”仅保留在优先的主要面板下；它以 JSON 文本和单个复制按钮显示，不再嵌套另一套树／搜索。业务内容不合并，不跨步骤或对照节点去重。图连线保留来源与去向片段和节点跳转，不重复附加完整原文；步骤的独有事件字段仍可在“事件元数据”查看。

选中节点、固定对照、Agent 局部视图、子调用收起和图视口按实例与任务恢复。记录未完整加载时保留尚未取得提示；新事件不抢占选择，图拓扑变化保持当前视口。窄屏提供“查看所选步骤详情”定位按钮，任务列表仍使用原来的单列切换。

新任务由运行后端持续采集边界输入输出，资源准备失败也保留已受理输入；Agent 最终输出在投递前独立记录，投递失败不会抹掉已生成内容。模型输入关联本次上下文，Native/LangGraph 可查看实际逐轮公开响应和工具调用建议，Codex 标注适配器可见范围，不把一次执行当成完整内部模型轮次。输入资源、Provider 回执和交换结果各自按真实 ID 关联。任务完成、消息已送达、群 journal 已提交是不同事实。

平台输入只保存受控消息字段，采集范围明确，不归档认证 Header、连接凭据、完整下载 URL 或原始帧全集；准入前拒绝仍不创建任务或归档正文。任务流只使用当前四层观测版本的事件，不再根据旧事件名称推断层级或补造调用标识，也不回退为旧式步骤列表。缺少当前结构时显示未采集；还有事件页时先显示尚未取得，有确切入口证据才能说明未经过某层。正文到期或截断后只能查看实际保留部分。

步骤、上下文、日志和原始记录的展开状态按任务恢复。轮询补充结束事件或父调用、加载下一页后保持当前阅读锚点；用户滚动和点击优先。无真实步骤关联的日志保留在任务末尾，不按时间或 logger 名称分配到某层。

历史配置在任务流程内以只读字段和文本展示：展开任务输入下的“执行时配置”可看完整快照，在所选步骤检查器中可看该步骤关联组件当时的配置。没有配置来源选择、编辑按钮或跳转；缺失快照显示未记录，不使用最新配置补充历史。新记录保留原值；旧版已省略或替换的内容无法还原，页面会说明历史采集限制。分层配置展示当前实例；历史任务只使用执行时快照，不用当前值回填。进入配置编辑再返回任务流时，保留任务、筛选条件、展开状态和阅读位置。模型与工具调用按真实 trace/span 合并开始和结束，Gateway 主调用关联到本任务的“Agent 执行”，子步骤可从树或图展开查看。上下文按快照 ID 关联，权限与日志按真实调用标识归属；仅有任务关联的记录留在任务末尾，不按工具名或时间猜测。历史缺失的父调用在步骤展开区说明，结束事件缺失仍明确显示。后台代码任务通过工具返回的真实任务 ID 保留关联及调用时状态；独立 code-worker 的完整执行过程尚未接入此索引，界面显示此缺口。

### 持续记录与留存

Agent 执行内部使用统一过程卡片展示 Native、LangGraph 和 Codex。模型卡直接提供“本轮发送的消息”和响应；工具卡提供参数、执行结果与“实际交给模型的结果”，后者保留宿主处理后的内容。子 Agent 的委托输入、配置、返回及内部步骤就地查看，收起父卡不会隐藏子步骤。缓存返回和提前失败也有委托记录。

Codex 过程适配器识别命令、文件变更、MCP、搜索、计划、思考活动及逐条公开消息。已提供的活动正文和错误可展开；思考没有公开摘要时显示未提供正文。一次 Codex exec 标为“Agent 后端执行”，只有实际模型调用边界才标号。宿主工具执行与 Provider 活动标明来源，没有共享 ID 时不按名称合并；不采集隐藏推理或推断未公开轮次。

卡片收起时仍提供可见区域的有界输入输出预览；展开直接显示已采集正文。流式消息按同一消息 ID 更新，结束或补页不会产生重复卡片；新增观测消息不用于聊天交付。三种 Runtime 的格式在运行端适配，Console 统一读取现有事件和正文接口，历史未采集的信息不回填。

Gateway 运行进程持续写入独立的 `observability/index.sqlite3` 和按任务保存的详情文件，Console 关闭不影响记录、恢复校对或定期清理。任务生命周期、授权决定及消息交付由原有业务状态持有者决定，观测只保存投影；诊断失败不改写权限、任务结果或交付事实。成功、失败、取消和等待恢复的任务均记录，准入前拒绝单独进入实例审计。

任务摘要、结构化阶段指标、错误分类和私有配置原值快照长期保留。任务输入输出、工具参数与结果、上下文与提示词正文、关联日志按任务结束时间保留 30 天；运行中与等待恢复的任务不进入到期清理。单条正文上限 64 KiB、上下文每份 8 MiB、每任务详情合计 64 MiB，同时服从有界复制的结构和聚合字符串预算。到期、未采集、已截断、采集失败分别显示。清理只操作观测详情，保留业务任务状态、交付回执原件、会话记忆、人格和工作区文件。

任务日志只采集带真实任务关联的运行日志；“服务日志”仍为独立参考，不并入任务证据。Channel 投递的开始与返回合并为一个步骤，按该消息的出站记录和回执自动更新为 Provider 已确认、失败或交付结果未知；即使返回事件尚未加载，已有回执也可更新状态。历史投递使用本任务既有的固定出站消息标识关联，不改写旧记录。任务完成本身不能证明投递成功，也不推断平台显示或用户已读。隐藏推理及 Provider 未提供的内部状态不记录。

### 只读接口

`GET /api/bots/{instance_id}/gateway-observation/runs/{run_id}/stream?after={seq}` 提供 SSE，
支持 `Last-Event-ID`，只发送该实例任务的持久事件及有界增量正文，禁止缓存。

- `GET /api/bots/{instance_id}/inspection?run_id=...&event_seq=...`：当前、已加载与任务/阶段执行时配置；阶段必须属于选中任务。当前条目补充 `effective_config/effective_environment`，比较结果通过 `configuration_status`（`applied/pending/unknown`）及原因返回；原始字段和 `pending_changes` 保持兼容。
- `GET /api/bots/{instance_id}/gateway-observation`：分页历史和最多 100 条实例准入审计。筛选参数为 `since/until/state/config_id/runtime_id/model/component/error_code/search/min_ms/page/limit`；时间使用 Unix 秒，单页最多 100 条。
- `GET /api/bots/{instance_id}/gateway-observation/metrics`：同一筛选范围的聚合指标、组件分组和趋势。
- `GET /api/bots/{instance_id}/gateway-observation/runs/{run_id}`：任务摘要、首批阶段、出站状态和交付回执。
- `GET /api/bots/{instance_id}/gateway-observation/runs/{run_id}/events?after=...&limit=...`：按序号增量读取，默认 200 条、最多 500 条。
- `GET /api/bots/{instance_id}/gateway-observation/runs/{run_id}/details/{body_id}`：按需读取当前实例、当前任务范围内的不透明正文引用。

Console 只读查询观测索引，不复制整份 Gateway 业务状态库，也不受旧 64 MiB 状态快照上限约束。目录、数据库及正文校验 owner、私有权限、普通文件与链接边界；读取使用有界查询和 no-follow descriptor。响应带来源、时间、配置版本、完整性或正文留存状态，并使用 `Cache-Control: no-store`。无法安全读取时返回脱敏错误。Console 不创建 Agent、连接插件、调用模型或推进 writer generation。

接口中的 `layer` 继续使用既有配置与观测分组标识；实体 ID、配置指纹和历史快照保持原有含义。任务事件的 `data.runtime_layer/operation/flow_version` 表达四层职责和交接阶段，`source/target` 随 metadata 持久化，开始/结束正文仍经既有不透明引用按需读取。新增字段不触发数据库或历史记录迁移。

尚未生成观测索引的实例明确显示运行观测不可用。Console 不再回退读取或复制 Gateway 业务状态库；底层独立诊断读取能力仍可用于运维，不作为旧版任务流展示入口。不会从当前配置补造历史快照，也不会将缺失过程填成成功。

### Legacy 任务记录

Legacy `/tasks` 接口继续只服务 ACP/adapter artifact；Gateway 访问它时保持原有 unavailable 契约，前端使用上述原生接口，不回退旧 Relay/cc-connect/ACP 记录。以下任务列表、八层转换、上下文和删除说明仅适用于 legacy edge。

Legacy 实例仍在“任务”中使用原有任务记录组件，不在前端扫描任务推导服务状态。可选择任务并
查看外部渠道、adapter、ACP、中间件、主 Agent、模型、工具/子 Agent/流程和回复交付证据。
统一的“任务”入口复用原有 Legacy 任务组件；分层配置、运行状态与任务使用同级页签。
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

## 源码入口

- [src/chatcopilot/gateway/observation_store.py](../../src/chatcopilot/gateway/observation_store.py)
- [src/chatcopilot/gateway/observations.py](../../src/chatcopilot/gateway/observations.py)
- [console/backend/routes](../../console/backend/routes)
- [console/web/src](../../console/web/src)
