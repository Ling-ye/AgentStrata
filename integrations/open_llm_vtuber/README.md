# Open-LLM-VTuber 可选适配

本组件在独立的客户端环境中运行，不被 AgentStrata 核心导入。
对话使用既有 GatewayClient；头像工具通过客户端后端的 MCP 端点提供。

安装、环境、固定版本和实际验收步骤见[Live2D 接入指南](../../docs/guides/live2d.md)。
接口归属见[运行链](../../docs/reference/runtime.md#独立交互客户端)，
设计及证据边界见[规格](../../specs/interactive-client/spec.md)。

- `agentstrata_vtuber/bridge.py`：对话、取消、原请求恢复与历史映射。
- `agentstrata_vtuber/avatar.py`：可替换的 AvatarController 和浏览器回执。
- `agentstrata_vtuber/control.py`：本地 MCP 与浏览器 Origin 边界。
- `agentstrata_vtuber/agent.py`：唯一导入上游 Agent 类型的模块。
- `patches/` 与 `upstream.json`：固定版本的小补丁；不包含第三方模型、SDK 或编译产物。

后端原项目使用 MIT，前端使用 Open-LLM-VTuber License 1.0，模型及 Live2D SDK 另有许可。
独立安装第三方项目时保留其原许可证；商业打包需按实际使用的组成部分核对授权。

自有 Python 适配器采用 AGPL-3.0-only，署名为 Copyright (C) 2026 Lingye。
`patches/frontend.patch`（包括为该前端添加的内容）按上游前端条款单独许可，排除于项目
默认 AGPL 授权；收费访问/托管、商业重分发/改名及商业嵌入须取得上游规定的商业授权。
完整原文、固定 revision 与后端 MIT 声明见[第三方声明](../../THIRD_PARTY_NOTICES.md)。
