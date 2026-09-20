---
id: agent-configuration-presentation
type: architecture
status: implemented
created: 2026-09-20
---

# Agent 配置用途、来源与运行证据

## Summary

Console 的 Agent 层完整展示实例参数，区分主 Agent、基础模型、研究模型和独立代码任务。
模型解析与运行时同源，默认值、停用项和不适用参数均可查；不改变模型选择或新增编辑入口。

## Design

- 保持[四层运行时](../runtime-four-layer-definition/spec.md)和
  [六层依赖](../domain-layered-dependencies/spec.md)。Core 配置解析接受显式实例环境与查找路径；
  Component Catalog 提供无运行副作用的子 Agent 定义合并；BotSpec 负责 inspection 投影；
  Console 只消费投影，不创建模型、MCP 或会话。
- 复用 inspection 的 current、loaded、execution，保留实体 ID。当前模型与预算使用
  effective_config，字段来源与用途为展示元数据，不参与配置生效指纹。
- 主 Agent 摘要标明实例默认模型，chat/research/code 各自说明消费者；research 逐字段继承，
  Code profiles 与任务 profile 遵守环境覆盖规则。代码生成的 Codex 策略标为宿主固定策略。
- 提示词独立分组；搜索、委托、工具包、MCP 和工具保留完整参数。子 Agent 合并目录定义、
  实例覆盖及预算，自定义 Agent 不受预设编辑草稿过滤。字段只在一个详细位置展示。
- 普通字段默认展开，长正文、schema 和原始来源按需查看。缺少运行观测不生成逐项未知标签；
  静态配置不显示加载状态，MCP 和工具的状态必须有有效运行证据。
- 当前保存配置与服务值分别表达；历史仅使用原快照，缺失字段显示未记录，不回填当前配置。
  顶部运行状态页签、已有编辑及保存流程保持。现行交互说明维护于 Console 正文。

## Acceptance

- Codex、Native、LangGraph 的实例默认模型正确；模型槽、环境覆盖与继承不混淆。
- 各实例解析无进程环境污染，inspection 不产生运行副作用。
- 预设、自定义 Agent、停用 provider、默认值和不适用参数完整可见。
- 草稿、搜索、深链接、历史配置与无观测状态准确；展示元数据不导致待应用。

## Verification

执行配置解析、Console 投影与观测定向 Python 测试、前端测试和构建、仓库 full 检查，
并检查桌面与窄屏、状态缺失、模型覆盖和共享草稿。浏览器合成验收不代表真实模型或 QQ E2E。
