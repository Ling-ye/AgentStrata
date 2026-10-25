# Open-LLM-VTuber 可选适配

本组件在独立的客户端环境中运行，不被 AgentStrata 核心导入。
对话使用既有 GatewayClient；头像工具通过客户端后端的 MCP 端点提供。

安装、环境、固定版本和实际验收步骤见[Live2D 接入指南](../../docs/guides/live2d.md)。
接口归属见[运行链](../../docs/reference/runtime.md#独立交互客户端)，
安装生命周期见[插件规格](../../specs/external-software-plugins/spec.md)，
交互证据边界见[客户端规格](../../specs/interactive-client/spec.md)。

- `agentstrata_vtuber/manage.py`：独立 prepare/check/doctor/run 命令，部署逻辑随 wheel 分发。
- `agentstrata_vtuber/bridge.py`：对话、取消、原请求恢复与历史映射。
- `agentstrata_vtuber/avatar.py`：可替换的 AvatarController 和浏览器回执。
- `agentstrata_vtuber/control.py`：本地 MCP 与浏览器 Origin 边界。
- `agentstrata_vtuber/agent.py`：唯一导入上游 Agent 类型的模块。
- `agentstrata_vtuber/resources/patches/` 与 `resources/upstream.json`：固定版本的小补丁；不包含第三方模型、SDK 或编译产物。

后端原项目使用 MIT，前端使用 Open-LLM-VTuber License 1.0，模型及 Live2D SDK 另有许可。
独立安装第三方项目时保留其原许可证；商业打包需按实际使用的组成部分核对授权。

自有 Python 适配器采用 AGPL-3.0-only，署名为 Copyright (C) 2026 Lingye。
`agentstrata_vtuber/resources/patches/frontend.patch`（包括为该前端添加的内容）按上游前端条款单独许可，排除于项目
默认 AGPL 授权；收费访问/托管、商业重分发/改名及商业嵌入须取得上游规定的商业授权。
完整原文、固定 revision 与后端 MIT 声明见[第三方声明](../../THIRD_PARTY_NOTICES.md)。

插件版本为 `0.4.0`，受管环境固定 Python 3.12，支持声明为 `>=3.12,<3.13`。
前端补丁支持[Windows 桌面壳](../live2d_desktop/README.md)的模式切换、隐藏停麦、
空闲切换角色和会话重连；普通浏览器仍使用原有界面与操作流程。
prepare 声明一个 `main` 可观测 Agent，由管理器登记后供 Console 只读发现；登记接口见
[插件规格](../../specs/external-software-plugins/spec.md#可观测-agent-登记)。
构建检查使用 `scripts/plugin_artifacts.py`；wheel/sdist 携带本目录的 LICENSE、COPYRIGHT、
NOTICE.md 和两份上游许可原文，安装时不依赖源码 checkout。

`assets.py` 将模型和字典保存在 `data/live2d`，运行时通过固定补丁读取该目录。
模型导入不改写固定上游源码，导入 journal 未恢复时拒绝启动。生成和导入见
[Live2D 指南](../../docs/guides/live2d.md#生成自己的角色)。
