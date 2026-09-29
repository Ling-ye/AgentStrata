---
id: unified-llm-configuration
type: architecture
status: accepted
created: 2026-09-29
---

# 统一模型配置与能力发现

## Summary

机器人、辅助 Agent、独立 code worker、Harness、代码熵回收和 Evaluation 从同一个
模型配置文件取得连接、方案和用途引用。Console 集中编辑配置并从实际连接发现模型。
保存后新任务使用新配置，常驻机器人通过现有应用／重启入口加载。

## Design

遵守[四层运行时](../runtime-four-layer-definition/spec.md)、
[六层源码依赖](../domain-layered-dependencies/spec.md)和
[Runtime 路由契约](../agent-runtime-routing/spec.md)。Core 管配置和模型传输，厂商
adapter 管协议，原有 Runtime 管 Agent 循环，宿主管身份、权限、任务和交付。

### 配置与解析

- 统一保存配置为 `~/.config/agentstrata/llm.json`，可通过绝对路径
  `AGENTSTRATA_LLM_CONFIG` 指定。所有宿主部署使用同一来源。
- 首次尚无保存文件时，可读取同目录的 `llm.defaults.json`；自定义路径对应同名
  `*.defaults.json`。手动修改或前端保存后，以整份保存配置为准，不叠加默认字段，
  不因保存文件损坏而回退默认值。显式任务快照不使用默认文件。
- `connections` 声明连接类型、端点、认证引用和请求超时；`profiles` 声明连接引用、
  模型与可选推理强度；`bindings` 将实例用途或后台用途映射到方案。
- 连接类型为 `codex`、`openai_responses`、`openai_compatible`。订阅认证引用现有
  main/worker lane；API Key 引用环境变量。可用 `env_file` 指向同一私有凭据环境文件。
  不新建登录系统，不复制 token 到模型配置或任务快照。
- BotSpec 只保留 `binding`；子 Agent 使用 `model_binding`。运行预算、执行命令与
  超时仍由原领域维护。未配置用途明确报错，不回退到内置模型或旧模型环境变量。
- 复用 `ResolvedModelRoute`、`ModelSelection`、`ModelClient` 和 Runtime adapter；
  统一解析和客户端工厂不创建另一套 Agent 执行协议。方案标识和保存版本不进入模型
  行为指纹；现有 `/model` 保留会话／单次选择语义。

### 发现与校验

Codex 复用原生 App Server `model/list` 与现有凭据租约，按实际连接和认证 lane
查询模型及支持的推理强度。API 查询实际连接的 `/models`。厂商参数仅采用结构化
响应明确提供的值，不用 Models.dev、SDK 内置枚举、模型名称或桌面缓存补齐未知能力。

目录返回来源、获取时间、模型标识、显示名，以及可选的推理选项和上下文窗口。
上下文在第一版只读；未知显示「接口未提供」。API 仅返回模型名称时，不开放推理强度。
保存校验服务端最近成功目录，直接 HTTP 请求也不能保存未发现的模型参数。

目录在进入页面、切换连接和手动刷新时获取；保留最近一次成功结果。刷新失败显示
错误及上次成功时间，不清空成功结果，不切换已保存模型。目录不进入每次推理的
必经路径；已有 worker 执行前检查保留。连接草稿可先发现，再与方案一起原子保存。

### Console

- 模型配置页统一维护连接、方案与用途；业务页面复用方案选择器。
- `GET/PUT /api/llm/config` 读取／保存配置及解析摘要；配置 revision 只用于并发
  保存冲突，冲突返回 409。文件锁、同目录原子替换与私有文件权限保证一致性。
- `GET /api/llm/catalog` 读取最近结果，`POST /api/llm/catalog/refresh` 刷新指定
  连接或其草稿。写入与刷新沿用本机、同源限制，响应禁止缓存。
- 常驻实例明确展示待应用；复用原有应用／重启流程，不实现跨会话热切换或广播。

### 任务冻结与旧入口移除

新建任务冻结实际模型配置。Harness 批次及其子任务沿用同一快照；独立 code worker
和测评子进程读取任务私有目录内的配置快照。测评覆盖主模型、辅助模型和 Judge；
重试不回读全局方案。周期调度只保存方案引用，每次创建任务时解析当前版本。

直接删除旧 BotSpec 具体模型字段、旧模型环境覆盖及独立 Harness/Judge 模型设置，
不提供旧配置迁移器。原有 API key 与 Codex 登录凭据保持。仅可清理模型目录缓存和
失效原生会话绑定，且须确认相关执行已停止；任务历史、聊天、人格、记忆、工作区与
远端 PR 不在清理范围。历史记录不补填当前配置。

## Acceptance

- 所有使用方通过集中配置解析与现有 adapter 获取模型能力。
- 前后端共同拒绝未发现的模型参数，推理强度随目录更新而无需新增枚举。
- 并发保存、连接／认证隔离、执行参数与保存值一致。
- 新任务使用最新配置，已有任务保留冻结值，机器人应用前后状态准确。
- 旧入口明确拒绝，业务历史与凭据不受配置清理影响。

## Verification

定向覆盖配置与目录、真实请求参数、Runtime 辅助模型、模型命令、独立 worker、
Harness、周期调度、测评与 Judge，以及 Console 接口和前端。再运行仓库 full 检查。
实际模型发现、真实推理和 QQ 交付分别记录，不把 mock 或构建成功作为端到端证据。
