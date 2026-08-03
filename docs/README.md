# AgentStrata 文档中心

这里是面向使用者、运维者和贡献者的文档入口。根目录
[`README.md`](../README.md) 是唯一的项目介绍页；本页只负责导航，不复制正文。

## 按目标阅读

| 我想要…… | 从这里开始 | 接着阅读 |
| --- | --- | --- |
| 了解项目定位与能力边界 | [`README.md`](../README.md) | [`architecture.md`](architecture.md)、[`runtime.md`](runtime.md) |
| 创建或配置机器人 | [`bot-spec.md`](bot-spec.md) | [`../bots/_template/README.md`](../bots/_template/README.md) |
| 首次安装到 Linux / WSL | [`deployment.md`](deployment.md) | [`operations.md`](operations.md) |
| 更新、重启、看日志或诊断 | [`operations.md`](operations.md) | [`../deploy/wsl/README_WSL.md`](../deploy/wsl/README_WSL.md) |
| 管理控制台或运行评测 | [`console.md`](console.md) | [`evaluation-glossary.md`](evaluation-glossary.md) |
| 准备后续 GitHub Release | [`releasing.md`](releasing.md) | [`../CHANGELOG.md`](../CHANGELOG.md) |
| 获取社区支持 | [`../SUPPORT.md`](../SUPPORT.md) | [`../SECURITY.md`](../SECURITY.md) |
| 管理 Docker MCP 服务 | [`../deploy/docker/README.md`](../deploy/docker/README.md) | [`operations.md`](operations.md) |
| 接入或修改外部工具 | [`external-tools-architecture.md`](external-tools-architecture.md) | [`architecture.md`](architecture.md) |
| 参与开发 | [`../CONTRIBUTING.md`](../CONTRIBUTING.md) | [`sdd.md`](sdd.md)、[`ai-debugging.md`](ai-debugging.md) |

## 文档分层

- **入口**：`README.md` 回答“这是什么、适合谁、如何开始”。
- **操作**：`operations.md` 是日常命令的唯一集中速查；安装设计归
  `deployment.md`，异常恢复归 `deploy/wsl/README_WSL.md`。
- **参考**：架构、运行时、BotSpec、控制台和外部工具文档解释稳定契约与实现边界。
- **决策记录**：`specs/` 保存已接受或已实现的设计决策，不是新读者的必读目录。
- **协作规则**：`AGENTS.md` 面向 AI 协作者，不能替代用户文档或贡献指南。

同一命令只在其事实源中完整说明；其他文档使用链接。这样可以避免一次行为变更需要
同步修改三到四份文档，也让过期描述更容易被发现。

## 命名约定

公开产品、发行包和命令行程序使用 **AgentStrata** / `agentstrata`。
`chatcopilot` Python namespace、`CHATCOPILOT_*` 环境变量、systemd unit 名和
`~/ChatCopilot*` 运行路径仍是兼容契约。文档只在引用这些真实接口时保留旧名称，
不再把 ChatCopilot 当作产品名。
