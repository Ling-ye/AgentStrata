---
id: evaluation-deepeval
type: feature
status: implemented
created: 2026-09-09
---

# DeepEval Agent 评测与可组合趋势

## Summary

Agent 能力轨道使用 DeepEval 4.2.2 执行确定性指标与默认质量判分。Console 直接展示实际 Case 输入输出，趋势按用户选择的 Agent、模型和测试规模拆线。启动参数、QQ 轨道和其他 Benchmark 执行入口保持。

## Design

- 遵循 [四层架构基线](../runtime-four-layer-definition/spec.md)：Evaluation 是四层之外的独立配套部分，适配器只依赖已有 Agent、Core 和 Evaluation 契约。Console 继续通过 Evaluation service 查询和控制，不创建模型或 worker。
- 现有 Trial 隔离进程运行 Agent 后，将实际请求、返回、工具和证据转换为 DeepEval 用例，经公开 `evaluate()` 接口运行指标。保留现有事实验证函数及 all/any 语义，不保留第二个 Agent 判分引擎或降级分支。
- 版本化 Case 定义明确质量评分是否适用，单轮 GEval、多轮 ConversationalGEval 使用固定步骤和参考标准，默认阈值 0.7。必需事实条件和必需质量指标都满足才通过；权限和写入事实不能由自然语言高分覆盖。不同 actor 不合成为同一个会话。
- 固定评分模型通过 `CHATCOPILOT_EVALUATION_JUDGE_*` 配置，客户端和 PromptPlan 使用现有实现。创建前预检依赖与配置，冻结脱敏评分配置和指标版本；秘密不写 artifact。评分异常区别于 Agent 执行失败，评分和 Agent 用量分别计量，预算由原 Trial/ Evaluation 监督。
- 真实输入在 Agent 调用处采集；判分前沿现有有界 Trial IPC 发布仅供阅读的执行证据。它不成为可 resume 的完整 Target 组 checkpoint，也不计入已完成样本。Core 唯一保存权威记录，评分取消或失败仍保留已经生成的回答。
- DeepEval 在子进程导入前禁用 dotenv、遥测、云上报及结果缓存，临时文件只在隔离临时目录。仓库测试显式禁用其自动 pytest 插件；不启用生产 tracing、Confident Cloud、数据生成或第二个调度器。
- 现有 artifact 扩展输入、多轮交互、采集状态、指标和评分版本。维持 Trial 2 MiB、单正文 128 Ki 字符等边界，运行中交互观测总量有界为 512 KiB，预览与采集截断分别标识；旧记录不补造实际请求、质量分或 Git。复用 Case 详情读取并按 Trial、Target、attempt 精确选择。
- 趋势复用现有只读查询并增加分页、时间及跨实例范围。每点为 Evaluation/Target；按选中维度分线，Git 和完整比较指纹只作元数据，不强制拆线。默认 30 天、当前机器人、Agent 与模型拆线。通过率分母包含失败/异常/跳过；质量分显示有效/应评分样本数；只有完整有效非 dry-run 的记录进入曲线。严格 compare/resume 保持原约束。
- Case 列表常显输入/输出预览，原处展开多轮数据和指标；桌面并列、窄屏纵排。趋势点击定位记录，返回保持条件；异步查询以实例/评测/Trial 标识隔离。

## Acceptance

- 三个 Backend 的直接 Agent 轨道真正经过 DeepEval，确定性失败不被质量分覆盖，缺少评分配置在创建前失败。
- 判分错误、超时和取消保留 Agent 输出，不改写执行结果或已完成组恢复语义；不发生隐式 dotenv/云上报/缓存命中。
- 不同 Git 默认可连线，Agent/模型/规模可多选并自由拆线；质量缺失不填零，异常和部分记录不冒充完整趋势。
- 单轮、多轮、动态输入、资源、重复 Trial、长正文及历史缺失就地可读，详情查询不会串任务。
- 桌面与 390px 的筛选、键盘、多个 Case 展开及返回状态通过实际浏览器验证。

## Verification

2026-09-09，隔离 Python 3.13.15 环境与本次 Console 生产构建完成验证：

- `scripts/check_repo.py full`：12 项全部通过，包括 SDD、公开信息边界、架构、依赖一致性、UTF-8、Ruff、类型契约、组件目录、安装依赖、wheel/sdist 验证、全量 Python 与 Console 构建。Python 3137 passed、1 skipped、7 warnings、132 subtests passed。
- `npm --prefix console/web test`：13 个文件、141 项通过；`npm --prefix console/web run build` 通过。
- DeepEval、运行采集、三个 Backend 配置下的数据接入、只读模型、趋势和脱敏专项共 109 项通过；SDK/采集 14 项专项在修复长字符串正则后由 166.04 秒降到 9.76 秒。
- 评分/采集/指标投影 3 个文件的 mypy 通过，安装与发布规则专项 26 项通过。
- 原生 Chrome 使用真实本地 BFF/UDS 查询及确定性 artifact，覆盖桌面与 390px、跨实例分页、合并/拆线、输入输出、多轮同时展开、单次正文读取、键盘关闭、读取失败重试和迟到响应隔离，无 pageerror。

构建遇到历史缓存中的 L01 转发文件，保留缓存后干净重建；full 误收集历史评测 workspace 的同名测试，固定仓库 testpaths 后保留全部真实测试与历史数据。一次原有进程退出耗时断言失败，单独及最终 full 均通过，未放宽断言。实际命令、阶段失败和最终日志保存在本次本地验证记录中，不借用此前结果。

这些验证使用受控模型适配器并真正调用 DeepEval SDK，不代表商业模型质量或 QQ 端到端证明。没有部署、重启真实机器人、修改真实配置、清理真实 workspace、暂存或提交。
