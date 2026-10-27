# Agent 与应用桥接

七个版本化场景覆盖多轮文字、角色动作、组合动作效率、跨客户端隔离、QQ 准入及未知交付。
所选 Agent 使用配置中的 runtime 和模型；浏览器与 OneBot 是受控对端。
本套件基于服务仓库的受管适配源码，不访问生产实例、真实 QQ 或语音设备。
用例和评分由宿主冻结，执行观测不包含候选自报的通过结论。

| Case | 检查依据 |
| --- | --- |
| `live2d-dialogue` | 同一会话的两个独立回合、前轮标记的正确回答、客户端原样接收宿主输出 |
| `qq-dialogue` | 多轮回答、OneBot 实际发送内容与绑定 run 的 provider acknowledgement |
| `live2d-motion` | Agent 调用 `greet`、实际 MCP/动作控制器派发一次配置动作、匹配浏览器回执 |
| `live2d-pose` | 一次写工具调用依次设置 `happy` 表情和 `greet` 动作，两项配置值与独立回执均正确；两次单独工具调用不能满足效率目标 |
| `live2d-session-isolation` | 另一个已认证客户端不能读取该会话或回合 |
| `qq-admission` | 未授权私信被拒绝，不创建会话、不调用模型、不发送消息 |
| `qq-delivery-unknown` | 对端未确认时保留未知，不能伪造确认或盲目重发 |

`quick` 选择两道对话题；`full` 为全部七题；`security` 为会话隔离与准入；
`reliability` 为动作回执与未知交付；`efficiency` 为组合动作与 QQ 准入保护。
使用确定性原生评分，不调用 LLM Judge。
两种 runtime 分别使用其现有 Agent profile 建立基线；不会覆盖生产实例配置。

执行需要完整服务源码树和有效模型配置，纯 wheel 环境缺少受管适配源码时阻断。
不挂载生产数据，不使用真实 QQ、Live2D SDK 渲染或语音设备；受控对端回执不能代替真实交付。
未知交付题通过只表示不确定性处理正确。操作见[测评指南](../../../../../docs/guides/evaluation.md#live2d-与-qq-桥接基线)，
版本与结果解释见 [Evaluation](../../../../../docs/reference/evaluation.md#agent-与外部桥接评测)。
