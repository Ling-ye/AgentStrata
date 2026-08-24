# BotSpec 配置参考

BotSpec 是可部署机器人实例的声明边界。可运行实例位于 `bots/<bot-id>/bot.yaml`；
`bots/_template/` 提供 QQ 和 Feishu 模板但不会被实例发现器当成真实机器人。

## 最小示例

```yaml
id: my-bot
display_name: My Bot

platform:
  type: feishu
  adapter: feishu_acp

llm:
  chat:
    env_prefix: MY_BOT

prompts:
  schema_version: 2
  identity: prompts/identity.md
  response_style: prompts/response-style.md

tools:
  packs:
    - workspace.read_write
    - memory.chat
  mcp:
    servers: mcp/servers.yaml

agents:
  backend: native

context:
  memory_store:
    provider: markdown
    namespace: my-bot
    schema: memory/schema.yaml

workspace:
  root_env: CHATCOPILOT_WORKSPACE_ROOT

deploy:
  target: wsl2
  instance_id: my-bot
  workspace_root: ~/chatcopilot-workspaces/my-bot
  env_file: ~/.chatcopilot-my-bot.env

packaging:
  allowlist: package.allowlist.yaml
```

## 顶层字段

### `platform`

- `type`：当前公开 adapter 为 `qq` 和 `feishu`。
- `adapter`：对应 `qq_acp` 或 `feishu_acp`。

平台技术能力由 adapter 声明，实例开关位于 `tools.features`。QQ Owner/Admin 只按
稳定 `user_id` 授权；Feishu 保留 adapter 的显示名兜底。

### `access`

`access.owner_only_project_access` 只控制获准消息进入 Agent 后的项目、主机和高级能力
投影，不参与消息准入。QQ 准入不允许在 BotSpec 中改字段名或开关：ACP 固定读取 bot-local
`local.env` 中的 `QQ_ALLOW_FROM` 与 `QQ_ALLOW_GROUPS`。前者只包含稳定发送者 QQ 号，
后者只包含稳定群号；缺失或空值不授予权限，只有整个值精确为 `*` 才允许全部，有限
名单只接受逗号分隔的数字 ID。旧准入字段会直接导致 BotSpec 校验失败。

QQ Relay 不读取这两份名单，也不解析角色：私聊原样转发，群聊只转发 OneBot 结构化
`at` segment 明确指向当前 `QQ_ACCOUNT` 的消息；未携带同一 OneBot 强 token 的下游连接会在
连接 NapCat 前被拒绝。cc-connect 固定使用 `allow_from = "*"`，
白名单与角色语义只由 ACP 解释。

### `llm`

- `llm.chat`：日常对话模型，`env_prefix` 决定 API key/base URL/model 的变量前缀。
- `llm.research`：研究模型槽；`model` 是纳入版本管理的默认模型，`env_prefix` 指向机器覆盖配置；
  未提供的 base URL、API key 和 timeout 继承 `chat`。统一搜索路由和 `PersonaDraftAgent` 使用该槽，
  不能误用主 Codex 模型名或日常模型槽。
- `llm.code`：Codex lane 的模型、reasoning effort、profile 白名单、任务 profile、
  超时和允许角色。

启用 `dev.code_tasks` 时必须用 `llm.code.code_task_profile` 引用已声明 profile。机器
环境变量优先级高于 BotSpec 默认值；secret 只进入 `local.env` 或 credential store。

### `prompts`

- `schema_version` 必须为 `2`。
- `identity` 与 `response_style` 必填。
- `refusal_style`、`role_styles.<role>` 和 `mode_styles.<mode>` 可选。

所有值都是相对于 BotSpec 目录的 UTF-8 普通文件，不能越界或使用符号链接。Bot prompt
只描述机器人身份和表达风格；安全、角色授权、记忆、人格式持久化、搜索触发、工具可见性
和成功事实由可信运行时生成，BotSpec 不能覆盖。旧字段不会自动转换或发出兼容警告，校验会
直接失败。不要在 YAML 写机器绝对路径或 secret。

### `tools`

- `packs`：启用的静态 tool-pack id，例如 `workspace.read_write`、`memory.chat`、
  `feishu.document`、`career.intelligence`、`dev.code_tasks`；也可选择按会话构造的
  `persona.control`。
- `features`：非工具 schema 的运行能力，例如 `chat.file_uploads`、
  `chat.image_inputs`、`chat.private_workspace`。
- `mcp.servers`：bot-local MCP binding 文件。
- `hide`：按工具名隐藏运行时工具。

具体目录由 `tool_packs/catalog.py` 管理；catalog 只定位显式 `ToolProvider`，精确工具成员
由领域 provider 自己声明。静态、MCP、搜索、委托、人格和 session-local 工具都进入同一个
Registry 快照，Agent 与 Console 使用对应 surface 的同源投影。BotSpec 不直接指定可 import
的任意 Python 模块。

`persona.control` 向 Owner 主 Agent 提供 session-bound `persona_manage`，支持
`show/set/append/research/refresh/clear/confirm/cancel`。自然语言和 `/persona` 都由主 Agent
理解并调用，不再存在 `agents.persona_control` 或宿主 detector/interpreter。工具执行端仍按
真实角色、当前请求原文、会话 scope、受保护提案和 mutation receipt 失败关闭。

### `agents`

- `backend`：`native`、`langgraph` 或 `codex`。
- `presets`：公开内置 preset 为 `adapter_forge`、`browser_reader`、`developer`、
  `mcp_query`。
- budget/override/custom：限制 model turn、tool call、timeout、selector、context 和
  cache。
- `unified_search.enabled`：为 Native / LangGraph 启用唯一的 `search_information` 入口。
- `unified_search.providers`：按顺序声明进程内 Web provider；每项使用
  `id / kind / enabled / endpoint / credential_env / timeout_seconds / max_results`。
  BotSpec 只保存凭据环境变量名，不保存凭据值。Tavily 与 Brave 只接受审核过的官方
  HTTPS endpoint；SearXNG 只接受回环 endpoint。
- `codex.owner_access`：仅允许 `worktree`。
- `codex.member_access`：仅允许 `workspace`。

Codex 主 backend 不创建 Native / LangGraph 的 `search_information`。Evaluation 可以在
隔离进程把同一 BotSpec 投影为 Native target，但不得写回线上 backend。小红书等
`risk: search` MCP binding 仍由统一搜索机制作为受限垂直来源执行。

### `context`

- `memory_store`：长期记忆 provider、namespace 和 schema。运行时目标不由模型或 workspace
  路径参数选择：私聊绑定当前稳定发送者，群聊绑定当前稳定群；`memory.chat` pack 名称保持兼容。
- `wiki`：私有 Markdown Wiki 的 `root_env`、读取角色和私聊限制。
- `playbooks.manifest`：bot-local Skill manifest。
- `rag`：只读知识源。
- `codebases`：逻辑仓库注册表，物理路径通过 env 解析。
- `dev`：源码根 env、允许/拒绝路径和 shell timeout。

职业情报工具的默认关注公司为空；用户或 workspace watchlist 必须显式提供目标。
显式目标可以命中经过审阅的公开 provider，未命中或 provider 不可用时返回结构化
搜索降级，不会把 provider 目录当作个人默认关注列表。

### `workspace`

`root_env` 只声明环境变量名。Middleware 按平台身份在根目录下创建会话隔离空间，
不会把真实运行路径写回 BotSpec。

### `deploy`

声明实例 id、WSL 部署目录、workspace、日志、runtime env、cc-connect 配置目录和
project name。`~` 在部署边界展开；其他 shell 变量和命令替换不执行。

### `access`

声明私聊/群聊白名单和群聊提及要求。Owner/Admin 默认集合为空，角色必须由部署 env
显式配置。

## 通用 Feishu tool pack

公开 catalog 保留：

- `feishu.document`
- `feishu.sheet`
- `feishu.bitable`
- `feishu.wiki`
- `feishu.messaging`

这些工具以 bot identity 工作，需要把目标资源授权给对应应用。公开模板不得包含真实
tenant、文档 ID、账号或 endpoint。

五个 Feishu pack 共享同一实现模块，但文档、表格、Bitable、Wiki 和消息工具按 pack
精确隔离；只读 `feishu_api_get` 作为显式声明的公共逃生门随任一 Feishu pack 提供。

## 创建与校验

```bash
python -m chatcopilot bot new my-bot --platform feishu
cp bots/my-bot/local.env.example bots/my-bot/local.env
chmod 600 bots/my-bot/local.env

python -m chatcopilot botspec validate bots/my-bot/bot.yaml
python -m chatcopilot botspec show bots/my-bot/bot.yaml
python -m chatcopilot bot doctor --bot bots/my-bot/bot.yaml
python -m chatcopilot bot provision-env --bot bots/my-bot/bot.yaml
python -m chatcopilot run --bot bots/my-bot/bot.yaml
```

公开内置实例可直接校验：

```bash
python -m chatcopilot botspec validate bots/lingye-copilot-qq/bot.yaml
```

部署说明见 [deployment.md](deployment.md)，日常运维见 [operations.md](operations.md)。
