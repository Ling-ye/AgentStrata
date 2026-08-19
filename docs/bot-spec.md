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
  mention_name: My Bot

llm:
  chat:
    env_prefix: MY_BOT

prompts:
  persona: prompts/persona.md

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
- `mention_name`：群聊展示/提及名称，不参与稳定身份授权。

平台技术能力由 adapter 声明，实例开关位于 `tools.features`。QQ Owner/Admin 只按
稳定 `user_id` 授权；Feishu 保留 adapter 的显示名兜底。

`access.whitelist_env` 声明用户白名单变量；可选的
`access.group_whitelist_env` 声明稳定群聊 ID 白名单变量。启用群聊门禁时，发送者命中
用户白名单或当前 `chat_id` 命中群聊白名单均可进入群聊，但群聊白名单不会授予私聊
权限。群聊变量缺失或为空时不新增权限，只有显式 `*` 才允许所有群聊。QQ 实例使用
`QQ_ALLOW_FROM` 与 `QQ_ALLOW_GROUPS`，真实 ID 只放在 `local.env`。

### `llm`

- `llm.chat`：日常对话模型，`env_prefix` 决定 API key/base URL/model 的变量前缀。
- `llm.research`：研究模型覆盖；未提供字段继承 chat。
- `llm.code`：Codex lane 的模型、reasoning effort、profile 白名单、任务 profile、
  超时和允许角色。

启用 `dev.code_tasks` 时必须用 `llm.code.code_task_profile` 引用已声明 profile。机器
环境变量优先级高于 BotSpec 默认值；secret 只进入 `local.env` 或 credential store。

### `prompts`

- `persona` 必填。
- `refusal` 可选。
- `roles.<role>` 和 `modes.<mode>` 可按实例覆盖。

所有路径相对于 bot 目录解析。不要在 YAML 写机器绝对路径或 secret。

### `tools`

- `packs`：启用的静态 tool-pack id，例如 `workspace.read_write`、`memory.chat`、
  `feishu.document`、`career.intelligence`、`dev.code_tasks`。
- `features`：非工具 schema 的运行能力，例如 `chat.file_uploads`、
  `chat.image_inputs`、`chat.private_workspace`。
- `mcp.servers`：bot-local MCP binding 文件。
- `hide`：按工具名隐藏运行时工具。

具体目录由 `tool_packs/catalog.py` 管理；每个 pack 用 module binding 声明精确工具名，
Agent 与 Console 使用相同投影。BotSpec 不直接指定可 import 的任意 Python 模块。

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
