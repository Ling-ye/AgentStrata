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

## Backend 创建具体 session

通用 AgentRuntime 只准备公共输入，Native/LangGraph/Codex adapter 创建各自 session；`BackendOpenRequest.options` 只承载类型化目录、隔离、恢复和角色提示参数，不传构造函数。

## 新增 Gateway 通道

新的原生传输放在 `channels/<name>/`，实现连接生命周期、codec、provider capability、资源获取与回执，并在实例装配入口显式接线；不能分配 AgentStrata 角色或授权工具。`platforms/<name>/adapter.py` 的 `ADAPTER` 自动发现只保留给尚未迁移的 legacy edge；平台分支仅限装配入口，不进入共享执行逻辑。

## 任务运行四层观测

任务流按真实 Channel/Gateway/Application/Agent 交接分段，运行职责标记与配置 layer 分开。新增边界用成对 Started/Finished 及真实 trace/span，正文走有界详情与现有留存；准入且 run 建立后才归档平台输入，观测投影不进入业务 ingress 序列化/指纹。Agent 公开输出独立于投递保存，交换提交只报告实际结果；Codex 只标适配器可见范围，隐藏推理不采集。观测失败不能改变权限、执行或交付事实。规格见 `docs/reference/observability.md`。 Console 不再从旧事件名补造调用关联或回退读取 Gateway 业务状态库；任务流只消费当前四层观测版本。

## Owner 斜杠指令准入与生命周期

去除平台 envelope 后，用户正文去除前导空白并以 ASCII `/name` token 开头、后接空白或正文结束时才识别为斜杠指令；绝对路径、URL、`//name` 和正文中间的 slash 仍是普通输入。

所有已识别指令一律只允许本轮认证 Gateway `Principal`、准入和身份激活共同确认的可信 Owner；统一门禁位于身份激活之后、资源 materialization 及 Session/Agent/模型/工具之前，群准入、昵称、历史回合或共享 session 不得提升权限。

`/help` 必须从当前 Bot 实际注册且启用的同一命令目录生成；`/state` 只投影当前会话和可信 runtime 绑定的当前 Bot systemd unit 的有界脱敏状态。

`/restart` 不接受目标或参数，只重启当前 Bot unit，不清理 workspace、journal、memory、persona、backend resume 或 task/job 状态，也不操作外部 OneBot provider；仅在接受回复送达和指令 task 终态持久化后，才允许通过 Bot cgroup 外的 systemd transient unit 延迟执行，任何身份、投递、持久化、systemd、同实例 transient-unit 冲突或调度异常都失败关闭，禁止用进程内后台任务、`nohup` 或 `setsid` 降级，也不得把“请求已接受”描述为“重启已完成”。

timer 注册后的回执落盘失败只能 best-effort 停止 transient units；即使目标 generation 尚未变化也不得声称已撤销，因为 systemd manager 可能已经排队 restart。

## 任务诊断与 Gateway durable state 分层

只有经过 transport verification、identity 与 admission 的消息才能创建 `task_...` 并进入 Agent；拒绝只保留有界、无正文的 authorization decision receipt。Gateway SQLite 另行拥有 ingress、session、run、event cursor、outbox 与 delivery receipt，不能把 task JSON 当成平台投递事实。`job_...` 仍是后台长任务，Owner job 按 actor digest 位于受保护状态；群内 workspace 不可读取。Gateway 运行端持续写独立观测索引与任务正文，Console 只读查询分层配置快照、分页历史、阶段、指标、审批和回执；正文按终态结束时间保留 30 天，活动及恢复任务不清理，摘要长期保留；禁止推进 generation、读取原始 ingress 或套用 legacy ACP 八层证据。无 run 关联键的准入审计只能作为实例审计；诊断写失败不改变权限或交付结果，历史缺记录与截断必须显示。

## ACP 是 Gateway client edge

`protocols/acp/server.py` 只映射 ACP 帧、session lifecycle、prompt/cancel 与 Gateway typed RPC；它不能 import 或重新拥有 Agent、QQ、BotSpec、authorization、workspace 或 task runtime。连接中断恢复使用原始 params/idempotency key、`runs.get` / `runs.latest` 与 `deliveries.get`，不得以新输入替代旧 run。
