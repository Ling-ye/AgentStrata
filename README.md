# AgentStrata

单代码库、多机器人平台。通过 BotSpec 组合模型、工具与上下文，用明确职责连接平台接入、智能体执行、观测、测评和修复。

## 开始开发

```bash
uv sync --frozen --extra agent --extra acp --extra dev
uv run agentstrata --help
```

从 [开发指南](https://github.com/Ling-ye/AgentStrata/blob/main/docs/guides/development.md) 配置环境，再按 [任务地图](https://github.com/Ling-ye/AgentStrata/blob/main/docs/README.md) 找到本次领域。AI 协作者先读 [AGENTS](https://github.com/Ling-ye/AgentStrata/blob/main/AGENTS.md)。

## 部署 QQ 机器人

在 WSL/Linux 源仓运行 `bash deploy/wsl/quickstart.sh`；可先加 `--dry-run` 查看变更，使用 `--resume` 从已有状态继续。准备条件与人工登录见 [首次部署](https://github.com/Ling-ye/AgentStrata/blob/main/docs/guides/deployment.md)，不自动执行付费模型或 QQ 消息测试。

## 核心能力

- 四层消息职责与领域内部单向依赖，见 [架构](https://github.com/Ling-ye/AgentStrata/blob/main/docs/reference/architecture.md)。
- Native、LangGraph、Codex 共用执行契约；工具和上下文按权限与任务装配。
- Console 区分过程、执行、评分和交付事实；Evaluation 独立管理题目与评分。
- Harness 冻结来源和验收，独立审查后按受控流程交付 PR；修复、合并和部署分别判断。

## 项目边界

项目提供源码与自托管工具，不提供模型、平台账号或第三方凭据。状态与可用入口以当前源码和文档为准；本地检查不等于真实平台或模型验收。

Python namespace 保留 `chatcopilot`，环境变量保留 `CHATCOPILOT_*`。版本与依赖以 [pyproject.toml](https://github.com/Ling-ye/AgentStrata/blob/main/pyproject.toml) 为准。

[贡献](https://github.com/Ling-ye/AgentStrata/blob/main/CONTRIBUTING.md) · [安全](https://github.com/Ling-ye/AgentStrata/blob/main/SECURITY.md) · [支持](https://github.com/Ling-ye/AgentStrata/blob/main/SUPPORT.md) · [行为准则](https://github.com/Ling-ye/AgentStrata/blob/main/CODE_OF_CONDUCT.md) · [变更记录](https://github.com/Ling-ye/AgentStrata/blob/main/CHANGELOG.md) · [MIT 许可](https://github.com/Ling-ye/AgentStrata/blob/main/LICENSE)
