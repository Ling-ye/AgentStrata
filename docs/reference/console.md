# Console 与前端边界

修改页面、API 或配置编辑时阅读。Console React 是 AgentStrata Studio 的统一工作台外壳，提供应用入口、控制与观测；运行状态、执行、评分和交付事实仍由各领域提供。

独立应用使用相同 Console；`CHATCOPILOT_SOURCE_ROOT` 可显式选择完整安装副本，默认仍使用 Console
所在源码根。安装声明限定其应用发现范围，任务、配置、反馈和版本控制复用现有接口。
安装与本机优化见[独立应用指南](../guides/instances.md#独立应用交付与本机改进)。

## 界面与数据

固定侧栏的「许可与源码」通过只读 `/api/legal` 显示项目版权、AGPL-3.0-only、无担保
说明、对应源码和第三方声明。`/third-party-notices.txt` 提供第三方全文；该路径及
`/LICENSE`、`/COPYRIGHT`、`/THIRD_PARTY_NOTICES.md` 在 Vite 开发模式与生产产物中一致。
未设置部署对应源码时明确提示，配置要求见[公开契约](publication.md#许可与对应源码)。

保持现有 React、Vite/Rollup、Arco Design 和 TanStack Query。共享视觉语义复用样式 tokens、业务组件与状态映射；操作页信息密集、可扫描，优先完整呈现加载、空、错误、禁用和窄屏状态。

服务端读取走 Query；SSE 使用专用 hook。局部选择与展开留在组件中，异步响应必须绑定实例与任务。使用原生 Arco API；标签内部保持横向单行，超长时省略并可查看全文。

观测与私有值见 [任务观测](observability.md)，开发步骤见 [开发指南](../guides/development.md)。

## 源码入口

- [console/web/package.json](../../console/web/package.json)
- [console/web/src](../../console/web/src)
- [console/backend/routes](../../console/backend/routes)
- [console/control/yaml_editor.py](../../console/control/yaml_editor.py)
- [console/control/catalog.py](../../console/control/catalog.py)

## 控制台契约

- 工作台是实际使用和管理入口，不是营销页：信息密集、安静、可扫描，优先支持重复运维操作和异常定位。
- Console 管理视图直接展示实例配置与私有观测原值，包括身份名单、凭据、环境引用和路径；不再次脱敏已有可读取字段。值仅进入私有存储及禁止缓存的 API，不进入公开源码或诊断导出；历史已省略字段不能补造。共享 artifact、Evaluation、群聊/工具授权及隐藏推理边界保持，契约见 `docs/reference/observability.md`。
- 保持 React 18 + Vite/Rollup + Arco Design + TanStack Query；不默认引入 Tailwind、shadcn、MUI、Storybook 或 Playwright 视觉测试等新栈。
- 触碰页面时使用原生 Arco API，不恢复旧 UI 语义兼容层。
- 文本、状态标签和按钮层级优先复用 `styles/tokens.css`、`styles/components.css` 与 `shared/ui/status.ts`。
- 修改后优先用当前 AI 环境已有浏览器工具检查桌面和窄屏；没有浏览器时至少完成构建并说明未做视觉验证。

### 统一应用工作台（首期）

一级入口为「我的应用」「创建应用」「模型与连接」「高级管理」。原有实例、服务、测评与
Harness 页面保留在高级管理中，不再另造管理后端。应用地址使用
`#applications?application=<app-id>&tab=<tab>`，支持概览、使用、配置、记录和改进。

统一目录是来源视图，不是第二份 BotSpec 注册表。仓库实例使用 `repo:<instance-id>`，
组件安装使用 `plugin:<instance-id>`，Studio 项目使用 `studio:<project-id>`。显示名称可改，
身份不能由名称推断；按规范化配置路径去重，Studio 生成的 QQ 配置保留 Studio 生命周期归属。
列表只表示已发现应用，不表示进程、渠道或端到端验证成功。

首期能力按来源开放：

- 仓库应用复用原实例管理器的启停、配置和记录入口。测评与 Harness 链接预选当前实例，
  仍走已有领域接口。应用工作区的测评与 Harness 列表通过服务端解析应用身份，
  按精确实例 ID 在分页前筛选；进入高级管理后仍明确显示其全局历史边界。
- 组件安装保留既有只读边界，不因出现在应用列表中获得启动或配置权限。
- Studio QQ 应用可以创建、编辑连接、检查、启停和提出配置需求；管理模型只接收名称、
  回复要求与用户需求，凭据在专用配置入口填写。模型连接引用 Console 的模型配置文件，
  不复制账号或凭据。配置提案需用户点击应用，不自动部署或启动。
- 已有桌面开发项目只读展示管理与开发记录，已就绪的构建可经原产物摘要校验后下载；
  这不是启动已安装应用，安装仍由用户执行。开发仍使用原 Studio。统一创建入口暂只提供 QQ，
  不生成无法继续操作的桌面项目。

「使用」与「改进」不是同一会话。Studio QQ 应用在新启动时建立专属 `console-preview`
Gateway 客户端，身份为 `user`、工具白名单为空；凭据只保存在本机私有运行配置。
试聊只返回工作台，不向 QQ 投递，不复用 owner 身份、QQ 会话或管理对话。
旧运行进程没有该身份时，明确要求用户手动停止再启动，不自动改动运行中的应用。
应用 ID 必须进入路由、查询键、草稿和异步操作范围。

试聊消息有持久请求 ID，每应用只允许一个非终态请求。重复提交不能更换应用或内容；
连接结果未知时先查询原 run，不生成新请求重发。只在明确不存在原 run 后，用户才能
以原 ID 重试；停止请求不等于已停止，最终状态以 Gateway 查询为准。
Gateway 返回 `recovery_required` 时保留原请求并提示高级诊断；没有活跃 worker 的请求
不保证能够立即取消或恢复，更不能换 ID 自动重跑。试聊成功不代表 QQ 平台权限、渠道连接或真实收发已验证。

Studio 生成的 QQ BotSpec 位于仓库外，当前不能直接接入仓库 Eval/Harness resolver；
不放宽源码路径检查来伪造支持。Gateway 业务任务流也不等于旧实例任务流。
应用记录页区分试聊记录、开发管理记录、测评记录和 Harness 任务，不把 Console 后台任务当作业务会话。

管理请求使用稳定消息 ID，重复请求不能更换应用或文本。Console 只恢复自己的配置处理
状态，不消费原 Studio 的桌面开发队列；不把网络失败解释为成功。新应用接口仅允许本机
同源访问并禁止缓存，校验错误不回显凭据请求体。

| 端点 | 方法 | 用途 |
| --- | --- | --- |
| `/api/applications` | GET / POST | 聚合目录 / 创建 QQ 应用 |
| `/api/applications/{id}` | GET | 来源、能力和应用详情 |
| `/api/applications/{id}/configuration` | GET / POST | 支持的应用配置 |
| `/api/applications/{id}/check`、`start`、`stop` | POST | 交给来源管理器执行确定性操作 |
| `/api/applications/{id}/chat` | GET / POST | 专属身份业务试聊与持久记录 |
| `/api/applications/{id}/chat/{message}/retry`、`abort` | POST | 原请求恢复与停止 |
| `/api/applications/{id}/improvement-readiness` | GET | 只读核对源码接入条件，不代表执行就绪 |
| `/api/applications/{id}/evaluations`、`harness` | GET | 仓库应用专属分页历史 |
| `/api/applications/{id}/builds/{message}` | GET | 已验证桌面构建下载，不启动安装 |
| `/api/applications/{id}/builds/{message}/desktop/{action}` | POST | 明确选择并校验安装绑定（bind）；launch 因运行依赖证据缺失而拒绝；不接收路径/命令 |
| `/api/applications/{id}/messages` | GET / POST | 应用记录 / QQ 管理需求，不是业务聊天 |
| `/api/applications/{id}/proposals/{message}/apply` | POST | 确认当前应用的最新配置提案 |

分阶段设计与后续边界见[统一应用工作台规格](../../specs/desktop-app-studio/spec.md#统一应用工作台与渐进整合)。

### 未接入应用的就绪条件

Studio QQ 的配置目录不是独立安装。把外部 BotSpec 加进一个路径白名单仍不足以运行测评或
Harness：`bot_relative_path` 必须在冻结的基线与每个候选中存在，实际运行身份要能对应同一源码
摘要，开发可写范围也必须属于目标应用。现有 Evaluation service 与 HarnessController 各自
只有一个源码根所有者，不能按浏览器提交的路径切换任意仓库。

QQ 现提供显式的“准备源码 → 提示词改进与测评 → 安装验收版本”流程。应用专属服务以冻结
源码为根，首版只允许修改身份和回复提示词；全局 Eval/Harness 入口仍不可复用。开发请求
绑定应用、目标和源码版本，安装从所属任务重新核验完整接受回执，不接受浏览器传入路径或
验收声明，也不自动启动 QQ。源码安装、进程加载及真实交付仍分别取证。受控交付的唯一契约
见[源码交付](delivery.md#studio-qq-提示词交付)。

新桌面构建保存完整程序目录的启动清单，并在原构建回执中记录清单摘要。统一应用的使用页
先选择已就绪构建，再通过 Windows 文件选择器明确选择该构建已安装的 EXE。固定本机脚本
核对程序、资源与运行源码摘要、安装身份、文件归属、写权限和重解析点；仅在全部通过后
将项目、消息、清单、脚本摘要及程序路径写入私有绑定。目前启动仍拒绝执行，不接受 HTTP
传入路径或命令，不扫描猜测安装目录、不注册系统协议、不自动安装或升级。
当前只支持同一 Windows 用户的标准安装及未切换的精确运行版本；缺少清单的旧构建和
仅导入载荷的 Live2D 项目仍从已安装客户端打开。更改脚本、程序或运行版本后必须重新核对，
不能靠重新绑定绕过构建摘要。准备环境会生成 Python、插件依赖和 MCP 运行文件，但原
bootstrap 没有形成绑定可信下载物的完整执行文件清单。仅匹配 artifacts_digest 声明不能证明
这些已解包文件未被修改，因此 UI 明确禁用启动，HTTP 与原生脚本同样拒绝。不能用绑定时
读取现有环境的摘要、或由本地环境自行写出的声明代替独立构建依据。后续方案见工作台规格。

### 控制台页面

| 页面 | 功能 |
| --- | --- |
| 总览 | 实例状态汇总 |
| 服务管理 | 按 Channel 接入与外部能力查看服务职责、状态、诊断与日志 |
| 机器人实例 | 任务 / 分层配置 / 运行状态，三个同级页签；分层配置使用四层导航、分组列表与详情抽屉；任务内左侧列表、四层运行轨迹、Agent 内执行列表/关系图和结构化检查器，支持两步对照；服务日志统一入口 |
| 机器人定时任务 | 选择实例，配置调查要求、群号、时间/时区和星期，预览、启停与查看报告及投递回执；[调度边界](schedules.md) |
| 组件目录 | 按 tools / prompts / agents / context 四个 surface 统一浏览工具、提示词、Agent 和上下文组件（只读卡片） |
| 测评页面 | 开始测试 / 运行记录 / 进步趋势 |
| AI Harness 改进 | 应用接入与改进需求、失败修复和历史；详情显示候选对照、独立验收、实例版本、分层流程及独立命令日志 |
| 代码熵回收 | 全仓熵回收发起、可选定时与历史；复用 Harness 详情展示主题、规则、源码证据和交付 |
| 模型配置 | 集中维护连接、模型方案与用途引用，按实际连接发现模型和推理选项 |
| 设置 | 控制台本身 |

### 控制台 API

固定资源路由（例如定时任务 `/api/bots/{id}/schedules`）先于通用机器人动作路由
`/api/bots/{id}/{verb}` 注册，避免资源名称被解释为控制动作。相关接口回归同时覆盖单独
router 和完整 Console 应用，检查路由共存时的保存、权限校验及原有启动/停止/重启入口。

AI Harness 修复记录按业务阶段、准备版本与修复轮次展开。步骤收起时保留关键输入与结论，
展开后使用结构化树／JSON 查看结果及依据；完整执行正文按需读取。当前或最终步骤默认展开，
新步骤出现和轮询保留用户的展开选择，任务切换隔离请求与正文。

已完成命令（包括非零退出码）只在独立日志区平铺展示，可选择执行来源、分页及筛选非零退出码；
公开过程消息显示在所属步骤内部，不变成流程节点。运行中的概览、流程、已展开步骤与日志更新，
终态补取最后结果；缺失与截断显式提示。数据语义及接口由[修复流程记录](harness.md#修复流程记录)维护。
角色步骤详情显示上下文大小、命令输出计数和裁剪告警，不展示私有 SourceIndex、FailureBrief 正文或源码关键词。

| 端点 | 方法 | 用途 |
| --- | --- | --- |
| `/api/bots/{id}/gateway-observation` | GET | 当前实例 Gateway run 和独立准入审计 |
| `/api/bots/{id}/gateway-observation/runs/{run_id}` | GET | 当前 run 的诊断事件和交付回执 |
| `/api/catalog` | GET | 统一组件目录（tools + prompts + agents + context） |
| `/api/catalog/{item_id}` | GET | 单个目录条目 |
| `/api/bots/{id}/tools` | GET | 读取实例当前工具配置 |
| `/api/bots/{id}/tools` | PUT | 写回工具配置；`?apply=true` 时同步到运行实例并重启 |

### 应用改进工作台

「接入与改进需求」选择仓库应用，按该应用已保存的 LangGraph/Codex runtime 筛选；
展示独立 Agent profile 与 Eval 预检条件。开发 worker 另选模型，不改写应用 runtime、
模型绑定或现有会话。需求必须给出具体目标与源码可写范围；外部仓库沿现有固定版本、许可与用途包络调查。

Console 和 CLI 共同调用 `HarnessController`。应用目录、评测预检、候选对照和版本状态均为只读查询；
创建、取消、恢复、版本准备/启用/撤回/对账均为显式写操作，保留本机同源边界。
页面不保存调度状态，不自动推进恢复，不把网络错误解释为任务取消或操作成功。

对照按同一候选轮次展示基线、候选与独立确认，缺失结果保持未记录；已接受任务优先展示回执绑定的轮次。
目标检查、回归保护、审核、接受回执和实际运行身份分别呈现。插件安装可作为启用目标，准备时核对
其与冻结仓库应用的基线、配置和模型是否配对；页面选择不跳过版本控制层的检查。
旧协议任务只读展示，停止执行进度轮询，不提供继续、启用或撤回操作；历史库切换沿现有 `cutover`，不新增迁移器。

| 端点 | 方法 | 归属 |
| --- | --- | --- |
| `/api/harness/applications` | GET | 仓库应用、runtime、Agent profile 与登记启用目标 |
| `/api/harness/applications/{id}/preview` | GET | 原 Eval 应用预检，不发起模型执行 |
| `/api/harness/improvements` | POST | 原 `integrate` / `improve` 任务与稳定请求 ID |
| `/api/harness/tasks/{id}/comparison` | GET | 已保存的基线、候选、独立确认与接受事实 |
| `/api/harness/tasks/{id}/activation` | GET | 版本操作状态与实际运行身份，不执行对账 |
| `/api/harness/tasks/{id}/activation/{action}` | POST | `prepare` / `activate` / `rollback` / `reconcile` / `cancel`，复用既有版本事务 |

使用步骤见[通过 Console 提交需求](../guides/harness.md#通过-console-提交需求)。

### 四层配置工作台

配置导航按 [四层运行时职责](../../specs/runtime-four-layer-definition/spec.md) 组织；这是 Console
展示归属，不改变后端实体 `layer`、BotSpec 格式或历史任务快照。Agent 层各组默认展开，
普通字段直接显示保存配置解析值、用途与来源，包括默认值、停用项和不适用参数；长正文、
schema、原始配置及操作放在详情中。其余层沿用摘要列表与详情抽屉。

| 层 | 分组与归属 |
| --- | --- |
| Channel | 渠道连接、平台适配；包括 QQ/OneBot 和现有 Feishu adapter |
| Gateway | 网关与协议、身份与准入；包括 Owner/Admin、私聊准入和固定权限策略 |
| Application | 会话与工作区、记忆与知识、资料与项目、输入处理；包括 RAG、Wiki、Skills、代码仓库和运行特性 |
| Agent | 主 Agent 与模型、提示词、搜索与研究、子 Agent 与委托、工具包、MCP、具体工具 |

实例名称、ID、部署、服务和版本信息由实例标题旁的「实例信息」抽屉提供，不构成第五层。
Application 管资料来源与装配，Agent 管调用这些资源的工具，已配置的关联条目可相互跳转。
同一实体拆成不同分组时保留原始 ID，以分组和 ID 组合定位，不重复展示同一配置字段。

- 跨层搜索支持名称、ID、字段和配置值；结果显示所属层和分组，选择后打开对应详情。
  地址使用 `instance/tab/layer/section/entity`，未指定条目时恢复该实例上次所选层，首次进入 Channel。
  原「能力与工具」入口已移除，旧无效页签地址回到任务页。
- 桌面详情抽屉宽 520px；内容区域不足 860px 时导航改为层选择器、列表堆叠、抽屉全宽。
  关闭详情保留列表位置与焦点。未配置、不适用、读取失败、截断及过期运行状态分别显示。
- 同实例共享一份草稿，切换层或页签、刷新观测不覆盖修改；放弃恢复已保存配置。
  离开页面或切换实例时提醒丢弃草稿，刷新和关闭浏览器使用原生未保存提醒。

Agent 顶部展示 Backend 与实例默认模型；`chat` 是基础模型，`research` 是研究槽，
`code` 是 Codex 默认模型及配置档。独立代码任务按自己的配置档执行；当前会话 `/model`
选择以任务记录为准。研究参数逐字段继承基础模型，环境覆盖与 Code profile 解析同运行时。
主循环预算与 Codex 超时分开说明；宿主固定策略不作为 BotSpec 可编辑参数。
预设与自定义子 Agent 展示合并后的定义、预算及模型来源，自定义项不受预设草稿过滤。

静态配置不显示加载状态，缺少观测不生成逐行「运行状态未知」。工具包、工具和 MCP
仅在观测有效时显示加载、注册或连接事实；实例注册不等于所有调用者均有使用权限。
快照缺失或过期在顶部统一说明，详情保留原因；保存配置与有效服务值不同时分别展示。
顶部「运行状态」页签保留。设计及验收见
[Agent 配置展示规格](../../specs/agent-configuration-presentation/spec.md)。

### 工具配置编辑机制

- 前端 `BotToolEditor` 沿用 `tools.packs/features/hide/mcp.servers` 与 `agents.presets/workflows` DTO。
  工具包、MCP、子 Agent 和 Workflow 在 Agent 层操作；运行特性在 Application 层操作。
  工具包与运行特性分别从对应目录添加；无可添加 Workflow 时不展示添加按钮。
  模型、环境变量、权限和其他未有写入入口的字段保持只读。
- 后端 `console/control/yaml_editor.py` 使用 `ruamel.yaml` round-trip 编辑应用所引用的 Agent profile 与 MCP 文件，保留注释和格式；工具或子 Agent 编辑不会把字段写回 `bot.yaml`。该依赖已声明在 `console/requirements.txt`，`deploy_console.sh` / `setup_console.sh` 安装时会一并装入 venv。配置来源标明 AgentProfile，观测和 Eval 指纹包含解析后的配置。
- `console/control/catalog.py` 通过 `component_catalog` 读取 tool pack / tool feature / MCP catalog / subagent preset / workflow DTO，并聚合提示词占位和上下文来源占位为统一 `CatalogItem`。
-  编辑后点「保存并重启」会先取得同实例 TaskManager 串行资格，再写入源仓配置并调用统一 `update_instance.sh`；该入口通常同步后快速应用配置并重启，只有依赖、安装脚本变化或实例 venv 缺失时才完整 bootstrap。仅「保存配置」同步写源仓。配置修改留在 WSL 源仓，由用户在 WSL git 工作区提交。
- 仅在存在草稿、待应用配置或正在应用的任务时展示底部操作栏。未部署实例仅可保存；保存失败保留草稿。
  应用任务启动不等于配置已写入或已生效，任务最终状态独立查询，关闭日志不停止检查。
  「已应用」只采用有效的运行观测；服务停止、观测过期或读取失败时显示暂无法确认。

## 代码熵回收入口

独立代码熵回收页使用统一 Harness 创建、任务查询与详情，展示回收主题、规则与源码依据、
阅读范围、未决项、验证和 PR 结果。步骤、产物和分页命令日志复用修复详情。定时默认
关闭，配置入口沿用本机写操作边界；行为见[代码熵回收](code-health.md)。

## 微信渠道绑定

微信条目在「机器人实例 → 分层配置 → Channel」提供二维码、验证码、取消及重新绑定。
绑定必须在实例停止时进行，并持有同一 Gateway 实例锁和配置任务资格；Console 不创建 Agent 或接收聊天消息。
绑定与实际连接分别展示：连接状态通过已认证的 Gateway `health` 查询，不用进程存活或旧日志代替。

`/api/bots/{id}/channels/weixin/status` 查询绑定及连接状态；`login` 的 POST 创建绑定会话，
`login/{login_id}` 的 GET 查询状态、DELETE 取消，`login/{login_id}/verify` 的 POST 提交验证码。
接口只接受本机同源请求并禁止缓存，验证码和凭据不进入操作日志。
取消、过期或被替换的会话不能保存凭据；同账号可重新绑定，不同账号要求建立新实例。
