# 工具注册与扩展

新增工具或修改发现、权限和投影时阅读。模型发现与实际执行使用同一 ToolRegistry；域工具通过明确 provider catalog 装配。

## 当前公开领域

```text
external_tools/
├── career/             用户指定公司/岗位的情报、证据与 workspace-local 快照
├── codebase/           注册仓库的只读检索兼容入口
├── codex_cli/          Codex 命令构造与进程边界
├── dev/                受控文件、shell、代码任务与 adapter forge
├── feishu/             文档、表格、多维表格、Wiki、云盘检索与消息
├── mcp_admin/          已审阅 MCP catalog 的发现、批准和探针
├── repository_tasks/   Native/LangGraph 的不提交仓库任务
├── unity_codebase/     Unity 项目只读检索与 Skill wrapper
├── web_fetch/          已知 URL 的静态读取
├── wiki/               私有本地 Markdown Wiki
├── windows_fs/         Windows / WSL 受限只读文件能力
└── shared/             ToolDef、进程、env 与服务辅助
```

## Tool pack

`tool_packs/catalog.py` 是静态 catalog。Entry 可以声明：

- structured policy module 和 builder；
- 一个显式 `ToolProvider` 模块；
- 可选通用 HTTP route module。

`contracts.tool_packs.ToolPackEntry` 不携带领域专用后端字段。BotSpec 只选择 pack id，
Agent 工具发现统一走 `agent/tools/registry`。builtin 与 external provider 都通过
`ToolProvider.packs` 声明自己拥有的完整 `ToolDef`；catalog 不复制精确工具名。一个 provider
可以拥有多个 pack，但每个 pack 只允许一个 provider，重复 pack 或工具名在注册阶段失败关闭。
MCP、搜索、委托、人格与 session-local 工具在会话装配时使用同一 provider 契约注册，不维护
第二条列表拼接路径。

Console 通过 `component_catalog.iter_tool_pack_tools()` 读取同一 provider 投影，不自行维护
工具名映射。新增或调整 pack 后运行：

```bash
python scripts/check_component_catalog.py --json
```

门禁会拒绝缺失 provider、重复 pack 或工具、跨 provider 冲突、无效 tool-pack policy、
handler 签名、权限或 JSON schema 异常，以及 MCP、subagent、workflow 之间的静态工具名冲突。仓库门禁严格读取
packaged MCP catalog，拒绝运行时宽容读取会跳过的损坏记录或重复 ID；它不执行 handler，
也不连接远端 MCP。

## 三入口分层

大型领域按以下方向组织：

```text
ToolDef spec / CLI / HTTP route
             ↓
          Service
             ↓
       modules / clients
```

- `spec.py`：声明 ToolDef、参数 schema 和结果格式。
- `service.py`：领域动作的唯一门面。
- `cmd.py` / `cli.py`：命令行解析与退出码。
- `modules/`：远端 client、解析和领域实现。
- HTTP route：只处理协议、鉴权和 service 调用，不复制业务逻辑。

小型只读领域可以保持扁平结构，但仍需遵守依赖方向。

## 通用 Feishu 能力

公开 pack 为 `feishu.document`、`feishu.sheet`、`feishu.bitable`、`feishu.wiki` 和
`feishu.messaging`。它们使用应用身份：需要 App ID、App Secret，并要求目标资源已
授权给应用；不要求用户 OAuth。

底层命令执行复用 `external_tools/shared/lark_cli.py`，负责隔离 HOME、认证错误分类、
OpenAPI 响应检查与通用 GET 逃生门。源代码、示例和测试不得包含真实 tenant、文档
标识或稳定账号。

## 职业情报

`career.intelligence` 保留 watchlist、岗位快照、证据等级、薪资样本和 JD 分析，但
默认关注公司为空。用户必须指定公司或岗位；显式目标命中经过审阅的公开 provider
时，可以读取公开招聘接口或生成限定官方站点的降级检索。未配置专用 provider 时
返回 `fallback_query`，由统一搜索入口查找官方职位详情，再通过 ingest 工具写入
当前 workspace 的 SQLite 数据库。

已知 provider 会校验 fallback 岗位的官方域名和详情页形态；接口失败或只有招聘
入口页时不会伪造完整快照，也不会推进“疑似下线”计数。provider catalog 只表达
能力，不表达用户偏好。

公开夹具使用中性公司、城市和保留示例域名。

## 第三方资源边界

公开版本不自动下载或安装 MCP/Skill：

- `discover_mcp_server` 只读查询内置 catalog 与官方 Registry。
- `approve_mcp_server` 只启用仓库内已审阅条目。
- `probe_mcp_server` 只对已绑定服务执行 initialize 与 list_tools，不调用远端工具。
- 其他资源由维护者审阅源码、许可证、启动命令、secret 和远端写行为后手工接入。

## 新增领域

1. 在 `external_tools/<domain>/` 建立清晰的 spec/service/module 边界。
2. 在 `tool_packs/catalog.py` 注册静态 entry，并为每个模块列出精确工具名。
3. 通过 `shared.tool_spec` 或 contracts 构造 ToolDef/HandlerResult。
4. 在需要的 BotSpec 中显式选择 pack。
5. 增加单元测试、架构检查和用户文档。
6. 架构或公共契约变化先创建或更新 `specs/<id>/spec.md`。
7. 运行 `scripts/check_component_catalog.py`，确认没有未分配或跨 surface 冲突。

## 统一访问声明

ToolDef 使用 `access="owner" | "member"`，默认 Owner；公共查询和当前会话基础工具显式声明 member。
`workspace.read_write` 提供 `write_workspace_file`，可写入或删除当前会话普通文本文件；
人格、记忆、任务、后端状态仍不能通过普通文件工具修改。
旧最低角色、工具名称特判与 Backend 路由权限不再参与业务授权。可见性和执行复检使用同一规则，
Admin 与 User 均为成员。ToolContext 保留可信调用者及 ExecutionScope，委托不能提升角色。
文件与命令必须使用宿主绑定资源，能力装配和输入校验不能冒充权限不足。

`persona.control` 的操作策略经唯一 PromptPlan 注入，正文生成与提交走原人格工具和状态服务。
详细契约见 [运行权限规格](identity-resources.md)。

## 源码入口

- [src/chatcopilot/tool_packs/catalog.py](../../src/chatcopilot/tool_packs/catalog.py)
- [src/chatcopilot/agent/tools/registry.py](../../src/chatcopilot/agent/tools/registry.py)
- [src/chatcopilot/external_tools](../../src/chatcopilot/external_tools)
- [src/chatcopilot/contracts/tools.py](../../src/chatcopilot/contracts/tools.py)

## External tools 禁止 import

`chatcopilot.agent.*` / `chatcopilot.botspec.*` / `chatcopilot.middleware.*` / `chatcopilot.platforms.*`；共享工具契约从 `chatcopilot.contracts`、`chatcopilot.core` 或 `external_tools/shared` re-export 取。

## 工具发现统一走 `agent/tools/registry`

- **工具发现统一走 `agent/tools/registry`**；具体工具包 catalog 位于 `tool_packs/catalog.py`，只把 pack id 映射到显式 `ToolProvider` 模块，不再复制工具名。领域 provider 自己声明 pack 与完整 `ToolDef`；静态、MCP、搜索、委托、人格和 session-local 工具均注册到同一个 `ToolRegistry`，Agent 与 Console 消费同源快照。重复 provider、pack、tool，缺失 provider，非法 schema 或旧 handler 签名都在物化阶段失败关闭。`scripts/check_component_catalog.py` 验证 pack、feature、MCP、subagent、workflow 和跨 surface 工具名一致性。`contracts.tool_packs` 只保留 DTO；控制台和控制面只读 `component_catalog`，不直接 import `agent.subagents.*` 或 `botspec.registry`；BotSpec 只声明 `tools.packs`，不让 Agent 层 import BotSpec 或中间件类型。`playbooks.reader` 在 runtime 物化时闭包绑定当前 Bot 的不可变 Skill 索引，不得恢复进程级可变 Skill registry。

## 职业情报 provider 不是关注列表

`career.intelligence` 的默认 watchlist 必须为空；只有用户显式目标或 workspace-local watchlist 才能触发查询。 经过审阅的公开 provider 只作为能力目录：直接源仅读取公开招聘端点，失败时返回结构化 research fallback；已知公司 fallback 写入必须校验官方域名和职位详情页，禁止把稳定 tenant 招聘端点、个人目标或社区/搜索页固化为官方岗位。

## Codebase (legacy)

`external_tools/codebase/` 中 `codebase.read` 只读检索仍可用；`codebase.change` 已从工具包 catalog 移除，托管写入流程由 dev tools 替代。
