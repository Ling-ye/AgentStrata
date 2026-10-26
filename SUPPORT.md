# 支持与问题反馈

AgentStrata 的问题可能发生在应用接入、Agent 执行、Harness、Eval 或版本启用的不同环节。先定位具体应用和操作，再沿对应文档核对实际状态。

## 找到合适的入口

| 你需要什么 | 入口 |
| --- | --- |
| 了解项目与架构 | [README](README.md)、[架构总览](docs/reference/architecture.md) |
| 首次安装与应用部署 | [部署指南](docs/guides/deployment.md)、[QQ](docs/guides/qq.md)、[Live2D](docs/guides/live2d.md) |
| 配置模型、工具与实例 | [配置参考](docs/reference/configuration.md)、[实例运维](docs/guides/instances.md) |
| 发起需求、修复或评测 | [Harness 指南](docs/guides/harness.md)、[Evaluation 指南](docs/guides/evaluation.md) |
| 启用、撤回或恢复版本 | [版本操作](docs/guides/instances.md#启用与撤回已验收版本) |
| 排查错误与环境问题 | [诊断指南](docs/guides/debugging.md)、[WSL 排障](docs/guides/wsl-troubleshooting.md) |

完整导航见[文档中心](docs/README.md)。

## 报告问题或提出需求

通过 [GitHub Issues](https://github.com/Ling-ye/AgentStrata/issues) 提交可复现问题、明确的功能建议或使用问题。先搜索已有记录，再选择合适的 issue 模板。

尽量提供以下信息：

- 使用场景、具体操作、预期结果和实际结果。
- AgentStrata 版本或 commit、操作系统、Python 版本，以及相关插件版本。
- 所选 runtime、模型与连接类型；只提供无秘密的配置片段。
- 最小复现步骤、相关错误码和已脱敏的日志。
- 验证所用的外部服务是真实环境还是受控替身，以及问题是否能够重复发生。

Harness / Eval 问题还应说明来源类型、失败阶段、基线与候选条件、必要证据是否齐全。版本启用问题应区分候选接受、启用操作状态和实际运行身份；不要仅凭“任务完成”或“进程启动”判断结果。

请用合成标识替换私有任务 ID、账号、群号、租户地址、项目名称和机器路径。不要上传 API Key、token、认证目录、完整私有证据归档或未经脱敏的截图。

## 安全问题与响应范围

漏洞和可能涉及秘密、权限或数据完整性的问题，按 [SECURITY.md](SECURITY.md) 的私密流程报告，不先发布到公开 issue。

这是社区开源项目，不提供固定响应时间或服务等级承诺。维护者邮箱用于安全协调和行为准则事务，日常排障请使用公开 issue，以便其他使用者检索和复用解决办法。
