---
id: tool-progressive-disclosure
type: architecture
status: implemented
created: 2026-09-24
---

# 工具按需披露

## Summary

主 Agent 的实际工具仍由选定 pack、ToolRegistry 快照和宿主权限共同决定。Native 与 LangGraph 只直接提交会话基础工具的 schema，其余工具通过会话内搜索、描述和单次调用桥接；Codex 使用 App Server 的原生 `deferLoading`。按需披露只改变模型看见 schema 的时机，不改变身份、准入、权限、执行或交付归属。

## Design

遵守[四层运行时](../runtime-four-layer-definition/spec.md)和[六层源码依赖](../domain-layered-dependencies/spec.md)。`ToolDef.disclosure` 仅为 `direct` 或 `deferred`，默认后者。交付、人格、记忆、Skill 读取、结果回读、统一搜索与委托能力在各自定义处标为 `direct`；没有第二份按工具名维护的分类清单或 BotSpec 开关。独立 subagent 继续使用自己的显式工具投影。

Application 先绑定可信 Principal、资源与权限。Agent 从同一会话的 Registry 快照筛出可见真实工具，再冻结包含其工具包来源、直接/延迟标记与 schema 的披露视图。PromptPlan 的 `tool_names` 仍是可用真实工具名；结果、网页和远端 MCP 工具描述不能成为宿主规则。`CapabilitySnapshot.loading` 与真实标记一致，改变分类会改变能力摘要，旧 Codex 会话按现有绑定规则失效重建。

Native/LangGraph 的模型 schema 为直接工具和三个固定入口：`tool_search({queries,limit})`、`tool_describe({names})`、`tool_call({name,arguments})`。查询最多三个，每个最多返回十个匹配；描述最多五个工具；调用每次只允许一个延迟工具。检索按名称、别名、工具包和简介确定性排序，未命中返回已授权工具包提示，不改为全量 schema。桥接名称为保留名，真实工具冲突在物化时拒绝。目录提示只有已授权的工具包和名称；完整简介和输入 schema 分别由检索与描述结果提供。

`tool_call` 必须解析到当前视图中的延迟工具，并走既有 ToolExecutor，复检权限、输入 schema 和可信当前请求正文。工具结果继续经过同一 payload filter、结果引用和模型投影；审计记录真实工具名、原模型调用 ID 和单次执行结果。搜索与描述消耗模型迭代和时间，不消耗业务工具调用数；桥接调用消耗一次。直接调用延迟工具、猜测未授权名称和无效参数均失败关闭，不泄漏隐藏工具信息。取消或未知结果不自动重放写入。

Codex 继续向 App Server 提供 `agentstrata` namespace；`deferLoading` 从同一个 `ToolDef.disclosure` 生成，不增加宿主桥接。Codex 上下文快照的 schema 与 token 估算只代表 adapter 可见的 `dynamicTools` 请求，不代表原生模型最终加载的工具集合。

## Acceptance

- 同一授权会话的目录、schema、能力摘要和可执行工具一致；成员搜索或猜测不能取得 Owner 工具。
- Native/LangGraph 的两种模型传输只提交直接工具加固定桥接 schema；一次延迟调用只产生一次真实工具执行和结果。
- Codex 保持原生延迟加载，并与其余主运行时共享直接/延迟分类；subagent 工具集不受主会话桥接影响。
- 上下文观测保存实际提交的 schema，并区分 Codex adapter 可见数据与原生内部加载；静态 schema 缩减不冒充真实模型任务质量提升。

## Verification

执行目录物化、桥接查询与权限、真实 ToolExecutor 调用、Native/LangGraph/Codex 投影、取消和回归的定向测试，再运行组件目录、架构与仓库 full 检查。真实模型、QQ 与部署验证分别报告，不由 mock 或局部测试替代。
