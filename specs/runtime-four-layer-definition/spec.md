---
id: runtime-four-layer-definition
type: architecture
status: implemented
created: 2026-09-08
---

# 机器人运行时四层架构基线

## Summary

AgentStrata 由机器人运行时，以及启动装配、控制观测、独立测评等配套部分组成。机器人运行时按
**渠道适配（Channel）→ Gateway → Application → Agent** 四个职责层组织，各层通过结构化
契约协作，由实例宿主管理生命周期。

本规格是长期有效的运行时架构基线。后续涉及运行时架构、跨层契约、运行部署或相关数据迁移的
SDD 必须遵循并引用本规格；局部规格不能另行定义运行层级。基线变更须明确提出并单独审议，
同时更新相关规则与验证。工作流见 [SDD-lite](../../docs/maintenance.md)。本次固化不改变运行行为、
公开接口、存储、Console 布局，也不推进其他 Legacy 清理项。

## Design

### 四层职责

| 职责层 | 固定职责 | 不属于该层的职责 |
| --- | --- | --- |
| 渠道适配 Channel | 平台连接、原生结构化事件校验与转换、平台资源获取、实际投递和 Provider 回执 | 分配业务角色、授予工具权限、拥有 Agent 会话 |
| Gateway | 可信主体、准入与角色策略调用、持久 session/run、取消与恢复、ingress/outbox、交付协调 | 操作可变 actor 内部状态、越过 Application 执行 Agent |
| Application | actor 会话、工作区、资源与上下文准备、Agent 调用、交换提交或丢弃 | 实现具体平台传输、解释 Backend 私有协议 |
| Agent | Backend、模型调用、工具执行、上下文窗口处理、搜索与子 Agent 委托 | 解释平台身份、拥有渠道连接或 Gateway 交付事实 |

这里区分消息处理职责与文件位置：实例装配即使位于 gateway/runtime.py 或
application/agent_runtime.py，仍属于分层之外的生命周期职责，不因此增加串行层或强制搬迁目录。

### 配套部分与支撑模块

- 启动装配、实例宿主、Console 和 Evaluation 位于四层之外。实例宿主管理装配、启停与资源释放；
  Console 使用现有查询和控制入口；Evaluation 具有独立生命周期，可按目标复用隔离 Agent 或消息链。
- Contracts、Core、授权策略、模型访问、工具和存储通过契约支撑运行，不构成第五个串行层。
  分层不要求独立进程、新服务或另一套注册与装配框架。
- Legacy 是待退出的存量实现，不作为第二套架构标准。新设计不得扩展旧运行链；清理按已审议的
  独立项目推进，不能以本基线为由自动删除现有功能或数据。

### 结构化交接与实际流程

- Channel 与 Gateway 使用 CanonicalInboundEvent、OutboundEnvelope、DeliveryReceipt 等现有契约；
  原始平台帧由 Channel 处理，跨层只提供受控投影和绑定证据。
- Gateway 与 Application 使用 PreparedTurn、TurnOutcome、ExchangeRef 等现有契约；可操作的
  actor 内部状态留在 Application。Application 在获准后可通过 ResourceFetcherPort 请求平台资源。
- Application 与 Agent 使用 AgentTask、AgentEvent、AgentResult；Backend 私有对象由其 adapter
  处理，不暴露给不应持有它们的层。结构化协作可以是进程内调用，不强制新增网络协议或序列化。
- 箭头表示主要入站职责顺序，不表示所有操作必须走完四层。准入拒绝可以提前结束；本地 ACP
  客户端可直连 Gateway；独立评测可直接调用隔离 Agent。资源获取、返回结果与交付确认按真实契约协作。
- 执行结果与消息交付分别记录。Provider 已确认的交付与后续交换提交失败是两项事实；交付未知
  不等于已确认。观测只记录真实交接，不为凑齐四层生成步骤或回填未采集的输入输出。
- Console 的配置分组、组件分类、API layer 字段及历史实体与快照不等同于四层运行职责，不受
  四层数量限制。

### 依赖门禁与 SDD 约束

消息方向与 Python import 方向分别约束。Gateway 注入 Channel 入站回调；Channel 不反向 import
Gateway。现有 [架构检查器](../../scripts/check_architecture.py) 是静态依赖门禁：

- Channel 不依赖 Gateway、Application 或授权策略实现。
- Application 不直接依赖具体 Channel、Gateway、协议或 Legacy 实现。
- Agent 不反向依赖 Application、Gateway、Channel、BotSpec 或平台实现。
- Contracts 与 Core 不依赖上层运行实现。

相关 SDD 在现有 Design 中说明受影响职责、交接契约与依赖方向，并引用本基线。装配、Console、
Evaluation 的设计按需说明与运行时的边界。普通修复遵循 SDD-lite 原有范围，无需统一填表或
填写“不适用”；不新增 frontmatter 字段或顶层章节。职责归属由规范与评审约束，跨层行为由
针对性测试验证，静态检查不能证明全部运行时语义。

## Acceptance

- 本规格是唯一长期基线，SDD 规范、模板、协作入口和当前架构说明引用同一事实源。
- 后续相关规格明确说明职责、结构化交接和依赖方向；普通修复不增加文档负担。
- 装配、Console、Evaluation、直接 Gateway 客户端与提前结束的操作不被误算为额外运行层或缺层错误。
- 代表性违规依赖被现有检查拒绝；当前合法依赖和配套入口通过，不增加第二套检查器。
- 历史规格保留设计取舍和实施背景；当前规范引用不将旧分层描述作为现行标准。
- 本次不改变运行流程、数据和界面；保留已有改动与暂存状态，不提交、部署或操作真实实例。

## Verification

- 运行 SDD 与架构检查，验证反向依赖拒绝、结构化端口、实例装配、ACP 直接入口、Console 和独立 Evaluation 边界。
- 核对相关规格与文档链接、四层职责和交接一致性；元数据与章节结构遵循 SDD-lite。
- 涉及运行行为时执行 Channel、Gateway、Application、实例宿主及交付/取消定向回归；涉及展示时检查前端分组和响应式交互。
- 静态依赖与受控 Agent/OneBot 夹具不能证明真实模型、QQ 或生产启停。

检查范围与命令按 [开发与验证约定](../../docs/guides/development.md) 选择；单次结果保留在交付说明或 CI 日志中。
