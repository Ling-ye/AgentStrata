# 文件维护的业务测评题

工具组织考试，测评集规定题目和条件，评分器负责判卷。Console 按「测评集 → Case」浏览，业务工具是筛选条件；DeepEval 4.2.2 是执行指标的框架，不是题库分类。

## 选择合适的测评集

| 测评集 | 用途 | 主评分 | 环境 |
| --- | --- | --- | --- |
| `project-business-v1` | 业务任务完成 | 严格 GEval，显示「LLM 判定」 | 固定只读测试工具数据 |
| `agentstrata-capabilities-v1` | Agent 行为回归（63 个 Case） | 原有程序断言及适用的质量门禁 | 原有隔离执行环境 |
| GAIA、BFCL、IFEval、SWE-bench、AgentBench | 公开基准 | 原生规则，独立 GEval 附评默认关闭 | 每个适配器声明的数据与环境 |

公开基准的评分器实现来源另行标注。BFCL 当前是项目适配的部分协议匹配；GAIA 为项目实现的官方归一化规则；SWE-bench 使用固定版本的上游 grader；AgentBench 使用已配置环境的结果。查看题目不会准备数据或执行模型。完整范围见 [运维文档](operations.md#基准工作台与手动测评)。

## 新增普通业务题

编辑 `src/chatcopilot/evals/suites/project-business-v1/cases.yaml`，增加一行 Case 定义，例如：

```yaml
- case_id: query-another-wording
  input: 帮我查一下 comparison-token 的值。
  context: 使用固定测试环境提供的查询工具。
  expected_behavior: 实际查询 comparison-token，并准确转述返回值，不猜测结果。
  category: 信息查询
  tools: [lookup_eval_fact]
  resources: [facts]
```

这条题使用已有工具和 `facts` 资源，无需增加 Python Case ID 分支。`case_id` 保持稳定；提问变化或预期变化后，题单与定义摘要会随之变化。维护文件后，用 `sha256sum` 计算改动文件的 SHA-256，并更新同目录 `manifest.yaml` 的对应 `files[].sha256`；发布题集变化时同步更新 `data_version`。这属于数据完整性维护。

字段约定如下：

| 字段 | 发送给 Agent | 含义 |
| --- | --- | --- |
| `input` | 是 | 第一轮用户提问 |
| `context` | 是 | 任务背景，不能设置真实群身份、角色或权限 |
| `turns` | 是 | 可选的完整多轮提问列表，首项必须等于 `input`；逐轮交给同一隔离会话 |
| `expected_behavior` | 否 | 任务完成标准 |
| `capability_tags` | 否 | 可选的多个能力标签，用于选题筛选，不进入 Agent 输入 |
| `reference` | 否 | 可选的额外评分参考 |
| `tools` | 工具定义 | 已注册的受控工具依赖；未知工具显示未配置 |
| `resources` | 不直接发送 | 工具数据资源引用，需通过实际工具调用取得对应返回值 |

业务适配器目前支持已有的 `lookup_eval_fact(key)` 受控查询。`fixtures/facts.json` 中每个 key 声明 `ok`、字符串 `value` 和可选 `error`。只返回所查询 key 的结果，不把完整表交给 Agent。不支持从 YAML 加载 Python、shell、动态模块、真实账号或任意网络端点。添加新的工具类型仍需要一次受信适配开发和相应验证。

新增测评集沿用相同插件和 Case 文件格式：在 `suites/<suite-id>/manifest.yaml` 声明 `plugin_id: business-agent`、`driver_id: agent_configured`、`source_type: project`、`purpose: business_task` 及数据、评分和资源字段即可。服务目录自动发现，前端无需修改固定列表；公开安装包的资源清单仍需纳入相应文件。

## 群成员查询例题

`qq-group-members-example` 表达了「当前群 GROUP_A，查询人数和成员号」的任务。`group-example.json` 仅含合成符号，参考名单不发送给 Agent。因为当前未接入 `qq_group_members` 业务工具，目录和 Case 详情显示未配置，不能选择执行。

教学验证覆盖查对群、查错群、漏成员、人数错误、正确说明预设查询失败。测试使用构造的工具记录和受控 Judge 返回，验证资料传递、SDK 二值判分与错误分类，不证明真实模型的判卷质量，也不证明 QQ 成员查询已可用。将来接入真实工具后，固定数据日常回归与真实只读查询验收分别执行。

## 怎样阅读结果

业务题的主评分使用固定的 `business-task-geval/v1` 步骤和 `strict_mode`。单轮映射 `Golden` / `LLMTestCase`，多轮映射 `ConversationalGolden` / `ConversationalTestCase`，数据集不会混用。GEval / ConversationalGEval 读取最终回答、可见回合、实际工具参数与返回、期望及参考资料，输出通过或不通过及理由。

| 结果 | 意义 |
| --- | --- |
| LLM 判定通过 / 不通过 | Judge 完成评分；这是模型评价，不是平台事实证明 |
| 已采集，没有工具调用 | 工具记录是空列表；Judge 按本题是否要求查询判定 |
| 工具返回失败 | 确实调用并返回错误；如果本题期望正确处理故障，可以通过 |
| 证据采集异常 | 回合或必需工具证据缺失、截断，不记作模型答错 |
| 运行异常 | Agent 或环境未正常完成 |
| 评分异常 | Judge 配置、调用或 SDK 结果异常，不自动换模型或补零 |

运行详情保存实际评分材料、评分器来源与版本、模型、端点指纹、超时、推理强度、固定步骤、阈值和输入范围。温度沿用 Provider 默认值，并明确记录为 `provider_default`；SDK 的原始分值与最终二值结果由所固定的实现处理，不自行重写结果。Judge 理由用于解释评价，工具执行事实以采集记录为准。

业务 LLM 通过率只统计成功判分的样本，并展示评分覆盖；缺失评分的点不连成完整可比曲线。公开基准原生成绩与独立质量分分别绘制，Judge 或量表变化不影响原生成绩。数据、环境或评分条件变化会拆分可比曲线。历史自定义 GEval 记录仍可阅读，明确标为非原生成绩，新公共基准运行不能用它替代原生评分。

## 验证与应用

Case 数据解析、评分和服务契约可在开发工作区用以下命令验证：

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_business_evaluation.py -q
```

该测试使用真实 DeepEval SDK 和受控模型，不调用付费 Judge 或 QQ。真实模型和外部平台验收另行进行。开发工作区测试不会把文件写回运行目录、应用依赖或重启服务；环境配置仍遵循 [Evaluation 运维说明](operations.md)。
