# 外部服务与第三方能力

修改 MCP、搜索 provider 或容器装配时阅读。安装和探针属于有明确目标的运维操作，不因文档巡检自动执行。

## 源码入口

- [src/chatcopilot/botspec/mcp_catalog.yaml](../../src/chatcopilot/botspec/mcp_catalog.yaml)
- [src/chatcopilot/agent/mcp](../../src/chatcopilot/agent/mcp)
- [deploy/docker](../../deploy/docker)
- [src/chatcopilot/agent/search](../../src/chatcopilot/agent/search)

## MCP 与容器放置

共享 catalog 文件在 `src/chatcopilot/botspec/mcp_catalog.yaml`，读取入口在 `chatcopilot.core.mcp_catalog`；bot 级绑定在 `bots/<bot-id>/mcp/servers.yaml`。只有浏览器、账号态或重量级共享引擎保留容器；`services.sh start` 必须先发现至少一个 BotSpec 并通过完整 BotSpec 校验，再只从 canonical `BotSpec` / `McpServerConfig` runtime DTO 对账 SearXNG engine、Playwright 与小红书，禁止另写 raw YAML enablement 解释器；发现或校验失败时禁止改变容器，缺省禁用或 `exposure: disabled` 不启动，`doctor all` 只检查 desired 服务。SearXNG / Playwright / 小红书宿主端口固定为 `18064 / 18066 / 18060`，Compose、MCP catalog、direct provider、Console 和探针必须一致；机器 env 或 Compose `.env` 端口覆盖一律在副作用前拒绝。小红书 MCP 使用固定 digest 的官方 `xpzouying/xiaohongshu-mcp:v1.2.6` 镜像，Agent 端继续通过 `search_only_tools` 只暴露 `search_feeds`。

## 第三方能力安装

公开版不自动下载、安装或启用第三方 MCP/Skill。`discover_mcp_server` 只读查询内置 catalog 与官方 Registry；`approve_mcp_server` 只启用仓库内已审阅的 catalog 条目；`probe_mcp_server` 只对 BotSpec 中已经存在的 binding 执行 initialize + list_tools，不调用远端工具、不改配置。其他服务必须由维护者核实源码、许可证、运行命令、secret 引用和远端写行为后手工安装并添加 BotSpec binding。 `adapter_forge` 是与 LPM 无关的 Owner-only 源码适配 preset；Owner 必须先用 `prepare_adapter_source` 核对不可变公开源码 envelope，再显式调用 `approve_adapter_source` 写入 bot-local、Git 忽略、同一稳定 `user_id` 一次性消费的批准记录。forge 只通过 `start_code_task` 修改源码，不安装 marketplace 资源、不恢复旧插件生命周期。

## 搜索 MCP 探针

`python -m chatcopilot.agent.search.probe` 在机器人外直连 `risk: search` MCP server，逐个调用 `search_only_tools` 并报告参数、结果数、错误码；用于排除 router、cross-check、subagent 和 LLM 总结层干扰。

## 直接 Web provider 边界

Tavily / Brave 缺少有效 credential 时保持 unavailable，不得启动占位容器；SearXNG provider 的 loopback endpoint 需要 Docker 中的 SearXNG engine。Sequential Thinking 已删除；Taoke 在源码、镜像、远端配置和凭据行为完成独立审阅前不得重新进入 reviewed catalog。
