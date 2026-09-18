# 开发任务地图

先选本次任务，读到足以行动为止。操作指南解释怎么做，领域正文解释为什么和修改边界，再沿源码入口核对实现。

## 从任务开始

| 本次任务 | 先读 | 需要机制与边界时 |
| --- | --- | --- |
| 准备开发环境、选择检查 | [开发](guides/development.md) | [架构总览](reference/architecture.md) |
| 首次部署或管理实例 | [首次部署](guides/deployment.md) / [实例运维](guides/instances.md) | [配置](reference/configuration.md) |
| QQ 登录、认证或外部服务 | [QQ](guides/qq.md) / [服务](guides/services.md) | [身份与资源](reference/identity-resources.md) / [服务边界](reference/services.md) |
| 修改消息或平台接入 | [运行链](reference/runtime.md) | [身份与资源](reference/identity-resources.md) |
| 修改 Agent、提示词、记忆 | [Agent](reference/agent.md) | [上下文](reference/context.md) |
| 新增或修改工具 | [工具](reference/tools.md) | [配置](reference/configuration.md) |
| 修改评分或运行测评 | [测评操作](guides/evaluation.md) | [Evaluation](reference/evaluation.md) / [服务协议](reference/evaluation-service.md) |
| 编写题目和验收样本 | [用例指南](guides/evaluation-cases.md) | [题目约束](reference/evaluation-cases.md) / [术语](reference/evaluation-glossary.md) |
| 修复运行错误 | [Harness 操作](guides/harness.md) | [修复契约](reference/harness.md) / [黄金原则](reference/harness-principles.md) / [交付](reference/delivery.md) |
| 修改 Console | [Console](reference/console.md) | [观测](reference/observability.md) / [归档](reference/local-traces.md) |
| 排查运行失败 | [诊断](guides/debugging.md) | [WSL 恢复](guides/wsl-troubleshooting.md) |
| 维护文档或准备发布 | [文档维护](maintenance.md) / [发布](guides/releasing.md) | [公开边界](reference/publication.md) / [规格](../specs/README.md) |

## 模块入口

- [Bot 模板](../bots/_template/README.md)
- [Docker 服务](../deploy/docker/README.md) / [跨平台部署公共脚本](../deploy/common/README.md)
- [当前 Agent 题库](../src/chatcopilot/evals/suites/agentstrata-agent-tasks-v1/README.md)
- [历史工程夹具说明](../src/chatcopilot/evals/suites/agentstrata-capabilities-v1/README.md)、[QQ 合成测试](../src/chatcopilot/evals/suites/agentstrata-qq-message-flow-v1/README.md)、[业务样例](../src/chatcopilot/evals/suites/project-business-v1/README.md)

机器人 Prompt、Skill、数据和第三方许可由各自运行或许可契约管理，不能为了文档精简改变它们的行为。
