# AgentStrata Live2D Windows 客户端

版本 `0.2.0`，固定 Electron `45.0.0`。桌面端只管理窗口与本机 WSL 独立应用生命周期，
交互页面来自 `open-llm-vtuber 0.5.0`，可选角色管理来自 `live2d-generator 0.4.0`。
数据与凭据继续保存在 WSL。设计见[桌面规格](../../specs/live2d-desktop/spec.md)，
安装和使用见[Live2D 指南](../../docs/guides/live2d.md#windows-桌面客户端)。

## 开发与构建

Node.js 24，使用锁文件安装。WSL 可以运行 TypeScript 编译和纯逻辑单测：

```bash
ELECTRON_SKIP_BINARY_DOWNLOAD=1 npm ci
npm test
```

在 Windows PowerShell 中运行下列脚本。源码可以位于 WSL，但 npm、Electron 和打包器
始终在 Windows 原生目录运行。`BuildRoot` 默认为`%USERPROFILE%\.agentstrata\applications\live2d-desktop\build`。

```powershell
.\scripts\build-windows.ps1
```

如果源码在 WSL，先把入口脚本复制到 Windows 本地，再传入 WSL 源码路径。这样无需改变
PowerShell 执行策略，也不会把 UNC 路径作为 npm 的工作目录。替换示例中的源码位置：

```powershell
$desktopSource = '\\wsl.localhost\Ubuntu-22.04\path\to\AgentStrata\integrations\live2d_desktop'
$desktopBuild = Join-Path $env:USERPROFILE '.agentstrata\applications\live2d-desktop\build'
New-Item -ItemType Directory -Path $desktopBuild -Force | Out-Null
Copy-Item "$desktopSource\scripts\build-windows.ps1" "$desktopBuild\build-windows.ps1" -Force
& "$desktopBuild\build-windows.ps1" -SourceRoot $desktopSource
```

脚本依次执行锁定依赖安装、逻辑单测、受控 HTTP 页面 Electron smoke 和 x64
NSIS/ZIP 构建，结果在 `BuildRoot\release`。smoke 使用 Chromium 合成音频设备验证采集、
隐藏停采和视频拒绝，不访问 WSL，不使用真实角色或实体麦克风。
`npm start` 启动开发版；`npm run pack` 只构建安装包，不上传或自动更新。
`build-windows.ps1 -VerifyOnly` 只安装依赖、测试和运行 smoke，不请求安装包或写入新校验和。
日常 CI 在相关变更时使用该模式；手动完整 CI 和现有本地默认命令仍完整打包。
安装包使用显式文件清单，排除源码测试、后台、私有角色和配置。发布或本地交付需同时
提供匹配的完整 AgentStrata 源码快照及依赖锁文件；未经签名的本地测试包不代表商业发行。

## Windows 桌面工具

`./scripts/prepare-windows-mcp.ps1` 在独立目录准备固定 Windows-MCP 源码、Python 3.14.7
与锁定依赖，复用上游 stdio，不创建控制网络服务。脚本核对下载摘要、拒绝接管不明目录，
关闭遥测和自动界面监听。安装输出的原生 Python 路径填入应用 `local.env`。
主屏读取扩展位于 `src/chatcopilot/external_tools/windows_desktop`，截图走统一 MCP 图片通路。

## 实现入口

- `src/wsl.ts`：WSL 发行版探测与固定参数 CLI 调用，不经 shell。
- `src/controller.ts`：空闲确认、停机、应用、启动、失败恢复与结果状态。
- `src/desktop.ts`：单实例、托盘、窗口模式、媒体权限、管理页及受限 IPC。
- `src/preload.ts`、`src/security.ts`：有限桥接、窗口与主 frame 来源校验。
- `ui/`：独立本机设置页；设置文件仅保存路径与窗口偏好。

前端适配仍在 VTuber 固定补丁内，保持其上游许可。Electron/Chromium 许可由打包器
附带，插件/SDK/模型的许可不因桌面壳而改变，见 `licenses/NOTICE.md`。
