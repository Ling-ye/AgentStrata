# Console 与前端边界

修改页面、API 或配置编辑时阅读。Console 负责控制与观测，运行状态、执行、评分和交付事实仍由各领域提供。

## 界面与数据

保持现有 React、Rsbuild/Rspack、Arco Design 和 TanStack Query。共享视觉语义复用样式 tokens、业务组件与状态映射；操作页信息密集、可扫描，优先完整呈现加载、空、错误、禁用和窄屏状态。

服务端读取走 Query；SSE 使用专用 hook。局部选择与展开留在组件中，异步响应必须绑定实例与任务。使用原生 Arco API；标签内部保持横向单行，超长时省略并可查看全文。

观测与私有值见 [任务观测](observability.md)，开发步骤见 [开发指南](../guides/development.md)。

## 源码入口

- [console/web/package.json](../../console/web/package.json)
- [console/web/src](../../console/web/src)
- [console/backend/routes](../../console/backend/routes)
- [console/control/yaml_editor.py](../../console/control/yaml_editor.py)
- [console/control/catalog.py](../../console/control/catalog.py)

## 控制台契约

- 控制台是运维工作台，不是营销页：信息密集、安静、可扫描，优先支持重复运维操作和异常定位。
- Console 管理视图直接展示实例配置与私有观测原值，包括身份名单、凭据、环境引用和路径；不再次脱敏已有可读取字段。值仅进入私有存储及禁止缓存的 API，不进入公开源码或诊断导出；历史已省略字段不能补造。共享 artifact、Evaluation、群聊/工具授权及隐藏推理边界保持，契约见 `docs/reference/observability.md`。
- 保持 React 18 + Rsbuild/Rspack + Arco Design + TanStack Query；不默认引入 Tailwind、shadcn、MUI、Storybook 或 Playwright 视觉测试等新栈。
- 触碰页面时使用原生 Arco API，不恢复旧 UI 语义兼容层。
- 文本、状态标签和按钮层级优先复用 `styles/tokens.css`、`styles/components.css` 与 `shared/ui/status.ts`。
- 修改后优先用当前 AI 环境已有浏览器工具检查桌面和窄屏；没有浏览器时至少完成构建并说明未做视觉验证。

### 控制台页面

| 页面 | 功能 |
| --- | --- |
| 总览 | 实例状态汇总 |
| 服务管理 | 按 Channel 接入与外部能力查看服务职责、状态、诊断与日志 |
| 机器人实例 | 任务 / 分层配置 / 运行状态，三个同级页签；分层配置使用四层导航、分组列表与详情抽屉；任务内左侧列表、四层运行轨迹、Agent 内执行列表/关系图和结构化检查器，支持两步对照；服务日志统一入口 |
| 组件目录 | 按 tools / prompts / agents / context 四个 surface 统一浏览工具、提示词、Agent 和上下文组件（只读卡片） |
| 测评页面 | 开始测试 / 运行记录 / 进步趋势 |
| AI Harness | 修复发起与历史；详情抽屉显示任务概览、分层流程、独立命令日志和原始资料 |
| 代码治理 | 远端 main 巡检、验证证据、PR 自动交付、归档清理与分页历史；共用 Harness 任务宿主 |
| 设置 | 控制台本身 |

### 控制台 API

AI Harness 修复记录按业务阶段、准备版本与修复轮次展开。步骤收起时保留关键输入与结论，
展开后使用结构化树／JSON 查看结果及依据；完整执行正文按需读取。当前或最终步骤默认展开，
新步骤出现和轮询保留用户的展开选择，任务切换隔离请求与正文。

已完成命令（包括非零退出码）只在独立日志区平铺展示，可选择执行来源、分页及筛选非零退出码；
公开过程消息显示在所属步骤内部，不变成流程节点。运行中的概览、流程、已展开步骤与日志更新，
终态补取最后结果；缺失与截断显式提示。数据语义及接口由[修复流程记录](harness.md#修复流程记录)维护。

| 端点 | 方法 | 用途 |
| --- | --- | --- |
| `/api/bots/{id}/gateway-observation` | GET | 当前实例 Gateway run 和独立准入审计 |
| `/api/bots/{id}/gateway-observation/runs/{run_id}` | GET | 当前 run 的诊断事件、审批和交付回执 |
| `/api/catalog` | GET | 统一组件目录（tools + prompts + agents + context） |
| `/api/catalog/{item_id}` | GET | 单个目录条目 |
| `/api/bots/{id}/tools` | GET | 读取实例当前工具配置 |
| `/api/bots/{id}/tools` | PUT | 写回工具配置；`?apply=true` 时同步到运行实例并重启 |

### 四层配置工作台

配置导航按 [四层运行时职责](../../specs/runtime-four-layer-definition/spec.md) 组织；这是 Console
展示归属，不改变后端实体 `layer`、BotSpec 格式或历史任务快照。完整字段、环境解析值和运行信息
放在详情抽屉中，列表仅显示名称、关键值和真实状态。

| 层 | 分组与归属 |
| --- | --- |
| Channel | 渠道连接、平台适配；包括 QQ/OneBot 和现有 Feishu adapter |
| Gateway | 网关与协议、身份与准入；包括 Owner/Admin、私聊准入和固定权限策略 |
| Application | 会话与工作区、记忆与知识、资料与项目、输入处理；包括 RAG、Wiki、Skills、代码仓库和运行特性 |
| Agent | 模型与提示词、工具包、MCP、搜索、子 Agent 与委托、具体工具 |

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

### 工具配置编辑机制

- 前端 `BotToolEditor` 沿用 `tools.packs/features/hide/mcp.servers` 与 `agents.presets/workflows` DTO。
  工具包、MCP、子 Agent 和 Workflow 在 Agent 层操作；运行特性在 Application 层操作。
  工具包与运行特性分别从对应目录添加；无可添加 Workflow 时不展示添加按钮。
  模型、环境变量、权限和其他未有写入入口的字段保持只读。
- 后端 `console/control/yaml_editor.py` 使用 `ruamel.yaml` round-trip 编辑 `bot.yaml` 和 `mcp/servers.yaml`，保留注释和格式；该依赖已声明在 `console/requirements.txt`，`deploy_console.sh` / `setup_console.sh` 安装时会一并装入 venv。
- `console/control/catalog.py` 通过 `component_catalog` 读取 tool pack / tool feature / MCP catalog / subagent preset / workflow DTO，并聚合提示词占位和上下文来源占位为统一 `CatalogItem`。
-  编辑后点「保存并重启」会先取得同实例 TaskManager 串行资格，再写入源仓配置并调用统一 `update_instance.sh`；该入口通常同步后快速应用配置并重启，只有依赖、安装脚本变化或实例 venv 缺失时才完整 bootstrap。仅「保存配置」同步写源仓。配置修改留在 WSL 源仓，由用户在 WSL git 工作区提交。
- 仅在存在草稿、待应用配置或正在应用的任务时展示底部操作栏。未部署实例仅可保存；保存失败保留草稿。
  应用任务启动不等于配置已写入或已生效，任务最终状态独立查询，关闭日志不停止检查。
  「已应用」只采用有效的运行观测；服务停止、观测过期或读取失败时显示暂无法确认。
