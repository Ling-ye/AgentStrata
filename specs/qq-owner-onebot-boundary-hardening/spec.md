---
id: qq-owner-onebot-boundary-hardening
type: architecture
status: implemented
created: 2026-07-23
---

# QQ Owner 身份与 OneBot 边界加固

## Summary

-  QQ Owner/Admin 只允许稳定 `user_id` 授权；QQ 昵称不参与角色判定，飞书现有姓名兜底保持兼容。
-  OneBot `3001` 与 NapCat WebUI `6099` 只绑定 `127.0.0.1`，不提供局域网直连。
-  OneBot 强制使用 32–128 位 URL-safe token；`sync-token` 幂等复用或生成强 token，并同步实例 `local.env`、运行时 env 与 NapCat 正向 WebSocket 配置。
-  token 缺失、非法、未启用认证或 QQ WebSocket 使用非回环地址时，QQ 实例必须拒绝启动或报告不健康，不得静默降级。
-  本规格不修改群聊 @ 失败回退和 Windows 自启动行为。

## Design

### Role resolution

-  `PlatformAdapter` 暴露 `allow_role_name_match`，默认值为 `True`；QQ adapter 将其设为 `False`。
-  `resolve_role` 始终优先精确 `user_id`，仅在调用方允许时使用 Owner/Admin 姓名兜底。
-  ACP bridge 按当前平台能力解析角色，并把当前 `SessionIdentity` 传入 agent runtime 与 backend open request。

### Codex host authorization

-  Codex host 模式同时要求 `role_hint=owner`、非空 caller `user_id`、caller ID 属于 `CHATCOPILOT_ADD_OWNER_IDS` 且属于非通配 Bot allowlist。
-  任一条件失败时拒绝创建 host Codex session，并返回稳定、可测试且不泄露身份或 secret 的诊断码。
-  caller ID 的单向摘要进入 session policy fingerprint；caller 变化后旧 resume ID 不得复用。

### QQ configuration boundary

-  `QQ_ACCESS_TOKEN` 是 QQ 必填 secret，格式为 `^[A-Za-z0-9_-]{32,128}$`。
-  QQ WebSocket URL 只允许 `ws://` 或 `wss://`，且 hostname 必须是 `localhost`、`127.0.0.1` 或 `::1`。
-  provision、cc-connect TOML 渲染、gateway start/status 共用同一套 token 与 URL 校验规则。
-  TOML 字符串使用标准 JSON/TOML 兼容转义，诊断与日志不输出 token 内容。

### Gateway boundary

-  NapCat 容器发布端口固定为 `127.0.0.1:$WS_PORT:3001` 与 `127.0.0.1:$QQ_WEBUI_PORT:6099`。
-  gateway 检查现有容器端口 HostIp；发现非 `127.0.0.1` 绑定时保留现有数据卷并重建容器。
-  gateway 提供独立 `bootstrap` 动作：只验证回环 URL、保留 volume 并创建/重建回环容器，供 localhost WebUI 首次配置 token；该动作不启动 Bot service、不要求或改写 token，也不把未认证状态报告为健康。
-  gateway 健康检查执行无 token 负向探针和带 token 正向探针：无 token 必须被拒绝，带 token 必须成功。
-  gateway 日志只报告探针状态与 token 长度，不输出 token。
-  控制台的 NapCat WebUI 登录动作调用独立 `bootstrap`，等待 localhost WebUI 就绪，再从容器日志按需恢复 WebUI 管理 token 并返回 `no-store` 登录链接；该 token 不等于 OneBot `QQ_ACCESS_TOKEN`。
-  WebUI token 可从已停止容器的历史日志恢复；正式 `start/restart` 仍先校验 OneBot 强 token，校验失败时不得先停止现有容器。
-  gateway 提供幂等 `sync-token`：复用合法 `QQ_ACCESS_TOKEN`，缺失或非法时在进程内生成 64 位 hex token；以 stdin 传递 token，原子更新 bot-owned `local.env` 且保留所有未知/高级键，再同步 NapCat `3001` WebSocket 配置。
-  `sync-token` 在更新运行时 env、重启 NapCat 和双向认证探针全部成功后才报告完成；失败状态可安全重跑，日志只输出 token 长度。
-  控制台返回 gateway stderr 前移除 ANSI 控制序列，浏览器错误提示只包含可读诊断。

## Acceptance

-  非 Owner QQ ID 即使使用 Owner 昵称也解析为 User；显式 QQ Owner ID 解析为 Owner；飞书姓名兜底保持有效。
-  伪造 `role_hint`、缺失 caller ID、caller 不在 Owner 配置或不在 allowlist 时，Codex host session 均被稳定拒绝；合法 Owner 可创建 host session。
-  caller ID 变化时 session policy fingerprint 变化，旧 Codex resume ID 被废弃。
-  空/弱 token、非回环 URL 无法通过 provision、渲染或 gateway 校验；合法 token 可安全渲染且不进入诊断输出。
-  Docker 启动参数只发布回环端口；旧全接口容器被判定需要重建，原数据卷名称保持不变。
-  `bootstrap` 可在 token 尚未配置时安全提供 localhost WebUI，但 `start/status` 仍拒绝空 token，Bot service 保持停止。
-  控制台点击 WebUI 登录可从停止状态安全启动 NapCat、取得带管理 token 的 localhost 链接；历史日志已有 token 时，停机状态也可读取。
-  控制台 NapCat `restart` 缺失 OneBot token 时返回失败但不先停止容器；WebUI bootstrap 不削弱正式 OneBot 启动门禁。
-  `sync-token` 不删除 `local.env` 中的模型、路由、Git、MCP、NapCat quick-login 等现有键；文件权限保持 `0600`，token 不进入 argv、日志或 Git。
-  同步后源仓 `local.env`、生成的运行时 env 与 NapCat `3001` token 相同且格式合法；gateway 双向认证、实例更新与 systemd 启用成功。
-  控制台 restart 失败消息不包含 `\x1b` ANSI 序列。
-  无 token OneBot 动作必须收到 `1403` 拒绝，带 token 必须能执行 `get_status`；不得因 NapCat 先完成握手再拒绝而误判为未鉴权放行。
-  文档不再说明 QQ token 可为空，并明确人工配置顺序、`0600` 权限和回滚安全边界。

## Verification

-  聚焦身份、QQ 配置、Codex host、gateway 与 ACP bridge 测试通过：最终增量组为 `29 passed, 16 subtests passed`，兼容回归组为 `19 passed`。
-  `python scripts/check_sdd_specs.py`、Lingye BotSpec validate、相关 shell `bash -n` 与 `git diff --check` 通过。
-  最终 `scripts/check_repo.py fast` 通过：`938 passed, 1 skipped, 38 subtests passed`；Ruff、typed contracts、架构与 requirements 检查通过。
-  最终 `scripts/check_repo.py full` 通过：`1058 passed, 1 skipped, 48 subtests passed`；依赖一致性、wheel 构建和 Console production build 通过。
-  使用当前真实空 token 执行 `provision-env` 与 `qq_gateway.sh status` 均按设计拒绝，诊断为 `QQ_ACCESS_TOKEN` 缺失且未输出 secret。
-  真实 `sync-token` 成功复用 64 字符 token，源码 `local.env`、运行时 env 与 NapCat `3001` 配置三者长度均为 64 且完全相同；两个 env 文件权限均为 `0600`。
-  NapCat WebUI 控制台修复先以 4 个失败测试复现停机 token、bootstrap、restart 与缓存问题；实现后聚焦回归为 `24 passed, 11 subtests passed`，Console production build 与 `bash -n deploy/wsl/qq_gateway.sh` 通过。
-  最终 `scripts/check_repo.py fast` 通过：`942 passed, 1 skipped, 38 subtests passed`；Ruff、typed contracts、架构、requirements、SDD 与 UTF-8 检查通过。
-  真实 `POST /api/infra/napcat:lingye-copilot-qq/webui-session` 返回 200、`Cache-Control: no-store`、带 token 的 localhost URL；Windows 侧跟随链接返回 200，容器保持 `127.0.0.1:3001/6099` 回环绑定。
-  真实缺少 `QQ_ACCESS_TOKEN` 的控制台 restart 返回 409 后，NapCat 容器仍为 running，WebUI 继续可达；证明校验失败不再先停止容器。
-  当前 Codex 会话未提供浏览器控制接口，因此未执行真实点击与截图；前端交互已通过 TypeScript production build、API 实测和 Windows HTTP 路径验收。
-  NapCat 4.18.8 实测在握手后发送 `1403` 并关闭未鉴权连接；先以 2 个失败测试复现探针误判，再改为执行 `get_status` 并忽略先到达的 lifecycle 事件。
-  token 同步与探针增量回归通过：`33 passed, 14 subtests passed`；Ruff、`bash -n deploy/wsl/qq_gateway.sh` 与 SDD metadata 检查通过。
-  最终 `scripts/check_repo.py fast` 通过：`947 passed, 1 skipped, 38 subtests passed`；Ruff、typed contracts、架构、requirements、SDD 与 UTF-8 检查通过。
-  真实实例更新通过，`chatcopilot@lingye-copilot-qq.service` 为 `active + enabled`，运行时健康检查确认 `cc-connect` 正在运行且 OneBot 已连接、QQ 已登录。
-  重载 Console 后真实调用 `POST /api/infra/napcat:lingye-copilot-qq/restart` 返回 `ok: true`；输出无 ANSI、无启动期临时错误，重启后双向认证继续通过。
