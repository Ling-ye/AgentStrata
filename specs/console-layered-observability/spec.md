---
id: console-layered-observability
type: architecture
status: implemented
created: 2026-09-07
---

# Console 运行观测

## Summary

机器人实例以“任务”作为默认工作台：左侧检索和切换任务，右侧逐步展示执行过程和数据，分层配置使用独立同级页签。持续自动记录全部已创建任务，支持历史检索和执行时配置文本回看。服务管理与组件目录采用统一的紧凑管理界面，不展示源码教学文案。

## Design

### 工作台

沿用 React、Arco Design、TanStack Query。机器人实例使用任务、分层配置、运行状态、能力与工具四个同级页签。头部保留实例选择、服务状态及运维操作，服务日志统一为一个入口，删除实例 MCP/工具包计数行。运行统计不再出现在实例界面，也不发起统计查询；既有后端聚合接口与记录保持。任务输入、连续步骤卡片、最终结果及交付回执沿页面排列；不使用固定高度的流程滚动窗口或共用详情页签。任务流参照 `a4e1f129bb0542f879e01dd8256572b88f2a15ea` 的纵向连接线、状态点和步骤卡片布局；左侧显示名称、状态、流转关系和摘要，右侧显示开始时间、耗时及已记录 Token，参数与结果在本步展开，可以同时展开多步。当前执行和异常步骤默认展开，用户调整优先于自动更新；支持展开全部和收起全部。真实父子关系使用缩进和连接线，子步骤不依赖父步骤展开。

同一 trace/span 的开始和结束归为一次调用，上下文只按明确 snapshot ID 关联，权限和日志只按真实调用标识归属。无法定位到步骤的任务记录留在任务级区域，不按名称或时间推测归属。上下文、日志和原始记录一次展开即加载；会话历史和模型可见输入以角色消息卡片分组展示，桌面并列，窄屏上下排列；正文随可见区域按需读取，长正文先预览，再展开全文。任务执行与交付分别显示，缺失、到期、截断和采集失败保持明确。

“任务”工作台可用宽度达到 960px 时使用双栏：左侧固定 300px，随页面滚动保持可见，列表自身滚动；右侧占据剩余宽度，流程沿页面自然展开，没有固定高度或拖动分隔条。不足 960px 时使用同一工作台的单列列表/详情切换，“任务列表”按钮返回列表，选择任务后显示详情，不新增抽屉或页签。参数与结果、上下文按正文区域宽度排列。

左侧常显时间范围、状态和任务 ID 搜索，其余条件在“更多筛选”原处展开。列表仅显示开始时间、执行状态、短 ID、耗时和模型，完整 ID 可查看和复制，不逐项读取正文。服务端每页 50 条，默认最近 24 小时。首次恢复当前实例选择，否则选择第一条；轮询、新任务、筛选与分页不抢占正在阅读的任务，不在当前列表的任务在详情头部说明。列表刷新保持滚动位置；任务展开状态和阅读位置分别按实例与任务保存，首次查看从详情顶部开始，返回已读任务恢复位置。列表与详情加载失败分别处理。

分层配置与能力配置在同级页签内直接展示，不使用辅助抽屉；具体归属以“配置归属与应用状态”为准。当前配置、加载状态和配置来源分开展示，当前字段不被旧快照覆盖。历史配置留在任务流内：任务级快照可就地展开，步骤直接显示精确关联组件的执行时配置文本，不提供编辑或跨页跳转；未记录的快照不得用当前配置补造。返回任务时保留选择、筛选、展开状态和阅读位置，窄屏页签可横向导航。

### 四层任务过程

Gateway 任务界面只展示真实四层观测，不再从旧事件名称推断职责、补造 trace/span/outbound 关联，或将无四层边界的记录回退为旧式步骤列表。保留通用调用树与当前模型、工具、权限、日志读取；仅消费 flow_version=1 的运行事件，缺少当前结构时显示未采集提示及独立的任务输入/结果。已采集的当前版本调用即使父阶段缺失，也保留真实调用和缺口说明。Legacy 平台运行链不在此次移除范围。Console 查询只使用独立观测索引，不回退复制 Gateway 状态库。

本次按操作者指定的精确 run ID 清理已完成任务历史：核对终态、会话未占用、无审批/客户端幂等依赖后，移除终态 run、旧诊断和关联事件，再清理独立观测摘要、阶段、正文及仅由该任务引用的快照。保留入站去重记录、outbox/Provider 回执底账及会话/工作区数据，防止旧消息重放。通过一次性受限维护操作实施，不增加通用删除界面或数据库迁移。

任务按真实交接顺序分段，每段标明渠道适配、网关、应用或 Agent 职责；同一层参与返回、投递或交换提交时再次出现。段落常显动作、状态、开始时间、耗时及有界输入输出，段内保持连续步骤卡片、真实父子与并行关系。段落不是折叠导航，不增加四层页签；不改变任务列表、配置页或能力分类。

输入输出和层内模型、工具、上下文、权限、执行时配置及日志就地查看。开始与结束合并为一次调用，段落和卡片用真实 trace/span 标识，不按配置 layer、名称或相邻时间重排。旧聚合事件与新细分边界不重复展示或统计。工具权限决定留在实际调用，缺少阶段关联的任务日志留在任务末尾。多步及详情展开状态按实例/任务保存；补页、迟到父节点和轮询保留当前卡片及阅读偏移，用户主动操作优先。

运行端在现有观测记录中增加 RuntimeStageStarted/RuntimeStageFinished，data 持久保存 flow_version=1、runtime_layer、operation、交接 source/target 与真实 trace/span/parent 标识；普通调用使用同一运行职责标记及明确的段落引用。开始正文保存输入，结束正文保存输出，列表与 metadata 不携带正文。既有配置 layer/实体 ID/快照/指纹和数据库 schema 不变。

准入且 run 建立后才归档渠道输入、规范消息和任务输入，资源准备失败也可查看已受理请求。CanonicalInboundEvent 的可选观测投影仅随现有有界队列驻留内存，不参与权限、相等判断或幂等；业务 ingress 写入及恢复校验统一只序列化 evidence/segments/resource_tickets。投影只含受控消息字段和采集范围，不含认证 Header、凭据、完整资源 URL 或原始帧全集。恢复缺失投影显示未记录，不反造原生报文。

Application 记录资源准备、actor 会话及 AgentTask 交接，独立保存 AgentResult 的公开输出和 stop_reason，使投递失败仍有执行结果证据。模型事件增加可选 visible_response；Native/LangGraph 保存实际逐轮公开正文与工具调用建议，Codex 仅保存适配器可见输出，不声称内部模型轮次可见，不保存隐藏推理。模型输入按 context_snapshot_id 关联；补齐既有 InputResourcesDispatched 的 request_id/resources。

Gateway/Channel 记录真实出站内容及回执，Application 在实际操作后记录群 journal 提交、丢弃、非群无需 journal 或失败，保持任务、投递与交换结果独立。scope 覆盖已获准的准备、执行、投递与提交；没有真实 span 的日志不按 logger 名称猜测归属。观测异常不改变业务结果。

未采集四层边界的任务不展示旧式步骤；不以最终结果、当前配置或后续上下文回填各层输出。入口能证明没有经过某层时显示未经过，否则显示未记录；尚有事件页时区分尚未取得与真正缺失。保留 Legacy 入口，不制造 QQ Channel 记录。正文继续按 64 KiB、上下文 8 MiB、每任务 64 MiB 和 30 天终态留存处理，明确过期、未采集、截断与采集失败。

### 配置归属与应用状态

分层配置只呈现基础机制，按部署与服务、Gateway 与协议、Channel 与平台、身份与权限、会话与工作区、Agent 与模型、子 Agent 与委托、记忆与 RAG、MCP、提示词与实例信息分组。记忆、RAG 数据源、MCP 连接设置及子 Agent 预算留在基础页。能力与工具按独立功能与插件、Skills、搜索 Provider、工具包、具体工具、运行特性分组；Wiki、代码仓库条目和开发专属参数归此页。MCP 工具和委托工具在能力页展示参数与权限，通过稳定组件 ID 定位服务器或子 Agent 配置。

展示模型拆分主 Agent 与上下文的混合对象，不改变底层 layer、组件 ID 或历史关联，不删除运行投影的组件。字段直接展示，长文本与参数原处展开；保留搜索、平铺分组锚点和单层分类筛选，删除能力页内部四面页签和笼统编辑跳转。任务页删除实例准入审计区域，不新增替代面板；后端准入审计与任务权限事件保持。

MCP 添加、删除与开关，以及子 Agent/Workflow 选择放在基础页；工具包、特性与隐藏工具编辑放在能力页。两个页签共用实例内存草稿和一次完整保存，切换页签与查询刷新不覆盖未保存编辑。保存配置只写配置；保存并重启沿用既有流程，完成后刷新 inspection、tools 和 inventory，等待新鲜运行快照确认。无编辑接口的字段保持只读，不增加通用 YAML 写入。草稿正文不进入浏览器持久存储。

当前配置使用与部署及启动相同的纯解析逻辑，补齐默认值、展开部署路径并保留实例显式覆盖；不引入 Console 进程环境。inspection 保留原字段，并补充有效环境值、比较状态与原因。新鲜、可比较且一致为 applied（已应用）；有可确认差异为 pending（待应用）；缺失、过期、读取失败或版本不可比为 unknown（暂无法确认）。空值按实际环境渲染及启动补缺语义处理。历史执行快照原样读取，缺失项不回填。解析不创建 Agent、连接 MCP、加载检索索引或调用模型。

组件目录使用明确的 tab=configuration 或 tab=capabilities；旧 entity 链接及原基础页中的能力链接按稳定 ID 转到正确页签，保留实例与任务参数。本次不增加接口、数据库、迁移或留存规则，不包含群聊 Workspace 重置工具。

### Agent 执行过程适配

主 Codex 的持续流式过程由 [Agent 流式观测](../agent-streaming-observability/spec.md) 接续：
主会话使用 App Server，原 exec 适配器保留给独立工具；统一事件、四层关联及历史存储契约保持。

三种 Backend 通过唯一 AgentEvent 契约提供过程。Native/LangGraph 共用 TurnOps 的真实模型与工具边界；Codex 过程适配器解释公开 JSONL 与宿主 Relay 事件。共享过程投影负责分类、结构化正文与关联字段；Gateway 只绑定任务和阶段并写入既有观测索引，前端统一展示适配器不解析 Backend 私有帧。不新增运行层、注册中心、数据库、队列或旧任务流回退。

Agent 段内逐步卡片常显类型、状态、时间、耗时、用量及有界输入输出预览。模型直接展示本轮有效输入与公开响应，工具展示参数、执行结果与实际交给模型的结果，子 Agent 展示委托输入、返回及可见子步骤；Codex 的命令、文件变更、MCP、搜索、计划、思考活动和逐条公开消息均可读。父卡收起不隐藏子调用，不增加内部页签或抽屉。正文按可见区域加载，展开状态、补页与阅读位置按实例和任务保持。

工具以真实 tool_call_id 和发起模型 span 关联；公开消息用稳定消息 ID 和递增 revision 合并，更新不会生成多个卡片。开始、更新、完成分别持久化，不根据名称或相邻时间猜测关联。Provider 与宿主事件无共享 ID 时分别标明来源。只观测到完成时不补造开始时间；缓存命中不伪造模型调用。新增观测消息不作为 QQ 交付事件，不能改变原回复或 credential lease 门禁。

模型输入复用对应 context_snapshot_id；请求参数只包含真实提交值。Codex exec 只表示适配器执行范围，不冒充内部第一个模型轮次；思考活动只保留明确公开的摘要，没有正文时显示未提供，不读取隐藏推理。观测故障只形成采集缺口。沿用私有原值、正文配额与到期规则，新增消息不重复计入模型/工具/Token，历史缺失不回填。

验收包含三种 Backend、多轮消息、工具处理前后结果、嵌套子 Agent/缓存/失败/取消、Codex 六类活动及更新、跨页/迟到/重复/并行关联、流式交付隔离、用量不重复、桌面及 390px 浏览器阅读恢复。本次独立运行专项、前端、生产构建与 full；确定性夹具不作为真实模型或 QQ 证据。

### 自动记录与查询

运行进程持有独立的 SQLite 观测索引和按任务保存的详情文件，Console 仅进行受限只读查询。查询不复制整份业务状态库，不受旧 64 MiB 快照限制。任务、权限与交付结果仍归现有权威状态持有者；观测是可校对的投影，失败不能改变执行结果。运行端在启动时校对历史摘要、定期刷新运行快照并清理到期详情，不依赖 Console。

摘要长期保留，包括阶段结构、状态、耗时、错误分类、模型调用、实际 Token 用量、配置版本和私有参数原值。输入输出、上下文、提示词正文及任务关联日志按任务结束时间保留 30 天。运行中和等待恢复的任务不清理。正文每条 64 KiB、上下文每份 8 MiB、每任务详情合计 64 MiB；到期、未采集、截断和采集失败分别表示。清理仅作用于观测文件，不删除任务控制记录、回执原件、会话记忆、人格或工作区。

既有 Gateway 私有配置与工具观测保持宿主可见字段原值。新增平台输入及四层交接投影仅选择必要字段，排除连接凭据、认证 Header、完整资源下载 URL 和机器路径；不改变现有配置原值展示。隐藏推理及二进制正文不采集。控制台可见性与读取边界以 `specs/console-operator-visible-values/spec.md` 为准。详情读取按实例、任务及不透明引用校验，拒绝越界、链接、异常所有权和文件权限。准入前拒绝保留为实例审计，不创建虚假 run；工具可见性与执行门禁分别记录。没有准确任务关联的实例日志仅作时间窗口参考。

历史默认 24 小时，可选择 7 天、30 天或自定义范围；支持状态、配置版本、Backend、模型、组件、错误码、耗时和 ID 筛选。后端分页、阶段事件增量查询和指标查询共用筛选语义。指标保留样本数，20 个有效完成样本后返回 P50/P95；并发时长与嵌套 Token 不重复累计，未知不当作零。只提供自动记录，无人工备注、收藏、双任务手动对比或自动配置优化。

### 接口与兼容

新增 `/api/bots/{instance_id}/inspection`，返回当前、已加载或任务执行时的配置投影。扩展 `gateway-observation` 接口族的历史分页、任务详情、增量事件、详情正文和聚合指标；返回来源、时间、版本、留存与完整性字段。详情使用 `no-store`，不接收任意文件路径。Gateway 任务查询只使用独立观测索引，旧事件不转换为当前四层任务流。Legacy 平台任务继续使用其既有独立入口，缺失快照不能从当前配置补造。旧受控接口与运维操作保留。“任务”合并只组合现有列表与 RunInspector，不新增后端接口、存储或迁移。导航统一使用 `tab=tasks`，旧 `tab=observation`、`tab=history` 保留实例与任务参数并进入任务页；Legacy 任务从统一入口消费原有接口。任务页不可见时停止轮询，切换任务后旧请求不得覆盖当前内容。浏览器持久状态仅保存 ID、筛选、展开状态与阅读位置，不保存任务正文或配置内容。

服务列表显示类型、状态、版本、实例关联及日志操作；组件目录显示实例配置与已观测运行状态，并能跳转到实例对应组件。运行部署、准入、角色、授权、Evaluation 生命周期及现有 Git 工作保持原有边界。

## Acceptance

- 指定终态任务不再出现在历史、详情或正文查询中，重建观测投影不会恢复该任务；其他任务及去重/回执底账保持。
- 删除旧宿主事件适配和旧式任务流回退；当前四层调用、真实父子/并行、交付状态与实例隔离通过回归。

- 四层往返按真实交接展示，每段输入输出可读，层内两个模型步骤、工具结果和上下文可同时展开；相同调用只出现一次。
- 拒绝准入无任务正文或资源副作用；资源准备失败保留已受理输入，模型返回后的投递/交换失败仍有独立 Agent 输出。
- 新观测投影不改变原 ingress 字节、幂等或恢复；新增 metadata 方向正确持久化，正文与列表分离，模型/工具/Token 不重复计数。
- 正常/取消/未知交付/重复确认/提交失败及非 Channel 入口均按真实事实呈现，历史缺口不补造。
- 跨实例、同名重复/并行、子任务、跨页与迟到数据不串内容；多层详情开关和阅读位置在刷新及任务切换后保持。
- 本次执行专项、前端及 full 验证，实际浏览器覆盖桌面/390px/键盘/分页/正文按需加载，保存本次证据；不将夹具当真实模型或 QQ 证明。

- 基础与能力归属符合上述分组，两个不同实例的 RAG 数据源、代码仓库、Skills、搜索和 MCP 按实例显示；不重复整份混合配置，不遗漏只读字段。
- 跨配置页编辑 MCP、子 Agent 和工具后一次保存全部草稿；迟到请求、轮询和实例切换不串数据。旧链接及 MCP 服务器/工具双向定位有效。
- 默认值、路径展开与有语义的空值不误报待应用；真实模型、权限、MCP 和引用文件变更可以识别。过期、停止与不可比较时不显示已应用或当前健康。
- 任务页无实例准入审计区域；任务选择、阅读状态、历史配置和权限事件保持。浏览器检查桌面与 390px、键盘导航、共享草稿、失败与过期状态。
- 本次独立执行前后端回归、生产构建、SDD、架构、组件目录、公开信息及 fast 检查；使用隔离夹具验证保存与应用，不改真实实例，不部署、不暂存、不提交。

- 两个不同配置实例展示不同模型、工具、MCP、搜索和上下文。分层配置自动更新最新字段，运行状态不冒充配置已应用；历史配置以不可变只读文本留在任务流程内。
- Console 关闭时仍记录；成功、失败、取消和恢复任务可检索，新增任务不改变当前选择。
- 超过 50 条任务和 64 MiB 详情可以分页读取；30 天清理只删除到期观测正文，保留摘要与活动任务。
- 步骤就地展示执行时配置、参数、结果和日志；截断、缺失与交付未知明确显示。
- 指标按调用计数、样本数和配置版本分组，不重复计算并发耗时及 Token。
- 浏览器完成历史筛选、历史配置就地展开、详情展开、编辑返回及桌面/窄屏操作；验证四个同级页签、唯一服务日志入口、无运行统计入口或请求、无配置/历史抽屉。
- 一次展开即在本步查看参数和结果；多个步骤同时展开，刷新、切换配置和编辑返回不重置阅读状态。上下文和日志没有二次读取按钮。
- 重复同名调用、并行子任务、多页事件和延迟响应不串数据；大段正文按可见区域读取，任务列表与流程共用选择和查询状态；筛选、分页和单独区域失败不影响另一侧。
- 桌面验证 300px 列表及长任务自然滚动、列表锚点保持；390px 验证单列切换与键盘操作。切换任务、实例及编辑返回恢复对应展开状态与位置；页面刷新可恢复跨页事件中的阅读锚点。

## Verification

2026-09-09，Agent 执行过程统一适配与逐步卡片：

- Native/LangGraph 的实际多轮输入、工具调用关联、处理前后结果，以及 Codex 六类活动、公开消息、更新/完成、子 Agent 缓存与失败均接入共享过程投影。采集异常标记缺口；观测配额达到上限不影响最终回复。
- 最终仓库 full 的 12 项检查通过：Python 全集 3039 passed、1 skipped、139 subtests passed；跳过项为 Windows 原生大小写语义测试。前端 12 个文件、137 项测试通过，生产构建通过。Codex/过程边界专项为 52 passed、11 subtests passed。
- 原始 full 首次受到旧 build 缓存和 reports 下历史测评工作区收集的影响；保留旧构建缓存后重建，pytest 显式限定仓库 tests，完整执行原有检查项。临时候选索引仅用于制品与公开边界检查，真实 Git 暂存区哈希不变；未修改校验脚本或历史测评数据。
- 本次生产构建由 Windows Chrome 完成 10 组浏览器检查：三种 Backend、四层结构、输入输出、多步骤展开、子 Agent、消息更新、分页及刷新恢复、键盘、390px、日志单次加载与正文失败重试。使用真实适配器和观测索引生成的隔离数据，再在浏览器拦截 API，非真实模型或 QQ 端到端证据。
- 规格和 Console 文档已同步；代码保持未暂存、未提交，不部署、不重启真实实例、不修改真实配置或 workspace。

2026-09-09，指定历史清理与旧任务流兼容分支移除：

- 已确认指定任务终态且没有活动会话、审批或客户端幂等依赖；清除其终态任务历史、63 条观测事件、53 份正文文件及 4 份无其他引用的配置快照。入站去重、投递回执底账、会话及 workspace 保持。真实搜索返回 0，详情、事件和正文接口返回 404，复查未重新出现。
- 一次性维护脚本在隔离任务中验证精确删除、相邻任务/快照保持、重建投影不恢复目标、活动任务与不安全文件拒绝，共 5 项通过；脚本和私有预检留在仓库外的本次证据目录。
- 观测索引、旧状态独立诊断、Console 接口、Gateway 调度和假 OneBot 往返相关 Python 测试：75 passed；前端 11 个文件、123 项通过；生产构建通过。
- 当前构建的 Windows Chrome 完成 4 组检查：当前四层与就地调用、旧格式不再显示旧卡片或权限失败、真实关联的交付未知、390px 输入输出排列；0 页面异常。API 使用确定性夹具，不连接真实模型或平台。
- Ruff、SDD、架构、公开信息边界和 git diff --check 通过。未运行 full profile，未部署或重启服务。原有未提交工作和暂存状态保持；此处仅记录本次结果，下方旧记录不作为本次验收。

2026-09-09，四层任务过程本次验证（下方旧记录仅供历史参考）：

- Python 3.13.15、pytest 9.1.1，固定 PYTHONPATH=src 并确认加载本次隔离工作树。渠道、序列化、模型响应与相关 Backend 专项 173 passed、19 subtests passed；后端观测与真实 Actor 交接专项 120 passed；Agent 阶段错误关联追加专项 1 passed；消息往返/接口/旧状态读取联调 23 passed。
- npm --prefix console/web test：11 个文件、123 项通过；生产构建通过。Windows Chrome 使用本次构建完成 16 组检查，0 页面异常；覆盖桌面/390px、并行重复调用、多级展开、分页、刷新恢复、迟到父节点/请求、实例隔离、未经过/未记录、交付未知和正文到期。API 使用确定性夹具。
- 仓库 full 完整 profile 通过：3024 passed、1 skipped、139 subtests passed，SDD、公开信息、架构、依赖、UTF-8、Ruff、类型、组件目录、pip 一致性、wheel/sdist 精确成员与隔离运行及 Console 构建均通过。唯一跳过为 Windows 大小写语义专属测试。
- 打包以临时 Git 清单纳入新增文件，实际暂存区不变。初轮 Git 夹具受到临时对象目录变量影响，另一轮遗漏临时目录创建；最终以仓库外启动器仅在声明需要候选清单的检查中提供 Git 变量，测试使用干净环境，不改变检查项或业务代码。相关 Git 专项单独复验 89 passed，原失败日志保留。
- 浏览器复现并修复了已读 4 页却在第 3 页找到锚点后停止恢复的问题；现在先恢复已读页数，再定位卡片。修复后目标偏移从 -10.984375px 恢复为 -10.9375px，前端测试和浏览器均重新通过。回执校验、采集状态优先级、无 trace 错误关联及旧状态新字段遗漏也经专项回归覆盖。
- git diff --check 与受影响文档链接检查通过；各专项与 full 有重叠，不合计计数。命令、原始日志、构建哈希与浏览器截图在本机本次证据目录留存。

本次保留已有改动及暂存状态，交付未暂存、未提交，不部署、不替换运行中的前端产物、不修改真实配置或 workspace。没有执行真实模型、QQ/NapCat、MCP/CDN 或生产启停验收；本地确定性组件与浏览器夹具不作为这些场景的证明。

2026-09-08，基础配置与能力归属调整的本次验证（此前记录仅供历史参考）：

- `npm --prefix console/web test`：11 个文件、115 项通过；`npm --prefix console/web run build` 通过。
- `PYTHONPATH=src /tmp/agentstrata-gateway-fix-venv/bin/python -m pytest tests/unit/test_console_configuration_resolution.py tests/unit/test_console_component_catalog.py tests/unit/test_console_observation_api.py tests/unit/test_console_operator_values.py tests/unit/test_gateway_observation.py tests/unit/test_observation_workbench.py tests/unit/test_botspec_provision_env.py tests/unit/test_botspec_runtime_env.py tests/unit/test_console_update_instance.py -q --basetemp=/tmp/asg-config-final-regression`：136 passed。
- `PYTHONPATH=src TMPDIR=/tmp/asg-cfg-fast /tmp/agentstrata-gateway-fix-venv/bin/python scripts/check_repo.py fast`：2870 passed、1 skipped、125 subtests passed，所有 fast 静态检查通过。首轮两项失败涉及过期快照夹具与旧保存回调文本断言，已修正并重跑通过。最终导航、展示与 Legacy 解析细节另经上述最终相关回归、构建和浏览器验证。
- Windows 原生 Chrome 使用本次构建完成 15 组检查：配置归属、MCP 服务器/工具双向定位、参数表、跨页草稿与轮询、隔离 GET/PUT 保存、更新替身和运行快照确认、历史阅读恢复、390px 与键盘操作、旧链接、冷启动指定实例、迟到请求、隐藏停止轮询、过期与接口单独失败。无页面异常；更新替身不执行 systemd 操作。另以隔离索引验证停止回执立即使新鲜配置变为 unknown。
- SDD、架构、组件目录、公开信息和 `git diff --check` 通过；架构为 520 modules、1593 static edges、0 cycles。截图、操作结果、精确命令与构建摘要保存在本机本次验证目录。
- 未运行 full 打包/完整集成集合，未验证真实 Legacy 平台、模型、MCP、QQ 或 systemd 更新。没有部署或修改真实机器人配置；保留既有脚本改动，交付未暂存、未提交。

2026-09-08，合并“任务”工作台的本次验证：

- `npm --prefix console/web test`：10 个文件、102 项测试通过，包含实例选择隔离、旧链接、筛选状态恢复、阅读锚点与请求取消。
- `npm --prefix console/web run build`：TypeScript 与生产构建通过，浏览器验证使用本次构建的 `index.e6a81326c2.js`。
- `PYTHONPATH=src /tmp/agentstrata-gateway-fix-venv/bin/python -m pytest tests/unit/test_console_observation_api.py tests/unit/test_gateway_observation.py tests/unit/test_observation_workbench.py tests/unit/test_gateway_application_dispatcher.py tests/unit/test_agent_trace.py tests/unit/test_main_agent_backend_unification.py tests/integration/test_gateway_onebot_roundtrip.py -q --basetemp=/tmp/asg-task-workspace-pytest`：110 passed、15 subtests passed。
- Windows 原生 Chrome 完成 18 组检查：7 组工作台操作、5 组异步与异常场景、6 组模型父调用和 Channel 投递回归。包含两个实例、超过 50 条记录、118 个步骤的跨页读取与刷新恢复；300px 左列、页面滚动与列表锚点、筛选和翻页不抢选、隐藏停止轮询、配置编辑返回、390px 单列与键盘操作。正文区域按自身宽度排列，移动列表打开后直接进入视口。
- 异步验证单独注入列表和详情 503、迟到详情与事件、正文到期；同名工具参数没有串步，日志一次展开只读取一份正文。正常操作无页面异常、失败 API 或非 GET 请求；异常流程只有预期的 503（含自动重试）。Channel 的确定性 Native Agent 与假 Provider 验证投递中自动转为 Provider 已确认，以及失败、交付未知和历史缺口。
- `python3 scripts/check_sdd_specs.py`、`python3 scripts/check_architecture.py`、`python3 scripts/check_public_repo.py` 和 `git diff --check` 通过；架构检查为 519 modules、1585 static edges、0 cycles。
- 过程报告与截图位于本机临时证据目录，本次仅修改前端和相关文档，没有新增接口或迁移。未运行仓库完整验证，也未对 Legacy 页面重新做浏览器操作；Legacy 相关前端测试保留在上述测试集中。未调用真实模型、发送真实 QQ 或部署。以下历史结果不作为本次验收依据。

2026-09-07，当前配置与历史配置文本简化的本次验证：

- `npm --prefix console/web test`：9 个文件、84 项测试通过，覆盖当前字段与运行状态分离、过期状态、缺失快照、历史文本不可变及精确组件关联。
- `npm --prefix console/web run build`：TypeScript 与生产构建通过。
- `PYTHONPATH=src /tmp/agentstrata-gateway-fix-venv/bin/python -m pytest tests/unit/test_console_observation_api.py -q --basetemp=/tmp/asg-config-auto-pytest`：1 passed，覆盖实例隔离、事件所属任务校验、正文范围、配置修改与历史快照保持。
- Windows 原生 Chrome 对新生产构建完成 10 组检查：当前配置独立于所选任务、修改与自动刷新、未应用提示；历史配置就地展开、键盘操作、多步骤保持；编辑返回、并行同名调用、缺失快照、迟到请求；115 步分页与可见区域加载；组件目录跳转以及桌面与 390px 布局。展开全部新增 2 次配置读取与 4 次正文读取，无浏览器异常、失败 API 响应或非 GET 请求。
- `python3 scripts/check_sdd_specs.py`、`python3 scripts/check_architecture.py`、`python3 scripts/check_public_repo.py` 和 `git diff --check` 通过；架构检查为 519 modules、1584 static edges、0 cycles。
- 浏览器使用隔离的真实观测后端记录，并在 inspection 响应中注入来源标记、未应用、过期、缺失与延迟场景。本次未修改运行端或存储，未重跑仓库完整验证，未调用真实模型、发送真实 QQ 或部署。以下旧版验证记录不作为本次配置交互验收依据。

2026-09-07，同级页签与任务流样式调整的本次验证：

- `npm --prefix console/web test`：8 个文件、77 项测试通过。
- `npm --prefix console/web run build`：TypeScript 与生产构建通过。
- `PYTHONPATH=src /tmp/agentstrata-gateway-fix-venv/bin/python -m pytest tests/unit/test_console_observation_api.py tests/unit/test_gateway_observation.py tests/unit/test_observation_workbench.py -q --basetemp=/tmp/asg-bot-tabs-pytest`：41 passed。
- Windows 原生 Chrome 对新生产构建完成 9 组交互检查：五个同级页签、唯一服务日志、没有运行统计请求；62 条历史分页、刷新与选择保持；步骤对应执行时配置、编辑返回与定位调用；并行同名工具及独立模型上下文；115 步分页与长正文；桌面、760px 和 390px 排列及键盘展开；组件目录跳转。展开全部读取可见范围内的 6 份正文，无浏览器异常、失败 API 响应或非 GET 请求。
- `python3 scripts/check_sdd_specs.py`、`python3 scripts/check_architecture.py`、`python3 scripts/check_public_repo.py` 和 `git diff --check` 通过；架构检查为 519 modules、1584 static edges、0 cycles。
- 本次界面验证使用隔离确定性实例。运行端、存储和权限未修改；不重跑仓库完整验证，旧版本结果不作为本次验收依据。未调用真实模型、发送真实 QQ 或部署。

2026-09-07，交互简化的验证记录：

- `npm --prefix console/web test`：7 个文件、69 项测试通过，包含同名并行调用、精确上下文与权限关联、分页合并、缺失父阶段、重复事件和展开状态保持。
- `npm --prefix console/web run build`：TypeScript 与生产构建通过。
- 以独立 Python 测试环境运行 `tests/unit/test_console_observation_api.py`、`tests/unit/test_gateway_observation.py`、`tests/unit/test_observation_workbench.py`：41 passed。
- `python3 scripts/check_sdd_specs.py`、`git diff --check` 与 `scripts/check_public_repo.py` 通过。
- Windows 原生 Chrome 完成 9 组主流程和 5 组异常场景检查：多步骤就地展开、单次读取上下文和日志、配置组件实际定位到视口内、编辑返回与刷新保持、62 条历史分页、115 步跨页流程、统计面板切换、组件跳转及 1600px / 760px / 390px 布局。展开全部仅请求可见区域的 6 份正文；缺失父阶段、结束缺口、详情到期、采集失败、读取失败重试、延迟响应及交付未知分别验证。
- 浏览器验证使用隔离实例夹具和明确注入的异常响应。主流程无浏览器异常、失败 API 请求或非 GET 请求；异常测试注入一次可重试的 503 场景，不作为真实模型、QQ 或生产部署证据。
- 本次不改运行端采集、存储或权限契约，不重跑仓库完整验证；以下初始版本的全量结果不作为本次交互验收结果。

2026-09-07，初始版本的 WSL / Python 3.10 与 Windows 原生 Chrome 确定性验证记录：

- `PYTHONPATH=src python -m pytest tests -q`：2906 passed、1 skipped、133 subtests passed。测试使用独立短临时目录，未调用真实模型或真实 QQ。
- 观测存储、接口、权限、Gateway 生命周期和 fake OneBot 相关回归：66 passed。覆盖跨实例正文拒绝、事件所属任务校验、不可变历史配置、角色门禁、真实 Registry 与 MCP 失败投影、63 条历史分页、超过 64 MiB 总详情、30 天清理、活动保护、写失败与安全删除。
- `npm --prefix console/web test`：7 个文件、63 项测试通过；`npm --prefix console/web run build` 通过。
- 真实 Chrome 操作覆盖历史搜索与翻页、调用关联配置、参数/结果/上下文/日志、权限阶段、编辑返回、刷新保留选择、键盘调宽、统计、组件跳转、服务详情以及 1600px / 760px / 390px 布局。最终检查没有浏览器异常、失败 API 响应或非 GET 请求。
- SDD、架构边界、Ruff、mypy、组件目录、公开信息边界、依赖一致性与 UTF-8 检查通过；架构检查为 517 modules、1582 static edges、0 cycles。
- `scripts/check_repo.py full` 在 wheel 资源核验处停止：已有构建残留混入旧提示词资源。默认仓库根目录 pytest 还会发现被忽略的 Evaluation 工作区测试；正式 `tests/` 全集已另行执行通过。保留这些无关产物与用户未提交改动，不调整索引绕过校验。

后台代码任务当前保留真实任务 ID、调用时状态和关联缺口，独立 worker 的完整阶段未并入索引；历史未采集字段、Provider 内部状态和未关联的实例日志继续明确区分。验证不作为生产部署、真实模型或 QQ 端到端证据。交付保持未暂存、未提交，不部署。
