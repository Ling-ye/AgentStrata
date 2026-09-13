# 开发入口与验证约定

按任务从 [AGENTS.md](../AGENTS.md) 进入本页；部署运维的完整操作以 [operations.md](operations.md) 为准。

## 按开发任务定位命令

| 任务 | 命令事实源 |
| --- | --- |
| 校验 BotSpec、检查实例或生命周期 | [Bot 实例](operations.md#bot-实例) |
| 配置 Console、启动或更新服务 | [运维控制台](operations.md#运维控制台) |
| QQ 登录、token 同步、OneBot 诊断 | [QQ / NapCat](operations.md#qq--napcat) |
| 准备数据、运行测评、比较结果 | [Evaluation](operations.md#evaluation) |
| 准备或诊断 Harness 任务 | [单 Case AI Harness](operations.md#单-case-ai-harness) |
| 共享 Docker 服务与搜索 provider | [共享 Docker 服务](operations.md#共享-docker-服务) |

QQ Gateway 不调用 Feishu legacy 的 render-cc-config/render-session-env 流程。
首次部署使用 [引导式安装](deployment.md#三条命令开始)；维护者的源仓安装入口见本页末尾。

## 快速验证

开发过程中先跑改动模块及直接调用方的定向测试；普通改动完成后使用 `fast`，
按 `tests/fast.txt` 的完整文件清单执行约 1000 项日常回归和全部静态检查。
清单之外的功能有改动时补跑对应测试，不要求每次小修改执行全量。
跨层、部署、依赖、打包或广泛改动使用 `full`；CI 保留 Python 3.10/3.13 全量覆盖。
裸 `pytest` 仍是全量发现。清单按职责维护，不按数量截断参数矩阵。

```bash
# Public-boundary checks for the current change
python scripts/check_public_repo.py
bash scripts/check_secrets.sh changes

# Full-history gates before public visibility or a Release
python scripts/check_public_repo.py --history
bash scripts/check_secrets.sh history

# 日常入口；fast 包含全部静态检查和约 1000 项精选回归
.venv/bin/python scripts/check_repo.py fast

# Component Catalog 精确投影与跨 surface 一致性
.venv/bin/python scripts/check_component_catalog.py --json

# 全量入口额外执行 pip check、wheel 构建不变性、完整 pytest 与控制台生产构建
.venv/bin/python scripts/check_repo.py full

# 文档/配置/轻量代码改动
git diff --check
python -m compileall -q src bots tests
python -m chatcopilot botspec validate bots/lingye-copilot-qq/bot.yaml

# QQ 图片 / MCP / 搜索
python -m pytest tests/unit -q -k "qq or image or mcp or search"

# 控制台后端
python -m pytest tests/unit -q -k "console or eval"

# 前端
cd console/web && npm run build
```

构建工具必须由 `pyproject.toml[project.optional-dependencies].dev` 声明；禁止依赖虚拟环境中碰巧存在的未声明工具。

Python 依赖只能在 `pyproject.toml` 的现有 `agent` / `acp` / `console` / `evaluation` / `desktop` / `dev` 分组修改；随后运行 `scripts/sync_requirements.py` 更新兼容 requirements，并用 `--check` 验证无漂移。远端状态参与写入位置或去重判断时必须失败关闭，禁止把读取异常降级为空表、零行或空去重集。

Windows 上全量 pytest 可能被旧临时目录 ACL 干扰；优先 targeted tests，必要时设置可写 `TEMP` / `TMP` 和 `--basetemp`。WSL 的 `check_repo.py` 会把子进程 `TMPDIR` / `TEMP` / `TMP` 统一到显式 `TMPDIR` 或 `/tmp`，避免继承 Windows 临时目录。

## WSL 源仓环境安装入口

首次在 WSL 源仓直接安装、配置和运行项目时使用：

    bash deploy/wsl/install_wsl_env.sh

需要同时安装/修复控制台服务时加 --with-console；不要把 secret 写进脚本，机器私有值仍放 bots/<id>/local.env。
