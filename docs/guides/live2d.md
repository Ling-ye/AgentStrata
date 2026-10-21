# Live2D 交互客户端

安装 `open-llm-vtuber` 插件后，AgentStrata 会准备固定版本的 Open-LLM-VTuber、浏览器页面、
Mao 模型、SenseVoice INT8 CPU 识别和 Edge TTS 晓晓语音，并启动独立 Gateway。
AgentStrata 保存对话、权限与记忆；客户端负责语音、角色及播放。
职责见[运行链](../reference/runtime.md#独立交互客户端)。

## 安装

前提为 Linux/WSL x86_64、可用的 systemd 用户服务、Git、能访问固定下载源的网络，
以及已安装官方 AgentStrata 发行包和所选 Agent runtime 的依赖。使用 Codex 时需要其
原生 Linux 可执行文件及已认证的模型连接；模型配置见[配置参考](../reference/configuration.md#llm)。
认证由已有连接的 `env_file` 引用，部署不会把终端里的临时模型密钥复制进客户端。

```bash
agentstrata plugin list
agentstrata plugin install open-llm-vtuber
```

交互终端会列出已有模型绑定供选择。自动脚本应明确指定：

```bash
agentstrata plugin install open-llm-vtuber --model-binding chat
```

默认使用 Codex runtime。其他已配置的执行方式可用 `--option runtime=native` 或
`--option runtime=langgraph`；ChatGPT 订阅认证仍要求 Codex。插件用独立 Python 3.12
环境运行，上游依赖不会装入主程序环境。插件 Python 声明为 `>=3.11,<3.13`。

安装器从匹配的 GitHub Release 取得主包与插件 wheel，并校验 SHA256SUMS 和包身份。
开发版或未发布的候选使用 `--artifacts /path/to/release-bundle --source-url URL`；目录必须
包含两份 wheel 和 SHA256SUMS，源码 URL 应提供对应候选的完整源码及构建材料。
这条路径不表示候选已发布。正式发行自动使用匹配 tag 的完整仓库源码入口。

默认端口为页面 `12393`、Gateway `18791`。端口被占用时安装失败并报告端口，显式选择：

```bash
agentstrata plugin install open-llm-vtuber --model-binding chat \
  --option port=12493 --option gateway_port=18891
```

安装完成输出页面地址；浏览器通过当前页面来源连接后端。客户端先启动，再启动 Gateway，
避免 MCP 发现与对话连接互相等待。`--no-start` 只准备程序与服务，稍后可显式启动。

## 配置与生命周期

默认目录为 `$XDG_DATA_HOME/agentstrata/plugins`，未设置时为
`~/.local/share/agentstrata/plugins`；可用 `AGENTSTRATA_PLUGINS_ROOT` 或 `--root` 覆盖。
同一插件只维护一个受管安装，切换根目录不会接管另一个安装拥有的 systemd unit。

| 安装目录内路径 | 内容与保留方式 |
| --- | --- |
| `open-llm-vtuber/runtime/` | 独立 Python、第三方源码、工具及构建产物；卸载删除 |
| `open-llm-vtuber/config/` | BotSpec、适配配置、ASR/TTS 配置和私有环境文件；卸载保留 |
| `open-llm-vtuber/data/` | 会话、记忆、独立工作区、模型与下载缓存；卸载保留 |
| `open-llm-vtuber/logs/` | 安装阶段及客户端日志；卸载保留 |
| `open-llm-vtuber/installation.json` | 版本、阶段、服务及精确来源记录 |

```bash
agentstrata plugin doctor open-llm-vtuber
agentstrata plugin stop open-llm-vtuber
agentstrata plugin start open-llm-vtuber
agentstrata plugin uninstall open-llm-vtuber
```

同版本重复安装检查已有结果，保留配置；不会把新的命令行选项覆盖到现有配置。
更换版本前先停止，再按当前清单重装；需要重建损坏程序时先停止、卸载再安装。
没有在线升级、多版本保留或自动回滚。失败阶段写入安装记录，详细原因在私有阶段日志，
修正后可重新执行安装。既有手工部署不会被迁移、覆盖或删除。

修改模型、动作、端口或语音配置前先停止服务，维护 `config/` 中对应配置的一致性再启动。

## Console 任务与配置查看

插件 `0.2.1` 起，安装成功会登记 `plugin:open-llm-vtuber:main`。Console 的机器人实例
列表刷新后即可选择它，查看历史任务、模型与工具调用、上下文和只读分层配置。
启停及修改配置仍使用插件管理器。未登记的 `0.2.0` 安装先停止，再按新版安装流程重装；
现有配置、凭据、客户端历史、Agent 会话、工作区及模型继续保留。

Console 和 CLI 默认读取同一个插件目录。若安装使用了 `--root`，Console 进程也必须
设置同值的 `AGENTSTRATA_PLUGINS_ROOT`。systemd 部署可在用户服务 drop-in 中配置：

```ini
[Service]
Environment=AGENTSTRATA_PLUGINS_ROOT=/absolute/plugins-root
```

保存后执行 `systemctl --user daemon-reload` 并重启 Console。只读取当前用户显式配置的
一个根目录，不扫描其他用户或任意安装目录。页面只显示已经采集的 Agent 执行；ASR、
TTS、口型与播放过程仍由客户端管理，未新增到任务流。

## 凭据与动作边界

默认两个 token 独立生成，保存在私有 `runtime.env`，浏览器不持有凭据。
[适配配置模板](../../integrations/open_llm_vtuber/agentstrata_vtuber/resources/config.example.json)只登记
实际模型中的表达式与 motion，不接受任意文件路径或其他实例目标。

## 交互与诊断

浏览器先发送文字，再点击麦克风说话；识别到一段话后停麦，不自动持续监听。
完整回复由客户端合成语音并驱动口型。默认 Edge TTS 需要联网。
`/source` 显示源码入口，首次回复中的源码提示完整保留；显示历史不会回灌为模型记忆。

停止按钮结束本地播放并请求取消仍在运行的 Agent。`/resume` 查询原请求，`/stop` 请求
停止；结果未知时不提交另一个 Agent 请求。历史切换要求当前回合已完成或停止。
删除客户端历史只影响显示记录，不删除 AgentStrata 的长期记忆和运行证据。

- `doctor` 分别检查安装资源、Gateway 握手、MCP 和源码入口可获取性；浏览器尚未连接不使安装失败。
- 语音失败仍保留文字。停用头像 MCP 时，在适配 JSON 删除 `mcp_token_env`，并停用 BotSpec 对应 MCP server。
- `browser_not_connected` 表示未连接；`failed` 表示明确失败；`unknown` 表示缺少完整回执，重复调用可能重复播放。
- motion 的 `executed` 表示 SDK 已开始动作，不代表循环动作播放结束。播放记录不会改变 Gateway run 终态。
- 固定上游的整页刷新会触发旧 Live2D 视图销毁；其背景对象未初始化时，`LAppView.release`
  可能报告空对象异常。需分别观察旧页销毁和新页的连接、模型就绪状态；此问题归上游 SDK，
  插件不将旧页销毁错误写为 Agent 失败。
- 服务只监听回环，明确检查允许的 Origin。服务日志使用 `journalctl --user -u agentstrata-plugin-open-llm-vtuber-client.service`，Gateway unit 后缀为 `gateway.service`。
- 对应源码 URL 配置存在和网络可获取分别检查；HTTP 可达本身不证明内容完整或法律合规。

## 开发与验收

自有插件源码、版本锁、补丁、模板和法律材料位于[插件包](../../integrations/open_llm_vtuber/README.md)。
第三方本体、模型、SDK 和页面构建结果不纳入本仓源码或 wheel。
固定上游契约与构建检查使用 Python 3.12：

```bash
python scripts/check_vtuber_compat.py
```

该脚本与 CI 共用，安装发行包、应用固定补丁、构建页面并检查实际 HTTP/WebSocket/MCP；
语音及 Agent 使用测试替身，不下载识别模型、不调用真实模型，也不需要部署凭据。
项目完整检查仍为[开发指南](development.md#选择检查范围)中的 `full`。

真实验收单独覆盖多轮文字、按键录音、TTS、口型、表情、动作、停止及断线恢复，记录模型
与音频设备。录音文件输入、模拟浏览器回执和实体麦克风/扬声器应分别报告。
