# 维护测评用例

为需要验证的行为提供真实条件和可观察结果。当前项目 Agent 题库是
[agentstrata-agent-tasks-v1](../../src/chatcopilot/evals/suites/agentstrata-agent-tasks-v1/README.md)，
其余工程夹具按既有测试用途维护，不作为新增任务入口。

## 先确定题目在验证什么

写明用户能知道的输入、可用资源、可信身份与允许的副作用。缺少的信息允许合理澄清；
不能拿隐藏答案要求 Agent 猜测。结果导向题接受等效的合法操作，协议题才要求指定步骤。

题面不能携带参考答案、评分指令或实现脚本。事实检查实际产物、状态、依赖和保护集，
需要的语义由独立 Judge 判定；模型回答、工具调用与真实副作用分别验证。

## 修改题目与场景

1. 在 [cases.yaml](../../src/chatcopilot/evals/suites/agentstrata-agent-tasks-v1/cases.yaml) 中寻找最接近的场景，保留当前 schema、plugin 和 driver 契约。
2. 修改 `id`、`version`、`scenario_id`、`scenario_params`、`turns`、资源要求与 `policy`。场景实现静态注册，普通参数变化不新增按 Case ID 分派的代码。
3. 用 `judge.assertions` 绑定可信事实验证；`judge.quality` 明确必要语义和输入范围，不能关闭必须的语义条件来提高通过率。
4. 同时维护正常完成、违规行为和缺证据的样本。多主体场景保留完整回合和身份绑定，不能只看最后一条回答。
5. 数据变化后更新 manifest 对应资源的 SHA-256 与题单归属，核对包资源清单、许可和目录投影。

逐题约束见 [题目与判分依据](../reference/evaluation-cases.md)，执行与评分边界见
[Evaluation](../reference/evaluation.md)。可信状态和产物的判定入口位于
[agent_tasks](../../src/chatcopilot/evals/agent_tasks)。

## 验证与应用

先运行受影响场景与判分的本地回归：

```bash
.venv/bin/python -m pytest tests/unit/test_agent_task_scenarios.py tests/unit/test_agent_task_fairness.py tests/unit/test_agent_task_audit.py -q
```

检查 CLI/Console 是否保留精确题单、缺项阻断和旧成绩只读，并按 [开发指南](development.md)
选择仓库检查。运行真实模型前先核对题目、资源、Judge 与费用条件，按
[测评指南](evaluation.md) 显式启动。历史结果不补判，单次计数与失败记录留在测评产物或 CI。
