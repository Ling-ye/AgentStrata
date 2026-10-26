---
id: interactive-client
type: architecture
status: accepted
created: 2026-10-19
---

# 独立交互客户端与角色适配

## Summary

Gateway 宿主支持不配置原生渠道、显式绑定身份的客户端实例，首个可选适配是
Open-LLM-VTuber 浏览器客户端。AgentStrata 负责
会话、运行、工具和记忆；可选适配负责语音、角色与播放状态，不扩展 Gateway 呈现协议。

## Design

遵循[四层运行时基线](../runtime-four-layer-definition/spec.md)。实例装配支持没有原生
Channel 的 Gateway；对话直接经认证 Gateway client edge 进入 Application 与 Agent。
BotSpec 的 gateway.clients 只声明客户端 ID、凭据环境变量引用及宿主采信的角色，
不从客户端正文采信权限。就绪检查覆盖实际装配组件，保留私有状态、单实例 lease 与
writer generation。已有原生渠道继续要求真实渠道就绪。

可选适配独立安装在 integrations/open_llm_vtuber，不被核心运行代码导入。对话 Bridge
复用 GatewayClient，稳定映射客户端历史与 Gateway session，最终文本交给上游语音管线。
重连观察已知原 run，未知受理只用原参数与幂等键对账，不创建替代输入；停止后不自动重发。头像控制通过现有 MCP、ToolRegistry 和权限机制
提供 list_actions、set_expression、play_motion；MCP 配置为 main/interactive，目标和
动作由本地配置绑定。控制端口先启动，Bridge 延迟连接 Gateway，避免启动依赖循环。

AvatarController 的接口及播放状态只存在于适配组件。动作消费和反馈关联请求，实际执行、
接收、失败与未知分别报告，不承诺恰好一次。浏览器读写继续并发处理，不能让对话等待
阻塞动作回执。ASR/TTS 失败不改写 Agent 执行结果；停止播放不撤销已发生的工具副作用。
凭据留在后端环境，服务限回环地址，浏览器 WebSocket 检查允许的 Origin。

提交前停止不发 chat.send；提交中停止若暂缺 run_id，保持未知，原受理迟到后针对原 run 取消。
停止回执按 session/run 绑定，接受取消不等于执行终止；迟到回执不能清除下一请求。
关闭过程阻止新输入，恢复待处理状态不能被当成停止完成。
M2 的双 runtime、权限与回执边界由[桥接基线](../../docs/reference/runtime.md#外部桥接基线)维护。

实例使用独立工作区、Owner 和记忆，不迁移原渠道数据。第一版等待完整回复，语音使用
客户端配置，不包含连续语音、自动打断、桌宠打包、视觉或文件回传。

## Acceptance

- 不配置 QQ/微信即可启动、认证、进行多轮文字对话、取消和查询原 run。
- 未授权客户端不能执行模型或工具，也不能访问其他客户端的 session。
- 核心不依赖第三方角色包；关闭角色控制不影响纯文本对话。
- 角色动作只执行配置中声明的目标和名称，反馈与未知结果可辨识。
- 上游适配支持按键语音、语音回复、真实表情和预设动作；真实音频体验独立验收。

## Verification

执行 BotSpec、宿主生命周期、身份隔离、GatewayClient、Bridge 和头像控制定向测试，
再运行仓库 full 检查。用替换的 AvatarController 验证核心解耦，覆盖重连不重复运行、
取消、回调并发、TTS 失败、迟到动作反馈及实例停止。固定版本补丁通过检查及上游构建。
受控模型、模拟音频和浏览器夹具不代表真实麦克风或真实模型验收。
