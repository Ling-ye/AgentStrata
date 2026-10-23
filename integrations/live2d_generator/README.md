# Live2D 生成插件

独立的资产制作工作台：提示词 / 角色资料 → 立绘 → 分层 → 自动绑定 → Live2D 模型包。
已有 PNG/JPEG/WebP 可直接拆层，PSD 或语义命名的 PNG 图层 ZIP 可直接绑定。
生成器不依赖 AgentRuntime，对话、语音和播放仍由既有客户端负责。

安装、配置、示例和导入方法见 [Live2D 指南](../../docs/guides/live2d.md#生成自己的角色)。
设计和边界见 [生成规格](../../specs/live2d-generation/spec.md)。

- `types.py` / `config.py`：请求与 provider 端口、固定配置。
- `repository.py` / `service.py`：私有任务、输入校验和生成流程。
- `providers.py` / `rigging.py`：Images、See-through 与固定版本自动绑定器。
- `runtime.py` / `web.py` / `cli.py`：单进程任务控制、本地工作台和 CLI。
- `install.py`：向受管 VTuber 交付模型和恢复配置，不导入其 runtime。
- `manage.py`：接入现有插件管理器；独立 Python 3.12 环境。

模型是自动绑定初稿，质量取决于立绘姿态、拆层与绑定结果；不保证达到手工精修质量。
完整包必须包含真实 `.moc3`，不能以静态预览或 JSON 空壳作为完成结果。导出清单的
`validation: file-integrity` 只表示文件完整性，`sdk_validation: not-run` 不声称 SDK 已验证。
生成过程不读取或更换 AgentStrata 模型配置，HTTP/Space 服务由安装者独立配置。

自有代码为 AGPL-3.0-only。第三方程序在安装环境单独取得；上游、SDK、模型及参考形象
保留各自条款，见 [NOTICE](NOTICE.md)。不在本包中分发第三方代码、权重、官方参考图或
生成模型。固定版本与摘要位于 `resources/requirements.lock`，构建依赖位于 `build.lock`。
