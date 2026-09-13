---
id: progressive-disclosure
type: architecture
status: implemented
created: 2026-09-13
---

# 文档、工具结果与 Harness 证据渐进式披露

## Summary

根协作规则混合全局边界和领域细节；Skill 与 MCP 正文同时进入 summary 和 data，
历史压缩主要保留 summary。Harness 已将大证据外置，但首屏缺少阶段与失败导航。
本次保留现有文档事实源，按任务导航规则；工具正文去重并支持压缩后的快照回读；
Harness 使用阶段摘要和完整只读证据。长期记忆注入、人格、工具选择与模型策略不变。

## Design

遵循 [四层运行时基线](../runtime-four-layer-definition/spec.md)。Application 继续
绑定 Principal、工作区、permission_filter 和 payload_filter；Agent 在已有 Registry、
Executor 与 Backend 投影路径中管理会话结果引用。Channel、Gateway 的准入、交付与
交换提交不变。Harness 拥有独立生命周期，Evaluation 继续拥有执行与评分，均不增加消息层。

### 文档与规则

README 提前给出启动与任务入口；docs/README 保留唯一文档导航。根 AGENTS 保留全局
协作、授权、证据、秘密、架构和验证要求，领域契约移动到可按主题定位的文档，根入口
明确触发条件。移动保留原规则语义与事实源链接，不把历史验证变成当前验证。

### 工具结果

Skill 和 MCP 的 summary 只描述来源与读取状态，完整正文仍在原 data 字段，工具输入和
执行结果的字段不变。宿主 ToolDef metadata 显式声明可展开正文，首批仅 Skill body 和
MCP content；错误、mutation receipt 及其他工具不做通用截断。

Agent 会话装配通过现有 catalog 注册 read_tool_result；会话内缓存只保存经过宿主
payload_filter 的正文快照，引用为不透明 ID。读取复检原工具权限，不接受 actor、路径或
外部 URL，不调用原工具。文本和结构化 JSON 均可分页读取，返回字符范围、总长度、哈希与
下一页位置；可从给定位置搜索文字。正文已在当前上下文且版本相同时避免重复读，压缩后可重读。

小正文直接返回并附回读引用，长 Skill/搜索 MCP 正文提供首段预览与引用，工具成功状态、来源元数据和错误不变。
非搜索 MCP 的正文和可能的写入回执完整内联，历史摘要也保留其正文，不做通用截断。
原始执行结果与模型投影继续分别经现有观测记录；模型历史摘要保留引用。引用只属于当前
live Agent session，跨 actor、会话重建和关闭不可读取。缓存采用 16 MiB 的会话内 LRU
保留空间；这是回读缓存容量，不拒绝工具执行。超大单结果保留完整模型正文且显式说明未缓存；
引用失效返回结构化错误，不伪造空结果或自动重放。分页最多返回 8,000 字符，可继续读取。
这些边界限定宿主内存和单页响应，不构成任务总预算或磁盘长期证据留存承诺。

Native/LangGraph 的工具消息和 Codex relay 使用同一结果投影；relay generation 更换仍使用
当前 actor 的 Executor，最终 session 关闭时清空缓存。没有配置回读 provider 的独立调用
保留完整结果，历史摘要也保留正文，不生成不可读取引用。被测 Agent 不获得 Harness 原预期或评分材料。

### Harness 证据

保留完整 source、reproduction、verification、patch、regression 及其它实际证据，按现有
只读目录与哈希交接。准备、修复、审核提示提供不同的阶段目标；大节按需读取，小节保留在
首屏。完整原预期、失败、未运行、缺失项和操作者 feedback 不得被成功项摘要覆盖。
摘要只按实际字段提取，不调用模型生成或补造根因、验收成功和隐藏证据。

### 实施与回滚

依次完成文档导航、工具投影与回读、Harness 证据导航及定向回归，再执行仓库 full 检查。
无数据迁移，无旧字段转换，无部署与 Git 提交；代码回滚不会改变现有持久化结果格式。
真实模型成对 Evaluation 需另行显式启动，静态字节下降不能代表 token、延迟或成功率改善。

## Acceptance

- 典型部署、开发、Agent、Gateway、Evaluation、Harness 与前端任务能从根入口找到领域规则；
  原规则有明确去向，文档链接可解析，保留唯一首次部署与运维事实源。
- Skill/MCP 正文在模型投影中不重复；短结果可直接用，长结果能分页定位中后部证据。
- 历史压缩保留引用；回读不再次调用远端工具，引用不能跨会话或绕过权限与输出过滤。
- 错误与关键执行回执完整；缓存淘汰、关闭、缺失引用均给出真实可诊断结果。
- Native/LangGraph 共用投影与 Codex relay 行为一致，已有结果观测保留实际执行证据。
- Harness 保留完整证据与只读范围，摘要可定位失败和缺失项，不向被测模型泄漏评分预期。

## Verification

2026-09-13 使用仓库 `.venv` 的 Python 3.13.15、`PYTHONPATH=src` 完成以下验证。

- Skill、MCP、上下文、session 装配、结果回读与 Harness 定向集：105 passed。
- Native/LangGraph、Codex relay、PromptPlan 与执行器定向集：73 passed、9 subtests passed。
- 最后补充独立子 Agent 正文保留和提示词基线复验：81 passed、10 subtests passed。
  以上集合存在重叠，不相加作为总覆盖数量；新增的结果回读和证据索引测试纳入 `tests/fast.txt`。
- `scripts/check_repo.py full` 的 SDD、公开边界、架构、依赖清单、UTF-8、Ruff、类型、
  组件目录、pip check、wheel/sdist 精确成员与隔离运行校验均通过。
  架构为 610 模块、2044 条静态边、0 环；组件目录为 26 packs、71 static tools，0 issues。
  新增文件尚未暂存，包校验用临时 Git index 与临时对象目录表达当前候选；真实 index 的
  字节与暂存条目在两次候选验证前后均未变化，不提交、不发布。
- 最后一次完整 Python 运行：3802 passed、21 failed、1 skipped、154 subtests passed。
  21 个失败均来自验证专用 TMPDIR 过长导致的 AF_UNIX socket 路径/服务启动错误；保持产品
  与测试实现不变，改用短 basetemp 后完成全部受影响文件复验：

  ```bash
  TMPDIR=/tmp PYTHONPATH=src .venv/bin/python -m pytest tests/integration/test_evaluation_service.py tests/unit/test_evaluation_service_protocol.py -q -rs --tb=short --basetemp=/tmp/pd-uds
  TMPDIR=/tmp PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_harness_agent_verification.py -q -rs --tb=short --basetemp=/tmp/pd-hv
  ```

  分别为 25 passed 和 19 passed，覆盖并解决上述 21 个失败。完整运行的原始失败报告保留，
  不将其改写为一次全绿。唯一跳过项为 `test_windows_fs_path_guard.py` 的 Windows 大小写
  不敏感路径用例；进程监督测试另有 10 条 Python fork 弃用警告。
- `npm run build`（`console/web`）：TypeScript 与 Rsbuild 生产构建通过。
- `bash scripts/check_secrets.sh changes`：通过。扫描使用真实索引的临时副本与临时对象目录，
  覆盖 index、worktree 和未忽略的新文件；真实索引前后字节一致。
- 202 个文档导航链接可解析；原 81 条领域规则逐条核对，有且只有一个迁移目的地。
  根 AGENTS 从 74,424 字节/397 行变为 7,149 字节/77 行；README 从 431 行变为 162 行。
  全局提示与已有静态工具 schema 预算基线及上限未改动。
- 1000 行 `record i: evidence-i` 合成文本探针：原双份正文 payload 为 51,672 UTF-8 字节，
  首次预览与引用为 2,521 字节；完整 24,779 字符仍可回读。这仅是合成序列化字节变化，
  不代表实际 token、延迟、费用或任务成功率；后续回读仍有上下文成本。

未运行真实商用模型对照、真实 QQ/OneBot 消息往返或生产部署。长期记忆、工具按需发现和
Wiki/RAG 策略不在本轮变更中。验证工具补齐了项目已声明的本地开发依赖，未修改依赖声明。
