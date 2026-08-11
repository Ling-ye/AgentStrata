# Evaluation 评测术语

以下词汇是 AgentStrata 评测领域的统一口径。控制台、CLI、API、报告与规格都使用
这些名称，避免把资源生命周期、单次执行和评分结果混为一谈。

## 所有权与运行边界

**评测应用（Evaluation Application）**:
受管 Evaluation 的唯一应用生命周期实现，负责预检、活动 claim、
lifecycle state、worker 启动与恢复、取消和删除。
_避免使用_: Console manager、第二套 manager、worker facade

**评测服务（Evaluation Service）**:
与 AgentStrata 同仓库、同版本部署的本机同 UID 服务，通过受限 Unix
socket 对 Console BFF 暴露评测应用能力，不监听 TCP。
_避免使用_: 外部评测平台、远程 evaluator、Console 内嵌后端

**受管 Worker（Managed Worker）**:
由评测应用启动、执行 Evaluation Core 的独立子进程。它不依赖 Console
生命周期，并由新启动的 Evaluation service 依据 claim、state 和精确
worker 身份恢复观察。
_避免使用_: Console task、Console background job、Console worker

**控制台 BFF（Console BFF）**:
把现有 `/api/evals/**` HTTP/SSE 与页面契约投影到 Evaluation service 的边界。
它不拥有 Evaluation 生命周期、worker 或权威 artifact 写入权。
_避免使用_: Evaluation manager、artifact writer、worker supervisor

**独立本地评测（Standalone Evaluation）**:
操作者直接通过 CLI 启动的本地 Evaluation，不进入受管评测目录，
由 Evaluation Core 写完整记录。
_避免使用_: 受管 Evaluation、Console 评测

## 评测语言

**评测任务（Case）**:
一项具有固定输入、能力维度和判定方式的最小评测工作。
_避免使用_: 题目、测试（当它们指代完整评测记录时）

**任务集（Profile）**:
一组版本化的评测任务选择，以及用于重复执行这些任务的默认策略。
_避免使用_: Suite（当它指代跨套件任务选择时）、套餐

**评测套件（Suite）**:
一组来自同一基准标准、共享数据准备与评分口径的评测任务。
_避免使用_: Profile、任务集（当它指代单一基准标准时）

**候选目标（Target）**:
评测记录中接受任务的一个不可变受测配置快照，可表示 Agent backend、Chat LLM lane 或 dry-run 校验器。
_避免使用_: 模型（Target 不只包含模型）、机器人（Target 不等于 Bot 实例）

**评测记录（Evaluation）**:
在固定 Case 选择和执行策略下，对一个或多个候选目标进行的一次可持久化、可复查评测。
_避免使用_: Experiment、Run、Trial

**评测恢复（Evaluation Resume）**:
在请求、Case 快照、Bot runtime Target 和已有 Trial 结构均未变化时，从完整 checkpoint 继续一条未完成的 Evaluation；未形成 checkpoint 的 workspace 残留不属于可恢复状态。
_避免使用_: 恢复已完成 Evaluation、修改请求后重跑、复用旧 Trial、复用中断 Trial workspace

**单次尝试（Trial）**:
一个候选目标对一个评测任务进行的一次执行。
_避免使用_: Evaluation、Case

**配对结果（Case Comparison）**:
同一评测任务和重复序号下，两个候选目标的完整尝试所形成的可比较结果。
_避免使用_: Trial、总分

**能力维度（Dimension）**:
任务所代表的能力分组；MVP 使用指令遵循、知识与检索、工具编排和代码任务四类。
_避免使用_: 智能总分、排行榜分类

**评测状态（Evaluation Status）**:
评测记录的执行生命周期。受管 Evaluation 的该状态只由评测应用
管理，只描述排队、运行、完成或中断，不表达候选目标的能力高低。
_避免使用_: 通过、失败、胜负、能力结论

**尝试结果（Trial Outcome）**:
单次尝试的评分结果，例如通过、失败、跳过或执行错误。
_避免使用_: Evaluation Status、Case Comparison
