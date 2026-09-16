# 实例运维

管理已安装实例：先定位准确实例，再执行状态、配置、更新或重启。首次安装见 [部署指南](deployment.md)。

## 使用约定

除标明 PowerShell 的片段外，命令都在 Linux/WSL 的 **AgentStrata 源仓根目录**
执行。下文的 `<id>` 是 BotSpec 的实例 ID，例如 `lingye-copilot-qq`。

```bash
cd <path-to-AgentStrata>
```

先确认目标实例再执行写操作：

```bash
python -m chatcopilot bot list
python -m chatcopilot bot doctor --bot bots/<id>/bot.yaml
```

`local.env`、平台 token 和模型凭据不得写入命令输出、BotSpec 或 Git。Git 提交与
推送也不属于任何自动更新或发布动作。

## 部署完成后的起点

首次安装只使用 [`deployment.md`](deployment.md) 说明的引导入口；本手册不复制安装、
Docker 配置或 NapCat 扫码顺序。引导流程完成后，先保存输出中的 Bot ID，再确认实例与
Gateway 与外部 OneBot provider 的本地状态：

```bash
python -m chatcopilot bot doctor --bot bots/<id>/bot.yaml --json
bash console/scripts/ctl.sh status <id>
bash deploy/wsl/qq_gateway.sh status --instance <id>
```

新手部署默认不安装 Console。需要 Console、Evaluation 或高级内置实例时，先阅读
[`deployment.md#高级实例与可选-console`](deployment.md) 的边界，
再使用本手册对应章节的明确命令。

## Bot 实例

### 配置与体检

```bash
python -m chatcopilot botspec validate bots/<id>/bot.yaml
python -m chatcopilot bot doctor --bot bots/<id>/bot.yaml
python -m chatcopilot bot provision-env --bot bots/<id>/bot.yaml --dry-run
```

修改 BotSpec、prompt、MCP 绑定、`local.env`、Python 代码或依赖后：

```bash
bash deploy/wsl/update_instance.sh --instance <id> --dry-run
bash deploy/wsl/update_instance.sh --instance <id>
```

更新默认使用快速路径；只有实例 venv 缺失或依赖、安装脚本发生变化时才完整
bootstrap。命令失败会停止后续阶段，不提供自动回滚。首次验收成功并需要开机自启时：

```bash
bash deploy/wsl/update_instance.sh --instance <id> --enable
```

### 生命周期

```bash
bash console/systemd/register.sh --enable <id>
bash console/scripts/ctl.sh start <id>
bash console/scripts/ctl.sh stop <id>
bash console/scripts/ctl.sh restart <id>
bash console/scripts/ctl.sh status <id>
```

直接检查 systemd：

```bash
systemctl --user is-active chatcopilot@<id>.service
systemctl --user status chatcopilot@<id>.service --no-pager -l
journalctl --user -u chatcopilot@<id>.service -n 120 --no-page
```

只有 BotSpec 启用 `dev.code_tasks` 时才有独立 worker；通用 starter 的该项为
`not_applicable`：

```bash
systemctl --user status chatcopilot-code-worker@<id>.service --no-pager -l
journalctl --user -u chatcopilot-code-worker@<id>.service -n 120 --no-page
```

### 聊天内 Owner 运维指令

用户正文去除前导空白后，以 ASCII `/name` 开头且后接空白或正文结束的消息统一视为
斜杠指令；`/tmp/report.txt` 一类绝对路径、URL、`//name` 和正文中间的 slash 不属于指令。
Gateway QQ 只接受本轮认证 `Principal`、准入和身份激活共同确认的可信 Owner；Feishu
legacy edge 继续使用其经过 transport 证明的 Owner。群名单命中、昵称、历史 Owner 回合或
共享 session 都不会授予权限。Application 在准入与身份激活后、资源 materialization 以及
Session、Agent、模型、工具副作用前执行统一门禁；非 Owner 的斜杠消息确定性拒绝。

当前内置运维指令：

| 指令 | 行为 |
| --- | --- |
| `/help` | 按当前 Bot 配置和运行能力列出实际可用的斜杠指令；不会把未启用能力列为可用。 |
| `/state` | 只读查看当前会话与当前 Bot systemd 实例的有界、脱敏状态。 |
| `/restart` | 请求只重启当前 Bot systemd unit；不接受实例或 unit 参数。 |

`/state` 的会话部分包括当前 backend、模型 profile、assistant mode 和 debug 状态等已有
安全字段；实例部分包括当前 Bot 的 load/active/substate。systemd 不可达、unit 未注册或结果
有歧义时显示未知或有界错误，不能因为机器人仍能回复就推断实例整体健康。输出不包含凭据、
环境变量、机器路径、完整准入名单、原始平台身份、其他 actor 会话或内部 traceback。

`/restart` 只执行进程级重启，不清理 workspace 文件、conversation journal、受保护 memory、
persona、backend resume state 或持久化 task/job 记录。对 Gateway Bot，这会重启当前 Gateway
systemd unit，但不会启动、停止或重启外部 NapCat/OneBot provider。它不保证
其他正在执行的进程内回合跨重启继续运行。机器人先验证当前 unit 正在运行且 user systemd 与
detached scheduler 可用，再回复“已接受重启请求”；只有该回复已送达且当前指令 task 的终态已
持久化，宿主才通过 Bot service cgroup 外的 systemd transient unit 延迟重启当前绑定实例。同一
实例使用稳定 transient unit 名，第二个待执行的聊天重启会冲突而不会重复排队；Console 或人工
systemd 操作仍是独立控制入口，竞态会表现为最终调度或状态证明失败。`nohup`、`setsid` 和进程内
后台任务不作为降级路径；投递、持久化、systemd、冲突或调度任一步骤失败都不能声称重启完成。
如果 timer 注册成功后 scheduled 回执落盘失败，宿主会 best-effort 停止 timer 与 worker；但即使
目标进程 generation 尚未变化，也不能排除 systemd manager 已经排队 restart，因此回复只会要求
从宿主核验，绝不声称“已撤销”。

这些聊天指令与本节前面的宿主命令操作同一个 Bot 实例，但不替代断连时的宿主排障入口。
外部 OneBot provider 的状态和重启属于下文独立操作，不能用 `/restart` 代替。完整契约见
[`../docs/reference/identity-resources.md`](../reference/identity-resources.md)。

## 运维控制台

控制台默认地址为 `http://localhost:8910`。`deploy_console.sh --status`
同时验证 Console HTTP 和独立 Evaluation service。`--restart-only` 只重启
Console。`--update-only` 原子获取 Evaluation maintenance lease；service 在同一
创建锁内证明空闲并持久化 marker，之后直到 Console 与 Evaluation 都重启并通过
健康检查前拒绝新建 Evaluation。活动记录、未知 lifecycle、遗留 claim、身份不明
worker 或 service 不可达都会在构建和重启前失败关闭。

不带参数的 `bash deploy/wsl/deploy_console.sh` 是日常全量机器更新入口：它先安装或修复
Console，再按 `bots/*/bot.yaml` 的 `deploy.instance_id` 更新并重启全部机器人。实例按稳定
路径顺序执行；某个实例失败时继续其余实例，最后汇总失败并返回非零。仅修复 Console 时
显式加 `--skip-bots`；页面“更新控制台”继续使用 `--update-only`，不会隐式重启机器人。

控制台页面中的“更新控制台”只通过 `systemd-run --user` 创建独立 transient
unit，再由该 unit 执行同一个 `deploy_console.sh --update-only`。`setsid` 和
`nohup` 不会脱离 `chatcopilot-console.service` 的 cgroup，因此不作为降级路径。
若 transient unit 无法创建，接口会在更新脚本运行和 maintenance lease 获取前返回
明确错误；此时在 WSL 终端手工执行下面的 `--update-only` 命令。

```bash
bash deploy/wsl/deploy_console.sh
bash deploy/wsl/deploy_console.sh --status
bash deploy/wsl/deploy_console.sh --update-only
bash deploy/wsl/deploy_console.sh --restart-only
```

```bash
systemctl --user status chatcopilot-console --no-pager -l
journalctl --user -u chatcopilot-console -f
```

Evaluation 后端使用独立 user service 和同 UID Unix socket：

```bash
python -m chatcopilot.evals.service health --json
python -m chatcopilot.evals.service health --require-idle
python -m chatcopilot.evals.service maintenance status
systemctl --user status chatcopilot-evaluation.service --no-pager -l
journalctl --user -u chatcopilot-evaluation.service -f
```

`health --require-idle` 只用于诊断，单次检查不能替代代码更新所需的原子
maintenance lease。只重启 Evaluation service 且不改变运行代码时，可以显式
重启并立即验证 UDS health；存活 worker 会由新 service 重新观察：

```bash
systemctl --user restart chatcopilot-evaluation.service
python -m chatcopilot.evals.service health --json
```

受管 worker 不依赖 Console 的 lifespan、cgroup 或 stdout pipe。重启 Console
不会向 worker 发送信号；不涉及代码更新的 Evaluation service 重启会用 claim、
state 和 PID argv 重新观察身份匹配的存活 worker。PID 存在但身份无法证明时，
服务保持 fail closed，不取消、不释放 claim 且不将记录误判为终态。应用代码
更新必须使用 `deploy_console.sh --update-only`，不能用“检查一次 idle 后手工更新”
替代 maintenance lease。

若更新进程异常退出，trap 会优先释放租约；若此时 Evaluation service 也不可达，
marker 会保留并继续拒绝创建。恢复 service 后先读取租约，再使用同一 ID 释放：

```bash
python -m chatcopilot.evals.service maintenance status
python -m chatcopilot.evals.service maintenance leave --lease-id <lease-id>
```

控制台页面、API、任务可观测和评测中心行为见 [`console.md`](../reference/console.md)。

## 兼容名称

以下名称仍是可调用或持久化契约，不代表当前产品品牌：

| 兼容名称 | 当前用途 |
| --- | --- |
| `python -m chatcopilot` | 与 `agentstrata` 指向同一 CLI |
| `CHATCOPILOT_*` | 已部署环境的稳定变量前缀 |
| `chatcopilot@<id>.service` | Bot 实例 systemd unit |
| `chatcopilot-console.service` | 控制台 systemd unit |
| `chatcopilot-evaluation.service` | Evaluation application 与 managed worker supervisor systemd unit |
| `~/ChatCopilot*` | 既有部署和数据路径；新文档不把它当作项目名称 |
