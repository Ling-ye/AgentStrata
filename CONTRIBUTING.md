# 参与开发

从 [开发环境与检查范围](docs/guides/development.md) 开始，按 [任务地图](docs/README.md) 定位领域正文。

## 参与 Harness 与 Evaluation

- 修改修复核心、角色协作或执行适配：先读[Harness 契约](docs/reference/harness.md)，再按源码入口进入实现；LangGraph 编排约束见[规格](specs/harness-langgraph/spec.md)。
- 修改题目、评分或比较方法：先读[Evaluation](docs/reference/evaluation.md)与[用例指南](docs/guides/evaluation-cases.md)，保留冻结题目、评分归属和可比条件。
- 修改代码熵治理或 Skill 学习：先读[治理契约](docs/reference/code-health.md)，区分产品候选、冻结宿主规则与受限学习任务。

修复与评测变更须提供对应失败路径及验证依据，区分目标改善、已有失败和新增回归。
受控 Runner、fixture 与本地检查应明确标注；真实模型、GitHub 交付和 QQ 送达分别报告，
不以其中一项替代其他项。检查范围沿用[开发指南](docs/guides/development.md#选择检查范围)。

## 提交可审阅的变更

先说明具体问题和预期行为，保留相关接口、权限、数据及用户工作流。修改关联代码时检查领域页的源码入口，必要时同步正文；不为每个代码变化制造文档修改。

架构、公开契约、部署和迁移使用 [SDD 工作流](docs/maintenance.md#sdd-生命周期)。普通修复提供相关回归，运行结果留在 PR 或 CI，不复制到规格。

PR 描述按 [模板](.github/pull_request_template.md) 说明问题、最终改动和实际验证。凭据、机器配置与私有执行数据不得上传；第三方资产保留来源和许可。Git 提交与发布由维护者授权，AI 协作约定见 [AGENTS](AGENTS.md)。
