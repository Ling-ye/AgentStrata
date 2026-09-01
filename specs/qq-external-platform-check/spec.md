---
id: qq-external-platform-check
type: architecture
status: accepted
created: 2026-08-17
---

# QQ 外部平台检查

## Summary

QQ 是否连接、NapCat/OneBot 是否接受受认证动作、当前登录账号是否与 Bot 配置一致，
以及 Bot 是否能访问指定群，属于平台和基础设施状态，不属于 Agent 能力。QQ 外部检查
不得创建 Evaluation、不得调用 LLM、不得写入 Agent Trial，也不得影响
`agentstrata-capabilities-v1` 的 verdict、通过率或能力覆盖。

没有独立发送 QQ 时，系统无法证明“外部用户消息进入 QQ 后触发 Agent 并得到回复”的
入站端到端链路。检查结果必须把该项标为 `not_tested`，不能用 OneBot `get_status`、
登录态、群信息或发送动作回执替代入站 Agent 证据。

系统可以额外运行 hermetic 模拟 ingress：在检查进程内为一次调用临时启动随机回环
端口上的假 NapCat 与真实 QQ @ Relay，发送合成 OneBot 消息并观察下游帧。
外部检查中的该探针只证明当前安装代码的 gateway JSON 解析、结构化 @ 触发和
WebSocket 转发可用，不连接真实 QQ、不连接 Agent/ACP，也不证明正在运行的 NapCat 曾产生
该事件。独立的 `agentstrata-qq-message-flow-v1` Evaluation 可以把同类回环帧继续传入
attestation、身份/权限、临时 persona、确定性 Agent sentinel 与回复投影，但同样不构成真实
QQ、NapCat 或 cc-connect E2E。

## Design

平台 adapter 提供统一的外部检查入口；CLI 使用
`python -m chatcopilot bot external-check --bot <bot.yaml>`，Console 的基础设施页面
通过 NapCat 服务的“诊断”动作运行同一无模型边界检查。检查只读取 BotSpec 与 bot-local
env，secret 不写入命令行、日志或结果。

QQ 默认执行只读检查：

1. 校验 `CHATCOPILOT_QQ_ONEBOT_WS_URL` 为带显式端口的回环 WebSocket URL，
   `QQ_ACCESS_TOKEN` 为强 token，`QQ_ACCOUNT` 为合法稳定 QQ ID；
2. 证明未认证连接不能通过 OneBot 边界，并用认证连接执行 `get_status`；只有响应中的
   `online=true` 且 `good=true` 才表示 QQ provider 已就绪，离线或异常必须独立失败；
3. provider 就绪后执行 `get_login_info`，证明登录账号与 `QQ_ACCOUNT` 完全一致；
4. 若 bot-local env 配置 `CHATCOPILOT_EXTERNAL_CHECK_QQ_GROUP_ID`，执行
   `get_group_info` 证明 Bot 能访问该群；未配置时该检查标为 `not_configured`；
5. 在两个随机回环端口上运行一次 hermetic gateway ingress probe：假 NapCat 先发送
   一个未明确 `@Bot` 的负例帧，再发送一个应被接受的合成群聊结构化 `@Bot` 帧；真实
   QQ @ Relay 必须携带认证连接假 NapCat、丢弃负例并把正例逐字节转发给临时下游；
6. 明确记录真实入站 Agent 链路为 `not_tested`。

模拟事件只复制 Relay 的固定结构化 `@当前机器人` 触发条件，所有
Bot ID、群号、发送者 ID、消息 ID、文本和 token 均为每次随机合成值；真实私有值既不
进入 relay 也不进入报告。报告只保存 `mode=hermetic_loopback`、
认证/正例转发/负例丢弃布尔值和帧 SHA-256。探针禁止绑定固定端口、禁止复用生产
`:3001/:3002`、禁止向运行中的 proxy 增加注入接口，并在成功、失败和取消路径关闭
全部临时 listener/connection/task。

CLI 可由操作者同时提供 `--send-message --confirm-external-write`，让受信代码向固定 env
群发送一条内部生成、长度受限的 nonce 探针，并要求 OneBot 返回有效 message ID。
仅提供其中一个参数必须在连接和发送前失败。群号、登录账号和 message ID 只输出由强
token 派生的 HMAC 摘要；不得输出 nickname、群名、token 或原始平台 ID。该动作只证明
“OneBot 接受了 Bot 的出站群消息动作”，仍不证明消息被外部用户看到或入站 Agent 链路
可用。

结果使用独立 `external-platform-check/v1` JSON 契约，至少包含 `scope`、`platform`、
`bot_id`、`verdict`、`agent_evaluation=false`、`external_write_performed`、逐项检查和
limitations。`failed` 表示配置或平台拒绝；`error` 表示 transport/协议异常；
`not_configured` 与 `not_tested` 是覆盖说明，不得计入 Agent 失败。

Console 基础设施页必须分别投影容器状态与 QQ 登录状态。NapCat 容器运行而
`online=false` 时，服务整体显示异常并明确标记未登录；状态读取失败时显示“登录状态未知”，
不得沿用旧缓存或显示为健康。Console 的自动轮询和手动登录检查优先读取认证
`get_status`，不能依赖 WebUI token 恰好仍在最近容器日志中。

`agentstrata-capabilities-v1` 目录固定为 25 个直接 Agent Case，不含 ACP 或 QQ；默认 `full`
只选择当前内置 Bot 可运行的 23 个，两个来源专用 Case 仅供显式 `custom`。
`agentstrata-qq-message-flow-v1` 固定为 7 个无外部写的合成后链路 Case。真实 QQ Case、
`qq-live` preset、生产 QQ sender env 和 Evaluation 的 QQ 外部写路径仍全部禁止；白名单、
角色、群聊 @、attestation 与 Owner-only persona tool boundary 由随机合成身份和临时状态验证，因为它们
测试的是 AgentStrata 自身策略而非公网 QQ 连通性。

## Acceptance

- Agent capability Suite 不包含 ACP、QQ live Case、`qq-live` preset、QQ sender 配置检查或
  QQ 外部写；`full` 正好包含当前内置 Bot 可运行的 23 个直接 Agent Case，两个来源专用 Case
  仅供显式 `custom`。QQ message-flow `full` 包含 7 个合成 Case。
- QQ 外部检查不创建 Evaluation/report，不调用模型，不读取或修改 Agent session。
- hermetic ingress 使用真实 QQ @ Relay 与回环 WebSocket，正例必须完整转发、
  负例必须被丢弃、上游必须看到正确 Bearer 认证；任一证据缺失均失败关闭。
- 模拟探针不得监听固定生产端口、不得连接真实 NapCat/cc-connect/ACP、不得暴露消息
  注入 API，也不得把合成消息解释为真实 QQ 或 Agent E2E 通过。
- 默认检查无外部写；发送探针必须同时具备固定群配置、`--send-message` 和一次性
  `--confirm-external-write`。
- 未认证 OneBot 被接受、认证动作失败、`online=false`、`good=false`、登录账号不匹配或
  显式群不可访问时 fail closed。
- 结果和 Console/CLI 输出不包含 token、原始 QQ 账号、原始群号、群名、nickname 或
  原始 message ID。
- 没有独立发送 QQ 时，入站 Agent 链路始终显示 `not_tested`。
- Console NapCat “诊断”属于基础设施任务，不进入 Evaluation lifecycle 或 artifact。
- Console 必须区分容器运行、QQ 已在线、QQ 未登录和登录状态未知；离线或未知不得显示为健康。
- 旧 Evaluation artifact 仍可读取；定义变化使旧 29-Case 结果不能与新 26-Case 结果
  恢复或错误比较。

## Verification

- `python3 scripts/check_sdd_specs.py`
- `.venv/bin/python -m pytest tests/unit/test_qq_gateway_health.py -q`
- `.venv/bin/python -m pytest tests/unit/test_console_napcat_webui.py -q`
- `.venv/bin/python -m pytest tests/unit/test_qq_gateway_ingress_probe.py -q`
- `.venv/bin/python -m pytest tests/unit -q -k "eval or evaluation or qq or external_check"`
- `.venv/bin/python -m pytest tests/integration -q -k "evaluation or acp or attachment"`
- `cd console/web && npm test`
- `cd console/web && npm run build`
- `.venv/bin/python scripts/check_repo.py fast`
- `git diff --check`
