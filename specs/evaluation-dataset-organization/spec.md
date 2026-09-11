---
id: evaluation-dataset-organization
type: feature
status: implemented
created: 2026-09-10
---

# 测评集、业务 Case 与评分器组织

## Summary

按已批准方案保留 DeepEval 和开始测试、运行记录、进步趋势三页工作台。开始测试按模型能力、Agent 能力、系统链路三个被测对象入口组织，默认 Agent 能力；入口内按项目测评、公开基准分组，再浏览测评集 → Case。来源、能力标签、用途、执行范围与就绪状态独立呈现。现有 63 个工程回归 Case 和历史 ID 保留；普通业务题通过 YAML 与已支持工具资源执行，以严格 GEval 作为主判。

## Design

遵循 [四层运行时基线](../runtime-four-layer-definition/spec.md)。Console 是控制观测入口，Evaluation application/service/Core 保持独立生命周期、预检、取消、监督和 artifact 所有权。执行适配器只返回 Case 结果，不接管生命周期，也不增加消息层。本规格修订 [基准工作台](../evaluation-benchmark-workbench/spec.md) 的目录和评分组织，保留既有 API 根、Suite/Case 与运行记录。

- Suite manifest 统一声明来源、用途、覆盖范围、执行对象及原生评分器来源和版本；前端从目录生成测评集，不维护 Suite 白名单。现有工程回归仍保留确定性断言与原有质量门禁。
- 对象类型使用 `subject_type: model | agent | system`，属于目录与新运行快照元数据，不替代执行 driver、旧 track 或运行时四层。BFCL 属于模型，业务题、Agent 行为回归、GAIA、IFEval、AgentBench FC、SWE-bench 与 WebArena 属于 Agent；QQ 合成回归和 Canary 属于系统。WebArena、Canary 仍待接入，QQ 必须保留 Legacy Relay / attestation / ACP 覆盖说明，不宣称当前 Gateway 或真实 QQ E2E。
- Benchmark 表达标准依据，Dataset 表达版本化题目与资源，Suite 绑定可执行题目、环境与评分方案，Case 表达单题。Target 是不可变被测配置；Evaluation 冻结题单、环境、评分与参数；Trial 是 Case × Target × 重复序号。沿用一运行一 Suite，不新增数据集服务或执行能力。
- Suite 与 Case 可声明多个 `capability_tags`；来源、能力、用途和状态不能彼此推导。用途仅作说明，不增加必填表单。待接入默认折叠，待准备可查看；空来源分组隐藏。选中 Bot、对象或 Suite 改变时重新初始化选题、筛选、评分及预算，迟到响应不影响新表单。
- 新运行保存对象类型及能力标签。历史读取不查当前目录补造字段，不迁移历史记录。结果分别展示执行事实、评分结果与统计；运行完成不等于通过，缺失评分不补零，可比趋势包含对象类型，不产生跨套件混合总分。
- 新增受信 business-agent 插件，文件声明 case_id/input/context/expected_behavior、工具依赖及资源引用。固定只读测试工具通过既有 ToolRegistry 和隔离 Agent 装配执行；不按 Case ID 分派。Case 文件与 fixture 继续校验哈希。新增业务工具或环境仍需正式适配。
- Agent 只收到提问、背景及显式任务附件；期望、参考结果、fixture 全量表和 Judge 步骤留在评分侧。背景不能设置宿主 role、会话身份或权限。工具 fixture 是测试环境数据，通过真实调用才返回。
- SDK 边界把单轮转为 Golden/LLMTestCase，多轮转为 ConversationalGolden/ConversationalTestCase，分别创建 EvaluationDataset。业务主判使用 DeepEval GEval/ConversationalGEval，固定版本评价步骤、strict_mode，最终回答与实际工具返回共同送评。
- 保存评分方案、DeepEval 版本、Judge 模型与参数、步骤、阈值、输入范围及实际评分材料。缺失或截断必需证据、执行异常、评分异常与正常不通过分开。无工具调用是已采集空记录；工具返回失败是实际记录，可以满足负向题期望。
- 公开基准新请求只支持原生或原生加独立 GEval 附评，默认原生；附评不覆盖原生结果。历史 GEval-only 记录保持可读，不能当成正式原生成绩。BFCL 部分协议仍标项目适配实现。
- 开始测试支持工具筛选；详情按 Agent 输入、评分资料、工具环境三部分呈现。运行记录区分执行、评分、证据；趋势分别展示原生指标和 LLM 判定，并用数据、评分条件与 Judge 指纹分组。
- QQ 群成员例题使用符号 GROUP_A 和合成数据，仅作未配置工具的教学 Case；不接入或声称已有真实 QQ 成员查询。查对群、错群、漏成员、人数错误和预设失败通过受控证据验证评分流程。

## Acceptance

- 已有工具和资源下新增普通业务 Case 只改数据文件及完整性摘要，无需新增 Case ID 分支。
- 63 个回归 ID 与确定性断言保持；新业务题明确显示 LLM 判定及理由。
- Agent 输入不包含评分资料，工具调用与返回进入 Judge；无调用、失败返回、缺失采集、Judge 异常可以区分。
- 公共基准原生成绩独立、适配范围明确；新测评集无需修改前端列表。
- 保存可复核的评分快照，变化条件不并入同一可比趋势；旧记录不补造新字段。
- 三类入口及来源分组完全消费目录；BFCL 不列入 Agent，IFEval 不列入模型，待接入不因分类改变而可运行。多标签筛选、跨 Bot/对象/Suite 切换隔离、旧记录未记录显示与不同对象趋势分组均通过验证。
- 不修改运行目录、已安装依赖、服务或 Git 索引，不提交或发布。

## Verification

使用真实 DeepEval 4.2.2 SDK 和受控模型、固定数据做单轮/多轮、证据与异常验证；补充目录、服务契约、趋势与前端测试，桌面和窄屏检查。执行相关仓库检查。离线验证不作为真实 QQ、付费模型质量或完整官方基准成绩。


2026-09-12，对象分层实现验证：

- 相关 Evaluation、目录、业务题、快照和服务请求测试：272 passed，2 个既有 fork 警告。
- `npm --prefix console/web test`：18 个文件、171 passed；`npm --prefix console/web run build` 通过。
- 当前虚拟环境未安装 Ruff，使用现有隔离工具目录运行 `PYTHONPATH=src:.cache/evaluation-workbench-tools .venv/bin/python scripts/check_repo.py fast`：1113 passed、34 subtests passed，2 个 fork 警告，其余门禁通过。
- Chromium 运行实际生产构建与模拟 API，验证三类目录、折叠与不可用状态、多标签、Bot/Suite/对象切换、同名 Case 隔离、迟到响应、冻结历史分类和 390px 无横向溢出。请求在模拟 API 截获，不执行 Agent、模型或 QQ。
- 63 个 Agent 回归、3 个业务 Case、7 个 QQ 回归与修改前结构比较：只增加能力标签，输入、ID、断言与评分定义未变。
- 额外对 7 个受影响 Python 模块执行 mypy，业务数据模块既有 `Traversable.joinpath` 类型报错仍存在；修改前文件的 shadow 检查复现同一报错。本次没有修改该行，不把该额外检查记为通过。

没有运行付费模型、官方完整基准或真实 QQ，没有部署或重启服务。浏览器脚本、模拟数据与截图保存在本地 `.cache/evaluation-subjects/`。
