# AgentStrata 架构

AgentStrata 是单代码库、多机器人运行平台。`bots/<bot-id>/bot.yaml` 选择实例
能力，公共基础设施通过稳定 contracts 连接，平台与领域实现不能反向渗入 Agent。

## 分层与依赖

```text
contracts
  ↑
agent / external_tools / platforms / botspec
  ↑
application assembly / middleware
  ↑
deploy / console / CLI
```

依赖只允许从上层面向下层契约：

- `contracts` 定义身份、workspace、Agent task/event/result、工具、MCP、Skill、
  subagent、runtime 和 tool-pack DTO。
- `agent` 实现主循环、backend、上下文、工具执行、搜索与 subagent，不 import
  BotSpec、middleware 或具体平台。
- `external_tools` 实现领域工具，只依赖 contracts、core 或 shared helper。
- `platforms` 为每个聊天系统提供 adapter；新平台通过
  `platforms/<name>/adapter.py` 的 `ADAPTER` 自动发现。
- `botspec` 解析实例声明并组装 runtime；它不把实例类型硬编码进 Agent。
- `middleware` 负责 ACP、MCP、通用 HTTP route registry、会话、workspace、权限和
  后台任务。
- `deploy`、`console` 与 CLI 是操作面，不定义跨层业务契约。

核心契约入口：

| 领域 | 模块 |
| --- | --- |
| 身份和角色 | `contracts/identity.py` |
| Workspace | `contracts/workspace.py` |
| Agent task/event/result | `contracts/agent.py` |
| 工具 | `contracts/tools.py` |
| Adapter approval | `contracts/adapter_approval.py` |
| Runtime、subagent、Skill、tool pack | `contracts/{runtime,subagents,skills,tool_packs}.py` |
| 开发任务范围 | `contracts/development.py` |

## BotSpec 组合

每个实例由四个主要表面和运行包络组成：

- `prompts`：persona、refusal、role 和 mode 提示词。
- `tools`：本地 tool pack、MCP binding、运行特性和隐藏工具。
- `agents`：`native` / `langgraph` / `codex` 主 backend，preset、预算和 Codex
  访问策略。
- `context`：RAG、Wiki、memory、playbook、代码仓库和开发范围。
- `platform` / `llm` / `workspace` / `deploy` / `access`：平台、模型、目录、部署与
  访问控制。

BotSpec 只声明 tool-pack id。具体目录在 `tool_packs/catalog.py`，每个 module binding
列出精确工具名；builtin 与 external 使用同一映射。工具发现统一走
`agent/tools/registry`，Console 通过 `component_catalog` 读取同一目录投影。

## Agent backend

三个 backend 共享 `AgentTask`、`AgentEvent`、`AgentResult` 和 turn runtime。backend
只在实例配置中选择，不按角色或单轮文本自动切换。

- Native：内置模型/工具循环。
- LangGraph：使用同一契约的图执行器。
- Codex：实例主会话使用 Codex；Owner 源码写入通过独立 code-worker 与草稿 PR
  交付，成员限制在个人 workspace。

主 Agent 是唯一向用户交付结果的执行者。Subagent 只通过 delegate 工具运行并用
`submit_result` 返回结构化结果。

## 工具、MCP 与搜索

Tool pack 通过 manifest 贡献 prompt fragment 和工具模块。通用公开 tool pack 包括
workspace、memory、persona、playbook、MCP 管理、Feishu、Wiki、职业情报、网页读取、
Windows/Unity 只读能力与受控开发工具。

MCP catalog 是经过审阅的静态目录。公开运行时不会自动下载、安装或启用第三方
MCP/Skill。`risk: search` 的 MCP binding 可产生只读搜索来源；统一搜索入口负责路由、
降级、去重、时间预算和来源合并。

## 平台与会话

QQ 和 Feishu adapter 把平台身份归一化为 `SessionIdentity`。Middleware 在每轮 prompt
边界组装角色、workspace、工具权限、文件发送器和后台通知 hook。群聊身份变化会重建
会话状态，Agent 不读取平台帧或 adapter 类型。

Feishu 保留通用文档、表格、多维表格、Wiki、消息和文件能力；QQ 通过本机
NapCat / OneBot 网关接入。账号、群号、用户 ID、tenant 端点和凭据都属于部署环境。

## HTTP 扩展

`agentstrata http-api-server` 启动通用 stdlib HTTP server。业务 route 只通过
`CHATCOPILOT_HTTP_ROUTE_MODULES` 显式注册；空 registry 仍提供 `/healthz`。默认认证
变量是 `CHATCOPILOT_HTTP_API_TOKEN`，route handler 不应绕过 service 层实现业务。

## Evaluation

Profile comparison 与 BFCL / GAIA / IFEval Suite 统一使用 `Evaluation`，以 `kind`
区分。状态、claim、进程、artifact 和 resume 校验都集中到一个 manager 与
`reports/evals/evaluations/<evaluation-id>/` 目录。

## 验证入口

```bash
python scripts/check_architecture.py
python scripts/check_sdd_specs.py
python -m chatcopilot botspec validate bots/lingye-copilot-qq/bot.yaml
python scripts/check_repo.py fast
```

字段参考见 [bot-spec.md](bot-spec.md)，运行时细节见 [runtime.md](runtime.md)，日常命令
见 [operations.md](operations.md)。
