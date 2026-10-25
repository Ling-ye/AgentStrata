# 开发与验证

从可编辑源码环境开始；机器人部署是独立任务。先选择本次领域，再读取关联正文和源码入口。

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

主分支推送、PR 和手动运行使用同一工作流，固定 Ubuntu 24.04。独立 job 分别执行
完整公开历史扫描、Python 3.13 的 `python` 门禁，以及 Node 22 的 `console` 门禁。
Python 使用固定 uv 版本和 `uv sync --locked`；前端使用 `npm ci`。VTuber 使用单独的 Python 3.12 job，运行
`scripts/check_vtuber_compat.py` 检查插件发行资源、固定上游补丁、前端构建和替代语音/Agent
的真实接口，不取得模型凭据。相同 Python 3.12 job 通过 `scripts/check_live2d_generator.py` 安装独立生成器并用
合成分层素材导出真实 moc3；不调用绘图/拆层服务，不证明真实角色质量或 Cubism SDK 加载。
这些检查本地使用同一脚本，不能替代真实语音和模型验收。
Windows 2025 job 执行桌面壳的 TypeScript 单测、受控页面 Electron smoke 和 x64 打包，
使用 `integrations/live2d_desktop/scripts/build-windows.ps1`。该 job 不启动真实 WSL、
模型或麦克风；硬件与实际后台联调独立记录。桌面壳测试不包含在 Linux `full` 中。
前端检查通过 Vite runner 在内存加载配置，避免向 Harness 的只读依赖目录写入配置 bundle；
本地 `file:` 包在隔离验证中映射到候选源码，Node 使用调用方已选择的可执行文件。
`--keep-going` 收集各检查的失败，但最终退出码仍然失败。
主包支持 Python 3.12–3.13；3.12 验证发行构建、安装和 VTuber 兼容，不运行主程序全量回归。
详细设计见[持续集成基线](../../specs/ci-validation/spec.md)。

依赖外部原生 Codex CLI、Windows 语义或选配 VTuber 上游源码的测试会明确报告跳过。
本地复核原生 Codex 权限时，将已安装的原生可执行文件目录放在 `PATH` 首位；
npm 启动脚本不是可单文件挂载的原生二进制。此类权限测试不调用模型。

Python job 安装并实际探测 Bubblewrap 与 ripgrep。Ubuntu 的用户命名空间限制由仅针对
`/usr/bin/bwrap` 的 AppArmor profile 处理，系统级限制保持开启。准备脚本只允许一次性
GitHub-hosted runner；不要在部署宿主上运行它。

PR 传入 base SHA，push 传入 before SHA，手动运行可填写 `docs_base`。分支保护应要求
`Public repository boundary`、`Python 3.13`、`Console` 和 `VTuber Python 3.12`。
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

使用仓库外短临时目录，避免 Unix socket 路径过长和历史测试目录污染。Windows 原生语义只在对应平台验证。新增未暂存资源的打包检查使用项目既有候选索引机制，不暂存用户文件。

## 交付

说明改动、实际检查、跳过项与剩余风险，运行记录留在交付或 CI。不要向文档追加执行流水。交互式 AI 不暂存、提交、推送或发布；真实模型、QQ 与部署需独立验证。

文档维护方式见 [维护入口](../maintenance.md)。

## 源码入口

- [CI](../../.github/workflows/ci.yml)与[Release 验证](../../.github/workflows/release.yml)
- [检查入口](../../scripts/check_repo.py)与[runner 准备](../../scripts/setup_ci_linux.sh)
- [秘密扫描](../../scripts/check_secrets.sh)与[历史误报指纹](../../.gitleaksignore)
- [依赖更新](../../.github/dependabot.yml)
