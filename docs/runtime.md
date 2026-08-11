# AgentStrata 运行时

运行时把 BotSpec、平台身份、模型配置、工具、上下文和权限装配成一个可持续多轮会话。

## 启动路径

```text
bot.yaml
  → load_botspec / validate
  → assemble_runtime_context
  → apply_runtime_env
  → platform adapter + tool registry + backend
  → ACP server
  → SessionState
  → AgentTask / AgentEvent / AgentResult
```

`python -m chatcopilot run --bot bots/<id>/bot.yaml` 是运行入口。CLI 只接受 ACP
transport；平台连接由 cc-connect 和对应 adapter 处理。

## Turn runtime

Native、LangGraph 和 Codex backend 共享 `agent/turn.py` 的事件、工具结果、生命周期
intent 和最终 `AgentResult` 语义。Backend 由 BotSpec 固定选择，不存在按消息文本切换
backend 的第二套路由器。

AgentSession 使用 soft cap、健康检查和 hard cap：

- 迭代 soft cap 到达后检查重复调用与连续失败；健康时可继续。
- hard iteration cap 无条件停止。
- soft timeout 到达后检查最近工具活跃；hard timeout 无条件停止。
- 缺失 tool result 的 orphan tool call 会在下次模型调用或截断前补成结构化错误。

## Session 与 workspace

平台 adapter 把消息字段归一化为 `SessionIdentity`。Middleware 根据私聊或群聊身份解析
独立 workspace，并在消息身份变化时重建 `SessionState`。

纯文本附件兜底只识别本地文件引用，先排除 `http://` 和 `https://` URL。文件回传、
后台通知、payload 过滤和任务提交都通过 hook 注入 Agent。

Feishu 支持用户文件流水线；附件最终进入当前会话 workspace 的
`.cc-connect/attachments/`。公开文档不假设任何具体机器人名称或 tenant。

## 工具与 subagent

工具 schema 按名称排序，properties 按 key 排序，以保持 prompt 前缀稳定。工具执行走
in-process `ToolExecutor`；MCP client 只负责外部连接。

Subagent 接收 TaskPack，使用受限 selector 和预算，最后必须调用 `submit_result`。
主 Agent 负责向用户解释、合并和交付；subagent 不能直接承担最终答复。

## Codex 主 backend 与代码任务

公开内置 QQ 实例使用 Codex backend：

- Owner 主会话读取源码 worktree，但源码 mutation 必须调用 `start_code_task`。
- 成员会话限制在个人 workspace。
- code-worker 从远端默认分支创建任务私有 clone，在隔离环境验证。
- 验证通过后，受信交付器生成任务分支提交、非强制 push 并创建草稿 PR。
- 不自动 merge、部署、重启或修改操作者 checkout。

Worker 凭据、Codex auth 和 GitHub token 分离；原始 GitHub token不进入 Codex sandbox
或 Git remote。

## 搜索

启用统一搜索后主 Agent 只调用 `search_information`。Router 用确定性规则处理 URL、
显式来源和大多数深度；仅复杂多实体比较需要路由模型。结果经过 URL 归一化、去重、
来源权重和时间稳定排序。

Web 来源按 BotSpec 中的顺序降级。Tavily、Brave 与 SearXNG 的薄适配器在 Agent
进程内执行；账号态或垂直来源仍可使用 search-only MCP。两条路径共享 circuit
breaker、deadline、结果归一化和相关性过滤。URL 深读先静态 fetch，遇到 JS shell、
短内容或可由浏览器处理的 HTTP 状态再升到隔离的 Playwright 服务。

## 任务标识

- `task_...`：单轮对话任务，查询 `get_task_status`。
- `job_...`：后台长任务，查询 `get_job_status`。

ACP 对完整状态 ID 使用确定性回复，避免状态查询消耗 Agent 工具预算。

## HTTP runtime

`http-api-server` 保留中性的 route registry。`/healthz` 在空 registry 下也可用；
自定义模块通过 `CHATCOPILOT_HTTP_ROUTE_MODULES` 显式加载，默认 bearer token 变量为
`CHATCOPILOT_HTTP_API_TOKEN`。

## 快速验证

```bash
python -m chatcopilot botspec validate bots/lingye-copilot-qq/bot.yaml
python -m pytest tests/unit/test_bot_session.py tests/unit/test_main_agent_backend_unification.py -q
python -m pytest tests/integration/test_acp_streaming_updates.py -q
```

架构边界见 [architecture.md](architecture.md)，部署与运维见
[deployment.md](deployment.md) 和 [operations.md](operations.md)。
