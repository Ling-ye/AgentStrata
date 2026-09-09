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
同时更新相关规则与验证。工作流见 [SDD-lite](../../docs/sdd.md)。本次固化不改变运行行为、
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

### 2026-09-08 实施记录（历史）

以下保留首次统一职责定义时的实施范围与表述，不作为新的分层定义：

- Channel 负责原生连接、结构化平台事件校验与转换、平台资源获取实现和实际投递；Gateway
  负责主体采信、准入、角色策略调用、run、持久化与交付协调；Application 负责 actor 会话、
  工作区、上下文和交换提交；Agent 负责 Backend、模型与工具执行和委托。
- 实例宿主负责装配和启停，不属于四层消息处理职责。GatewayRuntimeHost 保留现有名称、
  文件位置和唯一构建入口；AgentRuntime 继续指 Agent 执行引擎。装配位于分层之外不要求独立进程。
- 四层箭头表示入站职责顺序，不替代源码依赖规则。Gateway 注入 Channel 回调；Channel
  不导入 Gateway。授权、Contracts、模型访问、工具和存储是支撑模块，不新增串行层。
- Gateway 内部入站接口改名为 GatewayIngressPort，对应参数与成员使用 gateway_ingress，
  同步全部仓内消费者与导出。保持 authorize_inbound 和 handle_authorized_inbound 的调用语义。
  修正 CanonicalInboundEvent 注释：准入成功后、执行前持久化；不改变准入及副作用顺序。
- 沿用 CanonicalInboundEvent、OutboundEnvelope、DeliveryReceipt、PreparedTurn、TurnOutcome、
  ExchangeRef 及 Agent task/event/result。Application 在获准后经资源端口调用 Channel 下载实现；
  Gateway 确认交付后才请求 Application 提交交换。
- Console 通过现有查询与控制入口工作，Evaluation 使用独立生命周期与隔离执行；ACP 仍可作为
  本地协议入口直连 Gateway，Legacy edge 保留。配套部分不要求所有请求穿过四层消息链。
- Console 的配置与观测分组不等同于运行层。仅将组件目录的“全部层”“按后端层筛选组件”改为
  分组措辞，保留页面布局、基础配置/能力分类、API layer 字段、实体 ID、配置指纹及历史快照。
- 同步当前架构文档、协作入口、运行路径和相关规格引用。既有实施历史与验证结论保留原义。

当次验收范围：

- 当前架构入口一致描述四层职责、配套部分和实例宿主，不把授权或控制台画成消息必经层。
- 入站、取消、恢复与交付回归仍通过；准入拒绝不持久化正文、不触发附件、Agent 或工具副作用。
- 所有仓内入站调用方使用新命名；没有重复装配工厂或新增跨层依赖。
- Console 配置归属、历史任务快照和关联字段保持；组件目录采用分组措辞。
- 保留已有未提交改动和受保护脚本；不暂存、提交、部署或修改真实机器人配置与 workspace。

## Acceptance

- 本规格是唯一长期基线，SDD 规范、模板、协作入口和当前架构说明引用同一事实源。
- 后续相关规格明确说明职责、结构化交接和依赖方向；普通修复不增加文档负担。
- 装配、Console、Evaluation、直接 Gateway 客户端与提前结束的操作不被误算为额外运行层或缺层错误。
- 代表性违规依赖被现有检查拒绝；当前合法依赖和配套入口通过，不增加第二套检查器。
- 历史规格保留原始实施与验证记录；当前规范引用不将旧分层描述作为现行标准。
- 本次不改变运行流程、数据和界面；保留已有改动与暂存状态，不提交、部署或操作真实实例。

## Verification

### 架构基线固化验证

2026-09-09 本次使用隔离 worktree 与 Python 3.13.15 验证：

- SDD 与架构专项测试：29 passed，包含反向依赖拒绝、合法结构化端口、实例装配、ACP、Console
  与独立 Agent 评测的静态依赖用例。
- scripts/check_repo.py fast：9 项检查通过；3010 passed、1 skipped、122 subtests passed。
  跳过项为 Windows 路径大小写专属用例；架构为 517 模块、1637 条静态边、0 个环。
- 22 个本地文档链接可解析；历史 Design、Acceptance 和 Verification 的原文保留检查通过；
  git diff --check 通过。运行源码、SDD 格式检查器与现有架构规则没有修改。

本次只修改规范、引用和回归测试，保留原有 SDD 四个元数据字段和四个章节。未执行真实模型、
QQ 或生产启停端到端验证；以下历史结果不作为本次验收证据。

### 2026-09-08 首次定义实施验证（历史原文）

2026-09-08 本次在承接当前未提交内容的隔离工作树中验证，使用 Python 3.10.12、pytest 9.1.1，
固定 PYTHONPATH=src 并核对实际导入来源：

- Gateway、Channel、Application、实例宿主、观测配置与 Evaluation 服务接口专项回归：141 passed。
- npm --prefix console/web test：11 个文件、115 项测试通过；npm --prefix console/web run build 通过。
- scripts/check_repo.py fast：2903 passed、1 skipped、127 subtests passed；SDD、公开信息边界、
  架构、依赖清单、UTF-8、Ruff、类型和组件目录检查通过。架构为 521 个模块、1607 条静态依赖、0 个环。
- 当前生产构建的 Chrome headless 验收：1440×1000 与 390×844 下 5 项检查通过、0 页面异常，
  覆盖分组措辞、原分组筛选和键盘 Enter/Tab/Space；API 使用确定性夹具，未连接真实机器人。
- 9 个 Python 文件去除文档字符串并执行约定改名后的 AST 与实施前一致；配置投影、存储、展示
  映射和受保护脚本的内容及 mode 未变。69 个相对文档链接可解析，git diff --check 通过。

首次 fast 的公开信息检查命中两个临时依赖软链接中的机器路径；移除本次创建的链接后重新执行
完整 fast 通过，未修改扫描规则。默认仓库环境缺少 pytest，验证使用已有完整测试环境，不改动
默认环境。各专项与 fast 有重叠，不合计测试数量。

本次未运行 full profile 的打包及其他专属检查，未执行真实模型、QQ/NapCat、MCP/CDN 或生产启停。
浏览器范围为组件目录，不将其描述为机器人实例页或真实消息端到端验收。全部交付保持未暂存、
未提交，不部署、不替换运行服务前端产物，不修改真实配置或 workspace。
