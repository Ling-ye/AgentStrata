# AGENTS.md — AgentStrata

先读本页，再按任务进入同一份领域正文；不批量加载文档或历史规格。默认使用 WSL/Linux。

## 协作与授权

- 目标、范围和权限清楚时自主完成，避免过度设计和无关改动；结论以可复核证据为准。
- 修改前核对分支、工作区、暂存区和 worktree，保护用户已有改动。
- 交互式 AI 未获逐项明确授权不 add、commit、amend、push、PR、merge、rebase、tag 或发布。
  受控 worker 的例外见 [交付契约](docs/reference/delivery.md)，不授权交互式 AI 提交。
- 架构、权限、费用、公开契约和不可逆变更按需检查具体失败路径；兼容影响不明时先说明。
- 实际越过秘密、权限或数据完整性边界时立即披露。私有值放被忽略的环境文件，不写入源码、文档和公开日志。

## 必须保留的边界

- [四层运行时](specs/runtime-four-layer-definition/spec.md) 与 [六层源码依赖](specs/domain-layered-dependencies/spec.md) 同时生效。
- 身份、准入和权限先于模型、工具、附件和持久化副作用。Owner/member 由宿主采信，不从文本推断。
- PromptPlanBuilder 保持四个信任分区；按需资料不能提升为宿主规则。工具发现与执行复用同一 Registry 和 provider catalog。
- 执行结果、provider acknowledgement 和用户可见交付分别记录；静态、mock 和局部检查不等于真实模型或 QQ 端到端证据。

## 按任务读取

| 涉及 | 入口 |
| --- | --- |
| 消息、身份、资源、准入 | [运行链](docs/reference/runtime.md)、[身份与资源](docs/reference/identity-resources.md) |
| Agent、PromptPlan、记忆、工具 | [Agent](docs/reference/agent.md)、[上下文](docs/reference/context.md)、[工具](docs/reference/tools.md) |
| BotSpec、环境、部署、发布 | [配置](docs/reference/configuration.md)、[公开边界](docs/reference/publication.md) |
| Evaluation、评分、来源 | [Evaluation](docs/reference/evaluation.md)、[服务协议](docs/reference/evaluation-service.md) |
| Harness、治理、交付 | [Harness](docs/reference/harness.md)、[治理](docs/reference/code-health.md)、[交付](docs/reference/delivery.md) |
| Console、观测 | [Console](docs/reference/console.md)、[观测](docs/reference/observability.md) |
| 开发与验证、故障诊断 | [开发](docs/guides/development.md)、[诊断](docs/guides/debugging.md) |
| 文档与 SDD | [维护入口](docs/maintenance.md)、[文档治理基线](specs/documentation-governance/spec.md) |

只继续读取本次相关章节及源码入口。跨领域任务同时遵守关联边界。架构、公共契约、部署或迁移先引用或创建 SDD，普通修复直接实现并测试。

## 完成与维护

普通说明使用 docs 检查；代码修改按 [开发指南](docs/guides/development.md#选择检查范围) 执行定向及 fast/full 验证。检查通过后停止，不为形式反复运行。

报告实际结果、跳过项和剩余风险；运行流水放在交付或 CI，不追加到文档。文档改动与代码联动，现行事实只维护一处；旧内容提炼后删除，无法确认则报告待判断。

完整任务导航见 [文档中心](docs/README.md)。
