# Linux / WSL 首次部署

本文是 AgentStrata 首次安装的唯一事实源。面向新用户的推荐入口是终端向导；安装完成后的
更新、启停、日志、QQ gateway、Console 和 Evaluation 操作统一见
[`operations.md`](operations.md)，只有正常入口无法恢复时才使用
[`../deploy/wsl/README_WSL.md`](../deploy/wsl/README_WSL.md) 的异常排障步骤。

AgentStrata 的生产运行面是 Linux 或 WSL2，不支持 Windows 原生部署。Console 是可选管理面，
不是 QQ 首次部署的依赖。

## 准备什么

### 支持的主机

| 项目 | 支持范围 |
| --- | --- |
| 发行版 | Ubuntu 22.04 / 24.04 / 26.04；Debian 11 / 12 / 13 |
| 架构 | amd64（x86_64）；arm64（aarch64） |
| WSL | WSL2，PID 1 为 systemd，当前用户的 systemd bus 可用 |
| Windows 原生 | 不支持 |

衍生发行版、未知版本和其他架构会拒绝自动安装。不要修改检测结果或跳过检查；先按脚本给出的
人工前置条件准备环境。WSL 源仓必须位于 Linux 文件系统，例如 `$HOME/AgentStrata`，不要把
仓库放在 `/mnt/c` 后运行部署。`\\wsl.localhost` 是 Windows 访问 WSL 文件的桥接地址，
不能作为 Windows 进程直接运行本项目的部署路径。

需要提前准备：

- Git 和可访问公开下载源的网络。
- 一个支持 OpenAI-compatible API 的模型 Base URL、模型 ID 和 API Key。
- 一个用于登录 NapCat 的 QQ 账号，以及 Owner 的稳定数字 QQ 号。
- 可选的稳定数字 QQ 群号；不填则仅按 Owner 用户准入。
- 执行系统包安装时可使用 `sudo`。向导会先显示精确变更，再请求确认。

向导使用固定校验和的用户级 Python，不覆盖系统 Python，不修改 shell profile，也不默认
安装 Console、桌面、测试、飞书或第三方 MCP/Skill 依赖。QQ 推荐路径不安装 Node 或
cc-connect；只有单独部署可选 Console 前端或 Feishu legacy edge 时才需要对应工具链。

## 三条命令开始

```bash
git clone https://github.com/Ling-ye/AgentStrata.git
cd AgentStrata
bash deploy/wsl/quickstart.sh
```

默认创建 `my-assistant-qq`，展示名为“我的助手”。向导会：

1. 只读检查发行版、架构、磁盘、网络、systemd、用户 bus 和 Docker。
2. 展示待下载的运行时、系统包、Docker 仓库与目标路径；任何特权变更都先确认。
3. 创建不含搜索、MCP、人格、Codex 和代码任务的通用 QQ/Native starter。
4. 在终端采集 LLM、机器人 QQ、Owner 和可选群号；API Key 使用隐藏输入，Gateway token
   与 OneBot token 分别由本机生成，二者不能复用，秘密不会出现在命令行参数、JSON 或部署摘要中。
5. 启动 NapCat bootstrap，并只在当前交互式终端显示一次本地 WebUI 登录链接。
6. 等待你在浏览器扫码并回到终端确认，然后执行 token 同步和经过认证的 OneBot 状态检查。
7. 只调用一次统一实例更新入口，最后输出有界的检查结果与修复命令。

浏览器只用于本机 NapCat WebUI 扫码；整个流程不要求 Console。

### 预览、自定义和恢复

只读预览不会提示秘密或写入文件：

```bash
bash deploy/wsl/quickstart.sh --dry-run
```

指定公开 Bot ID 和展示名：

```bash
bash deploy/wsl/quickstart.sh \
  --bot-id my-assistant-qq \
  --display-name "我的助手"
```

WSL systemd、Docker group 或扫码登录需要暂停时，按输出完成动作后从实际机器状态恢复：

```bash
bash deploy/wsl/quickstart.sh --resume
```

向导不维护第二份流程状态文件。`--resume` 会检查 BotSpec、私有 env、Docker、NapCat 和
systemd 的当前状态；空的秘密输入表示保留现有值。它只接受由引导流程管理的 QQ/Native
starter，遇到 Codex、`dev.code_tasks` 或其他高级配置会拒绝覆盖。

如果 Docker 已由操作者或 Docker Desktop WSL 集成提供，但当前不可用，可禁止自动安装：

```bash
bash deploy/wsl/quickstart.sh --no-install-docker
```

退出码固定为：

| 退出码 | 含义 |
| --- | --- |
| `0` | 本地部署边界 ready |
| `1` | 部署失败；按错误修复后重试 |
| `2` | 参数或使用方式错误 |
| `3` | needs_user_action；需要重启 WSL、重新登录终端、扫码或显式确认 |

非交互 stdin 不能确认系统变更，也不会输出带 WebUI token 的链接。

## WSL systemd

向导不会尝试在当前 shell 中伪造 systemd 修复。若 PID 1 不是 systemd，按 Microsoft
官方 WSL systemd 文档在 `/etc/wsl.conf` 中启用：

```ini
[boot]
systemd=true
```

随后从 Windows PowerShell 执行 `wsl --shutdown`，重新打开 WSL，验证：

```bash
ps -p 1 -o comm=
systemctl --user is-system-running
```

再运行 `bash deploy/wsl/quickstart.sh --resume`。systemd user unit 被 enable 只表示发行版
启动后实例可自启，不代表 Windows 冷启动会主动唤醒尚未运行的 WSL 发行版。

## Docker 权限边界

可用的 `docker info`（包括 Docker Desktop WSL 集成）会被直接复用。Docker 缺失时，向导
只在支持的发行版上使用 Docker 官方 Ubuntu 或 Debian 安装步骤配置 apt 仓库，
安装 Docker Engine、CLI、containerd、Buildx 和 Compose 插件；不执行 convenience script。

- apt source、keyring 和软件包会在执行前列出。
- 发现 `docker.io`、旧 Compose、`podman-docker`、独立 containerd/runc 等冲突时，
  必须单独确认精确移除列表；脚本不 purge、不删除镜像、容器、volume 或 Docker 数据目录。
- 加入 `docker` group 近似授予 root 权限，因此需要独立确认。新组权限未生效时退出
  `needs_user_action`，重新打开终端后使用 `--resume`；不要修改 Docker socket 权限。
- OneBot `3001` 和 NapCat WebUI `6099` 始终只发布到 `127.0.0.1`。

## QQ 登录与准入

引导流程的固定顺序是：

```text
bootstrap -> 本地 WebUI 登录 -> sync-token -> authenticated status -> update_instance
```

`QQ_ACCESS_TOKEN` 是由主机生成的 32–128 位 URL-safe 强 token。NapCat WebUI 管理 token
是另一凭据，只用于 localhost 管理面板；带 token 的 URL 只显示在可信交互式终端，不写入
普通日志、配置或摘要。QQ Owner 与群准入只使用稳定数字 ID，昵称不参与授权；默认
`QQ_ALLOW_FROM` 只包含 Owner，群列表默认为空。

NapCat 是用户独立维护的外部 OneBot provider。每个 Bot 的 systemd unit 直接以前台
`python -m chatcopilot run --bot <deployed-bot.yaml>` 运行唯一 Gateway host，Gateway 直接
连接 `CHATCOPILOT_QQ_ONEBOT_WS_URL`。ACP 只是可选的本地协议 edge，通过 Gateway 的认证协议
接入；它不是 QQ transport，也不拥有平台身份、权限策略或运行生命周期。

空/弱 token、非回环 URL、未登录 NapCat、认证动作失败或配置不完整都会在 Agent 部署前
失败关闭。QQ/NapCat 日常启停和修复命令见
[`operations.md#qq--napcat`](operations.md#qq--napcat)。

## 如何理解“ready”

最终结果使用 `agentstrata-deployment-check/v1`，总状态是 `ready`、
`needs_user_action` 或 `failed`，每项检查都提供有界说明和修复动作。

`ready` 只证明当前机器上的配置、systemd Gateway MainPID、NapCat 和经过认证的只读 OneBot
边界就绪。它不证明真实 QQ 客户端展示、模型调用或用户已读。默认流程不会消耗模型额度，
也不会发送 QQ 消息，因此必须保留：

```text
llm_live_call=not_tested
qq_external_send=not_tested
qq_inbound_agent_roundtrip=not_tested
```

最后请自行向机器人发送普通私聊，或在获准群内明确 @ 机器人，确认真实的
“QQ 用户 -> 外部 NapCat/OneBot -> AgentStrata Gateway -> Agent/模型 -> QQ 回复”链路。自动化、本地
合成 QQ flow、只读平台探针或机器人主动发送都不能替代独立账号的入站往返证据。

维护者可在不启动 AgentStrata、NapCat、systemd 或 Gateway 的前提下，用一次性容器复核
六个受支持发行版的锁定 Python 运行时和 BotSpec smoke；该检查会拉取容器和运行时下载物到本机
Docker 缓存，但不会修改宿主 Python 或部署实例：

```bash
bash scripts/verify_guided_runtime_matrix.sh --all
```

脚本还会读取固定 NapCat digest 的 manifest，并要求同时包含 `linux/amd64` 与 `linux/arm64`。
没有 ARM runner 时，这只证明发布的多架构清单，不等于 ARM 运行时端到端验证。

## 配置、数据与 Secret

| 类型 | 位置 | 所有者 |
| --- | --- | --- |
| 源 BotSpec | `bots/<id>/bot.yaml` | Git 源仓 |
| 私有 env | `bots/<id>/local.env` | 操作者；mode `0600`；不进 Git |
| 实例副本 | BotSpec 的 `deploy.wsl_home` | 部署脚本 |
| 运行时 env | `~/.chatcopilot-<id>.env` | `provision-env` |
| workspace | `~/chatcopilot-workspaces/<id>` | `provision-env` 建目录；运行时使用 |
| 私有 Wiki | BotSpec `context.wiki.root_env` 指向的目录 | `provision-env` 建目录；Agent runtime 使用 |
| 日志 | systemd journal；可选 `~/chatcopilot-logs/<id>/gateway` | Gateway runtime |
| Gateway 状态 | BotSpec `gateway.state_root_env` 指向的私有目录 | `provision-env` 建目录；Gateway 写状态 |

`local.env` 是机器私有事实源。配置写入先在内存中构造并验证候选，只修改受管字段，保留
未知键和注释，再用同目录 mode `0600` 临时文件原子替换；符号链接、非普通文件、错误 owner
和多硬链接会被拒绝。`provision-env` 不 source 或执行文件，只解析简单赋值，并且只确定性
展开值开头的 `~`、`$HOME` 或 `${HOME}`。对 Gateway 实例，实际执行还会创建并复核
workspace、Gateway 状态目录和已启用的私有 Wiki：新目录使用 mode `0700`，路径中的符号链接、
错误 owner、可被 group/world 写入的 workspace/Wiki，以及非 `0700` 的 Gateway 状态目录都会被拒绝。
`--dry-run` 不创建这些目录或任何文件。不要手工修改实例副本或运行时 env。

BotSpec、示例和文档不得包含真实 API Key、平台 token、账号/群号、私有端点或机器绝对路径。
第三方 MCP/Skill 不会由引导流程自动下载、安装或启用。

## 高级实例与可选 Console

`lingye-copilot-qq` 是展示 Codex、搜索、MCP、私有 Wiki 和隔离代码任务的高级内置实例，
不是新手向导的默认机器人。高级 BotSpec、Codex main/worker 认证、共享 Docker 服务、Console
与 Evaluation 的安装和维护见 [`operations.md`](operations.md)；不要把高级实例的
`local.env.example` 复制到 starter。

可选 Console 与终端命令消费同一份 BotSpec provisioning plan 和安全 env writer。Console
可以填写配置并查看状态，但 QQ bootstrap/扫码/systemd 首次部署仍交回：

```bash
bash deploy/wsl/quickstart.sh --bot-id <id> --resume
```

部署入口内部只调用一次 `update_instance.sh`；不要在其后再手工调用 register/start。安装后的
更新、状态和日志统一回到 [`operations.md`](operations.md)。
