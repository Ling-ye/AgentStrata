---
id: evaluation-llm-official-output
type: feature
status: implemented
created: 2026-09-12
---

# LLM 官方题库与模型响应展示

## Summary

BFCL 接入固定 V4 官方单轮 13 类、3641 题及对应原生评分；IFEval 接入固定官方 541 题及必要分句资源。LLM 结果同时呈现正文与模型提出的函数调用，修复“调用已保存但页面显示未记录”。历史成绩不重跑或改写。

## Design

遵循 [运行时四层基线](../runtime-four-layer-definition/spec.md)。Evaluation 保持独立生命周期、隔离、取消、评分和产物所有权，模型入口通过唯一 PromptPlan 使用独立中性配置；不启动 Agent 会话或执行 BFCL 所提出的工具调用。

- BFCL 固定 revision `f7cf7359b7ac615a0b294831c5ba2bc95ee4a000`。类别为 simple_python/simple_java/simple_javascript/multiple/parallel/parallel_multiple/irrelevance，以及六个 live 类。保留官方题目、函数定义、候选答案及来源；严格核对 ID、数量和答案完整性。复用必要的官方 AST/相关性核心和 schema 转换，避免引入整套模型/向量库依赖。记录 namespace 与模型协议绑定适配，评分算法不另写近似版。多轮、搜索、记忆及格式敏感性不纳入本轮，也不产生 V4 全榜总分。
- IFEval 数据与既有检查器同为 `26d8ccdab6fec61b5c83ad6327ea8bda9e580288`；准备 541 题、必要 NLTK 分句资源，完整预检后发布缓存回执。严格为主，宽松和约束指标单列；缺约束或评分异常不作为通过。
- 默认 full 展示官方全量，balanced-100 仅显式启用，smoke 保留为明确调试模式；正式数据缺失不自动降级。准备过程写入私有版本化缓存，校验成功后发布来源/哈希/题数回执；沿用现有准备入口与 local.env 配置。
- 原生模型记录 model_request（实际 messages/tools）与 model_response（content/tool_calls/finish_reason/usage），不包含凭据或隐藏推理。先记录响应再评分，评分异常保留已有响应；客户端成功/失败均关闭。BFCL schema 转换、函数名映射与完整问题消息保留快照，提示条件差异明确说明。
- 列表预览增加有界 model_output_preview，详情沿用现有 API。正文、调用、混合、真实空响应、未采集、截断分别表达；原始参数字符串保留，解析异常可见。BFCL 只显示调用意图，不捏造执行结果。历史从已保存 final_text/evidence.tool_calls 投影，不回写或用当前配置补造输入。
- 前端按目录 category 提供数据类别筛选；选题与运行参数保持现有边界，不自动运行数千题。

## Acceptance

- 实际服务与页面加载 BFCL 3641/13、IFEval 541，来源版本、校验回执与完整数据一致。
- 官方候选值、可选参数、语言类型、并行顺序、额外调用和相关性边界有验证；缺答案及检查器异常阻断。
- 用户历史测评 `eval-f1adc816cdb64cf38da48495ceaf0729` 可直接查看已有 get_weather/circle_area 调用和参数，原成绩与输出事实不变。
- 覆盖纯文本、纯调用、混合、空、缺失、非法参数 JSON、截断和评分异常；不将 Agent 执行轨迹当作模型最终响应。
- 维护更新走 deploy_console.sh --update-only，保护机器人、AgentBench 环境、Token、历史和索引；同时核验 AgentBench 空闲 444/DB 忙 144/释放 444 的在线预检。真实付费模型试跑另行授权。

## Verification

2026-09-12 完成：

- 固定官方数据实际下载及哈希回执通过；BFCL 3641 题、13 类和对应答案逐项核对，全部 schema 校验；IFEval 541 题、25 种约束及 English punkt/punkt_tab 资源构造通过。
- 官方核心函数 AST 与固定源码逐项一致，namespace/type annotation/protocol profile 是明确适配边界；Java/JavaScript 参数使用官方语言预处理。3641 道 BFCL 的空调用边界均可评分，无检查器异常。
- 测评相关回归 857 passed；`scripts/check_repo.py fast` 1113 passed、34 subtests；前端 178 passed，生产构建通过；wheel/sdist 精确成员及隔离安装验证通过。测试使用固定数据和受控模型，不代表真实模型质量。
- `deploy/wsl/deploy_console.sh --update-only` 实际执行成功，维护锁内安装 absl-py/nltk/immutabledict 等锁定依赖，再重启 Evaluation/Console。服务使用的 `.venv` 中另有 33 项模型与评分测试通过，541 道 IFEval 预检通过。
- 真实 Console API 与浏览器核查：BFCL 3641、IFEval 541；明确选择 100 题；切换套件清除选择；历史两题展示 get_weather/circle_area 的调用和参数。逐题记录与原 summary 与更新前完全一致，浏览器全部请求只读，无模型执行。
- AgentBench 在线目录空闲 444、DB worker 被受控占用时 144、释放后 444。维护结束 ready=true、maintenance=false、active_count=0。机器人、Token、历史与实际 Git 索引保持；不提交或推送。

本次不运行真实付费模型或完整榜单，不证明模型区分度、难度稳定性或外部 QQ E2E。BFCL 原生规则只覆盖单轮范围，独立 PromptPlan 的实际请求条件不宣称等同官方裸提示。
