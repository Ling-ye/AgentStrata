---
id: agent-streaming-observability
type: architecture
status: implemented
created: 2026-09-11
---

# Agent 执行过程流式观测

## Summary

主 Codex Agent 改用 App Server stdio，在 Console 的既有 Agent 执行段持续展示公开消息、
推理摘要、命令输出、MCP 和文件变更。执行过程不等同于底层模型请求，缺失摘要不能补造。
独立 code-worker 和研究工具继续使用 exec；不部署、不修改历史任务或聊天交付方式。

## Design

遵循 [四层运行时基线](../runtime-four-layer-definition/spec.md)、
[Console 观测](../console-layered-observability/spec.md) 和
[私有原值展示](../console-operator-visible-values/spec.md)。Backend 负责协议及过程适配，
通过 AgentEvent 交给 Gateway 的既有观测索引；Console 只读该索引，不持有执行生命周期。

每回合在当前 actor 的 bubblewrap、运行 home 和 main credential lease 内启动 App Server。
initialize/initialized 后 start 或 resume 精确 thread，再 turn/start；只处理绑定当前 thread/turn
的事件。恢复沿用身份、策略和 credential generation 校验，失败不新建回合重试。取消先
interrupt，再有界终止进程组；退出进程后才结束凭据租约。过程消息不进入渠道回复，最终回复
仍须通过租约回写和原交付门禁。模型与附件沿用 PromptPlan 和模型选择契约，summary 为 auto。
宿主生成运行配置并隔离项目配置、规则；保持原有工具目录、ExecutionScope 和凭据路径保护。
未预期权限与交互请求拒绝，不扩展授权。首个协议验收版本为 0.147.0-alpha.1.2。
恢复启用该版本的 experimentalApi/excludeTurns，只取恢复元数据，不传回整份历史或读取隐藏正文。

AgentContentDelta 保存真实 span、活动标识、内容类别、revision 和追加文本；公开消息、
公开推理摘要及命令输出按约 250ms 合并，结束时刷新并保存最终快照。原始推理和加密内容
不进入观测。重复终态与迟到增量不覆盖已完成项。Provider 和宿主工具只在真实 ID 一致时关联。
正文沿用现有限额和留存，增量不反复复制全文，缺口显式展示，观测失败不影响执行事实。

新增 GET /api/bots/{instance_id}/gateway-observation/runs/{run_id}/stream?after={seq}，
支持 Last-Event-ID。SSE 从持久索引续读并附带有界增量正文，运行中约 250ms、终态后降低
查询频率，继续接收迟到记录。无缓存，断开只停止读取；不新增数据库或观测业务队列。

前端按真实活动合并消息、摘要和工具卡，终态读取快照；未完成项按需重建增量。
当前及失败项默认展开，用户选择优先；接近底部才自动跟随，上翻后提示新活动。
切换任务或隐藏页取消订阅，重连保留游标、阅读位置及展开状态，正文不持久化到浏览器。
Native/LangGraph 保留真实模型轮次，不将 Codex turn 或工具活动标为底层模型轮次。

接口不支持时明确失败，不自动回退 exec 或重放执行。部署与回滚单独由操作者执行；旧任务
与旧正文保持，旧有效 thread ID 仍通过原绑定校验后恢复，不扫描 rollout 作为运行数据源。

## Acceptance

- 新建、恢复、模型切换、附件、MCP、取消、超时和断线不重放均有测试。
- 公开摘要、消息、命令输出逐步可见；最终回答不混入 commentary，Token 不重复累计。
- actor、群/私聊、Owner/member、credential generation、未确认交换隔离保持。
- SSE 续读与终态迟到事件不丢失，增量/快照正确合并；缺失、到期、限额和故障明确。
- 桌面与窄屏保持四层布局，多卡可读，更新不抢占用户阅读。

## Verification

2026-09-11，WSL / CPython 3.13.15：

- `PYTHONPATH=.:src .venv/bin/python scripts/check_repo.py full` 通过全部 12 项检查，Python
  3408 passed、1 skipped、121 subtests passed；跳过 Windows 原生路径大小写专属用例。
  使用独立临时候选索引包含未暂存的新文件，实际索引 SHA-256 不变；wheel/sdist 的精确文件
  投影与隔离运行验证通过，架构为 543 modules、1779 static edges、0 cycles。
- 主 Backend、App Server、过程与群会话专项 109 passed、9 subtests passed。
  真实管道协议夹具覆盖握手、恢复、取消、超时、拒绝权限扩张及断线不重放；原生 Codex sandbox
  与 bubblewrap 用合成文件验证 Owner/member 资源范围、认证/Relay/config 路径拒绝和规则隔离。
- `npm --prefix console/web test`：15 个文件、151 项通过。额外覆盖正文配额耗尽且没有正文文件时
  仍显示截断原因；该提示修正后重新完成生产构建和公开边界检查。
- Chromium 真实浏览器完成 9 组检查，0 页面异常：1440px 公开消息与摘要及真实开始顺序、
  消息原位更新、命令输出、阅读位置、跟随新活动、刷新重建、390px 无横向溢出、终态快照替换、
  SSE 无缓存。使用隔离 App Server 事件夹具、真实观测索引、HTTP/SSE 与生产前端，不连接真实模型或 QQ。
- 本机 Codex 0.147.0-alpha.1.2 的隔离 App Server 初始化、受管配置读取和 thread/start 通过，
  instructionSources 为空；未发送 turn/start。空 thread 没有 rollout，不能据此声称真实历史恢复已验证。
- 附加 `bash scripts/check_secrets.sh changes` 在 90 秒时限内未完成，退出码 124，未取得
  Gitleaks 扫描结果；不将该项记为通过。独立的公开信息边界检查已通过。

未调用真实模型、未发送 QQ 消息、未部署或重启实例。源码和部署的端到端验证边界分别保留；
历史未采集正文不回填。最终交付保持未暂存、未提交。
