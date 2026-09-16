# 开发与验证

从可编辑源码环境开始；机器人部署是独立任务。先选择本次领域，再读取关联正文和源码入口。

## 准备环境

默认 WSL/Linux。安装仓库声明的开发依赖，使用同一个解释器运行检查：

```bash
uv sync --frozen --extra agent --extra acp --extra dev
uv run agentstrata --help
uv run agentstrata botspec validate bots/lingye-copilot-qq/bot.yaml
```

需要 WSL 系统工具或修复源仓环境时使用 `bash deploy/wsl/install_wsl_env.sh`，需安装 Console 时显式加 `--with-console`。前端沿用项目声明的 Node 工具链。

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

`fast` 按 tests/fast.txt 的完整文件清单执行；`full` 还检查依赖、发行资源、完整 Python 回归和 Console 构建。只要新增变化、失败或具体风险没有出现，已通过的检查不重复执行。

`docs/fast/full` 默认收集本 checkout 的已暂存、未暂存和未忽略的新文件，输出关联文档。
比较指定提交或分支时使用 `--docs-base`；删除与重命名同样参与关联。提示只要求核对，
没有事实变化时无需修改文档。

```bash
.venv/bin/python scripts/check_repo.py docs --docs-base HEAD~1
.venv/bin/python scripts/check_docs.py --changed-path src/chatcopilot/harness/models.py
```

检查入口也接受可重复的 `--changed-path`。隔离候选的清单由调用方提供，不从操作者仓库补取；
没有提供时会明确显示未评估变更影响，而非“无变更”。无效的显式基准会报错。
CI 按 PR 的 base SHA 或 push 的 before SHA 比较；手动运行可填写 `docs_base`，留空只分析工作区。

## 修改 Console

在 `console/web` 使用 `npm run dev` 开发，运行相关前端测试与 `npm run build`。复用 Arco、Query 和现有样式 tokens；检查桌面/窄屏、加载、空、错误、禁用、长文本和迟到响应。构建不代替可见界面验收。

## 依赖与验证环境

依赖只在 pyproject.toml 的既有分组修改，再运行 `scripts/sync_requirements.py` 和 `--check` 验证派生清单。构建工具必须有声明，不能依赖本机偶然安装的包。

使用仓库外短临时目录，避免 Unix socket 路径过长和历史测试目录污染。Windows 原生语义只在对应平台验证。新增未暂存资源的打包检查使用项目既有候选索引机制，不暂存用户文件。

## 交付

说明改动、实际检查、跳过项与剩余风险，运行记录留在交付或 CI。不要向文档追加执行流水。交互式 AI 不暂存、提交、推送或发布；真实模型、QQ 与部署需独立验证。

文档维护方式见 [维护入口](../maintenance.md)。
