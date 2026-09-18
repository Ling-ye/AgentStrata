# 架构总览

用这页定位职责和修改范围，再进入相应领域。AgentStrata 是单代码库、多机器人平台，实例由 BotSpec 声明能力与运行包络。

## 两个同时生效的视角

| 视角 | 回答的问题 | 权威定义 |
| --- | --- | --- |
| Channel → Gateway → Application → Agent | 消息在哪一层处理、如何交接 | [四层运行时基线](../../specs/runtime-four-layer-definition/spec.md) |
| Types → Config → Repo → Service → Runtime → UI | 领域内部的源码允许依赖谁 | [六层依赖基线](../../specs/domain-layered-dependencies/spec.md) |

四层描述运行职责；六层按基础到上层排列，使用方依赖矩阵允许的基础层。实例宿主负责装配与生命周期，Console 和 Evaluation 位于消息链之外。

## 按修改目标进入

| 目标 | 领域正文 |
| --- | --- |
| 平台接入、消息运行和取消/交付 | [运行链](runtime.md) |
| 身份、准入、文件与群隔离 | [身份与资源](identity-resources.md) |
| 模型、Backend、提示词和子 Agent | [Agent](agent.md) |
| 记忆、人格、搜索与按需资料 | [上下文](context.md) |
| 注册、权限和外部能力 | [工具](tools.md) |
| BotSpec、环境和配置投影 | [配置](configuration.md) |
| Case、评分、监督与结果 | [Evaluation](evaluation.md) |
| 修复与 PR 交付 | [Harness](harness.md) |
| 私有配置、过程展示和前端 | [Console](console.md) |

## 为什么按领域组织

运行方向不等于 import 方向；回调和端口连接职责，不让底层反向导入宿主。声明、运行状态、执行结果与投递回执各自有权威来源，不能用一项替代另一项。领域细则只在对应正文维护。

## 源码入口

- [scripts/check_architecture.py](../../scripts/check_architecture.py)
- [src/chatcopilot/contracts](../../src/chatcopilot/contracts)
- [src/chatcopilot/gateway/runtime.py](../../src/chatcopilot/gateway/runtime.py)
