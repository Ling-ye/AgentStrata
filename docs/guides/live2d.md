# Live2D 交互客户端

第一版使用 Open-LLM-VTuber 浏览器客户端，通过独立 Gateway 实例调用 AgentStrata。
AgentStrata 负责对话、权限、工具与记忆；客户端处理语音、角色及播放状态。
交互、工具和数据职责见[运行链](../reference/runtime.md#独立交互客户端)。

## 准备独立客户端

客户端与 AgentStrata 后端可以使用不同 Python 环境。固定的 Open-LLM-VTuber 版本要求
Python 3.10–3.12；不要复用使用 Python 3.13 的 AgentStrata 虚拟环境。

从 AgentStrata 源仓目录执行，使用新的第三方 checkout：

```bash
git clone https://github.com/Open-LLM-VTuber/Open-LLM-VTuber.git /tmp/open-llm-vtuber
git -C /tmp/open-llm-vtuber checkout --detach 992309c0aa19845960228f880013d4685fde93b5
git clone https://github.com/Open-LLM-VTuber/Open-LLM-VTuber-Web.git /tmp/open-llm-vtuber-web
git -C /tmp/open-llm-vtuber-web checkout --detach d176e7df2366952e3bacbf12cf9a8b18a4315932
.venv/bin/python integrations/open_llm_vtuber/prepare_upstream.py \
  --backend /tmp/open-llm-vtuber --frontend /tmp/open-llm-vtuber-web
```

在客户端独立环境中按上游安装说明安装依赖，再从该环境使用 pip 安装本源仓和适配包。
使用 Edge TTS 时还需要能从 PATH 找到 `ffmpeg` 和 `ffprobe`，用于解码其 MP3 输出：

```bash
# 以下两条在客户端的 Python 3.10–3.12 环境内运行，源仓路径按实际位置替换。
python -m pip install -e /path/to/AgentStrata-open
python -m pip install -e /path/to/AgentStrata-open/integrations/open_llm_vtuber
cd /tmp/open-llm-vtuber-web
npm ci --ignore-scripts
npm run build:web
```

将 `dist/web/` 中的内容复制到新后端 checkout 的 `frontend/`，保留资产相对路径。
不要覆盖已有客户端的私有配置或模型目录。临时目录适合验证，长期使用时换成自己的安装目录。
补丁同时将 `run_server.py` 的运行入口统一为已安装的 `open_llm_vtuber` 包，避免同一输入类型
分别以 `src.open_llm_vtuber` 和 `open_llm_vtuber` 加载后，Bridge 无法识别客户端输入。

## 配置客户端与凭据

复制上游 `config_templates/conf.ZH.default.yaml` 为新 checkout 的 `conf.yaml`，修改：

```yaml
system_config:
  host: 127.0.0.1
  port: 12393
character_config:
  live2d_model_name: mao_pro
  agent_config:
    conversation_agent_choice: agentstrata
```

只修改对应字段，保留上游 ASR/TTS 配置。第一次使用按上游说明准备识别模型和语音提供者；
配置默认语音可能需要联网，不代表离线能力。

复制[适配配置](../../integrations/open_llm_vtuber/config.example.json)到自己的私有配置目录，
将 `AGENTSTRATA_VTUBER_CONFIG` 设为该文件的绝对路径。
默认动作映射使用上游 Mao 模型已有的表达式及空字符串 motion group；更换模型时按其
`.model3.json` 中实际的 Expressions/Motions 更新映射，不生成新模型或动作。

[环境模板](../../integrations/open_llm_vtuber/.env.example)不包含秘密。为 Gateway 和头像控制
分别生成强凭据，放入被忽略的环境文件或进程环境；两个 token 必须不同。AgentStrata 进程的
`AGENTSTRATA_AVATAR_CONTROL_AUTH` 使用 `Bearer ` 加上头像控制 token，不能写入 YAML。
浏览器不持有这些凭据。

为独立实例配置 `CHATCOPILOT_GATEWAY_STATE_ROOT`、`CHATCOPILOT_WORKSPACE_ROOT`，并建立
空的独立工作区；state root 的父目录必须属于当前用户且权限为 `0700`。不使用现有 QQ/微信
工作区。模型连接继续来自统一模型配置，示例使用 `chat` 与 `research` binding。
需要 Owner 访问项目时，将 `CHATCOPILOT_DEV_ROOT` 显式指向获准的项目根目录；
这不改变客户端工作区和记忆的独立归属。

## 启动与交互

先在客户端环境加载私有环境文件，并从新后端 checkout 运行 `python run_server.py`。
MCP 控制口先就绪，启动时不要求 Gateway 已连接。随后在 AgentStrata 源仓环境启动：

```bash
.venv/bin/python -m chatcopilot botspec validate \
  integrations/open_llm_vtuber/examples/bot/bot.yaml
.venv/bin/python -m chatcopilot run \
  --bot integrations/open_llm_vtuber/examples/bot/bot.yaml
```

浏览器打开 `http://localhost:12393`。先发送文字，确认回复由 AgentStrata 产生，再点击
麦克风按钮说一段话。适配模式关闭自动开麦，识别到一段话后自动停麦；不是持续监听模式。
回复完成后由客户端合成语音并驱动口型。表情和动作通过 `avatar` MCP 工具调用，MCP 配置
见[实例示例](../../integrations/open_llm_vtuber/examples/bot/mcp.yaml)。

停止按钮终止本地播放并请求取消仍在运行的 Agent。`/resume` 查询原请求，`/stop` 发送
停止请求；不会把新输入替换成旧 run 的重放，也不撤销已经执行的工具操作。
切换历史前先完成或停止当前回合。客户端历史删除只清理本地显示记录，不删除 AgentStrata
的运行证据或长期记忆。

## 关闭能力与诊断

- 只需文字对话时，可关闭客户端语音设置；语音失败仍保留文字结果。
- 不需要头像工具时，从适配 JSON 删除 `mcp_token_env`，同时停用示例中的 MCP server。
  对话仍经原 Gateway 运行，浏览器 Origin 检查仍生效。
- `browser_not_connected`：浏览器未连接固定头像；`failed`：动作或模型不可用；
  `unknown`：未观察到完整回执，不能描述为已执行。重复请求可能重复播放动作。
  motion 的 `executed` 表示观察到 SDK 已开始该动作，不表示循环动作已经播放结束。
- 所有请求只监听回环地址。使用不同本机端口时同步更新客户端 URL 和明确的 allowed_origins，
  不添加通配 Origin。头像 MCP 地址为 `http://127.0.0.1:12393/agentstrata/mcp/`。
- Bridge 日志关联 session/run；播放及动作回执留在客户端组件，不参与 Gateway run 终态。

实际验收应覆盖多轮文字、按键语音、声音与口型、一个表情、一个 motion、停止和断线恢复。
记录使用的 ASR/TTS、模型与音频设备，分别报告真实检查和替代层；代码检查或模拟音频
不能代替真实麦克风、语音服务与模型往返。
