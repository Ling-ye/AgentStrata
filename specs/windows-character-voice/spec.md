---
id: windows-character-voice
type: architecture
status: accepted
created: 2026-11-23
---

# Windows 原生角色制作和语音训练

## Summary

Windows 桌宠的 Owner 可以通过现有工具注册表创建角色和查看任务，或为现有角色换声音。角色库继续拥有身份、外观与不可变版本。语音由可选的本机 GPT-SoVITS 环境训练和合成，当前角色应用与撤回复用桌面控制器。

## Design

- [生成器](../live2d-generation/spec.md)保有任务、立绘、拆层和模型校验。Windows 使用已有的私有文件支持及进程 Job Object。人设和立绘提示由 Agent 生成。MCP 和工作台进入同一任务接口。
- 音源保留来源与允许用途。可下载、许可可核实的公开音源和用户确认的本机输入进入处理。训练工作进程使用固定依赖，完成片段处理、两套权重训练及未见台词合成。缺少音源保留外观和提示词。重试沿用已校验阶段的证据。
- 角色版本以内容摘要引用私有语音资产。生成成功与客户端应用分别记录。Windows 安装副本的运行时角色设置和模型、音色切换由同一停止与恢复事务管理。
- 对话合成可选接独立的 IndexTTS 1.5 环境。IndexTTS 从一段本机口播参考合成，VTuber 使用现有经鉴权的回环语音接口，以参考音频摘要选择声音。

## Acceptance

Windows 原生安装可从桌宠 Owner 的对话调用工具，制作角色并预听。在不同角色间实际切换后，核对模型、文字、语音和口型。旧角色及 Edge TTS 使用原有配置。

## Verification

定向覆盖生成和训练、音源权限、版本备份和恢复、Windows 子进程退出及桌宠启用。运行项目 full 和 Windows 安装器检查。单独记录实际 GPU 训练、合成与桌面呈现证据。
