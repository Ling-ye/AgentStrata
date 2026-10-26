# Live2D 交互客户端

安装 `open-llm-vtuber` 插件后，AgentStrata 会准备固定版本的 Open-LLM-VTuber、浏览器页面、
Mao 模型、SenseVoice INT8 CPU 识别和 Edge TTS 晓晓语音，并启动独立 Gateway。
AgentStrata 保存对话、权限与记忆；客户端负责语音、角色及播放。
职责见[运行链](../reference/runtime.md#独立交互客户端)，连接、能力与回执对照见[桥接基线](../reference/runtime.md#外部桥接基线)。

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

默认使用 Codex runtime。其他已配置的执行方式可用
`--option runtime=langgraph`。两种 runtime 均按宿主模型契约校验所选绑定；LangGraph 使用
ChatGPT 认证时不要求运行 Codex app-server，Codex 本身仍要求官方 Responses 路由与原生执行文件。
插件用独立 Python 3.12
环境运行，上游依赖不会装入主程序环境。插件 Python 声明为 `>=3.12,<3.13`。

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
| `open-llm-vtuber/config/` | 应用描述、Agent profile、适配配置、ASR/TTS 配置和私有环境文件；卸载保留 |
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

应用文件 `config/bot.yaml` 引用 `config/agent.yaml`；runtime、模型用途、提示词和工具在 profile 中修改。
新安装的 LangGraph 和 Codex 都使用该安装自己的 `config/model.json` 快照，保留连接的凭据引用，
不复制 token；修改宿主其他应用或 Harness 的模型用途不会覆盖此快照。运行数据继续保存在原 `data/` 目录。
旧安装升级前按[配置迁移](instances.md#迁移内联-agent-配置)拆分原 `config/bot.yaml`，迁移不会替换已有模型选择。
旧实例若显式引用共享模型文件，迁移后仍保留该引用；需要独立调整时使用实例自己的模型配置文件。

## Console 任务与配置查看

Windows 桌面入口见下节；浏览器和 CLI 仍可独立使用。

## Windows 桌面客户端

`integrations/live2d_desktop` 提供 Windows x64 客户端 `0.1.0`，配套 WSL 中的
`open-llm-vtuber 0.4.0` 与 `live2d-generator 0.3.0`。先完成两项插件的受管安装，
再启动 Windows 安装版或解压 ZIP 中的 `AgentStrata Live2D.exe`。首次设置选择 WSL
发行版，填写 WSL 内 AgentStrata 命令绝对路径和插件根目录。程序从安装记录读取端口，
启动后台并检查 Windows 回环连接。无需向 Windows 输入模型、GitHub 或 Hugging Face 凭据。

托盘菜单切换窗口/透明桌宠、置顶、鼠标穿透、字幕输入框和工作台。
拖动角色调整位置，在角色上滚动滚轮调整大小；双击托盘恢复显示并取消穿透。关闭窗口隐藏到托盘并停止麦克风，
退出桌面保留后台与生成任务。仅点击麦克风开始一次语音输入，不自动唤醒、不采集屏幕或摄像头。

“我的角色”打开现有角色库与生成工作台。停止当前对话和语音后选择角色版本应用，
桌面会停止交互后台、调用原有事务、重新启动并连接；生成器保持运行。失败时保留
备份 ID；新角色启动失败会尝试撤回，恢复失败需要按显示状态在 WSL 检查 `plugin doctor`。
切换不创建独立长期记忆，当前窗口的历史在重连中继续使用；全新启动仍使用既有历史面板。
普通浏览器中的应用继续要求手动停止客户端。原生文件选择/下载用于导入和导出角色资料。

桌面设置位于 `%APPDATA%\agentstrata-live2d-desktop\desktop.json`，不保存服务凭据。
Chromium 会话缓存不持久化到角色库，全部角色资产
由 WSL 统一保存。私有预设目录和模型不随桌面包发行。
构建和测试见[桌面源码说明](../../integrations/live2d_desktop/README.md)，职责见
[桌面规格](../../specs/live2d-desktop/spec.md)。冷启动、实体麦克风与多屏/DPI 需在实际环境单独验收。

## Console 登记

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
停止。提交前停止不会启动 Agent；已提交而受理结果未知时，不会为了取消而重发输入。
`/resume` 使用原请求参数和幂等键对账，已有 run 则只查询原 run；取消被接受后仍需等原 run 终止。
结果未知或恢复待处理时不能按“已取消”继续发送。关闭对话后不再启动新输入，历史切换要求当前回合已完成或停止。
客户端进程重建后，`/resume` 观察所选历史最新的 run；若原请求记录已丢失，不应把该结果当作未确认输入的执行证明。
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
桥接与 runtime 的受控联调入口是 `tests/integration/test_application_bridge_baseline.py`，
使用生产装配验证 LangGraph/Codex 的路由、对话、取消和客户端隔离，不调用真实模型。

真实验收单独覆盖多轮文字、按键录音、TTS、口型、表情、动作、停止及断线恢复，记录模型
与音频设备。录音文件输入、模拟浏览器回执和实体麦克风/扬声器应分别报告。

## 生成自己的角色

`live2d-generator` 是独立的资产制作插件，默认工作台端口为 `12394`，不需要 Agent 模型绑定。
生成器与对话执行解耦；绘图、拆层使用单独配置的服务，自动绑定在 CPU 上完成。

```bash
agentstrata plugin install live2d-generator
```

未发布的源码候选与客户端一样使用 `--artifacts`、`--source-url`。候选包构建与安装验证：

```bash
python scripts/plugin_artifacts.py --plugin live2d-generator --output /tmp/live2d-artifacts
python scripts/check_live2d_generator.py
```

两条命令使用 Python 3.12 及仓库声明的构建依赖。构建输出只包含插件 wheel/sdist；受管
安装的 `--artifacts` 目录还须包含匹配的主包 wheel 和 `SHA256SUMS`。第二条命令在独立
环境安装主包与插件，验证合成分层素材到真实模型包，不调用图片服务或下载 GPU 权重。

安装后打开终端输出的工作台地址，填写名称、提示词或角色资料，也可上传图片、PSD 或
PNG 图层 ZIP。资料使用 JSON 文本键值表。示例按钮填入ヰ世界情緒的外观描述及用户提供的
[官方参考](https://kamitsubaki.jp/artist/isekaijoucho/)，生成结果是参考创作，不是官方模型。
参考链接只是资料，不会自动抓取网页图片；指定外观参考时应上传图片。

`0.2.0` 起生成任务会先建立角色草稿，成功后复制独立版本到持久角色库；工作台也可选择
已有本地角色继续生成。保存失败会单独提示并保留任务产物，可手动收录。
人设、声音、历史版本、完整备份与私有预设目录见[角色预设指南](character-presets.md)。

### 配置生成服务

安装目录的 `config/generator.json` 保存非秘密配置，`config/runtime.env` 保存私有凭据。
受管安装将 `CHATCOPILOT_SOURCE_URL` 写入同一环境文件，工作台页脚和 `/legal` 展示许可
与运行版本的源码入口；独立 CLI 使用者须自行设置。缺失时明确显示未配置，不能据此通过
对外部署验收。编辑前停止生成器，编辑后重新启动。完整字段见
[配置示例](../../integrations/live2d_generator/agentstrata_live2d/resources/config.example.json)。

| 输入 | 所需配置 | 行为 |
| --- | --- | --- |
| 提示词 / 资料 | `image_base_url`、`image_model`，以及一个拆层服务 | Images generation → 立绘 → 拆层 → 绑定 |
| PNG/JPEG/WebP | 一个拆层服务 | 默认保留图片直接拆层 |
| 图片 + 重新绘制 | 绘图与拆层服务 | Images edit 根据提示词和参考图重绘，再拆层 |
| PSD / PNG 图层 ZIP | 无图片或拆层服务 | 直接在本地绑定 |

绘图服务实现 [Images API](https://developers.openai.com/api/docs/guides/image-generation) 的
`POST /images/generations` 和 multipart `POST /images/edits`，返回 `data[0].b64_json`。
`image_base_url` 包含 API 前缀，例如 `https://api.openai.com/v1`；`image_model` 显式填写
该服务支持的模型。插件不从 AgentStrata 的聊天模型连接推断图片能力，也不使用聊天订阅
凭据替代图片 API 凭据。默认凭据变量为 `AGENTSTRATA_LIVE2D_IMAGE_KEY`。

拆层服务二选一，默认均未设置，不会隐式向公共服务上传图片：

- `decompose_url`：See-through HTTP 服务地址，支持 `POST /decompose` → `job_id`、
  `GET /jobs/{id}` → `queued/running/done/error`、`GET /jobs/{id}/result` → PSD。
  自托管实现参考 [image2live2d 服务](https://github.com/Wzhang3912/image2live2d/tree/714e7cc9191f1ef6c9a1732c80e8b566ec127d6b/service/seethrough)。
- `decompose_space`：实现 See-through `/inference` 的 Gradio Space，例如
  `24yearsold/see-through-demo`。公共演示有配额与可用性限制；只有显式配置后才会上传。

拆层凭据变量为 `AGENTSTRATA_LIVE2D_DECOMPOSE_KEY`：HTTP 使用 `X-Auth-Token`，Space 使用
指定的 Hugging Face token，不继承本机缓存身份。未配置时是匿名调用，公共 GPU 配额可能
不足以满足一次拆层申请；登录凭据的存在不等于额度充足，额度仍由 Hugging Face 账户决定。服务 URL 只接受 HTTPS 或回环 HTTP，
不得把 token 放在 URL。工作台会显示数据将发送到的服务；参数不能由单次任务更改。

`0.2.1` 起工作台会显示匿名调用或凭据已配置状态。Space 配额不足、认证失败、限流、
连接/超时与接口异常分别报告；配额错误保留服务提供的申请时长、剩余额度和建议重试时间，
不透传远端错误正文、URL 或凭据。额度由远端服务控制，本地修复不能增加第三方配额。

若要使用自己的 Hugging Face 账户额度，在生成器的私有 `config/runtime.env` 设置
`AGENTSTRATA_LIVE2D_DECOMPOSE_KEY`，再重启生成器；不在聊天、角色包或源码中填写 token。
权限应覆盖配置的 Space，配额规则以 [Hugging Face ZeroGPU 文档](https://huggingface.co/docs/hub/spaces-zerogpu)为准。

### 模型与质量

模型包包含真实 `.moc3`、`.model3.json`、纹理图集、基础表情、Idle 动作、可用物理和
`generation.json` 证据清单。文件完整性检查只验证格式、引用和摘要，清单不会自动宣称
Cubism SDK 加载已通过。工作台展示的是静态合成预览，实际动画在客户端检验。

自动绑定是可继续精修的初稿。正面、完整身体、手臂不遮挡脸部的立绘更适合自动处理；
三分之二侧脸、复杂遮挡及拆层错误会影响转头、口型或衣物变形。PNG 图层必须使用相同
画布，文件名为 `00_hair_back.png`、`10_face_base.png`、`11_eye_l.png` 等语义角色；
ZIP 仅包含一个图层目录。PSD 保留像素图层和原画布，自动识别 See-through 图层名。
`0.2.3` 起 PSD 导入会在每只眼睛已有的层级位置内按“眼白 → 瞳孔 → 睫毛”排序，避免
瞳孔被眼白遮住；其他部件的层级与遮挡保留。旧角色版本不变，可使用已保存的 PSD 在本地
重新绑定并保存新版本，无需再次调用拆层服务。

输入文件最多 48 MiB，图像/PSD 画布最多 16777216 像素，ZIP 最多 256 个条目及 256 MiB
展开体积，图层展开像素内存最多 256 MiB。工作台每次只执行一个任务，默认超时 1800 秒，
可通过 `timeout_seconds` 调整。`0.2.2` 起 Gradio 的 HTTP 请求也使用该值，避免任务总时限
尚未耗尽却触发底层默认的 5 秒超时；总时限仍由工作进程监督器执行。连接初始化超时会明确
说明尚未上传图片；提交后的超时不推断远端任务已停止。取消会终止本地工作进程，远端已提交推理可能继续运行。
重启后的未完成任务标为失败，不自动重复调用收费服务。失败任务保留已生成立绘和阶段，
可在工作台选择失败/取消的任务，点击“使用已保存输入重试”。这会创建新任务并记录
`retry_of`，保留原记录及角色关联。拆层失败时复用已生成的立绘，不再次调用绘图服务；
修正服务配置后重试会使用当前配置，不沿用失败任务的旧服务配置。服务建议的重试时间
只是提示，不会建立后台自动重试或阻止安装者在更新凭据后手动重试。

### 导入和恢复

客户端 `0.3.0` 起将模型与字典保存在 `data/live2d`，卸载程序时保留。旧版客户端先停止，
按新版本安装流程重装。生成完成后显式导入；生成过程不会自动切换正在使用的角色。
以下以默认插件根目录为例：

```bash
plugin_root="${XDG_DATA_HOME:-$HOME/.local/share}/agentstrata/plugins"
generator="$plugin_root/live2d-generator/runtime/venv/bin/agentstrata-live2d"
agentstrata plugin stop open-llm-vtuber
"$generator" install --bundle /path/to/model.zip \
  --vtuber-root "$plugin_root/open-llm-vtuber"
agentstrata plugin start open-llm-vtuber
```

使用过 `--root` 或 `AGENTSTRATA_PLUGINS_ROOT` 时将 `plugin_root` 设为对应目录。此模型包导入只更新
模型字典、当前角色显示名和模型动作白名单，保留 BotSpec、对话身份、提示词及模型连接。
命令返回 `backup` 标识；恢复之前的角色配置：

```bash
agentstrata plugin stop open-llm-vtuber
"$generator" restore --backup BACKUP_ID --vtuber-root "$plugin_root/open-llm-vtuber"
agentstrata plugin start open-llm-vtuber
```

普通写入失败自动恢复。进程中断留下 `config/pending-model-import.json` 时客户端拒绝启动，
使用其中的 backup 标识恢复。若配置已被后续编辑，恢复会拒绝覆盖；先人工核对差异。
恢复保留导入模型，备份和任务没有自动清理策略，由安装者管理私有数据目录。

也可直接使用 CLI：

```bash
"$generator" generate --config "$plugin_root/live2d-generator/config/generator.json" \
  --root "$plugin_root/live2d-generator/data/jobs" --name "我的角色" \
  --input /path/to/character.psd
```

失败任务也可通过 CLI 显式重试：

```bash
"$generator" retry --config "$plugin_root/live2d-generator/config/generator.json" \
  --root "$plugin_root/live2d-generator/data/jobs" --job FAILED_JOB_ID
```

同一任务目录不能同时运行工作台和 CLI 生成/重试，请先停止工作台，或为 CLI 选择独立目录。
模块职责见[生成器包](../../integrations/live2d_generator/README.md)，设计见
[生成规格](../../specs/live2d-generation/spec.md)。
