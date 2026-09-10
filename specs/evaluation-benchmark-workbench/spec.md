---
id: evaluation-benchmark-workbench
type: feature
status: implemented
created: 2026-09-10
---

# 基准工作台与独立评分结果

## Summary

采用方案 A：测评中心保留开始测试、运行记录、进步趋势。开始页按基准目录、题目预览与选择、评分及运行配置组织；重点提供 SWE-bench、BFCL、GAIA、AgentBench，并保留项目回归与 QQ 合成链路。框架、实际被测对象、基准来源、精确题单、评分方法和结果分别可见。

## Design

遵循 [四层运行时基线](../runtime-four-layer-definition/spec.md)。Evaluation 与 Console 位于消息运行时四层之外；Console 只消费既有服务投影和控制 API，Evaluation application/Core 继续拥有生命周期、Trial 监督、隔离、取消和权威 artifact。基准环境不是新的消息层或第二个 Evaluation manager。

本规格修订 [两轨入口](../evaluation-two-track-center/spec.md) 中主界面隐藏公开基准的限制、[DeepEval 评分](../evaluation-deepeval/spec.md) 的仅自建题库范围及 [趋势](../evaluation-progress-history/spec.md) 的默认自由混合规则。保留能力与 QQ 两方向，能力明确区分 Agent runtime 和 direct-LLM；历史数据只读，不回填当前版本、模型或评分结果。

### 基准与选题

- 静态受信目录提供框架、版本、执行对象、数据来源、支持范围、原生评分及适用语义指标。只从当前安装目录和所选数据快照获取题数，不把 smoke、未下载、尚未支持混为完整官方数据。
- 一次 Evaluation 运行一个 Suite，支持预设与精确 Case ID、重复次数和时间预算。题目查看不启动模型、下载或环境；准备操作沿已有入口独立执行。SWE-bench 与 AgentBench 环境未就绪时给出结构化阻断，不产生伪成功。
- GAIA 采用官方数字、列表和字符串归一化匹配，答案始终与 Agent 输入隔离。BFCL 保留明确类别覆盖与模型协议对象；未实现官方完整类别时不宣称完整榜单成绩。
- SWE-bench 验证真实预测补丁与容器测试；AgentBench 明确 FC 版本、环境和真实执行 loop。镜像、资源与答案访问服从评测隔离，取消与期限纳入现有生命周期。无法验证的环境保持不可用，不静默替换为问答或模型模拟。

### 评分与快照

- DeepEval 为能力测评统一指标执行框架。原生评分与 GEval 质量分别保存；公开基准的原生失败不能被质量高分覆盖。自建回归仍保留事实与必需质量共同通过的语义。
- 评分方案支持原生与原生加 GEval；自定义语义量表使用版本化受信配置。目录只列出当前 Suite 真正支持的方案。LLM-as-a-Judge 是大类，G-Eval 是方法，DeepEval GEval/ConversationalGEval 是实现。
- 判分前冻结框架版本、评分方案、量表步骤/阈值、judge 脱敏配置和基准/题单摘要，参与恢复指纹。Judge 单独计量，错误保留已执行的输出和原生结果。未评分或不适用不填零。
- 实际输入、输出、公开工具轨迹和评分证据沿现有有界 Trial 契约传递，答案/凭据/隐藏推理不进入被测输入或公开导出。

### 界面与趋势

- 开始页左列列出基准，中间浏览和选择题目，右列显示框架、对象、原生规则、语义评分、预算、题数与阻断原因。沿用项目 UI 组件，窄屏纵排。
- 运行记录展示创建时快照、原生分与质量及覆盖数；逐题可查看题目、输出、工具、评分方法和理由。执行错误与判分错误分别说明。
- 默认可比趋势按基准与精确题集、执行模式及所展示指标协议分组，目标模型/Agent 版本可以作为比较因素。GEval 比较包含 judge 与量表；原生趋势不因仅 judge 改变而断裂。探索视图保留自由拆线，明确标识条件差异。缺少可比元数据的旧记录只能探索，不补造快照。SWE-bench 额外按实际镜像身份区分条件，AgentBench 按实际 Controller 指纹区分；缺少环境回执时不能生成可比键。
- 不计算跨基准混合总分；部分、取消和缺失保留原始状态，不暗中删除失败改变分母。

## Acceptance

- 从开始页能看到实际框架、基准支持范围、准确题单和评分方法；选题与提交请求一致，迟到响应不能混入另一基准。
- 原生评分与语义结果独立，Judge 错误不能改写原生通过/失败；未就绪的环境在执行副作用前阻断。
- 每条运行记录保留可追溯快照；恢复拒绝题单、评分或执行定义漂移；历史读取不改原始 artifact。
- 可比趋势不合并不同题集、基准或评分定义，探索功能仍可使用；QQ 不冒充 Agent 或真实 QQ E2E。
- 相关 Python、前端、仓库门禁与桌面/窄屏交互验证通过，明确区分本地夹具、真实模型与完整官方基准执行。

## Verification

2026-09-10，本地 Python 3.13.15 隔离环境验证：

- 通过私有临时候选索引运行 `.venv/bin/python scripts/check_repo.py full`：12 项全部通过；Python 3240 passed、1 skipped、9 warnings、132 subtests passed。真实索引前后 SHA-256 相同。
- 随后补充 SDK dotenv 隔离与外部环境身份回归，执行 `test_evaluation_environments.py`、`test_evaluation_workbench.py`、`test_deepeval_engine.py`、`test_evaluation_insights.py`、`test_evaluation_read_models.py`：96 passed。两项新增测试不回填先前全量计数。
- `npm --prefix console/web test`：14 files、147 passed；生产构建通过。8 个新增核心模块的 mypy 与受影响代码 Ruff 通过。
- wheel/sdist 精确成员与隔离运行时检查通过；公开边界、变更秘密扫描、SDD、架构、依赖漂移与差异检查通过。临时索引不改变维护者暂存状态。
- Chromium 使用实际生产构建与明确的模拟 API，覆盖基准切换、题目详情、选题与评分请求、运行配置快照、可比趋势、390px 无横向溢出、不可用基准禁止启动，无 pageerror。

上述包括真实 DeepEval SDK 和 SWE-bench 5.0.2 原生评分器消费受控日志；AgentBench 交互、Docker 传输和模型使用受控替身。未运行真实商业模型、官方完整题库、真实 Docker 修复或外部 AgentBench 环境，不构成官方基准成绩或 QQ E2E。

当前 BFCL 仍明确提供 v3 数据格式的部分函数调用协议校准，不宣称 BFCL v4 全类别或官方 AST 完整等价。SWE-bench 只支持含当前执行字段的文本数据和预制镜像，采用受限容器与上游评分，区别于原始榜单执行配置；AgentBench FC 的实际环境由用户独立准备，其部署版本尚无上游可验证回执。所有缺项与准备条件在工作台显示。

验证日志与截图位于本次工作区 `.cache/evaluation-workbench-verification/`。
