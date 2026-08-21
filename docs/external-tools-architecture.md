# External tools 架构

`src/chatcopilot/external_tools/` 承载平台中立的领域能力。它不能 import Agent、
BotSpec、middleware 或具体 platform；共享 DTO 从 `contracts`、`core` 或
`external_tools/shared` 获取。

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
- 一个或多个 `ToolModuleBinding`，每个 binding 同时声明模块与该 pack 精确暴露的
  `ToolDef.name`；
- 可选通用 HTTP route module。

`contracts.tool_packs.ToolPackEntry` 不携带领域专用后端字段。BotSpec 只选择 pack id，
Agent 工具发现统一走 `agent/tools/registry`。builtin 与 external pack 使用同一张映射；
模块被多个 pack 共享时，运行时只取各 binding 明确列出的工具。一个工具需要跨 pack
共享时，必须在每个 binding 中显式声明，不能依赖模块级全量导出。

Console 通过 `component_catalog.iter_tool_pack_tools()` 读取同一精确投影，不自行 import
模块或维护第二张映射。新增或调整 pack 后运行：

```bash
python scripts/check_component_catalog.py --json
```

门禁会拒绝遗漏工具、幽灵工具、重复工具、跨模块冲突、无效 tool-pack policy、权限或
schema 异常，以及 MCP、subagent、workflow 之间的静态工具名冲突。仓库门禁严格读取
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

## 验证

```bash
python -m pytest \
  tests/unit/test_external_tools_registry.py \
  tests/unit/test_feishu_tools.py \
  tests/unit/test_career_intelligence.py -q
python scripts/check_component_catalog.py
python scripts/check_architecture.py
```
