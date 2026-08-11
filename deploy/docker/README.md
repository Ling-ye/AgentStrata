# AgentStrata Docker 共享服务

`deploy/docker/` 只承载需要独立依赖、浏览器隔离、账号状态或共享缓存的外部能力。
AgentStrata runtime、Tavily/Brave HTTPS adapter 和 SearXNG adapter 运行在 Linux/WSL
Agent 进程中；NapCat / QQ gateway 由 `deploy/wsl/qq_gateway.sh` 单独管理。

## 保留的服务

| Compose service | Profile | 用途 | 默认端口 |
| --- | --- | --- | --- |
| `searxng` | `search` | 共享无 key 搜索引擎；Agent 直接调用 JSON API | `18064` |
| `playwright-mcp` | `browser` | 隔离 Chromium 和动态页面攻击面 | `18066` |
| `xiaohongshu-mcp` | `experience` | 小红书浏览器、cookie 和登录态；默认按 BotSpec 关闭 | `18060` |

所有 published ports 只绑定 `127.0.0.1`。三个服务使用独立 bridge network，并配置
CPU、内存、PID、日志轮转和健康检查边界。镜像使用本机审阅通过的 immutable digest；更新
tag 前必须重新核对上游来源和新 digest。

Compose 中不再提供 Tavily、Brave、SearXNG MCP wrapper、Sequential Thinking 或 Taoke：

- Tavily、Brave 和 SearXNG adapter 是 Agent 进程内的无状态 direct provider；
- Sequential Thinking 与模型推理重复，已移除；
- Taoke 的源码、镜像、远端配置、凭据和行为未经独立审阅，不属于 reviewed deployment。

## Desired state

启用的 BotSpec 是 Docker desired state 的唯一配置源：

- `agents.unified_search.enabled: true` 且 providers 中存在启用的 `kind: searxng`
  时需要 `searxng`；
- MCP binding `playwright-browser` 启用时需要 `playwright-mcp`；
- MCP binding `xiaohongshu-search` 启用时需要 `xiaohongshu-mcp`；
- disabled provider/binding 不启动服务。

默认扫描 `bots/*/bot.yaml`。部署或测试需要限定 BotSpec 时，可用路径分隔符连接多个文件：

```bash
export CHATCOPILOT_BOT_SPECS="/path/to/first/bot.yaml:/path/to/second/bot.yaml"
```

只查看解析结果，不访问 Docker：

```bash
bash deploy/docker/services.sh desired
```

resolver 先运行完整 BotSpec 校验，再直接消费 canonical `BotSpec` 与
`McpServerConfig` runtime DTO；不会重新解释一套 raw YAML enablement 语义。
`research_router` alias、缺省开关、MCP `exposure: disabled` 与实际 Agent runtime
因此保持一致。未发现任何 BotSpec、boolean 含糊、引用文件错误或其他 fatal validation
都会 fail closed，且不执行 Docker 变更。脚本优先使用仓库 `.venv/bin/python`；缺失时
只接受能导入 `ruamel.yaml` 的 `python3`。

## 生命周期命令

无服务参数的 `start` 是 reconcile，不是“全部启动”：

```bash
bash deploy/docker/services.sh start
bash deploy/docker/services.sh status
bash deploy/docker/services.sh doctor all
```

reconcile 先启动全部 desired service；成功后停止 disabled retained service，并清理已经从
reviewed Compose 删除的旧 orphan container。named volume 不会因此删除。若 desired-state
解析或启动失败，脚本不会继续停止 optional service。

`doctor all` 只检查 desired service。诊断输出把 process、transport、credential/login 和
functional 层分开；单纯端口可连接不等于 provider 可用。

显式服务操作保留给登录、诊断和一次性测试，不会写第二份 enablement 配置：

```bash
bash deploy/docker/services.sh start xiaohongshu-mcp
bash deploy/docker/services.sh doctor xhs
bash deploy/docker/services.sh probe xhs --keyword "上海 二郎拉面"
bash deploy/docker/services.sh probe searxng --keyword "AgentStrata"
bash deploy/docker/services.sh probe playwright
bash deploy/docker/services.sh stop xiaohongshu-mcp
```

下一次无参数 `start` 会再次按 BotSpec 收敛；显式启动不会改变 desired state。

不要直接使用裸 `docker compose up`。所有服务都属于 profile，裸命令没有默认服务；管理入口
统一使用 `services.sh`。如需只读查看完整 Compose 状态：

```bash
docker compose -f deploy/docker/docker-compose.yaml --profile "*" ps -a
```

## 小红书登录态

小红书使用固定 digest 的官方 `xpzouying/xiaohongshu-mcp:v1.2.6` 镜像，AgentStrata
不维护派生镜像或本地补丁。cookie、浏览器 profile 和图片分别保存在 named volume，不写入
Git。

当 BotSpec 已启用小红书时：

```bash
bash deploy/docker/services.sh start
bash deploy/docker/services.sh login xhs --qrcode
bash deploy/docker/services.sh probe xhs --keyword "上海 二郎拉面"
```

临时登录或诊断可显式启动 `xiaohongshu-mcp`；完成后应停止，或者让下一次 reconcile 按
BotSpec 收敛。

## 配置和安全边界

从仓库根目录创建私有 Compose 配置：

```bash
cp deploy/docker/.env.example deploy/docker/.env
```

当前 Compose 只从 `.env` 读取可选的小红书 proxy。direct provider 的 API key 归 Bot 的
machine env 管理，不进入 Compose `.env`。真实 token、cookie、账号和 proxy 凭据不得提交。

SearXNG、Playwright 和小红书的宿主端口固定为表中的 `18064`、`18066`、`18060`；这是
Compose、MCP catalog、BotSpec direct provider、Console 与 doctor/probe 共用的 runtime
契约，不提供机器级覆盖。`services.sh` 遇到旧的 `XHS_MCP_PORT`、`SEARXNG_PORT` 或
`PLAYWRIGHT_MCP_PORT` 进程变量 / `.env` 键会在调用 Docker 或网络前失败关闭。需要改变
端口时必须先修改架构规格，并同时迁移上述全部消费者和回归测试，不能只改 Compose。

浏览器容器只用于公开页面读取；不要向 Playwright MCP 输入私有管理面、宿主 credential
端点或未经授权的内网 URL。容器边界降低宿主文件系统风险，但不替代 URL policy、网络出口
控制和上游镜像审阅。

## 修改 checklist

1. 先更新并接受对应 `specs/<id>/spec.md`。
2. 判断能力是否真的需要浏览器、账号态、复杂共享引擎或独立 trust boundary；薄 adapter
   默认留在 Agent 进程。
3. Docker 服务必须使用 profile、回环端口、独立 network、immutable digest、资源上限、
   日志轮转和兼容的 container hardening。
4. 在 `desired_state.py` 增加唯一、可验证的 BotSpec→Compose service 映射。
5. 为 enabled、disabled、跨 Bot 聚合、无效配置 fail-closed 和 reconcile 添加测试。
6. 依次运行 shell syntax、Compose config、focused tests、真实 reconcile 和功能探针；真实
   reconcile 会改变运行容器，必须在部署窗口执行。
