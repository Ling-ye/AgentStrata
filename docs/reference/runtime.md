# 消息运行链

修改 Channel、Gateway、Application 或 ACP 接入时阅读。身份与文件规则见 [身份与资源](identity-resources.md)。

## 主要交接

Channel 转换平台帧并负责实际传输；Gateway 采信身份、调用准入策略并协调 session/run 与交付；Application 准备 actor、工作区、上下文并提交交换；Agent 执行模型和工具。

交接采用现有事件、PreparedTurn/TurnOutcome 与 AgentTask/AgentEvent/AgentResult。准入可提前终止，ACP 可直连 Gateway，隔离测评可调用 Agent。资源下载经受控端口请求，不改变依赖和授权顺序。

## 源码入口

- [src/chatcopilot/channels](../../src/chatcopilot/channels)
- [src/chatcopilot/gateway](../../src/chatcopilot/gateway)
- [src/chatcopilot/application](../../src/chatcopilot/application)
- [src/chatcopilot/contracts/turns.py](../../src/chatcopilot/contracts/turns.py)
- [tests/unit/test_gateway_application_dispatcher.py](../../tests/unit/test_gateway_application_dispatcher.py)

## Gateway/Application 交接

Application 的 `ActorTurnExecutor` 准备回合、管理 actor 和待确认交换，`execute()` 只返回 `TurnOutcome(result, exchange)`，不暴露 actor_state。`ExchangeRef` 是绑定本进程、本轮、session 和 Principal 的不透明引用；Gateway 保留准入、run、取消、outbox、交付和 writer generation。Provider 确认且 generation 仍有效后调用 `commit_exchange()`，Application 复检 envelope/receipt 绑定并幂等提交；未确认群交换由 `discard_exchange()` 丢弃并逐出 actor。交付已确认而 journal 失败不能改写为未送达或自动重发。

生产 Channel 的 `accept_inbound()` 在身份与准入通过、ingress 持久化后返回；Gateway 从既有
ingress 表调度长回合，不让 OneBot 接收 worker 等待整个 Agent。默认最多 8 个并发回合及
1,024 条待执行/执行中消息，均由 [gateway 配置](configuration.md#gateway) 调整；容量耗尽
明确拒绝新消息，不逐出已接受消息。每个精确 conversation key 按持久化插入顺序执行，
不同会话不再因哈希锁碰撞串行。空闲与取消等待者退出后释放 Channel 会话锁。

同步 `handle_inbound()` 等待本条执行结果；取消会终止该条执行。停止 runtime 时取消活动
回合，尚未开始的 accepted ingress 保留，重启用保存的 Principal 恢复；已开始且无法确定
结果的记录继续遵守既有恢复规则，不自动重放。OneBot 接收队列默认 256 条，短时排队采用
有界背压，不因队列暂满主动断开连接；接收预算不保证 provider 在外部断连时补发消息。

OneBot 入站帧默认 4 MiB，可配置到 16 MiB；单文本段最多 256 Ki 个字符、每条最多
512 段。规范化 ingress JSON 单独允许 8 MiB，普通控制状态 JSON 仍限 1 MiB；配置更大
原始帧并不会绕过规范化落库预算。准入、账号、证据与资源绑定校验仍在持久化前执行。

## RuntimeAdapter 创建具体 session

生产 QQ 会话通过 ActorSessionFactory 注入绑定工作区与 Gateway session 的 FileSender。
文件在工作区内读取并校验，投递经真实 ChannelRuntime 与 OneBot driver；只有当前 run 仍活动且
完整 provider acknowledgement 匹配时返回成功。图片工具与最终文本拥有各自的出站和回执，不将文本
回复回执当成图片交付证据。发送结果未知时不自动重试。

QQ 出站资源的单段与每条消息合计预算均为 64 MiB 编码字符（包含 base64 内容和
`base64://` 前缀，文件发送工具单文件上限为 48 MiB 减 9 字节）；OneBot 出站帧默认及配置上限为
128 MiB，Gateway 出站 envelope 与完整任务结果 JSON 的落库上限均为 128 MiB，
成功和失败终态使用同一结果预算；普通控制状态 JSON 仍限 1 MiB。
文件发送器在提交前检查资源合计预算，实际交付仍取决于 provider 回执。

URL 图片下载与快捷发送复用该单文件及合计资源预算；显式 `max_bytes` 超限直接报错，
不再静默压低。快捷发送取消固定 5 张限制，在协议消息结构与合计资源预算内一次交付；
不自动拆分或重试。下载工具的 `limit` 默认仍为 3，允许显式增加。部分下载失败或预算
耗尽时只交付已成功下载的图片，并报告失败；没有下载成功时不发送。

QQ 入站附件预算为每条消息 32 个、单个 64 MiB、合计 256 MiB；Agent 图片文件校验
采用相同单文件上限，仍验证 MIME、内容签名、大小及哈希。下载 socket 等待为 60 秒，
每批下载预算为 300 秒：入站等待到期取消获取且不发布文件，URL 下载在候选、重定向、
读取及发布前检查同一截止时间。系统 DNS 和已开始的阻塞操作不能被同步截止检查强制终止。

OneBot 回执等待由 [channels.qq.action_timeout_seconds](configuration.md#channelsqq) 配置，
文件发送器的跨线程等待在其基础上增加 15 秒。结果未知不转化为可安全重试。

通用 AgentRuntime 只准备公共输入，Native/LangGraph/Codex adapter 创建各自 session；`RuntimeOpenRequest.options` 只承载类型化目录、隔离、恢复和角色提示参数，不传构造函数。

Gateway 群共享历史保存完整的用户与助手正文，不再在写入时截成 12,000/24,000 字符。现有 JSONL 格式保留，按最多 500 轮及 256 MiB 总容量轮转；旧记录中已经截断的内容无法恢复。模型上下文投影最多取 48 轮、64,000 个正文字符，超出部分明确标记截断，投影不改写存储正文。

## 新增 Gateway 通道

新的原生传输放在 `channels/<name>/`，实现连接生命周期、codec、provider capability、资源获取与回执，并在实例装配入口显式接线；不能分配 AgentStrata 角色或授权工具。`platforms/<name>/adapter.py` 的 `ADAPTER` 自动发现只保留给尚未迁移的 legacy edge；平台分支仅限装配入口，不进入共享执行逻辑。

## 任务运行四层观测

任务流按真实 Channel/Gateway/Application/Agent 交接分段，运行职责标记与配置 layer 分开。新增边界用成对 Started/Finished 及真实 trace/span，正文走有界详情与现有留存；准入且 run 建立后才归档平台输入，观测投影不进入业务 ingress 序列化/指纹。Agent 公开输出独立于投递保存，交换提交只报告实际结果；Codex 只标适配器可见范围，隐藏推理不采集。观测失败不能改变权限、执行或交付事实。规格见 `docs/reference/observability.md`。 Console 不再从旧事件名补造调用关联或回退读取 Gateway 业务状态库；任务流只消费当前四层观测版本。

## Owner 斜杠指令准入与生命周期

去除平台 envelope 后，用户正文去除前导空白并以 ASCII `/name` token 开头、后接空白或正文结束时才识别为斜杠指令；绝对路径、URL、`//name` 和正文中间的 slash 仍是普通输入。

所有已识别指令一律只允许本轮认证 Gateway `Principal`、准入和身份激活共同确认的可信 Owner；统一门禁位于身份激活之后、资源 materialization 及 Session/Agent/模型/工具之前，群准入、昵称、历史回合或共享 session 不得提升权限。

`/help` 必须从当前 Bot 实际注册且启用的同一命令目录生成；`/state` 只投影当前会话和可信 runtime 绑定的当前 Bot systemd unit 的有界脱敏状态。

`/restart` 不接受目标或参数，只重启当前 Bot unit，不清理 workspace、journal、memory、persona、runtime resume 或 task/job 状态，也不操作外部 OneBot provider；仅在接受回复送达和指令 task 终态持久化后，才允许通过 Bot cgroup 外的 systemd transient unit 延迟执行，任何身份、投递、持久化、systemd、同实例 transient-unit 冲突或调度异常都失败关闭，禁止用进程内后台任务、`nohup` 或 `setsid` 降级，也不得把“请求已接受”描述为“重启已完成”。

timer 注册后的回执落盘失败只能 best-effort 停止 transient units；即使目标 generation 尚未变化也不得声称已撤销，因为 systemd manager 可能已经排队 restart。

## 任务诊断与 Gateway durable state 分层

只有经过 transport verification、identity 与 admission 的消息才能创建 `task_...` 并进入 Agent；拒绝只保留有界、无正文的 authorization decision receipt。Gateway SQLite 另行拥有 ingress、session、run、event cursor、outbox 与 delivery receipt，不能把 task JSON 当成平台投递事实。`job_...` 仍是后台长任务，Owner job 按 actor digest 位于受保护状态；群内 workspace 不可读取。Gateway 运行端持续写独立观测索引与任务正文，Console 只读查询分层配置快照、分页历史、阶段、指标和交付回执；正文按终态结束时间保留 30 天，活动及恢复任务不清理，摘要长期保留；禁止推进 generation、读取原始 ingress 或套用 legacy ACP 八层证据。无 run 关联键的准入审计只能作为实例审计；诊断写失败不改变权限或交付结果，历史缺记录与截断必须显示。

## ACP 是 Gateway client edge

`protocols/acp/server.py` 只映射 ACP 帧、session lifecycle、prompt/cancel 与 Gateway typed RPC；它不能 import 或重新拥有 Agent、QQ、BotSpec、authorization、workspace 或 task runtime。连接中断恢复使用原始 params/idempotency key、`runs.get` / `runs.latest` 与 `deliveries.get`，不得以新输入替代旧 run。

## 宿主定时任务

机器人宿主可按已保存的[定时计划](schedules.md)提交公共研究回合，并经原有 Channel outbox
向固定 QQ 群投递。该入口不构造平台入站事件，不授予 Owner；Console 只配置和观察。
