# Harness 修复与交付

修改修复来源、验证计划、编程或审核时阅读。使用步骤见 [Harness 指南](../guides/harness.md)，主动巡检见 [代码治理](code-health.md)。

## 同一宿主，两种来源

Case/机器人失败修复以冻结证据复现问题；代码治理以远端 main 的源码按领域巡检。两者复用任务存储、worker、维护锁、进度与交付，Evaluation 继续拥有评分和来源记录。

新任务经过固定验收与独立审查后由宿主交付正式 PR。修复、发布、合并和清理分别记录；操作者工作区和交互式 AI 不获得该自动交付权限。完整边界见 [Git 交付](delivery.md)。

## 源码入口

- [src/chatcopilot/harness/api.py](../../src/chatcopilot/harness/api.py)
- [src/chatcopilot/harness/workflow.py](../../src/chatcopilot/harness/workflow.py)
- [src/chatcopilot/harness/verification.py](../../src/chatcopilot/harness/verification.py)
- [src/chatcopilot/harness/preparation.py](../../src/chatcopilot/harness/preparation.py)
- [src/chatcopilot/harness/models.py](../../src/chatcopilot/harness/models.py)

## 修复契约

### 控制与生命周期

`HarnessController` 保留公开操作与查询。`control_types` 定义 worker 存活三态及调度结果，
`control_service` 通过端口统一取消、恢复、接续与失联核对；`worker_runtime` 持有装配时的配置快照，
负责 systemd、冻结目录和进程事实。存活判断检查任务的执行锁、修复与交付 unit，以及尚未完成的
systemd job。调度结果 unknown 不等于进程停止，未知时保留取消请求与任务占用。

控制操作使用同一任务锁，并保留全局维护锁和 worker 执行锁。取消只在 worker 自行收尾，或宿主确认
停止后完成；测评取消的请求回执不等于外部测评已经结束。失败或未结束时保留关联供重试。
状态写入使用事务条件保护，迟到的 worker 不得把取消状态恢复成运行或成功。

`get/list/progress` 不探测进程或推进生命周期。Console 启动时装配控制入口；初始化失败仅使 Harness
入口不可用，不影响其他页面。现有每分钟定时入口先核对活动任务，再处理交付；单项失败不阻止其他任务。
CLI 的 `reconcile` 可主动核对，取消、恢复和接续也会立即检查运行事实。定时器不可用时不能承诺
失联状态的自动更新时限。完整设计见 [Harness 控制生命周期](../../specs/harness-control-lifecycle/spec.md)。

六层静态门禁目前只覆盖控制 Types/Service 及直接 CLI/Console 入口，包含函数内导入与重导出检查；
其他 Harness 模块仍按领域基线逐步整理。静态检查不是 Python 执行沙箱，也不能证明运行时状态正确。

### 验收与交付

- **单 Case Harness 独立性**：`chatcopilot.harness` 拥有按需 worker、任务与尝试数据库；
  通过来源、验证、编程和 Git 交付适配器协作，Evaluation 不反向依赖 Harness。测评数据库由
  Evaluation 服务校验并保存新增结果，历史文件不批量迁移；Console 组合查询两个模块，
  不跨库写表。修复入口、进度和历史只在并列的 AI Harness 页面展示，测评页和机器人
  任务流不管理修复状态。Evaluation 为每条 Case 执行记录提供稳定的 `case_instance_id`，
  绑定测评、Case、Target 与重复执行序号并维护定位索引；测评页可复制测评及 Case 实例
  ID，不能发起修复或轮询 Harness。Console Harness 只提交 Case 实例 ID，由服务端
  经 Evaluation 公开查询获取来源并复检失败状态；不恢复手工拼接引用或客户端指定
  测评/Target。日常任务只读绑定实例的 Gateway 观测，冻结来源、根因假设与验证计划；
  确定性缺陷用 pytest，Agent 行为用 Evaluation 登记的声明式 Case 实际运行模型与隔离
  产品工具。准备阶段产品只读，修复阶段测试只读；环境、评分和证据错误不能冒充产品失败。
  两种来源共用仓库单元与架构回归；模型每组至少三次且在验收前独立确认，预算不因阶段拆分重置。
  Case 来源允许 repair_hint，但不得覆盖原预期；原始观测与评分不改写。
  机器人任务可携带可选 `feedback`（修复提示、参考答案／预期行为），与原始观测分开
  保存并随任务冻结，贯穿准备、修复和可选审核，参与请求幂等及候选复用身份。
  参考答案是用户期望，不是已验证事实或宿主策略，不能替代真实本地复现；修改后发起新任务。
  候选测评必须加载实际冻结源码与获准 BotSpec／提示词变更；模型/backend、身份和资源授权
  固定，候选配置身份与比较不变量分开。Case、评分与控制实现保持受信版本，评分预期不进入
  被测 Agent。Agent 回归收录至 tests/agent_regressions/<digest>/case.json，经显式测评执行，
  单元测试不得隐式调用商用模型。
  worktree 目标及保护集验收通过才可接受候选，原始成绩不改写。新任务必须完成
  一次只读 AI 审核和宿主检查，再将修复与冻结测试提交到任务分支并创建正式 PR，
  等待 GitHub CI 后 squash 自动合并；结果、交付和清理分别记录，旧记录不迁移。
  规格见 `docs/reference/harness.md` 与 `docs/reference/harness.md`。
  准备过程在冻结前自动试运行、独立审查和修订草案；参考答案完整保留为必需验收项。
  同一计划可以组合 pytest 和真实 Agent，各自保留重复次数与证据。私有图片由 Evaluation
  经分块导入接口校验、按来源作用域和摘要保存，通过 AgentTask.resources 传入后端；
  验收同时检查宿主传图回执和语义评分，原图不公开收录。旧记录使用幂等接续并累计预算，
  缺原图时等待补充后继续本任务；子验证失败先写终态，再自动修订或报告技术失败。
