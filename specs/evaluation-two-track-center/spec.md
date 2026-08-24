---
id: evaluation-two-track-center
type: architecture
status: draft
created: 2026-08-22
---

# 两轨测评中心

## Summary

测评中心只向产品用户提供两个一级测试方向：`agent`（直接 Agent 能力）与
`qq_message_flow`（假设 QQ 用户已经发送消息后的链路正确性）。两者继续复用唯一的
Evaluation application、service、worker、生命周期、取消/重跑、证据与报告目录，不新增
第二套 manager、API 根或 artifact owner。

`agent` 轨道必须从 Evaluation Core 直接调用所选 Bot 的 Agent runtime，不经过 ACP、QQ
adapter、QQ @ Relay、cc-connect、平台身份门禁或回复投影。它回答“相同任务已经到达
Agent 时，Agent 本身能否正确理解、调用工具并作答”，包含人格生效行为和带独立参考值的
当日美元兑人民币汇率 Case。

`qq_message_flow` 轨道不评价模型能力。它使用合成身份、消息和确定性 Agent 回执，验证
AgentStrata 自己拥有的入站与出站代码链。它不能连接或写入真实 QQ，也不能把随机回环端口上的
假 NapCat、合成 OneBot 帧、测试替身或仓库自动化称作真实 QQ/真实用户端到端证据。

本规格修订 `evaluation-center-unification` 的控制台三分区信息架构、
`evaluation-plugin-capabilities` 将 ACP Case 混入产品能力 Suite 的约定，以及
`qq-external-platform-check` 中“合成 QQ 链路完全不进入 Evaluation”的限制。真实 QQ 登录、
NapCat/OneBot 在线状态、真实群访问和显式外部发送仍属于独立基础设施检查。

## Design

Suite manifest 增加受限字段 `track`，只接受 `agent`、`qq_message_flow` 或空值。控制台只列出
前两者，并固定显示两个入口；GAIA、BFCL、IFEval、Comparison Profile 与 planned Suite
继续保留 CLI、服务和旧记录读取能力，但不再占据主测评中心。旧 Evaluation artifact 不迁移，
记录页仍可按其原始 `kind` 与 Suite 安全读取、导出、取消或删除。

`agentstrata-capabilities-v1` 标记为 `track: agent`。其所有正式 Case 的 driver 只能是
`agent_isolated` 或 `agent_configured`；原 `acp_scenario` Case 迁出。执行器必须通过
`build_agent_runtime` 与 `AgentSession.run_task` 直接提交 `AgentTask`，并在证据中记录
`transport_layers_exercised: []` 与 `acp_exercised: false`。Case 可以注入可信的测试人格到
PromptPlan 输入来验证人格是否真实影响回答，但不得调用宿主 persona mutation、持久化文件或
ACP 前置工作流。

当日美元兑人民币 Case 要求 Agent 使用配置好的搜索能力回答日期、计价方向、数值与证据来源。
判分器在 Trial 内从独立的只读汇率参考源取得同一自然日或最近可用业务日的 USD/CNY 参考值，
校验回答中的方向、日期、来源和数值容差；参考源不可用时结果为 infrastructure error，不能把
“调用了搜索”或回答格式正确算作事实正确。参考快照只保存来源标识、日期、数值、抓取时间与
摘要，不保存 secret；回答与 oracle 使用不同的调用路径，避免同一工具输出自证。

新增 `agentstrata-qq-message-flow-v1`，标记为 `track: qq_message_flow`。其 driver 是 Core-owned
`qq_message_flow`，只接受静态 `qq-message-flow` 插件。每个 Trial 使用随机回环端口、随机合成
QQ 号/群号/消息 ID、临时 `0700/0600` 状态与确定性 Agent sentinel。正向 Case 至少串联并留下
逐层 receipt：真实 @ Relay 对合成 OneBot 帧的认证、结构化 @ 触发和字节转发，且 Relay 不读取名单；
message.received hook 等价的受信 session-attestation writer；sender envelope 解析与 one-shot
attestation 消费；ACP 唯一用户/群名单准入与真实角色解析；actor-bound session/task 构造；PromptPlan
提交给确定性 Agent；最终回复经 ACP session update 投影。任一中间层被测试替身替代时，证据
必须列出替代层。只有 `owned_chain_passed` 可以为 true；`full_external_e2e` 必须保持 false，并强制
列出 `qq_platform/napcat/cc_connect/agent_model` 替代层与 `external_qq_write` 排除层。

QQ 轨道固定保留七个 Case。合成 roundtrip 正例使用“sender 不在用户名单、当前群在群名单、
明确 @ 当前机器人”，并断言最终角色仍为 User、Agent 恰好调用一次；缺少 @ 的负例只证明 Relay
触发检查。其它 Case 至少覆盖：伪造或正文不匹配 attestation 的失败关闭、普通成员请求 Owner
动作被拒绝、远程 URL 不被当成本地附件、Owner 人格设置经
`persona_manage` 写入并在下一轮 PromptPlan 生效。人格 Case 使用临时保护状态、确定性
PersonaDraftAgent 替身和显式发出工具调用的 model-replaced sentinel，只评价授权、结构化调用、
receipt、原子持久化与下一轮加载，不评价真实模型的意图理解或写作质量。

QQ Case 中的 ACP 准入使用与生产相同的严格名单 parser：缺失/空值不授予，只有完整 `*` 为
通配，畸形有限列表失败。群名单命中不授予私聊或角色提升；拒绝回合在附件、journal、Agent、
模型和工具之前结束。Suite 不新增第八个 Case。

控制台首页只显示两张测试卡：`直接测试 Agent 能力` 与 `QQ 消息全链路`。选择 Bot 后每张卡显示
测试对象、不包含的层、Preset、Case 数、预计时间和当前就绪/阻断原因；启动动作仍走统一
`POST /api/evals/evaluations`。第二个页签是统一运行记录，显示轨道、Bot、状态、进度、通过/
失败/错误，并保留详情、取消、重跑、导出和删除。主界面不再暴露 Comparison、公开 benchmark
准备、Target 选择、LLM Judge、Suite catalog 或混合总分。

## Acceptance

- 测评中心主界面只有 `直接测试 Agent 能力` 与 `QQ 消息全链路` 两个测试方向；没有
  Comparison、benchmark、任意 Suite 或任务集入口。
- `agentstrata-capabilities-v1` 的每个正式 Case 都直接执行 Agent runtime，definition 与 Trial
  证据中不存在 `acp_scenario`、QQ ingress、transport attestation 或 ACP session update。
- Agent 轨道包含人格生效 Case；它不写真实 persona 状态，且用回答证据证明 PromptPlan 中的人格
  实际到达 Agent，而不是只检查配置字段。
- Agent 轨道包含当日 USD/CNY Case；数值正确性由独立只读 oracle 在声明容差内判定。oracle 不可用
  形成 infrastructure error，不能降级成 observational pass。
- QQ 轨道正向回合只有在网关转发、attestation、身份、权限、session/task、Agent sentinel 与回复
  投影全部有成功 receipt 时通过；证据同时列出 exercised/stubbed/excluded layers。
- QQ 轨道的无 @、actor/content attestation 不匹配和成员越权 Case 均失败关闭，且没有 Agent
  调用、受保护 mutation 或平台写入。
- QQ 人格 Case 通过真实 `persona_manage` handler 与 PersonaControlService 契约在临时保护域
  执行；sentinel 明确标记 synthetic/model-replaced，只有 committed receipt 后下一轮 PromptPlan
  才加载新人格，旧哈希在任何写前失败路径保持不变。
- QQ 轨道不读取真实 QQ ID、群号、token 或生产 state，不监听固定端口、不连接真实 NapCat/
  cc-connect/ACP 服务、不发送真实 QQ 消息，报告中所有身份与帧只保存摘要。
- 真实 QQ 外部检查继续独立展示 `not_tested` 入站 Agent E2E；两轨 Evaluation 的本地通过不会改变
  该结论。
- 现有统一 Evaluation 生命周期、单 Bot claim、spawn Trial、预算、取消、恢复、脱敏和 artifact
  完整性规则保持；旧 Comparison/benchmark 记录仍可读，CLI 兼容不作为主控制台入口。

## Verification

2026-08-22 实际验证：

- 仓库 fast profile：`2,120 passed, 1 skipped, 59 subtests passed`，SDD、公开边界、
  architecture、requirements、UTF-8、Ruff、mypy 与 component catalog 全部通过；唯一
  warning 为第三方 Starlette/httpx 弃用提示。
- QQ/ACP/Persona/Evaluation 聚焦矩阵：`396 passed, 10 subtests passed`。
- Release artifact 单元测试：`41 passed`；`compileall`、BotSpec validate、public-repository
  scan 与 `git diff --check` 通过。
- Console：Vitest `3` 个测试文件、`24` 个测试通过；TypeScript 与生产构建通过。
- `build_smoke.py` 成功构建 wheel/sdist，随后被 tracked-only verifier 按设计拒绝 13 个
  尚未由维护者暂存的新 Python 文件；维护者暂存后仍需重跑。

自动化没有连接真实 QQ、NapCat、cc-connect、PersonaDraftAgent/LLM 或真实主 Agent 模型，
也没有执行外部 QQ 写入。正向 receipt 的 `full_external_e2e` 保持 `false`，所有替代层和
排除层均显式记录。Console 桌面与窄屏的人工视觉检查本轮未执行。
