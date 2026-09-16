---
id: console-task-execution-graph
type: architecture
status: implemented
created: 2026-09-16
---

# Console 四层任务与 Agent 执行图

## Summary

Gateway 任务工作台提供四层总览、真实阶段分组、Agent 有向图和调用树；共享结构化检查器支持
固定两步对照。对象保留层级、类型与原始键，数组按索引逐层展开，正文按需读取。

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
- 结构化检查器只提供树、JSON 和键值搜索；仅 JSON 视图提供整块复制。null、
  空值、空容器和缺失分别显示。模型消息按角色分组，工具原始结果与交给模型的结果分别显示。
  搜索只检查当前已加载载荷，结果保留字段路径；树和搜索结果无字段级复制按钮。大数组渐进展开，不为图批量获取正文，不保存正文到浏览器存储。
- 右侧查看所选节点，最多固定另一节点对照；窄屏移至下方。保留配置、权限、日志、公开摘要、
  原始执行归档和精确来源定位。状态按实例/run 隔离，轮询、SSE、翻页不抢选择与阅读位置。
- Native/LangGraph 使用实际模型轮次，Codex 保留 backend_execution 和公开活动，不生成内部
  模型轮次或隐藏推理。观测异常不改变业务结果；私有原值与读取边界沿用现有契约。

同日展示精简：任务输入、请求参数和配置折叠标题只显示一次。工具接入正文只呈现提供工具，
来源、状态和错误留在步骤摘要。完整原文按实例/run/步骤的 body_ref 去重，主要面板优先；不同
业务面板仍保留，不跨步骤或对照节点去重，不读取正文判断重复。原始记录是 JSON 文本及单个
复制按钮；图连线预览不附加完整原文，事件元数据与独立 trace 仍保留。正文保持原始字段、
类型、空值及到期/截断/失败状态，不修改后端数据、接口、依赖和采集行为。

参考 Langfuse Agent Graphs 的展开执行图、Jaeger 多视图和 React Flow / ELK 布局机制。
本项目不根据时间/名称推断因果，
不引入外部观测服务。首版不做上下文自动差异、跨任务对照或可编辑工作流。

## Acceptance

- 四层往返、阶段分组、模型/工具/嵌套 Agent 关系可定位；同名并行调用不合并，不重复计数。
- 图、树和检查器定位一致；输入来源可以定位实际消息，两个节点可固定对照。
- 未载入、迟到父节点、重复事件、缺失结束、取消、未知交付和正文到期保留真实状态。
- 结构化展示保留类型、原始键与空值，搜索路径与 JSON 复制可复核；大数组与千级步骤保持按需展示。
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

### 同日任务界面精简验证

本轮仅修改前端展示、前端测试和现有文档；后端、观测字段、API、依赖和锁文件与本轮开始时
一致。逐字段路径/复制已删除，JSON 复制保留；原文入口按步骤正文引用分配。按操作者后续
要求，数据表格视图及嵌套数组表格按钮一并移除，对象与数组统一以树或 JSON 查看。

- `npm --prefix console/web test`：27 个文件、240 项通过。覆盖原文引用去重、主要面板优先、
  迟到正文/不同步骤隔离、树和数组无复制按钮及表格选项、JSON 单一复制与完整原文展示。
- `npm --prefix console/web run build`：TypeScript 与 Rsbuild 生产构建通过。
- 本次构建的真实 Chromium 合成数据验收：34 项通过、0 页面异常。覆盖重复标题、工具接入
  成功/失败、对象与嵌套数组树、原文纯 JSON、完整长文本复制、搜索焦点、原生文本复制、
  剪贴板失败反馈、两步对照、连线预览、SSE 更新、任务切换、正文到期/读取失败/截断及 390px。
- 使用独立短 TMPDIR 执行 `.venv/bin/python scripts/check_repo.py fast`：9 项门禁全部通过，
  1222 passed、34 subtests passed；两个既有警告为 Python 多线程进程使用 fork 的弃用提示。
  最终 SDD、公开边界及 `git diff --check` 通过。
- 本轮第一次 fast 因追加移除表格视图而主动停止；随后一次因测试临时根目录过长触发
  AF_UNIX path too long，结果为 1203 passed、19 failed。保持源码冻结，仅改用短临时目录后
  完整 fast 通过；没有修改测试门禁、产品服务或 socket 校验来规避失败。
- 会话外操作将本轮开始时已有的暂存内容提交；提交差异哈希与原有暂存差异一致。本轮 AI
  未暂存或提交，精简修改留在工作区，原有其他未提交内容保留。

各测试集合有重叠，不累计通过数量。浏览器使用隔离 HTTP 夹具，未调用真实模型、QQ 或
运行中的实例服务；没有部署或重启。历史验证结果不替代本轮验证。
