# QQ 与 NapCat

处理 QQ 登录、平台连接和健康检查；身份及准入规则见 [身份与资源](../reference/identity-resources.md)。

## QQ / NapCat

OneBot `3001` 和 WebUI `6099` 只允许绑定回环地址。WebUI 管理
token 只用于登录管理面板，不是 `QQ_ACCESS_TOKEN`。首次部署和扫码只使用
[`deployment.md`](deployment.md) 的 `quickstart.sh`；下面的 bootstrap 命令仅用于
已安装实例的 NapCat 登录修复。

修复回环容器并重新登录：

```bash
bash deploy/wsl/qq_gateway.sh bootstrap --instance <id>
```

在 `http://localhost:6099` 完成 NapCat 登录后，同步或生成强 OneBot token，再启动
外部 provider 和实例 Gateway：

```bash
bash deploy/wsl/qq_gateway.sh sync-token --instance <id>
bash deploy/wsl/qq_gateway.sh status --instance <id>
bash deploy/wsl/update_instance.sh --instance <id> --enable
```

日常状态、重启和日志：

```bash
bash deploy/wsl/qq_gateway.sh status --instance <id>
bash deploy/wsl/qq_gateway.sh restart --instance <id>
bash deploy/wsl/qq_gateway.sh logs --instance <id>
```

如果 `start/restart` 提示旧容器需要重建，应在可信交互终端确认，或在回环 Console 的
NapCat 卡片点击“重建并启动”。对应 CLI 的显式确认命令为：

```bash
bash deploy/wsl/qq_gateway.sh recreate --instance <id> --confirm-recreate <id>
```

`recreate` 只在容器镜像、端口、volume、crash guard、共享内存或重启策略确实漂移时执行；
先准备固定 digest 镜像，再保留 QQ 数据卷和 NapCat 配置卷替换容器，最后验证 OneBot 认证。
拉取镜像遇到 EOF、TLS 超时、连接重置等明确瞬时网络错误时最多尝试三次，其他错误立即失败；
镜像准备成功前不会替换旧容器。当前容器已符合配置时应继续使用普通 `restart`。

 `sync-token` 只原子更新 Bot 私有 `local.env` 中的
`QQ_ACCESS_TOKEN`，保留其他键，并同步运行时 env 与 NapCat `3001` 配置。
`start`、`restart` 和 `status` 都必须通过无 token 拒绝、带 token 可执行 OneBot
动作的双向探针；只完成 WebSocket 握手不算认证成功。

机器人加入的 QQ 群无需额外白名单，成员 @ 机器人即可交流。`QQ_ALLOW_FROM` 只控制私聊，
在 `bots/<bot-id>/local.env` 中维护允许私聊的发送者 QQ 号，由 Gateway 解释，不传给外部
NapCat provider 或可选 ACP edge。缺失或空值拒绝私聊；只有整个值精确为 `*` 才允许所有
用户私聊。有限名单只接受逗号分隔的数字 ID，空 token、尾随分隔符、混入 `*` 或非数字值
会阻止启动或 doctor。群聊准入不授予私聊权限，也不提升角色。

`QQ_ALLOW_GROUPS` 已删除，旧值不参与运行时校验、准入或新 runtime env 导出，可从私有配置
移除；使用严格的新手 `--resume` 流程时需先删除该键。新行为见
[QQ 群消息开放准入](../reference/runtime.md)。

Gateway 直接消费 OneBot v11 结构化事件：私聊按稳定发送者处理；群聊只有结构化 `at`
segment 明确指向当前 `QQ_ACCOUNT` 才进入处理，`@全体成员`、纯文本名字和伪造 CQ 文本均
不触发。身份、准入、任务持久化和权限审核都在任何 Agent、模型、工具或附件副作用之前
失败关闭。修改配置后运行 `update_instance.sh` 重新供应 runtime env 并重启精确实例；
systemd 的 `MainPID` 必须是该实例 Python 执行的 `chatcopilot run --bot ...`，不再渲染
QQ cc-connect 配置，也不启动 Relay。

BotSpec 选择 `persona.control` 后，Owner 可用自然语言或 `/persona` 提出持续人格要求；两者都会
原样进入主 Agent，由它调用 Owner-only `persona_manage`。不存在宿主 detector、解释器或直达写入
后门，因此若主 Agent 没有调用工具，本轮就不会修改人格。建议使用下列清晰格式减少模型误判：

```text
/persona show [global|group|user]
/persona set [global|group|user] <人格要求>
/persona append [global|group|user] <补充要求>
/persona research [global|group|user] <自然语言要求>
/persona refresh [global|group|user]
/persona clear [global|group|user]
/persona confirm
/persona cancel
/persona<自然语言人格要求>
```

未指定 scope 时群聊固定为 `group`、私聊固定为 `user`；群聊不能选 `user`，私聊不能选
`group`。`set/append/research` 的要求由宿主直接取自当前用户正文，不需要模型重复摘抄；`global` 必须
由当前消息明确提出。`set` 从要求生成完整文档；`append` 把当前层人格与补充要求交给
`PersonaDraftAgent` 并整体替换；`research` 强制搜索后生成；`refresh` 用当前权威人格重新研究并
整体替换。命名人物、角色、歌手或组织形象由该 Agent 通过统一搜索完成公开资料消歧。任一步骤失败
都保持旧人格不变。

明确的更新和清空都可直接提交；主 Agent 对依赖前文、作用域不清楚或仍含糊的要求传
`defer_confirmation=true`，此时建立与 actor/chat/scope/hash 及十分钟 TTL 绑定的
受保护提案。确认时，当前真实 raw user text 必须精确等于 `/persona confirm`，前后空格或普通
“确认”都不会写；取消可以自然语言或 `/persona cancel`。只有工具结果的
`data.committed=true` 及其 receipt 能证明人格已经保存或清空。群聊 `show` 不输出底层正文。

## QQ 外部平台检查

QQ/NapCat/OneBot 连通性属于平台与部署检查，不属于 Agent 能力 Evaluation。它不调用
商用 LLM、不创建 Evaluation、Trial 或 Evaluation 报告，也不影响 Agent verdict。
默认命令只执行读操作：验证回环 OneBot URL、强 token、未认证拒绝、认证
`get_status` 的 `online=true` 与 `good=true`、`get_login_info` 与配置的 `QQ_ACCOUNT` 一致；配置检查群时再验证 Bot
可以读取该群信息。hermetic 检查只会使用随机回环端口、假 OneBot provider 和确定性输入
验证仓库自有的 OneBot 编解码、结构化 @ 条件与 Gateway Channel 边界。Bot/user/group/token
全部为本次随机合成值，不复用 bot-local 私有身份。它不连接真实 QQ、ACP edge 或模型，
结束后销毁全部临时 listener：

```bash
export CHATCOPILOT_EXTERNAL_CHECK_QQ_GROUP_ID="YOUR_EXTERNAL_CHECK_GROUP_ID"

python -m chatcopilot bot external-check \
  --bot bots/lingye-copilot-qq/bot.yaml \
  --json
```

 外部检查复用目标 Bot 已有的 `CHATCOPILOT_QQ_ONEBOT_WS_URL`、`QQ_ACCESS_TOKEN` 和
`QQ_ACCOUNT`；WebSocket endpoint 仍必须是带显式端口的本机回环地址，token 仍必须是
32–128 位 URL-safe 强 token。检查群只能来自 ignored `local.env` 的固定
`CHATCOPILOT_EXTERNAL_CHECK_QQ_GROUP_ID`，不能由模型或 Evaluation Case 覆盖。

如需验证 OneBot 是否接受群消息动作，必须为单次命令同时提供两个显式参数。发送内容
只有固定前缀与随机 nonce，目标只能是上述固定检查群：

```bash
python -m chatcopilot bot external-check \
  --bot bots/lingye-copilot-qq/bot.yaml \
  --send-message \
  --confirm-external-write \
  --json
```

缺少独立发送 QQ 时，外部用户入站、Agent 处理和 QQ 回复的完整往返无法被自动验证，
报告必须显示 `qq_inbound_agent_roundtrip:not_tested`。即使可选发送动作拿到 OneBot
message ID，也只证明 OneBot 接受了动作，不证明群成员看到消息。JSON 输出不包含原始
QQ 号、群号、token、昵称、群名或 message ID，只保留 HMAC/digest 和结构化状态。
Console 的 NapCat“诊断”按钮运行同一个默认只读检查。
基础设施卡片同时自动读取 OneBot 在线状态：容器运行但 QQ 离线时显示“异常 / 未登录”，
查询失败时显示“运行中 / 登录状态未知”，不会再把容器存活当成账号在线。
点击“打开 NapCat 管理页”会直接打开当前 Bot `QQ_WEBUI_PORT` 对应的本机 WebUI；Console
只传递无 token 的回环 `/webui` 地址。如果浏览器尚未建立 NapCat 管理会话，先在 NapCat
自己的登录页完成认证，再进行 QQ 扫码或手机确认。需要管理 token 时，点击“获取并复制
Token”；该操作只允许来自本机回环 Console，请求成功后 token 只进入当前浏览器剪贴板，
不会显示在页面、写入 URL 或持久化。通过 SSH 使用时必须采用本地端口转发；局域网直连会
被 token 接口拒绝。QQ 账号已经在线时无需再次触发登录；Console 会显示“账号已在线”。
容器停机时，管理页、Token 和登录检查按钮会禁用，应先启动或按上面的受控流程重建。

平台 external-check 不再运行 Relay 模拟门禁。Evaluation 的当前 Channel 探针只证明隔离回环
中的合成 ingress 契约；它不证明运行
中的 NapCat 产生过该事件，也不证明 ACP edge、Agent、真实 QQ 客户端展示或用户已读，
这些证据不能互相替代。

正式 Trial 由 Core 在独立 `spawn` 子进程中执行，不在 Evaluation service/Core 主进程
内直接运行模型与工具。有效期限取 Case policy 的 `timeout_seconds` 和本次 Evaluation
剩余 `max_wall_seconds` 的最小值：Case 期限耗尽记录为基础设施错误 Trial；Evaluation
总预算耗尽则终止当前执行并保留 `partial`。取消或期限耗尽会先终止并回收 Trial
进程组及其模型/工具后代；Linux/WSL 还使用父死保护，Core 意外退出时不会留下继续执行
的 Trial 进程组。只有同一 Case/attempt 的完整 Target 组才会进入 checkpoint；取消或
总预算在组内触发时，未完成组的 Trial 和 workspace 会被丢弃，不能参与 resume、比较
或通过率。

`reports/evals/evaluations/` 保留给受管 service。Standalone CLI 使用显式
`--output`，并将记录放在 `reports/evals/manual/`。CLI 会拒绝缺失 `--output`
或指向受管根目录的执行请求，不能覆盖、resume 或修改 service 正在管理的目录；
两者的 artifact 写入模式不同。

受管目录的所有权固定为：application 写 `request.json`、`state.json`、claim 和
取消标记；Core 写 `result.json`、`summary.md`、`progress.jsonl` 和逐 Trial
证据；managed worker 写脱敏 `run.log`；Console 不写 Evaluation artifact。
手工修改这些文件会破坏 ID、fingerprint、checkpoint 和恢复校验。

Core 会在每个 Trial 前后冻结并核对权威 artifact 与当前 Bot claim 的
inode、owner、mode、link、时间和内容摘要；持久化漂移会使整次 Evaluation 进入
`error/indeterminate`、保留隔离 workspace 且禁止 resume。第一阶段插件仍限定为仓库内
静态受信实现，并与 Core 使用同一 OS 用户；该 guard 不能证明恶意代码没有“短暂修改后
原样恢复”。若将来开放第三方插件，必须先增加只读 authority mount 等 OS 级隔离。

当前服务只运行 AgentStrata 原生 Evaluation Core，不会安装或启用外部
评测引擎、实验追踪平台、remote evaluator 或 exporter。

仓库自动化测试验证上述选择、判分、预检、隔离、预算、取消和 artifact 契约，但其中
的 fixture、mock、dry-run 与隔离 transport 不是实际外部服务验收。除非维护者手动运行
并检查相应 Trial 证据，不得宣称真实商用 LLM、真实 QQ 或 Canary 自更新 E2E 已通过。

统一资源名与状态口径见 [`evaluation-glossary.md`](../reference/evaluation-glossary.md)，控制台和
API 见 [`console.md`](../reference/console.md)。
