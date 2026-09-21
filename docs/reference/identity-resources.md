# 身份、资源与执行边界

涉及身份采信、权限、群上下文、附件或持久状态时阅读。本页的边界同样约束人工修改和 AI 修改。

## 源码入口

- [src/chatcopilot/authorization](../../src/chatcopilot/authorization)
- [src/chatcopilot/application/resources](../../src/chatcopilot/application/resources)
- [src/chatcopilot/core/private_sqlite.py](../../src/chatcopilot/core/private_sqlite.py)
- [src/chatcopilot/contracts/resources.py](../../src/chatcopilot/contracts/resources.py)

## Core 并发槽位

`FileTokenLimiter` 在同一进程间锁内检查容量并创建 token，任务期间持有 token 文件锁。TTL 只能清理未锁定的遗留 token；禁止用文件名排序或年龄驱逐活跃持有者。相同限流目录的参与者使用一致版本和容量配置。

## Channel 资源抓取

QQ CDN 实现位于 `channels/qq_onebot/resources.py`，只通过 `contracts/resources.py` 的 `ResourceFetcherPort`/`FetchedResource` 交接有界字节。Application 负责票据、actor/workspace 绑定及原子文件发布；移动实现不得弱化 DNS/TLS、大小和文件校验。

## QQ Gateway 迁移不得削弱安全保证

QQ BotSpec 使用顶层 `gateway` 与 `channels.qq`；每个
  实例由 systemd 以前台 `python -m chatcopilot run --bot <exact-bot>` 运行唯一 Gateway，并
  在组装 Agent、推进 writer generation、连接 Channel 或监听端口前取得 state root 下的非阻塞
  singleton lease；竞争或 owner/mode/symlink/hardlink/inode 校验失败必须关闭，构建、取消、回滚和
  shutdown 都必须释放 descriptor。实例宿主通过 QQ Channel 连接用户独立维护的回环 NapCat/OneBot provider。
  Channel 校验账号、发送者、会话和结构化 @；Gateway 采信绑定证据、完成准入和角色计算并持久化
  受理记录与 run，Application 复检资源绑定。这些门禁必须先于 Agent、模型、工具、附件或
  journal 副作用；迁移不得删除或弱化既有 fail-closed、actor isolation、权限审核和
  evidence 分级保证。QQ 推荐部署不安装、渲染或启动 Node、cc-connect 或 QQ @ Relay；ACP 是
  可选本地 Gateway client edge，不拥有 Channel、平台身份、准入、权限或 Agent runtime。

## Legacy 平台身份归 adapter

只有 Feishu 等尚未迁移的 legacy edge 继续由 adapter 归一化 `session_key` / hook 字段；这些字段不能进入 QQ Gateway 的身份、准入或资源路径。

## QQ 群会话身份与逐轮身份分离

QQ Channel 从已认证 OneBot 结构化帧产生不可变 transport evidence；稳定群号只形成 `ConversationIdentity`，稳定发送者另形成当前 `Principal`。账号、event/message ID、sender、conversation、connection generation 与帧摘要必须绑定，显示名和 provider 实现名不参与授权。Channel 在生成规范事件前校验结构化 @，Gateway 在资源 materialization、task、Agent、模型、工具和 journal 副作用前完成主体采信、准入与角色计算；缺失、畸形、跨账号、跨会话、重复 ID 漂移或发送者不匹配时失败关闭。本地 fake OneBot 测试不能替代真实两账号 QQ ingress E2E。

## QQ 群共享上下文与目录

同一 QQ 群共享有界 conversation journal 和 `<workspace-root>/group_<safe-chat-id>/shared/` 中的普通文件，不同群、QQ 私聊与其它平台继续隔离；旧 `group_<id>/user_<id>/` 不自动迁移，也不能从 shared root 穿越。说话人变化时选择该 actor 绑定的执行 `SessionState`，通过 journal 注入群历史，不得复用其他 actor 的 executor、Codex resume、调用者身份或受保护任务。成员可写的 shared root 不保存权威 `IDENTITY.json`、`MEMORY.md`、runtime state、job/task 控制记录或 persona；权威群 persona 与群 memory 位于 workspace 根的 `.conversation-state/persistent/` 保护域，以平台、会话类型和稳定群号摘要寻址，不暴露原始群号。群 Codex 只在同一 live actor session 内 resume；未获得 provider acknowledgement 的交换必须逐出对应 live actor state，不能污染下一轮；成功投递后的 journal 写入使用稳定 outbound identity 幂等。

## 统一执行权限

业务权限只有 Owner/member。Owner 在实例资源与已配置项目范围内使用全部已装配工具，三个 Runtime、群聊与私聊一致；Admin/User 只使用明确声明 `access: member` 的公共查询、当前会话普通文件及记忆 read/append。ToolDef 默认 `access: owner`，不再按工具名、Runtime、private_chat_only 或旧访问模式叠加 Owner 限制。Application 下发 ExecutionScope；文件工具与命令进程必须执行资源范围，cwd 不能代替隔离。Owner Codex 可写获准目录；所有角色恢复 Codex 默认原生功能；成员原生读写只限当前普通工作区，项目仍仅授权 Owner。内层权限配置禁止原生命令读取 Codex auth.json 与 MCP relay 配置，外层 bubblewrap 执行资源挂载。保持 actor/resume 隔离，群输出独立脱敏，不替换可信角色。权威人格、记忆和状态仍经管理服务操作。规格见 `docs/reference/identity-resources.md`。

## 项目硬链接与执行终态

挂载装配不遍历文件树扫描硬链接；普通文件在执行路径范围内允许硬链接读取、原子替换和删除，接受已放入普通共享目录的 inode 内容通过该路径可见。权威状态仍保留单链接等对象校验，解压链接不得越出目标目录。Gateway 按 AgentResult 保存原始停止原因，`llm_error` 为失败；已确认投递保持独立事实，不因执行失败重发。规格见 `docs/reference/runtime.md`。

## 文件检查职责

Core 的 `file_integrity` 统一普通文件元数据和受信源码哈希读取；Evaluation 的 `private_files` 统一私有产物元数据检查，在实际 I/O 边界复用，不能用一次入口检查替代读写期间的身份复检。TAR 使用标准库 data filter 保留目标目录约束，允许包内链接且不设应用解压容量上限。规格见 `docs/reference/identity-resources.md`。

## QQ Gateway 是唯一准入 owner

Gateway 在认证 OneBot transport 后、资源下载和 Agent 副作用前完成准入。机器人加入的群无需白名单，有效群消息在结构化 @、发送者与会话身份校验后允许。`QQ_ALLOW_FROM` 只声明允许私聊的稳定发送者 ID；缺失或空值拒绝私聊，精确 `*` 允许全部私聊，有限名单只接受逗号分隔数字 ID。`QQ_ALLOW_GROUPS` 已删除，不解析、不参与准入、不生成配置或导出到新 runtime env。群准入不授予私聊权限或提升 Owner/Admin。旧 QQ BotSpec 准入字段及 `QQ_REQUIRE_AT_IN_GROUP` / `QQ_AT_ALL_COUNTS` 仍拒绝；ACP client 不解释 QQ 身份、名单或角色。规格见 `docs/reference/runtime.md`。

## QQ 群准入不提升项目权限

开放群准入不得把群成员提升为 Owner/Admin；每轮角色只按稳定发送者 ID 解析。真实 Owner 在私聊和群聊都保持 Owner prompt、工具、Codex 和代码任务权限，群聊输出与工具 payload 按公开群场景脱敏，不自动注入任何 actor 的私聊 memory/Wiki/RAG。User/Admin 群成员只保留公开搜索、当前群共享普通文件、当前群受保护 memory 的 read/append 和获准同步能力，不能读取项目/主机/配置/内部资料/其他用户数据、读取或修改任何 persona、清空整份群 memory、访问 Owner job 或获得高级工具。拒绝只写有界授权审计事实，不保存原始正文或 provider 资源 URL；通过准入后才持久化 ingress 和启动 task。任何 admitted-intake、task 或受保护状态持久化失败都必须在模型、工具和资源 materialization 副作用前失败关闭。非法工具访问声明在装配时失败关闭；未显式声明成员访问的新工具默认仅限 Owner。

## 纯文本附件兜底只识别本地文件引用

匹配路径或文件名前先排除 `http://` / `https://` URL。

## QQ 身份与 OneBot 边界

QQ Owner/Admin 只按稳定 `user_id` 授权，昵称不参与匹配；飞书 adapter 保留姓名兜底。 `QQ_ACCESS_TOKEN` 必填且必须为 32–128 位 URL-safe 字符；`sync-token` 幂等复用或生成强 token，只替换 bot-owned `local.env` 的对应键并保留全部其他键，再同步运行时 env 与 NapCat `3001` 配置；WebUI 管理 token 是另一凭据，只用于登录 localhost 管理面板。 OneBot `3001`、WebUI `6099` 只绑定 `127.0.0.1`；控制台 WebUI 登录只调用安全 `bootstrap`，正式 start/restart 仍在任何停止动作前校验强 token。 双向探针必须实际执行 OneBot 动作，以兼容 NapCat 握手后发送 `1403` 再关闭的拒绝语义；provision、渲染、gateway 与实例启动遇到空/弱 token、非回环 URL 或双向认证失败时必须 fail closed。

## QQ 外部平台检查

QQ/NapCat/OneBot 的真实连通性不属于 Agent Evaluation，不创建 Evaluation/Trial、不调用模型、不影响 Agent verdict。新 Gateway 的 hermetic integration 只在随机回环端口使用假 OneBot provider、真实 Channel/Gateway 和确定性 Agent，不能解释成真实 QQ/Agent E2E。旧 `qq_message_flow` suite 必须显式列出 `qq_platform/napcat/cc_connect/agent_model` 替代层并标记 legacy。可选群消息探针必须同时提供 `--send-message` 与单次 `--confirm-external-write`，目标只能来自 bot-local `CHATCOPILOT_EXTERNAL_CHECK_QQ_GROUP_ID`。缺少独立发送 QQ 时，真实入站 Agent 往返必须报告 `not_tested`，不得用模拟帧或 Bot 自发消息冒充端到端通过。
