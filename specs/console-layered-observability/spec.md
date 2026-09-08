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

分层配置在同级页签内直接展示，不使用辅助抽屉。分层配置覆盖部署、Gateway、Channel、身份权限、应用上下文、Agent 模型、工具插件、BotSpec 和协议数据版本；分组为平铺标题与定位锚点，组件字段及状态直接展示，不递归折叠配置对象。已配置、已加载、已连接分别显示。分层配置固定展示最新保存配置，加载与连接状态仅来自未过期的运行对象；当前字段不被旧运行快照覆盖，运行时动态组件保持真实状态，未应用变更只提示，不提供来源切换。历史配置留在任务流内：任务级快照可就地展开，步骤展开后直接显示精确关联组件的执行时配置文本，不提供编辑或跨页跳转；未记录的快照不得用当前配置补造。编辑复用现有入口，返回时保留任务、筛选条件、展开状态和阅读位置。页签可在窄屏横向导航，内容随页面宽度排列；刷新保留当前页签，任务选择和筛选条件按实例保留。

### 自动记录与查询

运行进程持有独立的 SQLite 观测索引和按任务保存的详情文件，Console 仅进行受限只读查询。查询不复制整份业务状态库，不受旧 64 MiB 快照限制。任务、权限与交付结果仍归现有权威状态持有者；观测是可校对的投影，失败不能改变执行结果。运行端在启动时校对历史摘要、定期刷新运行快照并清理到期详情，不依赖 Console。

摘要长期保留，包括阶段结构、状态、耗时、错误分类、模型调用、实际 Token 用量、配置版本和私有参数原值。输入输出、上下文、提示词正文及任务关联日志按任务结束时间保留 30 天。运行中和等待恢复的任务不清理。正文每条 64 KiB、上下文每份 8 MiB、每任务详情合计 64 MiB；到期、未采集、截断和采集失败分别表示。清理仅作用于观测文件，不删除任务控制记录、回执原件、会话记忆、人格或工作区。

Gateway 私有观测保存宿主可见字段的原值，包括凭据、平台身份和机器路径；隐藏推理及二进制正文不采集。控制台可见性与读取边界以 `specs/console-operator-visible-values/spec.md` 为准。详情读取按实例、任务及不透明引用校验，拒绝越界、链接、异常所有权和文件权限。准入前拒绝保留为实例审计，不创建虚假 run；工具可见性与执行门禁分别记录。没有准确任务关联的实例日志仅作时间窗口参考。

历史默认 24 小时，可选择 7 天、30 天或自定义范围；支持状态、配置版本、Backend、模型、组件、错误码、耗时和 ID 筛选。后端分页、阶段事件增量查询和指标查询共用筛选语义。指标保留样本数，20 个有效完成样本后返回 P50/P95；并发时长与嵌套 Token 不重复累计，未知不当作零。只提供自动记录，无人工备注、收藏、双任务手动对比或自动配置优化。

### 接口与兼容

新增 `/api/bots/{instance_id}/inspection`，返回当前、已加载或任务执行时的配置投影。扩展 `gateway-observation` 接口族的历史分页、任务详情、增量事件、详情正文和聚合指标；返回来源、时间、版本、留存与完整性字段。详情使用 `no-store`，不接收任意文件路径。旧 Gateway 状态和 Legacy 任务仅展示已有事实，缺失快照不能从当前配置补造。旧受控接口与运维操作保留。“任务”合并只组合现有列表与 RunInspector，不新增后端接口、存储或迁移。导航统一使用 `tab=tasks`，旧 `tab=observation`、`tab=history` 保留实例与任务参数并进入任务页；Legacy 任务从统一入口消费原有接口。任务页不可见时停止轮询，切换任务后旧请求不得覆盖当前内容。浏览器持久状态仅保存 ID、筛选、展开状态与阅读位置，不保存任务正文或配置内容。

服务列表显示类型、状态、版本、实例关联及日志操作；组件目录显示实例配置与已观测运行状态，并能跳转到实例对应组件。运行部署、准入、角色、授权、Evaluation 生命周期及现有 Git 工作保持原有边界。

## Acceptance

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
