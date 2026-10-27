# 参与开发 AgentStrata

AgentStrata 是面向外部应用的可定制 Agent 运行系统，四层架构共同承载应用接入、会话、上下文与 Agent 能力。贡献的目标是让自有 Agent 更可靠地完成任务，让外部能力更容易接入，并通过 Harness 与 Eval 为改进提供可复核的依据。

欢迎从一个具体需求、一条失败路径、一组评测用例或一处重复实现入手。先说明预期行为，再选择最小修改范围；已经成熟的基础设施优先复用。

[English contribution guide](docs/guides/contributing-en.md) · [新手任务](https://github.com/Ling-ye/AgentStrata/issues?q=is%3Aissue%20is%3Aopen%20label%3A%22good%20first%20issue%22%20no%3Aassignee%20-linked%3Apr) · [其他可参与任务](https://github.com/Ling-ye/AgentStrata/issues?q=is%3Aissue%20is%3Aopen%20label%3A%22help%20wanted%22%20no%3Aassignee%20-linked%3Apr) · [接入交流](https://github.com/Ling-ye/AgentStrata/discussions/categories/q-a)

中文和英文都可以。普通社区 PR 使用常规审查与 CI，不要求运行整套 Harness；正式自主改进验证由维护者按主计划组织。

## 找任务与认领

`good first issue` 面向第一次参与本项目的开发者，应有明确做法、源码入口和无需真实账号或付费模型的验证方法。`help wanted` 适合已经熟悉相关技术的人，环境与证据要求以任务正文为准。

在任务下留言说明想做的内容，维护者确认后指派。默认一次认领一个新手任务；简单且明确的小修复可以直接提交 PR。接入想法尚未收敛时，先在 Discussions 说明已有应用、期望能力、可用接口和可投入的范围。

维护者尽量在 3 个工作日内首次回应、5 个工作日内开始实质审查；这是运营目标，不是响应时间保证。遇到延迟会说明下一次处理时间。
约一周没有更新时，维护者询问是否需要帮助；两周仍无回应可说明原因后释放认领。维护者与 Harness 避免抢做已认领任务，紧急接手先沟通并保留贡献署名。

## 第一次提交 PR

1. 在 GitHub 将项目 Fork 到自己的账号。核对 Issue 的范围和验收；小改动可以直接开始。
2. 在自己的 Fork 页面复制 HTTPS clone 地址，用它替换下面的 `YOUR_FORK_URL`，准备分支：

   ```bash
   git clone YOUR_FORK_URL AgentStrata
   cd AgentStrata
   git remote add upstream https://github.com/Ling-ye/AgentStrata.git
   git switch -c fix/describe-your-change
   ```

3. 按[最小开发路径](docs/guides/development.md#按贡献类型开始)准备环境，修改一个明确目标。环境阻塞可以先开 Draft PR，说明已经尝试的步骤。
4. 运行任务要求的验证，检查 `git diff` 和 `git diff --check`，只加入自己要提交的文件。以下是贡献者自行执行的 Git 步骤；AI 助手仍须遵守 [AGENTS.md](AGENTS.md) 的逐项授权要求。

   ```bash
   git add PATH_TO_CHANGED_FILE
   git commit -m "简述改动目的"
   git push -u origin HEAD
   ```

5. 在 GitHub 从自己的分支向 `Ling-ye/AgentStrata` 的 `main` 创建 PR，按模板说明问题、验证和影响。有对应任务时填写 `Closes #任务编号`，仍在开发则使用 Draft。
6. 根据审查修改同一分支并再次推送。首次 Fork 工作流可能等待维护者批准运行，无需提供模型或部署凭据；请保留实际失败、跳过和环境阻塞信息。

## 选择贡献方向

| 方向 | 适合贡献的内容 | 首读 |
| --- | --- | --- |
| 自有 Agent 能力 | 能力装配、上下文、工具发现与使用，以及自有 LangGraph 图、停止和委托策略 | [Agent](docs/reference/agent.md#自有实现与优化边界)、[上下文](docs/reference/context.md)、[工具](docs/reference/tools.md) |
| 外部应用与桥接 | 消息渠道、Gateway 客户端、MCP、插件和适配器 | [运行链](docs/reference/runtime.md)、[身份与资源](docs/reference/identity-resources.md) |
| Harness | 需求调查、角色协作、候选开发、返工和独立验收 | [Harness](docs/reference/harness.md)、[图编排规格](specs/harness-langgraph/spec.md) |
| Eval | 题目、执行环境、评分、原生基准适配与候选对照 | [Evaluation](docs/reference/evaluation.md)、[用例指南](docs/guides/evaluation-cases.md) |
| 版本与运维 | 实例身份、源码交付、启用撤回和恢复 | [交付契约](docs/reference/delivery.md)、[实例运维](docs/guides/instances.md) |
| Console 与文档 | 任务操作、证据呈现、诊断与使用体验 | [Console](docs/reference/console.md)、[文档维护](docs/maintenance.md) |

模块关系见 [README](README.md#框架总览)。四层运行职责与领域内部的源码依赖约束同时生效，完整边界见[架构总览](docs/reference/architecture.md)。

后续优化沿唯一[主计划](specs/application-harness-framework/spec.md#自有-agent-能力优化路线)推进，先复用已有能力与 Eval 建立基线，再按实际缺口选择实现位置。LangGraph 循环也是正式优化对象；Codex 作为可选执行器和当前开发 worker，通过公开接口接入。

## 从源码环境开始

按[开发指南](docs/guides/development.md#准备环境)安装锁定依赖，并使用同一解释器运行检查。操作与领域入口统一由[文档中心](docs/README.md)维护。

使用 AI 协作时，先让它阅读根目录 [AGENTS.md](AGENTS.md)，再按当前目标进入对应领域、源码与测试。目标、权限、来源和验证方式应当明确；模型输出中的完成声明需要实际结果支持。

欢迎 AI 辅助。提交者应理解改动、核对测试结果并能回应审查；区分实际执行的验证与模型建议。大范围生成的改动请拆成可独立审查的部分，无需提交提示词、聊天记录或私有日志。未经核验的批量 PR 会被要求补齐依据或收敛范围。

## 提交可审查的改动

1. **说明问题与目标。** 给出触发条件、预期行为和实际结果。新增功能说明使用场景及验收方式，不需要人为构造历史故障。
2. **核对已有实现。** 优先沿现有 Channel、Gateway、RuntimeAdapter、ToolRegistry、Harness 和 Eval 扩展，避免增加重复的生命周期、注册中心或状态机。
3. **保留信任边界。** 身份和权限由宿主确定，外部内容不能提升为宿主规则。候选代码与本次评分、验收和控制程序分开。
4. **给出对应证据。** 说明改动如何满足目标，哪些原有行为受到保护，哪些条件或外部验证仍需单独确认。
5. **同步可维护的文档。** 机制写入对应 reference，步骤写入 guide，根 README 保持项目介绍和导航职责。运行日志留在 PR 或 CI。

架构、公开契约、部署和迁移先引用或创建 [SDD](docs/maintenance.md#sdd-生命周期)；普通修复不需要另造计划文件。PR 描述沿用[仓库模板](.github/pull_request_template.md)，交代具体问题、最终行为、验证结果及影响范围。

## 验证你的贡献

按[检查范围](docs/guides/development.md#选择检查范围)选择定向测试和 `docs`、`fast` 或 `full`。涉及插件、生成器和 Windows 桌面时，执行相应安装或平台检查。检查通过后停止；新增改动、失败或具体风险出现时再补充验证。

评测变更应核对输入、执行条件、评分与证据，区分目标改善、已有失败和新增回归。修改验证器时保留独立的正确、错误和缺证据样本，避免只验证实现自己声明的成功。

自有 Agent 改进以任务完成、工具与参数正确性、上下文使用及实际效果验收。配置调整、桥接扩展和 Agent 实现变化分别说明；比较 Agent 侧候选时固定外部工具、任务、模型与环境，没有有效改善空间就记录无需改动。

本地检查、受控服务、真实模型和外部交付分别报告。截图注明数据来源，benchmark 说明数据版本与评测范围；不要把模拟回执、进程启动或一次通过写成真实交付和长期效果。

真实模型或外部服务验证若是合并条件，维护者应在 Issue 中先说明环境、费用与验证负责人。社区 PR、Agent 效果改善与 Harness 实际生成候选的证据分别记录；不要求每个贡献者自行完成 M7。

## 合并之后

合并时说明改动在哪里生效，并推荐一个相关的后续任务。你可以继续维护接入示例、协助复现和答疑、审查同类改动，也可以按实际兴趣深入工具、上下文与执行策略。参与审查不要求先获得仓库写权限。

在[作品展示](https://github.com/Ling-ye/AgentStrata/discussions/categories/show-and-tell)分享接入成果时，提供支持版本、操作入口、验证范围、已知限制和原项目链接。独立应用可以继续独立托管，具体贡献和作者会在相应项目更新中说明。任务整理与运营步骤见[社区维护指南](docs/guides/community.md)。

## 许可、署名与私有信息

项目自有代码和文档采用 [AGPL-3.0-only](LICENSE)。提交贡献表示你有权按此许可提供；贡献者保留自己的版权，本项目不要求版权转让或 CLA。

引入第三方代码、数据或改编材料时，提供来源、固定版本、原署名、许可和修改说明，并更新[第三方声明](THIRD_PARTY_NOTICES.md)。Open-LLM-VTuber 前端补丁按其上游条款单独许可。

凭据、机器配置、平台账号、私有任务记录与未脱敏日志不得上传。安全问题按 [SECURITY.md](SECURITY.md) 私下报告，使用问题见 [SUPPORT.md](SUPPORT.md)。AI 协作中的 Git 与发布授权边界以 [AGENTS.md](AGENTS.md) 为准。
