---
id: runtime-four-layer-definition
type: architecture
status: implemented
created: 2026-09-08
---

# 机器人运行时四层职责定义

## Summary

AgentStrata 由机器人运行时，以及启动装配、控制观测、独立测评等配套部分组成。机器人运行时按
渠道适配 → 网关 → 应用 → Agent 四个职责层组织，各层通过结构化契约协作，由实例宿主管理
生命周期。此次统一现有实现的定义和命名，不新增运行框架、存储或协议。

## Design

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

## Acceptance

- 当前架构入口一致描述四层职责、配套部分和实例宿主，不把授权或控制台画成消息必经层。
- 入站、取消、恢复与交付回归仍通过；准入拒绝不持久化正文、不触发附件、Agent 或工具副作用。
- 所有仓内入站调用方使用新命名；没有重复装配工厂或新增跨层依赖。
- Console 配置归属、历史任务快照和关联字段保持；组件目录采用分组措辞。
- 保留已有未提交改动和受保护脚本；不暂存、提交、部署或修改真实机器人配置与 workspace。

## Verification

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
