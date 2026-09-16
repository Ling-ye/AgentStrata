---
id: console-task-execution-graph
type: architecture
status: implemented
created: 2026-09-16
---

# Console 四层任务与 Agent 执行图

## Summary

Gateway 任务工作台提供四层总览、真实阶段分组、Agent 有向图和调用树；共享结构化检查器支持
固定两步对照。对象保留层级、类型与原始键，数组支持表格，正文按需读取。

## Design

遵循 [四层运行时基线](../runtime-four-layer-definition/spec.md)。Console 位于运行链外，读取
既有观测索引、事件分页、SSE 和正文接口；Channel/Gateway/Application/Agent 的职责、准入、
执行、交付与交换提交不变。取代 [现有工作台](../console-layered-observability/spec.md) 中
步骤正文全部就地展开的布局，Legacy 独立入口保持。

- 四层摘要使用运行职责，不使用配置 layer。阶段按真实交接顺序分组，返回路径重复出现；
  未经过、尚未取得、未采集、到期和采集失败分别显示。执行结果和交付回执分别呈现。
- 调用图使用 React Flow 和 ELK，按次展开，子 Agent 可收起。树、图和检查器共享 trace/span
  身份、选择与关系；开始/更新/结束合并，正文和 Token 更新不重排布局。拓扑更新保留视口。
- 父子关系使用 parent_span_id，模型触发工具使用 model_span_id。AgentProcessAdapter 从
  ContextSnapshotPrepared.effective_messages 生成 input_tool_refs（tool_call_id、message_index）
  放入既有观测 metadata；只含实际 role=tool 消息，无正文，不修改 AgentEvent 或数据库 schema。
  工具结果只在同一 trace 和 Agent 调用作用域唯一匹配时连接到模型上下文；缺失/歧义不猜测。
  metadata 缺失的历史记录不回填；采集范围/截断标记继续适用。
- 结构化检查器提供树、对象数组表格、JSON、键值搜索、值/子树/JSON Pointer 复制。null、
  空值、空容器和缺失分别显示。模型消息按角色分组，工具原始结果与交给模型的结果分别显示。
  搜索只检查当前已加载载荷。大数组渐进展开，不为图批量获取正文，不保存正文到浏览器存储。
- 右侧查看所选节点，最多固定另一节点对照；窄屏移至下方。保留配置、权限、日志、公开摘要、
  原始执行归档和精确来源定位。状态按实例/run 隔离，轮询、SSE、翻页不抢选择与阅读位置。
- Native/LangGraph 使用实际模型轮次，Codex 保留 backend_execution 和公开活动，不生成内部
  模型轮次或隐藏推理。观测异常不改变业务结果；私有原值与读取边界沿用现有契约。

参考 Langfuse Agent Graphs 的展开执行图、Jaeger 多视图和 React Flow / ELK 布局机制。
本项目不根据时间/名称推断因果，
不引入外部观测服务。首版不做上下文自动差异、跨任务对照或可编辑工作流。

## Acceptance

- 四层往返、阶段分组、模型/工具/嵌套 Agent 关系可定位；同名并行调用不合并，不重复计数。
- 图、树和检查器定位一致；输入来源可以定位实际消息，两个节点可固定对照。
- 未载入、迟到父节点、重复事件、缺失结束、取消、未知交付和正文到期保留真实状态。
- 结构化展示保留类型、原始键与空值，搜索/复制路径可复核；大数组与千级步骤保持按需展示。
- 桌面与 390px 下完成选择、缩放、对照、键盘、实时更新与刷新恢复；浏览器无未处理异常。

## Verification

2026-09-16 在 main 完成本次实现，未暂存、未提交、未部署：

- `npm --prefix console/web test`：26 个文件、226 项通过。包含结构化类型/路径/搜索、明确关系与
  Agent 作用域、缺失/迟到/重复调用、四层返回、千级步骤投影和实际 ELK 高扇出布局。
  1002 节点/1001 连线布局测试约 1.5 秒完成，全部节点保留；默认昂贵布局已替换为轻量策略。
- `npm --prefix console/web run build`：TypeScript 与 Rsbuild 生产构建通过；图组件和 ELK Worker
  为按需加载产物。全文与结构化正文保持按节点按需读取。
- Agent 过程投影、观测工作台、Console API 和 Gateway 定向 Python 回归：66 passed。
- `scripts/check_repo.py full` 的 SDD、公开边界、架构、requirements、UTF-8、Ruff、类型、组件
  目录、pip 一致性及 wheel/sdist 精确成员/隔离运行检查通过；架构为 629 模块、2157 边、0 环。
  Python 全量为 4195 passed、2 failed、1 skipped、154 subtests passed。首次失败分别为测试期间
  持续修改源码触发候选摘要漂移，以及 SQLite 并发压力下初始化锁超时。停止修改与布局压测后，
  两个失败用例独立复跑 2 passed；不把首次 full 记录为通过，不重复整套全量。full 在失败处停止的
  Console 构建项由上面的独立生产构建覆盖。
- 本次生产构建在真实 Chromium 中完成 28 项合成数据浏览器检查，0 页面异常：四层阶段、两步
  对照、嵌套数组表格、复制原始字段路径、图与输入来源、子 Agent 折叠/聚焦、键盘、SSE 更新
  保持坐标、刷新恢复选择/视口、任务隔离、1002 步分页和大图筛选、正文懒加载、到期以及 390px
  无页面横向溢出。浏览器调用与截图使用隔离 HTTP 夹具，不连接真实机器人。
- 最终公开边界、SDD 和 `git diff --check` 通过。npm audit 报告既有 Vitest / @vitest/mocker
  开发依赖的两项中危告警，本次新增图依赖未列入告警，未顺带升级测试框架主版本。

各测试集合存在重叠，不累计为新的通过总数。没有调用真实模型、发送 QQ/NapCat 消息或修改
实例配置；这些场景及部署后的界面不在本次验证结论内。
