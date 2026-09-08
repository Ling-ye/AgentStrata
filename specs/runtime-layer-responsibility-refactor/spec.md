---
id: runtime-layer-responsibility-refactor
type: architecture
status: implemented
created: 2026-09-08
---

# 分层职责精简重构

## Summary

保留控制与装配 → Gateway → Application → Agent 的运行主链。Console 属于控制面，Channel、授权、模型访问、工具和存储通过已有契约参与。此次只集中配置解析与模型客户端生命周期、收紧 Gateway/Application 交接、将具体 session 创建归 Backend，并修正配置投影和 QQ 资源抓取的归属。

公开 BotSpec、HTTP/Gateway 协议、三个 Backend、持久数据格式和 Console 页面布局保持不变。不增加模型接入层、Provider 注册中心、通用阶段框架或大型应用服务；Legacy tasks/jobs 和独立 Agent SDK 不在范围内。

后续职责定义见 [机器人运行时四层职责定义](../runtime-four-layer-definition/spec.md)：Channel、
Gateway、Application、Agent 组成消息运行分层，装配、控制观测和独立测评单列。本规格保留
当次实施背景、配置与执行交接约束及其验证记录。

## Design

- 复用现有配置和组装入口，在实例组装时解析搜索与子 Agent 模型覆盖；执行路径使用已解析配置和实例客户端，客户端复用并由实例关闭。LLMClient 保留原位置和调用方式，限流的现有共享语义不变。
- 通用 AgentRuntime 准备公共输入；Backend adapter 创建具体 session。session_factory 不经 BackendOpenRequest.options 传递，只为目录、隔离和恢复等实际参数提供明确类型。
- Application 接管 Gateway 中的工作区、资源准备和 actor 请求组装。Gateway 继续持有准入、run、取消、outbox 和交付事实，执行结果只暴露结果及绑定本轮的交换引用，不暴露 actor 内部状态。
- Application 在可信交付确认后幂等提交交换；取消、交付未知和失效 generation 丢弃未确认交换。Provider 已确认但 journal 提交失败保留两项事实，不自动重发。同会话占用覆盖交付处理。
- BotSpec 字段解释和实例配置投影归现有 botspec 模块；通用摘要和序列化留 Core。QQ CDN 抓取实现归 QQ Channel，资源 DTO/端口归 Contracts；保持原有绑定、DNS/TLS、大小和文件校验。
- 保留唯一 PromptPlan、ToolRegistry、ToolExecutor，QQ actor 隔离和权限复检，以及 Evaluation 独立生命周期。拆分实现同步更新 Evaluation 指纹覆盖。
- 已有公开导出只在确有兼容契约处转发；内部消费者使用实际实现入口。代码回退不改写任何运行状态或观测记录。

## Acceptance

- 不同实例和 actor 的配置、客户端选择、权限及 resume 不串用；执行路径不重新解释模型环境覆盖。
- 三 Backend 执行、取消、关闭和 PromptPlan 行为保持；具体 session 构造不在通用 runtime。
- Gateway 不操作 actor_state，工作区/资源准备由 Application 执行；拒绝准入和关键持久化失败先于附件、Agent、模型和工具副作用。
- 正常交付、取消、交付未知、重复确认、失效引用和 journal 提交失败处理正确；未确认回答不进入共享历史。
- 当前配置、历史任务配置、任务流和能力页投影保持；只读配置查询不创建 Agent 或连接插件。
- 相关测试、仓库完整验证、前端测试和差异检查有本次结果。不得将确定性替身视为真实模型或 QQ 端到端证据。

## Verification

实施基线为 8586fc896ce24b1e6d6c1eb863b08644d95b15aa；主工作区既有 scripts/verify_guided_runtime_matrix.sh mode 改动受保护。实施在隔离工作树内进行，完成后交付未暂存差异，不提交、不部署、不修改真实机器人配置或 workspace。

2026-09-08 本次验证使用 Python 3.10.12、当前源码 PYTHONPATH 和独立短临时目录：

- 配置与模型客户端相关测试：149 passed、14 subtests passed；覆盖实例隔离、环境覆盖、客户端复用、关闭与部分构造失败。
- Backend 构造与 Evaluation 指纹相关测试：116 passed、11 subtests passed；包含 Native/LangGraph 多会话隔离及单 session 关闭边界。
- Application/Gateway 相关测试：65 passed；覆盖真实 Gateway 与 Application 配合确定性 Agent 的交付、取消、失效 generation、journal 失败和清理重试。新增异步测试的等待有界。
- 配置投影、观测与资源回归：59 passed、9 subtests passed；两个实例夹具的配置投影及指纹与基线一致。
- 完整门禁 `scripts/check_repo.py full` 通过：2998 passed、1 skipped、137 subtests passed；SDD、公开信息边界、架构、依赖、UTF-8、Ruff、类型、组件目录、wheel/sdist 隔离校验及 Console 生产构建均通过。架构检查为 521 个模块、1607 条静态依赖、0 个环。
- 前端 `npm --prefix console/web test`：11 个测试文件、115 项测试通过。

各专项范围存在重叠，不合并计数。完整验证曾发现两个 ACP 旧夹具缺少真实 SubagentSpec 字段，以及一个 Console 测试受系统时钟回拨影响；仅修正夹具和测试时钟，相邻回归 28 passed 后重新执行完整门禁。生产快照有效期判断保持不变。

打包清单使用独立临时 Git 索引表达未暂存的新增与删除，实际暂存区保持不动。唯一跳过项是 Windows 专属大小写行为；未执行实际模型、真实 QQ/NapCat、真实 MCP/CDN 或浏览器端到端验收。此次不改变 Console 布局，不部署或重启真实实例。
