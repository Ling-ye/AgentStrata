# 支持与问题反馈

AgentStrata 是面向外部应用的可定制 Agent 运行系统。问题可能发生在四层运行链、自有 Agent 能力、外部桥接、Harness、Eval 或版本启用环节。先定位具体应用和操作，再沿对应文档核对实际状态。

## 找到合适的入口

| 你需要什么 | 入口 |
| --- | --- |
| 了解项目与架构 | [README](README.md)、[架构总览](docs/reference/architecture.md) |
| 首次安装与应用部署 | [部署指南](docs/guides/deployment.md)、[QQ](docs/guides/qq.md)、[Live2D](docs/guides/live2d.md) |
| 配置模型、工具与实例 | [配置参考](docs/reference/configuration.md)、[实例运维](docs/guides/instances.md) |
| 理解自有 Agent、LangGraph 循环与可选 Codex 执行 | [Agent 能力与边界](docs/reference/agent.md#自有实现与优化边界)、[执行契约](docs/reference/agent.md#一条执行契约) |
| 发起需求、修复或评测 | [Harness 指南](docs/guides/harness.md)、[Evaluation 指南](docs/guides/evaluation.md) |
| 启用、撤回或恢复版本 | [版本操作](docs/guides/instances.md#启用与撤回已验收版本) |
| 排查错误与环境问题 | [诊断指南](docs/guides/debugging.md)、[WSL 排障](docs/guides/wsl-troubleshooting.md) |

完整导航见[文档中心](docs/README.md)。

## 报告问题或提出需求

通过 [GitHub Issues](https://github.com/Ling-ye/AgentStrata/issues) 提交可复现问题、明确的功能建议或使用问题。接入选型、尚未收敛的想法和使用交流可以进入 [Discussions 问答](https://github.com/Ling-ye/AgentStrata/discussions/categories/q-a)；已有 Question Issue 仍接受使用问题。先搜索已有记录，再选择合适的入口。

接入自己的应用时，说明现有应用、期望能力、可用接口和能投入的范围。形成明确开发任务后转为 Issue 并相互链接；接入成果进入[作品展示](https://github.com/Ling-ye/AgentStrata/discussions/categories/show-and-tell)。贡献步骤见[贡献指南](CONTRIBUTING.md)。

尽量提供以下信息：

- 使用场景、具体操作、预期结果和实际结果。
- AgentStrata 版本或 commit、操作系统、Python 版本，以及相关插件版本。
- 所选 runtime、模型与连接类型；只提供无秘密的配置片段。
- 最小复现步骤、相关错误码和已脱敏的日志。
- 验证所用的外部服务是真实环境还是受控替身，以及问题是否能够重复发生。

Harness / Eval 问题还应说明来源类型、失败阶段、基线与候选条件、必要证据是否齐全。版本启用问题应区分候选接受、启用操作状态和实际运行身份；不要仅凭“任务完成”或“进程启动”判断结果。

Harness 的 Codex 开发 worker 与被优化实例分别配置。反馈执行问题时注明出错的是业务回合还是开发 worker；账户或模型目录查询成功不能证明实际模型回合可执行。能力问题请说明任务、工具、上下文与实际效果，便于判断应调整 Agent 实现、配置还是桥接。

请用合成标识替换私有任务 ID、账号、群号、租户地址、项目名称和机器路径。不要上传 API Key、token、认证目录、完整私有证据归档或未经脱敏的截图。

## 安全问题与响应范围

漏洞和可能涉及秘密、权限或数据完整性的问题，按 [SECURITY.md](SECURITY.md) 的私密流程报告，不先发布到公开 issue。

这是社区开源项目，不提供固定响应时间或服务等级承诺。维护者邮箱用于安全协调和行为准则事务，日常排障请使用公开 issue，以便其他使用者检索和复用解决办法。
