---
id: architecture-boundary-hardening
type: architecture
status: implemented
created: 2026-08-21
---

## Summary

AgentStrata 的层级方向已经有局部禁止导入规则，但原门禁没有覆盖声明范围内的完整静态 import 图：当时纳入 `src/chatcopilot` 与 Console 的 401 个 Python 模块、1,149 条可静态解析内部边中仍有 5 个强连通分量，现有架构测试也只调用前缀规则而没有执行全部语义检查。PromptPlan 在 DTO 中区分了宿主策略、运行时事实和不可信数据，renderer 却把 Bot 身份与风格合并进系统策略或 Codex 的 `trusted_policy` 字段；显式启用的工具包在模块加载异常时还会静默消失。

本规格收敛五项同源问题：保留 PromptPlan 的端到端信任分区；用显式层级依赖图、受检 Python 模块的静态 import 图和兼容入口规则替换“局部黑名单即通过”；消除 ACP、Evaluation、QQ 和 workspace 的全部已知循环；按职责所有权拆出共享机制而不是机械切文件；完成生产代码与普通测试的 canonical import 迁移。原有 facade 和外部兼容导出继续存在，但内部实现不得依赖兼容入口。

最强失败路径是只改变文件位置而保留反向调用、把 Bot 文本换个 JSON 字段却仍赋予系统权限、或让更严格的工具发现破坏可选能力检查。本次设计因此以可执行图约束、封闭的 Prompt layer-kind/trust 映射、显式工具物化失败和兼容入口白名单作为验收边界。

## Design

Prompt trust vocabulary 增加 `bot_instruction`。`runtime_policy` 与宿主生成的 `capability_policy` 只能是 `trusted_policy`，`session_fact` 只能是 `trusted_runtime_fact`，`bot_identity`、`response_style` 与 Skills 索引只能是 `bot_instruction`，persona、memory、journal、网页和用户输入只能是 `untrusted_data`。Native renderer 仅把宿主策略和宿主事实放入 system envelope；Bot instruction 和 untrusted context 使用彼此独立的 user-context envelope。Codex renderer 使用 schema v2 的独立 `host_policy`、`runtime_facts`、`bot_instructions`、`untrusted_context` 和 JSON 编码用户字段。render receipt 记录每个分区的稳定摘要，任何 layer-kind/trust 错配都在调用 provider 前失败。

Native 的多个 renderer message 共同构成不可裁剪的 prompt prefix。Context manager 在 topic 切换、滑动窗口和 token 裁剪中整体保留该前缀，topic classifier 只读取前缀之后的真实对话，避免把 user-role 的 Bot instruction 误当作用户历史或在 unrelated view 中丢失。

`scripts/check_architecture.py` 解析绝对与相对导入，覆盖 `src/chatcopilot` 和 Console Python 模块，并建立两张静态图。第一张是显式 area DAG：contracts/project、core、catalog、domain、assembly/evaluation、entrypoint 只能依赖声明的下层 area；未声明的跨 area 边失败。第二张是模块图：任何包含两个及以上模块的强连通分量失败。兼容入口单独声明，只允许对应 facade、自身实现域和 `tests/unit/test_compatibility_exports.py` 使用；普通生产代码与测试必须使用 `contracts.tools`、`core.workspace_runtime`、`core.config`、`core.llm_client`、`core.concurrency`、`core.mcp_catalog`、`component_catalog` 和 `agent.search` 等 canonical surface。单元测试必须执行与 CLI 相同的检查入口和规则集合，避免静态门禁与测试门禁分叉。动态加载、非 Python 依赖和运行时调用关系不在该静态图内。

循环按所有权拆除。Workspace identity 只依赖 `WorkspaceView` contract，不反向导入 concrete model。QQ token/loopback 校验归 `platforms.qq.boundary`，WebSocket relay 与 allowlist 归 `platforms.qq.access_proxy`，`at_proxy` 只保留 CLI facade，ingress probe 不再导入 CLI 私有符号。Evaluation 的 env/event/usage helper 归 execution support，Bot runtime 和 permission projection 归 evaluation runtime；runner、capability executor 与 isolated executor 只沿单向依赖调用。ACP mode mutation 接收 prompt refresh callback，job dispatcher 接收最小 host port，不再导入 server 或持有整个 server 对象；server 只负责协议/生命周期编排。

工具发现把 catalog binding 视为显式部署契约。模块 import 失败、缺少 `TOOLS`、导出错误类型、或声明工具名没有被物化时抛出带 module/pack/tool 证据的 `ToolMaterializationError`；只有专门的审计调用方可以选择结构化收集错误，运行时不得把异常降级为空工具列表。外部 ToolDef facade 保留，但 Agent、middleware、Evaluation、入口实现和普通测试统一从 `contracts.tools` 导入。

唯一 PromptPlan 已删除的 builtin prompt 资源不再进入 package-data 或 Release allowlist；安装后 runtime probe 直接构造并渲染 canonical PromptPlan。该同步只移除已经不存在的打包输入，不恢复旧 prompt 体系。

兼容范围仅包括既有导出路径和 CLI 入口，不改变 BotSpec YAML、平台协议、工具名、AgentTask/AgentEvent/AgentResult、Evaluation artifact 或 QQ 权限语义。若受检静态图暴露的新依赖只能靠允许上层反向导入才能通过，改造应停止并调整所有权，而不是增加例外。

## Acceptance

- Prompt layer kind 与 trust 是封闭的一一职责映射；Bot identity、style、Skills、persona、memory 和 journal 都不出现在 Native system policy 或 Codex `host_policy`。
- Native、LangGraph、Codex 和 Evaluation 继续消费唯一 PromptPlan；Codex envelope schema v2 和 receipt 可复核四个信任分区。
- 架构 CLI 与单元测试都检查声明的 area policy DAG、受检模块静态 import 图、相对导入、模块 SCC、原有语义规则和兼容入口；任一检查失败均返回非零或测试失败。
- 受检 Python 模块静态 import 图的强连通分量从基线 5 个降为 0；ACP、Evaluation、QQ、workspace 的反向私有导入不再存在。
- Agent、middleware、Evaluation、入口实现和普通测试不再导入 `external_tools.shared.tool_spec` 或 `middleware.runtime.workspace`；旧路径只由兼容 facade、外部工具实现域及专门兼容测试使用。
- 显式工具包无法完整物化时失败关闭，错误包含绑定模块和缺失工具；正常 catalog 仍投影 19 个 pack、69 个 static tool、4 个 MCP entry、4 个 subagent、0 个 workflow。
- 原有稳定 facade、BotSpec、Agent backend、QQ ingress、安全状态、Evaluation artifact 和工具公开名称保持兼容。
- 受影响的 Prompt、工具注册、ACP job/mode、Evaluation、QQ ingress、workspace、BotSpec 和架构测试通过；全量 fast gate、public-repository gate、compileall、diff/status 检查报告实际结果和未验证边界。

## Verification

2026-08-22 在最终 `main` 工作树实际验证：

```bash
PATH=.venv/bin:/usr/local/bin:/usr/bin:/bin \
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. \
.venv/bin/python scripts/check_repo.py fast
# OK: repository fast profile
# 2,120 passed, 1 skipped, 59 subtests passed；唯一 warning 为第三方
# Starlette/httpx 弃用提示。SDD/public/architecture/requirements/UTF-8/
# Ruff/mypy/component catalog 全部通过。

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. \
.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/unit/test_eval_qq_flow_scenarios.py \
  tests/unit/test_qq_message_flow_suite.py \
  tests/unit/test_eval_capability_executor.py \
  tests/unit/test_eval_capability_scenarios.py \
  tests/unit/test_eval_capability_verifiers.py \
  tests/unit/test_eval_plugin_catalog.py \
  tests/unit/test_eval_fx_oracle.py \
  tests/unit/test_evaluations.py \
  tests/unit/test_qq_gateway_ingress_probe.py \
  tests/unit/test_acp_agent_bridge.py \
  tests/unit/test_persona_tools.py \
  tests/unit/test_persona_control_service.py \
  tests/unit/test_persona_task_observability.py \
  tests/unit/test_turn_tasks.py \
  tests/integration/test_access_control.py \
  tests/integration/test_acp_streaming_updates.py
# 396 passed, 10 subtests passed

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. \
.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/unit/test_release_artifacts.py
# 41 passed

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. \
.venv/bin/python scripts/check_architecture.py
# OK: architecture boundaries (450 modules, 1245 static edges, 0 cycles)

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. \
.venv/bin/python scripts/check_component_catalog.py
# OK: component catalog (19 packs, 69 static tools, 4 MCP entries,
# 4 subagents, 0 workflows)

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. \
.venv/bin/python -m compileall -q src console tests
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. \
.venv/bin/python -m chatcopilot botspec validate \
  bots/lingye-copilot-qq/bot.yaml
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. \
.venv/bin/python scripts/check_public_repo.py
git diff --check

cd console/web
npm test
# 3 files passed, 24 tests passed
npm run build
# TypeScript 与生产构建通过
```

`scripts/check_architecture.py` 只证明 `src/chatcopilot` 与 Console Python 源码中可由
AST 静态解析的 import、area policy、兼容入口和已声明语义规则；它不证明动态 import、
非 Python 依赖或运行时调用拓扑。`python scripts/build_smoke.py` 已成功构建 wheel 与 sdist，
但 tracked-only artifact verifier 按设计拒绝 13 个尚未由维护者暂存的新 Python 文件；AI
不执行 `git add`，维护者审阅并暂存后需要重跑该门禁。QQ message-flow 的本地通过只证明
receipt 列出的 AgentStrata-owned 合成后链路；`qq_platform`、`napcat`、`cc_connect`、
PersonaDraftAgent 和主 Agent 模型仍有替代层，不代表真实两账号 QQ、真实外部用户或商用模型
E2E。Codex renderer 测试也不证明 provider 内部 instructions。
