---
id: evaluation-dataset-organization
type: feature
status: implemented
created: 2026-09-10
---

# 测评集、业务 Case 与评分器组织

## Summary

按已批准方案保留 DeepEval 和三页工作台，以测评集 → Case 浏览。框架、数据来源、用途、执行适配器、评分器来源分别呈现。现有 63 个工程回归 Case 和历史 ID 保留；普通业务题通过 YAML 与已支持工具资源执行，以严格 GEval 作为主判。

## Design

遵循 [四层运行时基线](../runtime-four-layer-definition/spec.md)。Console 是控制观测入口，Evaluation application/service/Core 保持独立生命周期、预检、取消、监督和 artifact 所有权。执行适配器只返回 Case 结果，不接管生命周期，也不增加消息层。本规格修订 [基准工作台](../evaluation-benchmark-workbench/spec.md) 的目录和评分组织，保留既有 API 根、Suite/Case 与运行记录。

- Suite manifest 统一声明来源、用途、覆盖范围、执行对象及原生评分器来源和版本；前端从目录生成测评集，不维护 Suite 白名单。现有工程回归仍保留确定性断言与原有质量门禁。
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
- 不修改主工作区、运行目录、已安装依赖、服务或 Git 索引，不提交或发布。

## Verification

使用真实 DeepEval 4.2.2 SDK 和受控模型、固定数据做单轮/多轮、证据与异常验证；补充目录、服务契约、趋势与前端测试，桌面和窄屏检查。执行相关仓库检查。离线验证不作为真实 QQ、付费模型质量或完整官方基准成绩。
