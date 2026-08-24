---
id: qq-agent-runtime-reliability-hardening
type: architecture
status: draft
created: 2026-07-23
---

# QQ Agent 开发链路运行可靠性加固

## Summary

-  QQ 自然语言开发链路固定为 `QQ → NapCat → @ Relay → cc-connect → ACP → Codex → 校验/发布`；Relay 只承担传输边界和群聊明确 @ 触发，ACP 是用户/群名单的唯一准入权威。
-  群聊必须明确 @ 当前机器人是固定 Relay 语义，不再由 BotSpec 或 env 开关配置；旧 `access.group_require_mention`、`QQ_REQUIRE_AT_IN_GROUP` 和 `QQ_AT_ALL_COUNTS` 均拒绝使用。
-  MCP catalog 中的通用 `python` / `python3` stdio 命令必须绑定到当前 AgentStrata 运行解释器，避免 systemd PATH 缺少裸 `python` 时工具无故离线。
-  QQ Agent 使用的共享 Docker MCP 都是本机基础设施；无认证 HTTP 端口不得发布到 WSL 全接口。
-  本规格不自动修改用户个人 Codex 配置或 Windows 登录自启动任务。

## Design

-  QQ `provision-env`、doctor 和启动校验拒绝已删除的准入字段与 env 开关，不做双源一致性或兼容转换。
-  Relay 不获得 `QQ_ALLOW_FROM` 或 `QQ_ALLOW_GROUPS`；cc-connect 固定 `allow_from = "*"`，名单只由 ACP 进程从 bot-local env 加载。
-  `_start_qq_proxy.sh` 启动失败时，`start.sh` 直接失败；删除改写 cc-connect `ws_url` 直连 NapCat 的降级路径。
-  @ Relay 启动时自行校验强 token、回环 upstream/listen URL 和数字 `QQ_ACCOUNT`，并在连接 NapCat 前拒绝未携带同一 token 的下游 WebSocket，不只依赖部署脚本前置检查。
-  QQ 图片回传与后台文本通知在每次直连 OneBot 前重新校验强 token 和回环 URL，运行态配置漂移不得绕过 gateway 前置检查。
-  父级 `start.sh` 只向 Relay 传递必要的 OneBot URL、token 与机器人账号，并从 cc-connect 环境移除 ACP 专用名单；ACP wrapper 再从 bot-local env 加载名单。Relay 启动超时必须终止残留进程并清理 pidfile。
-  会话身份 hook 从部署树的稳定根 `CCP_HOME_DEFAULT` 选择实例 venv；cc-connect 为自身状态隔离而覆盖的 `HOME` 不参与解释器定位，也不得让 `message.received` 把可解析的 QQ session key 降级为空身份。
-  MCP runner 仅把命令名精确为 `python` 或 `python3` 的 stdio server 解析到 `sys.executable`；stdio args 在进程启动时展开环境引用，缺失引用失败关闭；显式绝对路径和其它命令保持原样。
-  Stateful SSE/streamable HTTP MCP 在首次初始化无工具时立即重建 runner 并重试一次；stdio、stateless HTTP 和工具调用超时不因此重复执行。
-  共享 Docker MCP 的所有 published ports 固定绑定 `127.0.0.1`；compose 重建保留现有 named volumes，`doctor all` 聚合并返回任一子服务失败。
-  Bot MCP binding 只启用具备实际运行前置条件的服务；未配置 API key 的 Brave 关闭，Playwright 容器恢复后再保留启用。
-  文档明确失败关闭、配置真源、MCP 解释器解析和 Windows 冷启动仍需独立授权的边界。
-  `update_instance.sh --enable` 只在 provision、同步、重建和 restart 成功后启用 unit；缺少 systemd 或注册失败时返回非零。

## Acceptance

-  旧群聊 @ 开关或旧 AccessSpec 准入字段存在时配置失败；有效 QQ 配置只渲染固定 Relay 拓扑。
-  @ Relay 普通启动失败、下游认证失败或 OneBot 往返探针失败时，QQ service 均不得启动 cc-connect，也不得修改配置为直连 NapCat。
-  @ 代理缺少机器人 QQ、使用弱 token 或非回环 URL 时自行拒绝启动。
-  QQ 图片回传和后台通知遇到空/弱 token 或非回环 OneBot URL 时，在建立连接前失败。
-  cc-connect 不继承 AgentStrata 白名单，ACP 能从 bot-local env 读取严格名单；Relay 启动超时后没有残留进程或陈旧 pidfile。
-  cc-connect 使用隔离 `HOME` 且 hook 不提供显式用户字段时，`qq:<user>` 仍由部署树 venv 解析为非空私聊身份，prompt 边界不得把已识别 Owner 降级到 default workspace。
-  `python` / `python3` MCP stdio server 在实例 venv 中使用当前解释器，`git-local` 不再因系统缺少裸 `python` 崩溃。
-  `git-local` 的 `${CHATCOPILOT_DEV_ROOT}` 在 runtime env 建立后解析为真实源码根，缺失时不得以字面路径启动。
-  GitHub/Playwright 这类 stateful HTTP MCP 可从一次瞬态初始化失败恢复，仍失败时保持 unavailable 而不伪造工具。
-  共享 Docker MCP 的 `docker inspect` 与 `ss` 不出现 `0.0.0.0/[::]:18060–18067`；既有健康服务重建后保持健康。
-  `services.sh doctor all` 在任一目标缺失或不健康时返回非零，而不是只打印失败后整体成功。
-  Lingye Bot 不再为缺少 API key 的 Brave 建立失败连接；Playwright binding 与实际健康容器一致。
-  隔离 Codex 开发探针能够创建实现和测试、观察失败、修复并运行通过，同时不创建 Git commit。
-  QQ 服务在强 token 双向认证验收前保持 disabled/inactive；NapCat 数据卷保留且端口仅回环监听。

## Verification

-  隔离 `HOME` 下的会话 hook 行为回归与身份刷新聚焦组通过：`9 passed`；shell 语法、SDD、Ruff、`git diff --check` 均通过；本次 `scripts/check_repo.py fast` 通过：`1006 passed, 1 skipped, 38 subtests passed`。
-  QQ provision、@ 代理、MCP runner、角色/ACP/Codex host、部署更新、Docker 边界与 Windows launcher 聚焦组通过：`149 passed, 22 subtests passed`。
-  shell 语法、Lingye BotSpec、SDD、`git diff --check` 与共享 MCP `doctor all` 均通过；当前健康容器的 published ports 均只绑定 `127.0.0.1`。
-  最终 `scripts/check_repo.py fast` 通过：`938 passed, 1 skipped, 38 subtests passed`；`scripts/check_repo.py full` 通过：`1058 passed, 1 skipped, 48 subtests passed`，wheel 与 Console production build 成功。
-  隔离 Codex CLI 开发探针已生成 `calculator.py` 和三项 `unittest`，先因模块缺失失败，再补实现并以 `python3 -m unittest -v` 全部通过；工作区未提交。
-  使用生产同一 Codex command/subprocess-env builder 的真实启动探针返回 `0` 和 `READY`；隔离 runtime home 后没有模型缓存字段错误或个人 MCP 初始化错误，因此无需改写用户个人 Codex 配置。
-  线上 NapCat 容器已保留原 QQ/NapCat named volumes并重建为 `127.0.0.1:3001/6099`；旧 QQ systemd user service 已 disable/stop，等待强 token 后再做正向验收。
