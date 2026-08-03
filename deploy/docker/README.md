# AgentStrata Docker MCP 服务

`deploy/docker/` 管理 AgentStrata 的共享 MCP 基础设施。AgentStrata runtime 运行在
Linux/WSL 中，Docker 只承载搜索、浏览器、账号态或第三方 MCP 服务。日常启停、状态、
日志和探针命令集中在 [`../../docs/operations.md#docker-mcp`](../../docs/operations.md#docker-mcp)。

NapCat / QQ gateway 不在这里管理；它由 `deploy/wsl/qq_gateway.sh` 负责。

## 前置条件

从 AgentStrata 源仓根目录创建私有配置：

```bash
cp deploy/docker/.env.example deploy/docker/.env
```

按需补齐 `.env`。真实 token、cookie 和账号信息不要提交。

## 服务

| 服务 | 用途 | 默认端口 |
| --- | --- | --- |
| `tavily` | Tavily Web Search MCP，优先网页搜索来源 | `18061` |
| `brave` | Brave Search MCP，可作为网页搜索降级来源 | 由 compose 配置决定 |
| `taoke` | 淘客 / 电商搜索 MCP | `18063` |
| `searxng` / `searxng-mcp` | 无 key 搜索 fallback 与 SearXNG MCP 包装 | `18064` / `18065` |
| `xiaohongshu` | 小红书搜索 MCP，登录态保存在 Docker volume | `18060` |
| `playwright` | 动态页面读取和浏览器渲染 MCP | `18066` |

具体启用项以 `docker-compose.yaml` 和
`src/chatcopilot/botspec/mcp_catalog.yaml` 为准。没有对应 API key 的服务应在 Bot
binding 中设为 `enabled: false`，不要让每轮 Agent 初始化承担确定失败的连接。

[KNOWN][HIGH] 所有 published ports 只绑定 `127.0.0.1`，供同一主机上的
AgentStrata 使用；共享 MCP 不提供局域网直连。`doctor all` 在任一服务缺失或不健康时
返回非零。容器间依赖必须加入 `NO_PROXY/no_proxy`，例如
`searxng-mcp → searxng` 不得绕到宿主 HTTP proxy。

`doctor` 只确认容器、端口和基础 MCP 调用。搜索结果为空、quota 耗尽或登录失效需要
结合单个服务日志判断。修改 compose 端口边界后，`services.sh start` 会重建对应容器，
但不会删除 named volumes。

## 小红书登录态

[KNOWN][HIGH] 小红书 MCP 使用固定 digest 的
`xpzouying/xiaohongshu-mcp:v1.2.6` 官方镜像，不在 AgentStrata 仓库中维护其派生源码
或补丁。

登录态和 cookies 由 Docker volume 保存。需要重新登录时使用
`services.sh login xhs --qrcode` 或控制台服务管理页面，不要把 cookies 写进 Git。

## 新增 MCP 服务 checklist

1. 在 `docker-compose.yaml` 中声明 service、回环端口、volume 和 restart policy。
2. 在 `.env.example` 中补充必要变量说明，不写真实值。
3. 在 `src/chatcopilot/botspec/mcp_catalog.yaml` 中登记 catalog ref。
4. 在目标 Bot 的 `bots/<id>/mcp/servers.yaml` 中用 `ref` 绑定。
5. 如需运维入口，补充 `services.sh` 的 `status` / `doctor` / `probe` / `login` 支持。