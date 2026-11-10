# AgentStrata Companion · Windows 桌面伙伴

这是基于 AgentStrata 构建的 Live2D 桌面应用。产品入口见[Companion](../../docs/products/companion.md)。
为保持现有安装和数据兼容，本轮仅统一界面展示名；安装器仍使用 AgentStrata Live2D Native，包名、应用标识和数据目录不变。

版本 `0.3.0`，固定 Electron `45.0.0`。运行端使用 Windows 原生 Python、Gateway、
LangGraph、VTuber 与 Windows-MCP，开发侧 Harness/Eval 保持 Linux。设计见
[桌面规格](../../specs/live2d-desktop/spec.md)，安装与使用见
[Live2D 指南](../../docs/guides/live2d.md#windows-桌面客户端)。

## 开发与构建

Node.js 24，使用锁文件安装。Linux 可以运行 TypeScript 编译和逻辑单测：

```bash
ELECTRON_SKIP_BINARY_DOWNLOAD=1 npm ci
npm test
```

先在 Linux 仓库根目录准备固定上游与前端构建，再冻结完整公开源码。输出目录必须不存在：

```bash
.venv/bin/python integrations/live2d_desktop/scripts/prepare-runtime.py \
  --build-root /absolute/native-build --output /absolute/native-runtime-payload
```

在 Windows PowerShell 中构建；源码与载荷可以来自 WSL，但用户运行时不访问它们。
`BuildRoot` 默认 `%USERPROFILE%\.agentstrata\build\live2d-native\desktop`。

```powershell
.\scripts\build-windows.ps1 -RuntimePayload '\\wsl.localhost\Ubuntu-22.04\absolute\native-runtime-payload'
```

脚本先核对桌面代码与载荷源码一致，再执行锁定依赖安装、单测、受控页面 Electron smoke
和 x64 NSIS/ZIP 构建，结果在 `BuildRoot\release`。构建包含匹配公开源码、上游源码、
前端构建和许可，排除用户私有角色、配置与聊天数据，不上传或自动发布。
`build-windows.ps1 -VerifyOnly` 只测试桌面壳，不要求或准备运行载荷，也不证明真实模型、
桌面任务或硬件音频。`npm run pack` 也要求事先在本地提供 `runtime-payload`。

## 原生环境与生命周期

`scripts/bootstrap-windows.ps1` 使用固定 uv 和 hash 锁准备 Python 3.13 主环境、Python 3.12
VTuber 环境及 Python 3.14 Windows-MCP。来源文件与解释器均归属安装副本。
`windows-runtime.lock.json` 固定原生工具；`windows-host-requirements.lock` 从主 `uv.lock`
导出，VTuber 使用自己的 `requirements-windows.lock`。ChatGPT 认证辅助程序按需下载，
日常 Agent 仍是 LangGraph。API 与授权信息由项目现有存储维护。

桌面通过通用 `app` CLI 准备、查询与停止，通过 `app run --watch-stdin` 保持后台归属。
Windows Job 管理后台与 MCP；用户程序显式脱离 Job。隐藏到托盘停止感知与麦克风，
退出及桌面进程异常关闭停止自有后台。Windows 首版只提供新安装和同版本修复；
不同源码不覆盖，M5 更新/撤回后续实现。

直接编辑文件无需打开设置页，配置位置、示例及生效顺序见
[本地模型配置](../../docs/guides/live2d.md#直接编辑本地模型配置)。

## 实现入口

- `src/backend.ts`：本机 CLI、运行进程及凭据传递，不经过 shell 拼接命令。
- `src/desktop.ts`：单实例、托盘、透明窗口、媒体权限和受限 IPC。
- `src/preload.ts`、`src/security.ts`：有限桥接及来源校验。
- `ui/`：安装与模型设置，凭据不写入窗口偏好文件。

持续感知、任务授权和 MCP 图片结果保持原有契约。原生窗口和完整运行链须在 Windows
独立验收；实体音频与合成播放回执分开报告。上游前端、模型与 SDK 保留各自许可，
见 `licenses/NOTICE.md` 和仓库 `THIRD_PARTY_NOTICES.md`。
