# 开发与验证

从可编辑源码环境开始；机器人部署是独立任务。先选择本次领域，再读取关联正文和源码入口。

## 按贡献类型开始

先按[贡献指南](../../CONTRIBUTING.md#第一次提交-pr) Fork 并建立分支。以下命令均在仓库根目录运行；核心 Python 环境为 3.12–3.13。

| 贡献类型 | 环境与第一步 | 合并前验证 |
| --- | --- | --- |
| 文档、链接和导航 | Python、Git 与 PyYAML；按下方最小环境步骤准备，无需模型凭据或机器人部署 | `docs`，并核对说明中的真实命令与链接 |
| Console | Node 22 与 npm；`npm ci --prefix console/web`，然后 `npm --prefix console/web run dev` | 相关测试及 `python3 scripts/check_repo.py console`；代码仍按下表补齐 `fast` 或 `full` |
| Python、桥接与工具 | 按下面的完整源码环境安装锁定依赖，再运行 Issue 指定的定向测试 | 定向测试后，按风险选择 `fast` 或 `full`；平台检查按任务说明执行 |

Console 开发服务器只提供前端，实际页面的 API 需要 Console 后端；不启动后端时使用现有测试中的受控数据检查局部逻辑。不要为了一个界面改动先部署真实 QQ 或模型。

新的纯文档工作区只需安装项目已声明的 PyYAML；已有完整源码环境可直接运行检查：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install 'PyYAML>=6.0'
.venv/bin/python scripts/check_repo.py docs
```

需要修改代码时再使用下面的 `uv sync --locked` 安装完整锁定依赖。

QQ 编解码与 Agent 工具发现已有本地测试入口，可在完整源码环境中分别运行：

```bash
.venv/bin/python -m pytest tests/unit/test_qq_onebot_codec.py -q
.venv/bin/python -m pytest tests/unit/test_tool_disclosure.py -q
```

这些测试使用合成输入或受控模型，不需要真实 QQ 账号和模型密钥，也不证明真实 QQ 交付或模型效果。首次安装仍需要下载开发依赖。
如果环境未就绪，可以先提交 Draft PR 并说明阻塞；维护者协助区分安装问题、既有失败和新增回归，合并前补齐要求的验证。

## 准备环境

默认 WSL/Linux。安装仓库声明的开发依赖，使用同一个解释器运行检查：

```bash
uv sync --locked --extra agent --extra acp --extra dev --extra evaluation --extra weixin
uv run agentstrata --help
uv run agentstrata botspec validate bots/lingye-copilot-qq/bot.yaml
```

需要 WSL 系统工具或修复源仓环境时使用 `bash deploy/wsl/install_wsl_env.sh`，需安装 Console 时显式加 `--with-console`。
完整检查需要 Bubblewrap、ripgrep，以及 `npm ci --prefix console/web` 安装的前端依赖。
`--locked` 同时检查依赖声明与锁文件是否一致，不在验证过程中重新解析依赖。

## 选择检查范围

| 变更 | 检查 |
| --- | --- |
| 普通说明、链接和导航 | `docs` |
| 局部功能与修复 | 定向测试，再 `fast` |
| 架构、权限、检查器、跨域、部署、依赖或打包 | 定向测试，再 `full` |

```bash
.venv/bin/python scripts/check_repo.py docs
.venv/bin/python scripts/check_repo.py fast
.venv/bin/python scripts/check_repo.py full
```

`fast` 按 tests/fast.txt 的完整文件清单，用 4 个 pytest worker 按文件并行执行；
`python` 执行所有静态检查、依赖与许可一致性、发行资源和完整 Python 回归，同样按文件使用 4 个 worker；
`console` 执行前端测试和生产构建；`full` 顺序组合 `python` 与 `console`。
每次测试使用独立的临时目录。WSL/Linux 上会等测试及其子进程退出后清理目录。
本地检查为每次运行保留独立报告和日志。用 `--report-dir` 可以指定报告位置。
只要新增变化、失败或具体风险没有出现，已通过的检查不重复执行。

`docs/fast/full` 默认收集本 checkout 的已暂存、未暂存和未忽略的新文件，输出关联文档。
比较指定提交或分支时使用 `--docs-base`；删除与重命名同样参与关联。提示只要求核对，
没有事实变化时无需修改文档。

```bash
.venv/bin/python scripts/check_repo.py docs --docs-base HEAD~1
.venv/bin/python scripts/check_docs.py --changed-path src/chatcopilot/harness/models.py
```

检查入口也接受可重复的 `--changed-path`。隔离候选的清单由调用方提供，不从操作者仓库补取；
没有提供时会明确显示未评估变更影响，而非“无变更”。无效的显式基准会报错。

## CI 与失败诊断

主分支推送、PR 和手动运行使用同一工作流，核心检查固定 Ubuntu 24.04。独立 job 始终执行
完整公开历史扫描、Python 3.13 的 `python` 门禁，以及 Node 22 的 `console` 门禁。
Python 使用固定 uv 版本和 `uv sync --locked`；前端使用 `npm ci`。VTuber 使用单独的 Python 3.12 job，运行
`scripts/check_vtuber_compat.py` 检查插件发行资源、固定上游补丁、前端构建和替代语音/Agent
的真实接口，不取得模型凭据。相同 Python 3.12 job 通过 `scripts/check_live2d_generator.py` 安装独立生成器并用
合成分层素材导出真实 moc3；不调用绘图/拆层服务，不证明真实角色质量或 Cubism SDK 加载。
这些检查本地使用同一脚本，不能替代真实语音和模型验收。
Windows 2025 job 执行原生 Python 的存储与进程边界测试，以及桌面 TypeScript 单测、受控页面
Electron smoke。日常使用 `integrations/live2d_desktop/scripts/build-windows.ps1 -VerifyOnly`。
手动完整 CI 先在 Linux 冻结公开源码与上游前端载荷，再交给 Windows 构建 x64 安装包；
构建必须显式提供匹配的 `-RuntimePayload`。原生存储测试用 `--confcutdir=tests/unit` 独立运行，
不加载 Linux Eval 的开发测试宿主。上述 job 不调用真实模型或实体麦克风，也不证明完整
应用实机验收；相关证据另行记录。Windows 检查不包含在 Linux `full` 中。
前端检查通过 Vite runner 在内存加载配置，避免向 Harness 的只读依赖目录写入配置 bundle；
本地 `file:` 包在隔离验证中映射到候选源码，Node 使用调用方已选择的可执行文件。
`--keep-going` 收集各检查的失败，但最终退出码仍然失败。
主包支持 Python 3.12–3.13；3.12 验证发行构建、安装和 VTuber 兼容，不运行主程序全量回归。
详细设计见[持续集成基线](../../specs/ci-validation/spec.md)。

可选扩展由 `scripts/ci_scope.py` 选择：VTuber 或生成器及其专属测试的变更分别运行对应
Python 3.12 安装检查，并验证桌面；桌面目录变更只增加桌面验证。核心源码、共享依赖、
CI/构建脚本及共享插件契约运行全部扩展。普通文档、Console 和其他核心测试由核心三项覆盖。
未知基准、首次推送和未分类路径保守选择全部扩展；手动运行始终完整执行。
选择结果和原因记录在 Summary，核心三个 job 不依赖变更选择。

CI 为 Python 和 Console 设置 `--report-dir`，通过 `scripts/ci_summary.py` 展示失败检查、
失败用例及全部跳过原因。报告正文和原始 JUnit 留在 runner 私有临时目录，不自动上传。
本地也可将 `--report-dir` 交给摘要脚本查看；缺失或未完成的记录显示为不可用或 incomplete。
复制或比对 Git 元数据的测试夹具关闭本仓库的 `maintenance.auto`，避免后台维护锁引起随机失败；
不修改操作者的全局或生产仓库配置，也不以忽略锁文件或自动重跑取代修复。

依赖外部原生 Codex CLI、Windows 语义或选配 VTuber 上游源码的测试会明确报告跳过。
本地复核原生 Codex 权限时，可使用原生执行文件或官方 npm 入口；宿主按已安装包元数据
解析当前 Linux 架构的原生载荷后单文件挂载，未知包装脚本或缺失依赖会明确失败。
此类权限测试不调用模型。

Python job 安装并实际探测 Bubblewrap 与 ripgrep。Ubuntu 的用户命名空间限制由仅针对
`/usr/bin/bwrap` 的 AppArmor profile 处理，系统级限制保持开启。准备脚本只允许一次性
GitHub-hosted runner；不要在部署宿主上运行它。

PR 传入 base SHA，push 传入 before SHA，手动运行可填写 `docs_base`。分支保护应要求
始终执行的 `Public repository boundary`、`Python 3.13` 和 `Console`。
扩展 job 保持原名，但不相关变更可跳过；workflow 不会自动修改远端分支保护配置。
新增、移除或更名检查后需由维护者同步 GitHub 设置，workflow 文件本身不会修改保护规则。

本地复现使用对应 profile；需要完整日志时用仓库外私有目录：

```bash
.venv/bin/python scripts/check_repo.py python --keep-going --report-dir /tmp/agentstrata-checks
.venv/bin/python scripts/check_repo.py console --keep-going
bash scripts/check_secrets.sh history --keep-report
```

秘密扫描公开日志只列规则和数量。`--keep-report` 在失败时将详细报告保存到自动创建的
私有临时目录，不自动上传。历史扫描命中旧提交时，修改当前文件不会删除旧命中；
先私下核实，真实凭据按安全流程处理，合成夹具只允许[精确历史例外](../reference/publication.md#敏感扫描测试夹具)。

## 修改 Console

在 `console/web` 使用 `npm run dev` 开发，运行相关前端测试与 `npm run build`。复用 Arco、Query 和现有样式 tokens；检查桌面/窄屏、加载、空、错误、禁用、长文本和迟到响应。构建不代替可见界面验收。

## 依赖与验证环境

依赖只在 pyproject.toml 的既有分组修改，再运行 `scripts/sync_requirements.py` 和 `--check` 验证派生清单。构建工具必须有声明，不能依赖本机偶然安装的包。
Windows host 锁也由这两个命令生成或核对：使用已准备的宿主 uv 0.12.5，对冻结 uv.lock 执行无网络导出，固定包含 core 与 agent；工具缺失或锁过期时检查失败，不自动安装工具、更新锁或同步环境。修改依赖后先显式维护 uv.lock。导出的范围与生产接入状态见[交付依赖声明](../reference/delivery.md#原生交付的应用-host-依赖声明)。
构建宿主可通过绝对路径 `AGENTSTRATA_NATIVE_HOST_WHEELHOUSE` 提供完整固定 wheel 缓存；原生资源准备使用 `--host-wheelhouse`。构建器只审计和复制原字节，不下载或修复缺项；未提供完整缓存时只生成 selected 输入，不能称为离线依赖就绪。完整缓存还生成 source 绑定的安装映射和 v3 环境输入，配套清单缺失或篡改会被载荷读取拒绝。显式 v3 可供私有隔离暂存使用，生产 bootstrap 尚未调用它；范围、入口转换及前缀/ABI 缺口见[固定 host 输入](../reference/delivery.md#固定-windows-host-wheel-输入与离线缓存)与[安装映射](../reference/delivery.md#windows-wheel-安装映射与隔离入口)。

原生构建还输出 runtime requirements，明确保留插件/MCP 及加载缺项；接受读取器和更新日志的测试使用独立固定的合成证据及新私有数据库，不运行目标软件。实际接入和边界见[接受与更新日志](../reference/delivery.md#独立运行接受与更新日志)及[隔离镜像安装恢复](../reference/delivery.md#隔离-host-镜像安装与恢复)。隔离编排已连接原始输入、接受、原子选择和日志；Windows 测试真实文件/SQLite 与自有故障子进程，所有目标归档内容均保持数据状态，生产安装和启动未启用。

Python 输入锁 v2 的生产载荷须携带原始供应方元数据，默认从宿主 `~/.cache/agentstrata/python-inputs/<表 SHA256>.json` 读取；Python 构建 API 的 `python_metadata` 和资源准备的 `--python-metadata` 可显式提供原始字节。缓存仍须匹配来源锁的 SHA256/Git blob，不能充当来源权威，不自动下载或修复。整张表保留为构建输入，不纳入公开源码或编码绕过来源规则。匹配的旧资源可复用原字节；缺失或不符时拒绝，无已装环境回退。读取器重新认证并派生 host/plugin/MCP 三用途输入，构建回执绑定元数据和校验记录；旧 v1 载荷仍可读取。MCP 3.14 的基础归档和专用 wheel 元数据审计不开放 venv/加载配方。运行 `tests/unit/test_windows_python_inputs.py`、`test_python_plan.py` 和 `test_python_environment.py`，区分合成归档故障测试、真实元数据解析与目标软件运行，见[生产输入边界](../reference/delivery.md#独立运行接受与更新日志)。

Windows-MCP v2 策略另要求原始 Git/Python 归档，显式参数为 `--mcp-source-archive`、`--mcp-python-archive`，完整 wheel 输入使用 `--mcp-wheelhouse`；默认固定缓存、审计 schema 及边界见[MCP 输入](../reference/delivery.md#windows-mcp-原始归档与依赖闭包)。源码 graph、完整 wheel METADATA 闭包及实际运行分别取证，不从安装目录生成信任根。新版 bootstrap 的 `-InspectInputsOnly` 是不写目录的输入检查，正常 MCP 准备继续拒绝未完成的独立运行接受。运行 `tests/unit/test_git_source_tar.py`、`test_windows_mcp_inputs.py`、`test_tar_inventory.py`、`test_dependency_closure.py`、`test_wheel_plan.py`；Windows 专列只读 PowerShell 和原始数据落地测试，不执行归档中的软件。

MCP v3 要求完整原始 wheel 集合，并固定自有源码复制配方。运行 `test_mcp_project.py` 与
`test_windows_mcp_layout.py`，核对项目/依赖目的树、受限入口、旧格式专用范围、回执和缺项拒绝。
`core.mcp_materialize` 的目标只能是全新私有实验目录；它复制并回读布局数据，不创建可执行
前缀或执行项目。插件来源由 `windows-plugin-inputs.lock.json` 单独固定，运行
`test_windows_plugin_inputs.py` 验证完整需求、原哈希集合、固定 CPU URL、缓存和派生报告攻击；
`--plugin-wheelhouse` 仅提供原始字节，不能通过本地构建回执登记新 pin。新来源的供应方构建
仍在源码转换、项目与资产闭包未准入时拒绝，来源/数据候选与真实运行验收分别记录。

使用仓库外短临时目录，避免 Unix socket 路径过长和历史测试目录污染。Windows 原生语义只在对应平台验证。新增未暂存资源的打包检查使用项目既有候选索引机制，不暂存用户文件。

## 交付

说明改动、实际检查、跳过项与剩余风险，运行记录留在交付或 CI。不要向文档追加执行流水。交互式 AI 不暂存、提交、推送或发布；真实模型、QQ 与部署需独立验证。

文档维护方式见 [维护入口](../maintenance.md)。

## 源码入口

- [CI](../../.github/workflows/ci.yml)与[Release 验证](../../.github/workflows/release.yml)
- [检查入口](../../scripts/check_repo.py)与[runner 准备](../../scripts/setup_ci_linux.sh)
- [秘密扫描](../../scripts/check_secrets.sh)与[历史误报指纹](../../.gitleaksignore)
- [依赖更新](../../.github/dependabot.yml)
