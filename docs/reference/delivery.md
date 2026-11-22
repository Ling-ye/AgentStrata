# 源码交付、实例版本与归档

本页维护 code-worker 与 Harness 的发布、对账和清理契约。交互式 AI 协作者未经逐项明确授权，
不得暂存、提交、推送、创建 PR、合并或发布；以下受控 worker 流程不授予交互式 AI 同等权限。

## 独立应用交付与本机改进

`HarnessController.export_delivery` 将完整验收且具有运行版本证据的应用候选导出为目录，
包含 `application.json` 内容清单、`source` 完整公开源码和 `install.sh` 入口。
交付保留原有配置、锁文件、Console、检查与部署工具；不携带原任务目录、Git 凭据或业务状态。
候选接受与安装后的实际模型、运行环境和外部效果分别确认。

`agentstrata app install` 校验交付内容，在全新目录建立安装副本与本地 Git 基线，
宿主根据私有安装身份生成本机部署位置。BotSpec、Agent profile 和实现文件均保持交付内容。
`.agentstrata-application.json` 记录唯一应用及交付来源；`.agentstrata-data`、私有环境及
受管版本目录不进入后续候选。既有目录不覆盖，相同交付可继续安装依赖。
依赖准备完成后仍需配置模型和目标应用的连接；`setup` 沿用现有部署工具，不把源码安装报告为机器人就绪。
应用同目录可用 `components.yaml` 声明插件及公开选项，入口调用 Plugins 完成软件准备，
再把组件服务登记到 `.agentstrata-versions/components.json`。Deployment 通过通用服务声明
按顺序启停并核对运行回执，不导入 Plugins。`app start/stop/status` 提供应用作用域的薄控制；
WSL 启动环境读取应用声明的客户端凭据及 MCP 环境引用，不依赖特定插件的变量名前缀。
桌面通过仅本机 CLI 的 `desktop-info` 取得呈现地址与控制凭据，不能将其输出公开。
Windows 包、Windows-MCP 环境和上游前端构建须随对应源码分别核验，M5 不回滚这些构建产物。

Windows 桌宠首版另以 `agentstrata-windows-runtime/v1` 记录首次源码构建，绑定基线提交、
精确源码清单及构建产物清单摘要，不能携带或伪造 `AcceptedCandidate`。安装保留该来源，
使用清单核验源码及 executable 意图，不要求用户安装 Git；已有已接受候选交付格式不变。
桌面通过前台 `app run --watch-stdin` 持有 Windows 后台；`status/stop/desktop-info` 沿用应用
作用域，进程与停止事件由 Windows 适配管理，不模拟 systemd 或新增控制网络协议。
Windows 首版支持新目录与同版本续装，`update/rollback` 明确不支持；开发侧 Harness/Eval
与 Linux M5 保持原有职责。模型设置使用既有 ModelSettingsStore，API 凭据不经过命令行参数。

Console 可通过 `CHATCOPILOT_SOURCE_ROOT` 显式选择安装副本，也可直接从副本启动。
Console 与 Evaluation 仅发现该安装声明的应用；Harness 和 Eval 继续在本机按需启用。
服务重启、模型设置、预算及权限遵循原有机制，不增加第二套控制或验收系统。

Gateway 将实际加载的应用源码版本写入执行时观测。用户从任务记录发起修复时，Harness 核对
应用和版本，以当前运行源码冻结本地候选；旧版本或缺少版本的记录需要先在当前版本复测。
需求改进和测评修复同样绑定安装版本，不调用远端 main 或 GitHub 发布。
Eval 从受管源码准备隔离执行，评分与检查仍由执行中的宿主提供。

更新与撤回使用既有 `AcceptedCandidate` 和下节 M5 操作；源码启用后，下一次任务以该版本为基线。
身份、权限、模型条件、在途任务及恢复边界保持。完整操作见
[独立应用交付与本机改进](../guides/instances.md#独立应用交付与本机改进)。

源码入口：[安装身份](../../src/chatcopilot/core/application_installation.py)、
[交付内容](../../src/chatcopilot/deployment/application_bundle.py)、
[安装与运行入口](../../src/chatcopilot/deployment/application_runtime.py)。

### 工作台应用安装包

桌宠构建以插件锁文件的 SHA-256 定位持久输入缓存，其中分别保存 wheels、sources 和 projects。构建端也可通过 `--plugin-wheelhouse`、`--plugin-sourcehouse` 和 `--plugin-projecthouse` 指定原始材料。构建器按所选源码重新核验原始文件及补丁，Windows 独立运行接受仍由对应的验证记录决定。

在具体应用的「下载与部署」选择接收方的操作系统。Windows x64 下载一个 EXE，支持 Linux x64 的应用下载一个 RUN，用 `bash 应用-Setup.run` 安装。纯网页由系统浏览器打开；原生桌面应用和本地辅助进程沿用 Windows 安装器。首次生成时确认完整的 Git 提交，或选择该应用已独立验收的 Harness 任务。构建端从独立的提交检出或已核对的冻结候选读取公开文件。旧 ZIP 可从原开发记录取回。

需要独立服务时，构建机使用 Docker 构建 linux/amd64 镜像，用 NSIS 3 构建 Windows 安装器（也可复用桌面构建工具的锁定 NSIS）。Linux 上构建原生 Windows 安装包使用桌面项目锁定的 Electron 和 NSIS，安装器绑定同一份桌面启动清单。安装器内含公开源码、Compose 配置、镜像和镜像摘要。接收方预装 Docker Engine 或 Docker Desktop（Linux 容器）和支持 `up --wait` 的 Compose v2。安装器离线导入镜像，复用本应用的命名卷，通过健康检查后打开入口。重复安装保留命名卷。安装器成功后在该应用目录的 receipts 中保存与安装包 SHA-256 对应的 JSON，可在 Studio「校验与运行记录」导入。记录说明接收方当次安装和服务健康检查结果，业务操作和渠道投递另行核验。Linux 安装位置为 `${XDG_DATA_HOME:-$HOME/.local/share}/agentstrata/apps/<bot>`，Windows 为 `%LOCALAPPDATA%\AgentStrata\Apps\<bot>`。

独立服务项目在 `integrations/<bot>/` 提供 `compose.yaml` 和 `install.json`。后者可指定网页入口，如 `{"entry":{"service":"app","port":8080}}`，以及给接收方的 `requirements`。每个服务声明健康检查和应用内的相对构建目录；预构建第三方镜像须固定 `name@sha256:…`，在 `image_licenses` 中按服务名指向随包的再分发依据。命名卷属于此应用，网页随机分配本机回环端口。在应用自己的首次设置中绑定其他账号与可选能力。

可执行客户端可在同一清单声明 `clients`，例如 `{"clients":{"windows-x64":"client/app.exe","linux-x64":"client/app"}}`。入口路径相对应用公开目录，Linux 程序须在 Git 中保留执行位。没有独立服务时，安装器一并安装客户端和随附的本地辅助进程。有 Compose 声明时，同一个安装包先启动并核对服务，再打开客户端。带网页服务入口的可执行客户端从 `AGENTSTRATA_SERVICE_URL` 读取该次部署的本机地址。公开目录中的静态网页可直接以 HTML 为入口。

QQ 与微信使用各自的 BotSpec、Gateway 和运行时。QQ 个人号需接收方自备已登录的 NapCat/OneBot v11 容器，命名为 `napcat-<bot>`，启用带 Access Token 的正向 WebSocket。Gateway 共享该容器的网络命名空间，在回环地址连接。QQ 官方渠道绑定 AppID、AppSecret，微信绑定自己的 iLink 凭据。原配置为 Codex 的 QQ 个人号和微信安装包在首次配置时可明确选择 Codex 或 LangGraph。Codex 先检查容器中普通用户的 bubblewrap 会话隔离，失败会退出；LangGraph 使用接收方绑定的聊天模型。选择保存在本应用的数据卷，重复运行沿用已选运行时。安装时填写个人模型连接。声明独立代码任务的机器人另选 Codex 开发模型，需要执行代码任务时在安装版本目录运行 `docker compose -p as-<bot> run --rm --no-deps gateway login-worker` 单独授权。可选 MCP、外部账号和个人工作区由接收方准备。账号保存在本应用数据卷，后续可在已安装版本目录用 `docker compose -p as-<bot> run --rm --no-deps gateway reconfigure` 重新设置，再用相同项目名重启 Gateway。


#### 给新应用出包

在 Linux 或 WSL 构建机安装 AgentStrata 后，可以直接从本地仓库的完整提交生成接收方安装包。例如，一个已有网页项目把 `index.html` 放在仓库根目录，可以运行：

```bash
agentstrata app package --source "$PWD" --revision "$(git rev-parse HEAD)" \
  --application field-notes --name "现场记录" --app-dir . \
  --platform linux-x64 --output ../field-notes-release
```

输出目录须是新目录。成功后其中包含 `application.run` 和 `build-receipt.json`；再加上 `--platform windows-x64` 可同时生成 `application.exe`。省略平台参数时构建计划声明的所有平台。Windows 封装需要构建机提供 NSIS 3 的 `makensis`，服务型应用还需 Docker Engine。Studio 继续支持仓库已有的 NSIS 工具链。

Studio 默认从 `integrations/<应用标识>/` 读取公开输入。命令行的 `--app-dir` 也接受提交中的其他相对目录。新声明可注明 `"schema": "agentstrata-install/v1"`、`recipe` 和 `platforms`；已有无 schema 声明继续可用。Studio 新建网页或服务时将确认的平台写入源码声明，独立构建可复用同一选择。原生 Electron 和托管 Gateway 的专用装配由 Studio 提供。

| 应用怎样启动 | 应用目录中的必要输入 | 配方 |
| --- | --- | --- |
| 浏览器打开静态页面 | `index.html` 及页面资源 | `static` |
| 接收方运行一个已有客户端 | `install.json` 的 `clients` 按平台指定可执行文件或 HTML；Linux 程序在 Git 中保留执行位 | `client` |
| 需要常驻服务，或客户端连接独立后端 | `compose.yaml` 和 `install.json`。网页入口使用 `entry.service` 和 `entry.port`；混合应用可同时声明 `clients` | `service` |

例如，给一套有网页入口的服务限定两个交付平台，可在应用目录写入以下 `install.json`：

```json
{
  "schema": "agentstrata-install/v1",
  "recipe": "service",
  "platforms": ["windows-x64", "linux-x64"],
  "entry": {"service": "web", "port": 8080},
  "requirements": ["接收方预装 Docker 和 Compose"]
}
```

两种格式共享公开载荷。构建回执记录实际配方、来源、各平台文件大小及 SHA-256；服务回执另含随包镜像的完整 ID。同一静态载荷采用固定的归档顺序、时间和权限，NSIS 内嵌文件的时间也固定，可用相同版本的工具链复核重复构建。Dockerfile 引用浮动基础镜像或在构建中下载依赖时，镜像内容可能随时间变化。历史版本继续按其原回执校验下载。

### 运行依赖输入清单基础

`core.archive_inventory.zip_inventory` 从不可变 ZIP 字节与宿主冻结输入中的 SHA256 派生
逐文件路径、大小和内容摘要，目录单独记录。先核对整个归档，再在创建 ZIP 元数据对象前
限制中央目录字节、条目数和路径长度；解压前检查单文件、总内容、目录树和深度预算。
输出包含归档摘要与版本化策略，不能作为接受回执、安装身份或启动授权；调用方必须独立
确认输入 pin 的可信来源。对现有 Python、site-packages 或本地 JSON 拍摄摘要不属于该接口。

```python
from chatcopilot.core.archive_inventory import zip_inventory

inventory = zip_inventory(pinned_archive_bytes, expected_sha256=frozen_input_sha256)
```

首版只接受单卷 ZIP32 的 stored/deflate 成员，采用 ASCII Windows 安全路径子集；路径不做
静默正规化。越界、保留设备名、大小写或文件/目录冲突、链接与特殊类型、重解析标记、
局部/中央记录不一致、重叠或未引用内容均拒绝。ZIP64、数据描述符、其他压缩方法、非 ASCII
成员及可改变名称/大小的扩展元数据尚不支持；不应为兼容输入而降低检查。文件按原字节
流式计数、校验 CRC 并计算摘要，包括 `.pth`、DLL、原生模块和元数据，不执行其中内容。
ZIP 格式及资源边界参考 [Python 3.13 ZIP 文档](https://docs.python.org/3.13/library/zipfile.html)。
Windows 安全路径子集、ASCII 等额外限制是本策略的保守选择。

这只是[可信运行依赖链](../../specs/desktop-app-studio/spec.md#桌面运行依赖的后续信任条件)的输入层。
受限 TAR 输入清单、Python 固定输入消费/基础复制计划与 wheel 的 Windows 字节映射见下节；venv、
脚本生成、插件/MCP 环境、更新及构建回执绑定仍未完成。现有安装流程尚未接入这些编译器，
UI、BFF 和原生脚本继续拒绝统一启动。既有 Windows runtime lock 的 `host_python`、
`vtuber_python` 仍是版本号；独立的 Python 输入声明已固定发布表及归档摘要，尚未被原有
provisioner 消费。下节的纯输入消费 API 要求独立宿主 pin，不能把 uv 已准备的解释器目录
反向登记为可信来源。

可在现有源码环境复现，无需模型、网络或安装器：

```bash
.venv/bin/python -m pytest tests/unit/test_archive_inventory.py -q
```

同一文件可在隔离 Windows 源码副本使用本机 Python 运行，按开发指南用 `--confcutdir=tests/unit`
排除 Linux Eval 宿主。测试使用合成归档，不证明完整运行环境已准备或真实应用已运行。
源码入口：[清单编译器](../../src/chatcopilot/core/archive_inventory.py)与
[边界回归](../../tests/unit/test_archive_inventory.py)。

### TAR 清单与 Python 固定输入

`core.tar_inventory.tar_inventory` 接受不可变归档字节、宿主冻结的 SHA256 和显式的 `gzip`
或 `none` 压缩策略。整个归档摘要通过后才创建解压器；解压输出按剩余预算限制分配，
同时记录完整 TAR 流的字节数与摘要。最终清单包含所有普通文件和隐含/显式目录，不调用
提取接口，不应用权限、所有权或时间，不导入归档内容，也不签发接受回执。

```python
from chatcopilot.core.tar_inventory import tar_inventory

inventory = tar_inventory(pinned_archive_bytes, expected_sha256=frozen_sha256, compression="gzip")
```

首版支持受限 USTAR、GNU 普通头及 long-name、PAX 单成员头的 path 和时间/所有权字段。
TAR 文本字段按 NUL 终止语义读取；终止符后的残余字节仍纳入归档与 TAR 流摘要。
GNU/PAX 路径覆盖属于显式格式解析，最终路径共用 ASCII Windows 安全策略，拒绝越界、
设备名、大小写别名、重复成员、类型冲突和不受支持的路径。GNU/PAX 元数据不得堆叠或
悬空，声明长度在读取前限制，PAX 重复键、size、linkpath、稀疏文件及未知字段拒绝。
路径覆盖也不能掩盖原始普通文件头的目录拼写，避免旧读取器在覆盖前推断不同成员类型。

成员头校验 unsigned checksum 和受限八进制数字；拒绝 base-256、旧 V7、链接、设备、
FIFO、稀疏类型、特权权限及不支持的扩展。数据填充必须为零，结束需两个零块，后续仅
允许受预算限制且按块对齐的零填充。gzip CRC/ISIZE 必须完整通过；拼接 gzip、拼接 TAR、
截断和额外非零内容被拒绝。归档、解压流、成员/树数量、路径、单文件、内容总量、扩展
元数据及结束填充均有独立预算。格式背景见 [Python TAR 文档](https://docs.python.org/3.13/library/tarfile.html)。

[Python 输入声明](../../integrations/live2d_desktop/windows-python-inputs.lock.json)固定官方 uv
下载表的完整提交、Git blob SHA1、文件 SHA256 与大小，再为 host、桌宠插件和 Windows-MCP
三条 Windows Python 用途记录精确 selector、归档 SHA256、格式与根目录。归档 URL 由固定下载表及 selector
唯一确定；消费方须先验证整张表的原字节，再核对选择结果和整个归档，不查询当前解释器、
site-packages 或机器摘要。生产构建和读取同时核对 host/plugin runtime lock 与 MCP executor 的版本。
普通桌面及 Live2D 的原生运行源码投影均保留该输入声明，沿用候选来源内容清单的绑定。

该文件是源码内的声明性输入锁，须随宿主接受的冻结来源核对；它不能因存在于安装目录
便成为信任根。原有 bootstrap 仍按版本调用 uv，尚未接入新归档清单。固定输入的消费与
基础目录复制计划见下节；venv、加载路径与 ABI 验证、脚本和配置生成、插件/MCP、全新
隔离 provisioner、完整成员核验以及构建/安装回执绑定仍须实现，三处统一启动门禁保持拒绝。

可在源码开发环境复现，无需网络、模型或安装器：

```bash
.venv/bin/python -m pytest tests/unit/test_tar_inventory.py tests/unit/test_archive_inventory.py tests/unit/test_wheel_plan.py -q
```

隔离 Windows 副本使用相同源码、输入锁和测试；实际发布归档清单与真实安装/运行分别
留证。源码入口：[TAR 清单](../../src/chatcopilot/core/tar_inventory.py)与
[攻击及格式回归](../../tests/unit/test_tar_inventory.py)。

### Python 固定输入消费与复制计划

`core.python_plan.resolve_python_inputs` 消费原始输入锁与整张官方元数据字节，调用方必须
显式提供独立冻结的 `expected_inputs_sha256`。先核对输入锁，再按锁中的完整字节数、
SHA256 与 Git blob 身份核对元数据，最后解析 selector 与下载位置。Git blob 是来源一致性
检查，信任根仍是宿主独立提供的 SHA256；候选应用、安装目录中的 JSON 和当前机器摘要
不得自行登记或替代该 pin。API 本身不联网，不选择本机解释器或查询已安装环境。

JSON 分配前限制字节、结构深度和 token 数；拒绝重复键、过长整数、浮点数及非标准常量。
输入锁采用封闭 schema；v1 保留原 host/plugin 两用途和稳定 CPython 3.12/3.13，v2 必须
完整包含这两用途及 windows_mcp_python。新增 MCP 用途只允许稳定 CPython 3.14，不能
扩大 host/plugin 或声明依赖闭包的版本范围。均限定 Windows x86_64、普通非自由线程构建。
分发名、系统、架构、libc、版本整数、variant、发布 build 和归档摘要均须逐项匹配。
下载位置仅接受固定供应商、版本与 build 的规范 HTTPS asset，拒绝凭据、端口、查询参数、
fragment 和路径变体；解析结果不能取代独立接受或授权。

```python
from chatcopilot.core.python_plan import resolve_python_inputs, windows_python_plan

resolved = resolve_python_inputs(
    frozen_inputs_bytes, supplier_table_bytes,
    expected_inputs_sha256=host_frozen_inputs_sha256,
)
plan = windows_python_plan(
    frozen_inputs_bytes, supplier_table_bytes, pinned_python_archive_bytes,
    expected_inputs_sha256=host_frozen_inputs_sha256, lane="host_python",
)
```

`windows_python_plan` 重新消费原字节与宿主 pin，不接受解析结果对象、安装状态清单或任意
目标路径。TAR 输入摘要通过后，将所有成员从固定 `python` 根映射到相对
`runtime/python/host/<归档摘要>`、`runtime/python/vtuber/<归档摘要>` 或独立的
`runtime/python/windows-mcp/<归档摘要>`。归档内容改变即得到新槽位，
不会在计划中覆盖旧版本路径。路径、目录树和深度预算同时约束最终目标；每项保留原成员
路径、大小与摘要，计划绑定输入锁、元数据、来源提交、完整归档及 TAR 流摘要。

关键解释器、DLL、运行库、标准库与许可证必须存在且非空；固定根外成员、额外解释器 DLL
以及未定义生成规则的启动配置被拒绝。首版不接收基础镜像内的 `.pth`、`._pth`、
`pyvenv.cfg` 或 sitecustomize/usercustomize 入口，不能靠路径映射将其默认为可信加载配置。
`windows-standalone-copy/v1` 仅描述逐文件复制，不进行文件系统写入，也不生成 venv 或
启动脚本。venv 和 wheel 的目标环境另行建模，不把基础镜像计划当成已安装环境清单。
MCP 3.14 当前仅支持基础归档数据计划及隔离落地/回读；环境 v1/v2/v3 均明确拒绝该用途，
不能误落到 vtuber venv，也不能据此声称 3.14 前缀、wheel 闭包或原生加载已支持。

此层不能证明目标目录归属、无重解析点、安全写入、全加载路径、ABI、实际进程或接受回执。
生产宿主准入与接入、完整 provisioner、venv/脚本与配置规则、插件/MCP、完整加载集合校验及
构建/安装身份回执绑定仍待实现；基础镜像的隔离暂存由下节实现，原安装器及三处启动门禁保持原行为。

离线回归入口：

```bash
.venv/bin/python -m pytest tests/unit/test_python_plan.py tests/unit/test_tar_inventory.py tests/unit/test_archive_inventory.py tests/unit/test_wheel_plan.py -q
```

源码入口：[固定输入与复制计划](../../src/chatcopilot/core/python_plan.py)及
[输入、URL、结构预算与映射攻击回归](../../tests/unit/test_python_plan.py)。

### Python 基础镜像隔离暂存

`core.python_materialize.materialize_python_isolated` 从原始不可变输入锁、官方元数据和归档
重新计算复制计划；调用方仍须显式传入独立宿主 pin。完整认证和文件数、目录数、总字节、
句柄预算检查先于工作目录打开，不接受现有环境摘要、已解析计划或本机回执作为来源授权。
默认最多暂存 5000 个文件、512 个目录和 256 MiB 内容，持有句柄上限 6000；POSIX 同时核对
现有进程容量并预留句柄，不调整系统或进程限制。

工作目录必须预先存在、为空、属于当前身份且为私有目录。API 不创建缺失的工作目录，
不修复既有权限；拒绝符号链接、Windows 重解析点、非本机固定卷和跨文件系统目标。
只在工作目录中以排他方式创建随机 `provision-` 子树，所有目标成员均为新文件。
POSIX 使用目录描述符与 no-follow，目录 0700、文件 0600；Windows 持有所有祖先目录的
只读共享句柄，禁止写入和删除共享，后续操作使用句柄解析的固定卷 GUID 命名空间，
新建对象使用当前 SID、SYSTEM 和管理员的私有 DACL。所有文件句柄保留至本次验证结束。
工作目录的归属和权限只是写入约束，不能赋予归档独立来源或执行许可。

严格 TAR 清单通过后，第二个流式读取器必须与该清单的路径、类型、大小和内容摘要一致；
仅从文件流读取字节，不调用文件系统解包接口。写入后回读所有文件，核对完整目录成员、
文件大小、SHA256、句柄与路径身份、单一硬链接及权限，并检测观察到的验证期间变化。
Windows 长路径使用原生命名空间，无需改注册表。返回后不保证持久不可变，也不抵御同一
账户或管理员在验证结束后改写文件；后续运行仍须完整环境策略和重新验收。

```python
from chatcopilot.core.python_materialize import materialize_python_isolated, verify_python_isolated

staged = materialize_python_isolated(
    frozen_inputs_bytes, supplier_table_bytes, pinned_python_archive_bytes,
    expected_inputs_sha256=host_frozen_inputs_sha256,
    lane="host_python", workspace=existing_empty_private_lab_directory,
)
checked = verify_python_isolated(
    frozen_inputs_bytes, supplier_table_bytes, pinned_python_archive_bytes,
    expected_inputs_sha256=host_frozen_inputs_sha256,
    lane="host_python", stage_root=existing_stage_directory,
)
```

独立回读 API 再次从原始输入和宿主 pin 推导集合，不加载暂存目录中的 JSON。
返回值只记录 `staged`、位置、计划与输入摘要及成员统计，不是安装身份或接受回执。
失败保留新建的私有现场，错误位置仅供检查，不是删除权限；本层不自动重试、清理、
移动、发布、复用或覆盖已有对象，也不执行其中解释器。生产安装器、bootstrap 和统一
启动尚未调用此 API；基础镜像暂存通过不能解除 UI、BFF 或原生脚本的拒绝门禁。
完整安装器的发布事务、完整 venv/脚本、插件/MCP、加载集合和回执绑定仍待实现；
基础镜像与 wheel 的受限组合落地见下文[环境隔离暂存](#windows-python-环境隔离暂存)。

无需网络、模型或安装器的定向验证：

```bash
.venv/bin/python -m pytest tests/unit/test_python_materialize.py tests/unit/test_python_plan.py tests/unit/test_tar_inventory.py tests/unit/test_archive_inventory.py tests/unit/test_wheel_plan.py -q
```

原生 Windows 副本用相同测试验证 NTFS ACL、共享句柄、junction 和长路径；实际固定归档
的隔离落地与原始输入回读另留交付证据，不表述为安装、升级或程序运行验收。
源码入口：[隔离暂存与回读](../../src/chatcopilot/core/python_materialize.py)、
[句柄文件操作](../../src/chatcopilot/core/isolated_files.py)和
[失败、替换及跨平台回归](../../tests/unit/test_python_materialize.py)。

### Windows wheel 字节安装计划

`core.wheel_plan.windows_wheel_plan` 接受不可变 `PinnedWheel` 元组：归档字节、独立冻结的
SHA256、分发名和精确版本，以及宿主冻结的展开兼容标签。先调用上节 ZIP 编译器核对所有
成员，再校验唯一 `.dist-info`、METADATA/WHEEL 身份与标签、RECORD 的完整覆盖、SHA256
及大小。归档内部 RECORD 只做一致性检查，不充当信任根；不查询当前解释器或已安装包。

```python
from chatcopilot.core.wheel_plan import PinnedWheel, windows_wheel_plan

plan = windows_wheel_plan(
    (PinnedWheel(pinned_wheel_bytes, frozen_sha256, frozen_name, frozen_version),),
    compatible_tags=frozen_windows_tags,
)
```

目标布局固定为未来受控 Windows venv 的相对 `Lib/site-packages`，根目录与匹配的
`.data/purelib`、`.data/platlib` 均映射到该位置。所有 wheel 共享路径树和字节预算，拒绝
重定位或跨包的文件覆盖、大小写别名、类型冲突及重复分发名；只合并同名目录。外来
`.dist-info`、`.egg-info`、`.data` 及通过重定位注入的安装元数据被拒绝。

每个输出文件记录目标、大小、摘要和输入归档摘要；复制项保留原成员路径。原始 RECORD
作为被替换输入保留其事实，生成项携带精确字节与版本化 recipe：`installer/v1` 写入固定
`agentstrata\n`，`record/v1` 按最终相对路径排序、使用 LF 与 SHA256 生成完整 RECORD，
自身行的摘要与大小为空。所有 `.py`、`.pth`、DLL、原生模块和许可证保持字节不变。

首版仅支持 Wheel-Version 1.0、保守的精确版本拼写与 SHA256 RECORD；METADATA 格式支持
1.1、1.2、2.0—2.4。邮件格式解析前限制头部最多 1024 行、单行 64 KiB，拒绝孤立 CR；
UTF-8 描述正文仍按原字节记录。拒绝入口点、scripts/headers/data scheme、已有 INSTALLER/REQUESTED/
direct_url.json 和旧式 RECORD 签名；这些输入必须等待独立定义转换，不能静默忽略。
此接口不生成脚本、`.pyc` 或路径配置，不解包、安装、导入包、写入磁盘或接受本地状态清单。
标签相容仅检查冻结策略的交集，不能证明 Python/DLL ABI、依赖求解或额外可加载代码安全。
`.pth` 的外部目标、插件查找路径和动态加载仍须后续策略核对。

该计划及 INSTALLER/RECORD 字节都是可重算的输出事实，不是已安装声明、接受回执或启动
授权。尚须完成完整 Python venv/加载策略、插件/MCP、应用脚本、完整隔离 provisioner、完整环境核验和
构建回执绑定；现有安装器未接入此接口，三处统一启动门禁保持拒绝。

无需网络、模型或安装器的回归入口：

```bash
.venv/bin/python -m pytest tests/unit/test_archive_inventory.py tests/unit/test_wheel_plan.py -q
```

Windows 隔离源码副本使用相同测试与输入字节；这不构成真实安装或应用运行验收。
源码入口：[字节安装计划](../../src/chatcopilot/core/wheel_plan.py)与
[攻击及映射回归](../../tests/unit/test_wheel_plan.py)。

### Windows Python 环境隔离暂存

`core.python_environment.windows_python_environment_plan` 要求独立冻结的环境声明 SHA256。
环境声明使用封闭 `agentstrata-windows-python-environment-inputs/v1` schema：`recipe` 固定为
`windows-isolated-venv/v1`，另有 `python_inputs_sha256`、`lane` 和精确 `wheels` 数组；
每个 wheel 只声明规范分发名、精确版本和 SHA256。先认证声明字节，再认证 Python 输入锁、
官方元数据、解释器归档及完整 wheel 字节集合。候选、已安装环境和本地 JSON 不得自行
产生或登记这个 pin；原始 wheel RECORD 只作一致性校验。

复制计划保留完整 Python 基础镜像，在 `runtime/venv/<lane>/<环境声明摘要>` 生成受限布局。
兼容标签从固定 Windows x64 CPython 版本导出，首版支持精确 `cp312`/`cp313` ABI 标签及
`py3-none-any`；不读取正在运行的开发解释器来决定目标兼容性。标签检查不能代替
Requires-Python、声明依赖闭包、扩展 ABI 或实际导入验证；v2 对声明闭包增加下节校验。

Scripts 下的解释器文件来自同一固定归档的 venv 重定向器：3.12 使用
`Lib/venv/scripts/nt/python.exe` 与 `pythonw.exe`，3.13 使用 `venvlauncher.exe` 与
`venvwlauncher.exe`。wheel 沿既有字节计划重定位，并生成版本化 INSTALLER/RECORD；
不调用解释器、pip、venv 命令、构建钩子、安装脚本或 shell。
v1/v2 环境拒绝 wheel 中的 `.pth`、`._pth`、`pyvenv.cfg`、sitecustomize/usercustomize
入口；v3 使用下述显式隔离映射。各版本仍拒绝与固定标准库或引导模块同名的顶层成员，
原字节计划仍只描述成员事实。

原生 Windows 的 `materialize_python_environment_isolated` 复用私有空目录、固定卷 GUID、
排他新建和持有句柄的文件边界。原始输入、完整复制计划和合并文件/目录/字节/句柄预算
先于文件系统操作。`pyvenv.cfg` 的 home 绑定实际锚定的基础镜像位置，关闭系统
site-packages；DLL 同目录的 `python312._pth` 或 `python313._pth` 只列出固定标准库、
DLLs 和当前环境的包目录，均由计划内相对路径计算，不包含当前目录或任意外部路径。
配置使用 UTF-8/LF，大小限制在较小启动器缓冲区以内。

`._pth` 不包含 `import site`，也不生成激活或应用启动脚本。此层只提供受限的 venv 文件
布局和显式包目录，尚不提供完整标准 venv 前缀语义；不把磁盘布局当作可用解释器。
路径隔离机制参考 [Python 路径初始化](https://docs.python.org/3.13/library/sys_path_init.html#pth-files)，
实际归档的进程初始化、环境变量、DLL 搜索、写入行为及兼容性仍须单独验收。
Linux 可计算相同纯计划并运行输入/路径攻击回归，文件系统绑定明确要求原生 Windows，
不生成指向伪 Windows 位置的配置来补造跨平台运行证据。

写入时第二个 TAR/ZIP 读取器须与严格清单和计划的成员、大小及摘要一致；只读取流，
不使用文件系统解包接口。生成配置也作为精确字节参与最终完整树回读。返回值为 `staged`，
包含独立输入摘要、逻辑计划摘要及文件系统绑定摘要；后者绑定固定卷位置、已核对的
当前所有者 SID、每个文件和目录的实际身份以及所有期望字节事实。它仅记录本次观察，不能证明来源、安装或接受。

```python
from chatcopilot.core.python_environment_materialize import (
    materialize_python_environment_isolated, verify_python_environment_isolated,
)

staged = materialize_python_environment_isolated(
    environment_inputs_bytes, python_inputs_bytes, supplier_table_bytes,
    pinned_python_archive_bytes, pinned_wheel_archive_tuple,
    expected_inputs_sha256=host_frozen_environment_sha256,
    workspace=existing_empty_private_windows_lab_directory,
)
checked = verify_python_environment_isolated(
    environment_inputs_bytes, python_inputs_bytes, supplier_table_bytes,
    pinned_python_archive_bytes, pinned_wheel_archive_tuple,
    expected_inputs_sha256=host_frozen_environment_sha256,
    expected_filesystem_binding_sha256=host_saved_staging_observation_sha256,
    stage_root=existing_windows_stage_directory,
)
```

回读必须重新消费全部原始来源字节和独立环境 pin，并核对调用方在暂存外保存的前次
观察摘要；不能从待验证目录读一个新摘要来代替。相同内容的文件或目录被替换、复制到
另一位置、配置或包被改写、缺项和额外成员均被拒绝。失败保留现场，不清理、覆盖、
复用、移动或发布；返回后也不保证文件持久不可变。

此 API 尚未接入生产安装器、bootstrap 或统一启动。应用源码注入、完整前缀/脚本、插件/
MCP、实际完整应用依赖集、原生加载集合、应用/launcher/安装身份接受回执及更新发布事务仍待完成；
原 bootstrap 的源码 `.pth` 不在这个受限 recipe 内。只有整条可信链安全审查及真实验收
通过后才可考虑接入；三处启动门禁继续拒绝，暂存事实不能签发接受或启动权限。

离线定向验证包含前序信任链：

```bash
.venv/bin/python -m pytest tests/unit/test_python_environment.py tests/unit/test_python_materialize.py tests/unit/test_python_plan.py tests/unit/test_tar_inventory.py tests/unit/test_archive_inventory.py tests/unit/test_wheel_plan.py -q
```

源码入口：[环境声明与加载规则](../../src/chatcopilot/core/python_environment.py)、
[隔离落地和绑定回读](../../src/chatcopilot/core/python_environment_materialize.py)及
[输入、跨版本与重放攻击回归](../../tests/unit/test_python_environment.py)。
单次真实归档/包的文件落地证据留在交付产物；应用运行与安装升级另行记录。

### Windows wheel 声明依赖闭包

环境声明 v2 保留 v1 的来源字段与复制 recipe，使用封闭
`agentstrata-windows-python-environment-inputs/v2` schema，额外要求 `requirements` 字符串
数组。它是宿主冻结的根需求，必须与同一个独立环境输入 pin 绑定。直接 API
`core.dependency_closure.windows_dependency_plan` 校验原始固定 wheel，返回字节计划和
声明闭包；环境 v2 在任何工作目录操作前调用它。

根需求、Python 版本和 wheel pins 均由宿主来源决定，不从已安装分发、环境摘要或解析结果
补取。校验器不是求解器：不会联网选版本、下载缺项、调用 pip 或回退到本机包。
每条活动依赖必须在精确冻结集合中存在并满足版本条件，每个选中 wheel 都必须可由活动
根需求到达；无关发行版也被拒绝，不能夹带额外可加载代码。

Requirements、版本与 marker 的通用语法由显式固定的 packaging 26.3 公开接口解析，
资源预算先限制单条文本、嵌套和 token，再限制根需求、总声明、extras 和求值次数。
运行库版本不符时拒绝，版本字符串只是兼容性拒绝条件，不能证明宿主代码来源；生产仍
须独立绑定校验器及其实际依赖字节。核心依赖声明、锁文件、生成 requirements 和许可证
范围保持一致，已有许可证原文及其来源不由此改写。

闭包处理精确 wheel 的 `Requires-Python`、`Requires-Dist` 和 `Provides-Extra`，使用固定
CPython 版本和 Windows x64 目标值。全部可用 marker 字段显式覆盖，不能借开发电脑的
默认环境作选择；无法从固定输入确定的 `platform_release`、`platform_version` 以及该
层没有定义的 `extras`/`dependency_groups` 字段均拒绝。根需求允许声明 extras，但不允许
在根 marker 中使用没有包上下文的 `extra`。

同一包由多个来源请求的 extras 会归一化并求并集，循环依赖通过有界固定点传播。
wheel marker 中的命名 `extra == ...` 按该包完整激活集合判断，支持与其他 marker 的
布尔组合；两个 extra 条件同时成立不会被拆成互斥的单值上下文。负向、集合比较或
其他未定义的 extra 操作拒绝。通用比较仍交给固定解析器，不执行任意 Python 表达式。

版本约束对宿主已经精确选择的发行版进行 PEP 440 检查，显式冻结的预发布版本也参与
约束判断，不据此自动选择预发布或升级。直接 URL/本地路径、可编辑来源、旧式/虚拟分发
关系和未知语法拒绝，错误不回显 URL；未激活的可选依赖保留条件摘要，不能据此声称
相关 extra 已具备。重复或模糊的 extra、重复 Requires-Python、缺项和冲突均在写入前失败。

当前消费的是已固定摘要的 wheel 元数据，不重建 sdist。`Dynamic` 注记以字段名记录，
不会让实际 wheel 的元数据在校验时重算；`Requires-External` 只记录摘要并保留未验证边界。
结果绑定每个元数据摘要、版本、激活 extras、活动/非活动依赖事实和目标环境，scope 仅为
`pinned-python-distributions`，`native_abi_verified` 始终为 false。声明闭包不能证明未声明的
动态导入、外部库、原生 ABI、程序行为或完整应用环境。

环境 v2 返回 `WindowsPythonClosedEnvironmentPlan`，计划摘要包含该闭包，文件系统观察
继续绑定这个摘要，因此修改根需求或依赖选择必须重新冻结、暂存和复验。v1 的数据形状、
复制计划和回读接口保留，仅有原来的暂存事实；旧记录不被静默补成依赖闭包或接受回执。
无论 v1/v2，当前状态仍为 `staged`，site、完整前缀和运行验收边界保持上一节的定义。

```python
from chatcopilot.core.dependency_closure import windows_dependency_plan

checked = windows_dependency_plan(
    host_pinned_wheel_tuple,
    python_version=host_frozen_target_python_version,
    requirements=host_frozen_root_requirement_tuple,
)
```

#### 原生交付的应用 host 依赖声明

原生载荷的两个生产入口 `studio.delivery.build_payload` 和
`integrations/live2d_desktop/scripts/prepare-runtime.py` 均在产出 artifacts 清单前校验
应用 host 依赖。根需求取自冻结源码的 `pyproject.toml` core 与宿主固定的 `agent` extra，
不允许候选或 UI 传入较小的根集合；目标 Python 取自同一源码中的 Windows runtime 锁。
`uv.lock` 及 host requirements 也必须属于调用方冻结的 source manifest。

受信构建宿主使用已准备的 uv 0.12.5，仅将 pyproject 和 uv.lock 复制到临时目录后执行
locked/offline export。默认依赖组、配置发现、缓存、Python 下载和源码构建均关闭，
子进程不继承父进程的凭据、代理、UV 配置或 PYTHONPATH；不复制或执行候选的构建后端。
宿主工具来自显式 `CHATCOPILOT_HARNESS_UV_BIN` 或已准备的固定版本工具路径，不从候选或
PATH 选择。版本检查仅拒绝不相容工具，不能证明工具来源；生产信任仍须独立绑定实际工具字节。
工作区、动态项目元数据、URL/本地依赖及带凭据的 registry 来源不在此 recipe 内。

导出结果与 `windows-host-requirements.lock` 的每条需求、marker、精确版本和全部 SHA256
按序比较，只有注释与空行不参与比较。缺项、哈希或版本漂移、过期 uv.lock、失败或
超时均阻止新载荷构建；不为使检查通过而修改来源锁或安装包。仓库原 requirements
生成和 drift 检查同时覆盖这份 host 锁，全部期望内容生成成功后才开始写派生文件。

载荷新增版本化 `windows-dependency-declarations.json`，绑定源码摘要、四个原始输入摘要、
core/agent 根需求、目标版本与规范导出摘要；artifacts 清单及已有 build receipt 记录它的摘要。
从旧 Live2D 资源复用二进制时，为当前源码重新生成声明，不复制旧来源的声明。
`read_bundle` 无需运行 uv 或候选代码即可按当前冻结源码重新核对声明的全部确定性字节，
因此重新计算 artifact 摘要也不能删减根需求或把 ABI/安装状态改成通过。
旧载荷缺声明时保留原有读取语义，不能因兼容读取而补成已验证声明或运行接受。

此项实际接入生产的**构建与载荷读取**，尚未让 bootstrap 使用 v2 隔离环境安装 API。
scope 仅为 `application-host-declarations`，状态为 `declared`；wheel 元数据闭包、原生 ABI
与安装接受标志固定为 false。它覆盖当前 host 的 core/agent 声明，不能代表插件、MCP、
其他 extra、未声明动态导入或所有加载对象已完整。后续须从这些绑定根需求选择实际固定
wheel 集合及声明闭包；现有 v3 映射见下节，前缀、完整加载策略与获授权的运行验收仍须完成。

#### 固定 Windows host wheel 输入与离线缓存

原生打包器和载荷准备脚本还会输出 `windows-host-wheel-inputs.json`。它从同一组冻结
pyproject、uv.lock、runtime 锁和 host requirements 推导目标需求，使用显式普通 CPython
3.12/3.13、Windows x64 与固定 packaging 解析器，不能取开发电脑默认平台或 ABI。
目标 tag 包括精确 CPython ABI、适用的 abi3 和通用 Python tag；按 tag 优先级与最高
build tag 选取一个 wheel，相同优先结果不唯一时拒绝。每个活动分发必须在 uv.lock 中
唯一匹配，所选 SHA256 必须同时属于该条 host requirement 的 hash 集合；缺 wheel
时不降级 sdist、不联网另选版本。非活动需求只记录摘要。

普通构建没有完整缓存时仅输出 `state: selected`，没有验证报告或离线 requirements，
不能把这个状态当成依赖已经准备好。构建宿主可以通过绝对路径环境变量
`AGENTSTRATA_NATIVE_HOST_WHEELHOUSE`，或 `build_payload(..., host_wheelhouse=...)` 提供
已准备缓存；原生资源脚本提供等价的 `--host-wheelhouse` 参数。缓存可含其他固定数据，
但只读入所选文件；缺失、大小、摘要或原字节审计失败时不复制任何 host wheel，也不修复缓存。
这些接口不自动下载、安装或运行外部软件。

完整缓存先逐个验证原始归档 pin、ZIP 边界、完整成员、元数据身份、文件名与 WHEEL tag
一致性及 RECORD 全覆盖/摘要/大小，再对整个集合执行冻结根需求的声明依赖与 extras 闭包。
仅输入审计支持 Core Metadata 2.5，并校验有界 Import-Name/Import-Namespace 声明与已声明
的冲突；大多数包未提供完整导入声明，不能据此证明全部 import 归属。Windows RECORD
的正反斜杠只在校验路径列时统一解释；归档和 RECORD 原字节不重写，标准化后的重复、
越界、缺项和哈希错误仍拒绝。原受限安装计划的 metadata、RECORD、脚本和 scheme 策略保持原样。

通过后只复制原始 wheel 到新建 `host-wheels/`，输出单一选中 SHA256 的
`host-requirements.lock`，以及 `windows-host-wheel-verification.json`。报告状态为
`verified-inputs`，绑定输入清单摘要、全部归档审计和声明闭包，并记录入口元数据、data
成员及原生文件供下一阶段使用。已有 artifacts/build 绑定包含这些摘要；载荷读取时
从冻结来源重算选择、原始归档审计和报告，并拒绝额外/缺少的缓存成员和被重签摘要的伪状态。
复用旧 Live2D 资源时按当前源码重建 host 选择与报告，不能复制旧版本的声明来冒充当前输入。

bootstrap 仅在完整报告、选择、固定 requirements、来源输入和 artifact 绑定一致时，
让 host bootstrap/application 两个环境使用 `--offline --no-index` 与专属 host-wheels。
原插件 requirements 和资源目录保持各自范围；不能让 host 缓存代替插件/MCP 完整集合。
未含完整报告的旧载荷仍沿用原准备流程，不自动补成离线或已验收状态。

这已接入现有**构建、读取与未来 bootstrap 输入消费**，但不执行安装验收。当前仍由原 uv
流程创建环境和生成脚本，而非 v2 受控隔离落地；前缀、路径配置、完整加载对象、原生
DLL/ABI 和实际程序均未由该报告验收。报告的 installation_mapping_verified、
native_abi_verified、installation_accepted 固定为 false，不能用报告或当前缓存的本机摘要
替代独立信任来源、发布接受或安装身份绑定。三处统一启动门禁继续拒绝。

源码入口：[目标选择与载荷](../../src/chatcopilot/deployment/windows_wheels.py)、
[原始 wheel 审计](../../src/chatcopilot/core/wheel_plan.py)和
[目标标记/输入闭包](../../src/chatcopilot/core/dependency_closure.py)。

#### Windows wheel 安装映射与隔离入口

`core.wheel_installation.windows_wheel_installation_plan` 将完整原始输入审计及声明闭包接到
显式 `windows-wheel-layout/v2` 转换；旧字节计划和 v1/v2 环境格式保持原有语义。
新计划保留每个来源成员的原始路径、归档摘要和字节摘要，统一检查所有发行版的目标
路径树及资源预算。普通模块和 purelib/platlib 资源映射到 site-packages；未知 scheme、
外来元数据、跨包覆盖及 Windows 大小写别名仍拒绝。

`.pth`、`._pth`、pyvenv.cfg 和 sitecustomize/usercustomize 成员改放
`Disabled/startup/<分发>/<原始路径摘要>.bin`；原 `.data/scripts` 文件放入
`Disabled/scripts/`，记录延后原因。这些目录不加入模块路径，原始脚本、启动钩子和
postinstall 均不执行。重生成的 INSTALLER/RECORD 覆盖重定位、隔离成员和生成入口，
原始 RECORD 继续按独立输入核验，不能改写后再充当来源。

console/gui 入口只接受有预算的严格元数据和模块/属性引用。命令冲突或大小写别名拒绝；
同名、同组、同目标及 extras 的多个提供者可合并，并在双方 RECORD 和入口描述中记录来源。
入口要求的 extra 未在闭包中激活时只记录延后状态。可转换入口生成 Python 包装脚本，
描述符明确选择 python.exe/pythonw.exe 及 `-I -S`；不生成二进制 launcher 或注册 PATH。
包装脚本在导入目标模块前检查隔离参数、禁用 site 和实际环境前缀，再解析属性、设置
argv 并传播退出结果。该检查只拒绝错误状态；它不自行修正前缀或证明目标运行成功。

环境输入 `agentstrata-windows-python-environment-inputs/v3` 必须显式使用
`windows-isolated-venv/v2`，在原环境字段之外绑定根需求。调用方仍须先持有独立采信的
输入 pin；它在文件系统操作前完成闭包及新映射，再复用私有 Windows 排他落地和身份回读。
禁用 site 的三个模块路径及标准库防遮蔽规则保持严格，不启用包提供的路径钩子、额外
DLL 目录或 pywin32 的安装动作。默认预算没有扩大；完整 host 集合须由可信调用方显式
选择有限的文件、目录和句柄预算，实验策略不能变成系统设置或自动安装授权。

完整缓存的生产构建额外生成 `windows-host-wheel-layout.json` 和
`windows-host-environment-inputs.json`，已有 artifacts/build receipt 绑定两者摘要；布局状态
为 planned，v3 输入绑定固定 Python 输入锁摘要和完整 host wheel 集合。新版输入审计报告
用 mapped_inputs 声明配套格式，读取时必须同时存在并按冻结源码、原始 wheel 重算；旧报告
只能保留原输入审计范围，不能授权新增映射。输入声明不含应用运行接受，来源及构建身份
仍由外层绑定；安装端不能自行采信这些摘要。

这已接入**生产构建与载荷读取**，并能供隔离暂存 API 消费；生产 bootstrap 尚未调用 v3
provisioner，仍沿原 uv 流程。文件回读、受控模块替身上的包装脚本测试和跨平台计划一致
均不能证明真实 `-S` 前缀、入口运行或原生加载成功。site 禁用时的 venv 前缀、完整应用
源码/插件/MCP、原生加载与 ABI、独立安装接受和更新事务仍待完成；三个启动门禁保持拒绝。

源码入口：[确定性映射](../../src/chatcopilot/core/wheel_installation.py)、
[环境 v3](../../src/chatcopilot/core/python_environment.py)、
[入口与篡改回归](../../tests/unit/test_wheel_installation.py)。

#### 独立运行接受与更新日志

生产 `build_payload`、原生资源准备及 `read_bundle` 已消费
`deployment.windows_python_inputs`。v2 来源必须携带原始
`windows-python-download-metadata.json` 和重算的 `windows-python-input-verification.json`；
读取时先按冻结源码认证输入锁，再验证整张供应方原始字节及每个精确用途，并核对 runtime/MCP
的解释器声明。artifact 摘要重签不能删除这两项或改写派生事实。v1 旧载荷可继续读取，
不继承新版来源或运行接受。报告仅为 resolved-inputs；归档字节审计、完整 wheel 闭包、运行及
安装接受均固定为 false，不能把输入一致性当成独立发布准入。

原始供应方表不纳入源码，不编码、裁剪表格或放宽公开来源规则。构建宿主可将原字节预先放在
`~/.cache/agentstrata/python-inputs/<供应方表 SHA256>.json`，默认离线读取；缓存路径中的
摘要须先满足固定格式，实际字节仍按来源锁的 SHA256/Git blob 认证，缓存自身不是权威。
构建方也可通过 `build_payload`/`pack` 的 `python_metadata` 或资源准备的 `--python-metadata`
提供完全匹配的原始字节。来源匹配的旧资源可以复用原字节，校验记录仍按当前源码重建；无输入时
拒绝，不下载、扫描既有 Python 或修复缓存。build receipt 分别绑定元数据及校验记录摘要，
host、插件和 Windows-MCP 作用域关联这些字节；bootstrap 仍未使用该元数据解包或执行 Python。
输入锁 pin 仍须随独立接受的发布来源取得，本机构建报告不签发这份权威。原始元数据只是
官方声明字节，不表示对应解释器归档已下载、审计、安装或运行。

生产构建、资源准备和载荷读取已绑定 `windows-runtime-requirements.json`；build receipt
记录其 SHA256。它从冻结应用源码派生 application、host、launcher、MCP 和工具作用域，
存在 components.yaml 时再绑定插件来源及 Windows-MCP。应用 ID 沿用已有显式身份，
不从目录名或已安装环境推断。每个作用域记录来源文件及输入集合摘要，读取时按源码
重算，重签 artifact 摘要不能删掉插件/MCP、替换来源或把 incomplete 改成 complete。
复用旧 Live2D 资源时为当前应用重建该清单，不继承旧清单的接受含义。

此版编译器固定输出 incomplete，没有自动晋级入口。host 的前缀/加载与 ABI、launcher
的受信发布镜像、应用源码注入及 MCP 执行器仍需独立核验。插件源码、Windows requirements、
资源锁和补丁属于可绑定的输入，尚不能证明完整插件 wheel、模型资产或运行环境。
Windows-MCP 现有独立的源码/解释器归档与 wheel 输入审计，范围见下节；它不代表桌宠插件的
全部环境、MCP 安装配方或实际前缀/加载已完成。不能复用 host 的解释器、缓存或摘要补造这些证据。
具体缺项由作用域 blockers 记录。

#### Windows-MCP 原始归档与依赖闭包

`windows-mcp.lock.json` v2 将项目 revision、原始源码归档 SHA256、pyproject/uv.lock 原字节摘要
和独立 MCP Python 用途绑定。`deployment.windows_mcp_inputs` 已接入生产资源准备、
`build_payload`、载荷读取及 runtime requirements；存在 components.yaml 时按原 bootstrap 的
实际准备范围要求 MCP 输入。旧无 schema 来源保留原来的 incomplete 含义，不自动采用新证据。

Git 源码 TAR 使用独立的 `git_source_tar_inventory`：只允许第一项是固定名称、52 字节且
完整匹配冻结 revision 的单个全局 comment，不接收全局 path、其他字段或重复全局记录。
原 `tar_inventory` 继续拒绝全局元数据。完整归档摘要在解压前验证，所有路径、类型、内容、
预算及 gzip/TAR 完整性沿用严格规则；元数据仅在内存中双解析回读，必须与完整清单逐项一致，
不提取源码到安装或执行项目。源码根必须精确对应该 revision。

原始 pyproject 和 uv.lock 决定全部默认根需求、约束、传递节点、extras 及显式 Windows/3.14
marker 环境，再选择唯一的兼容原始 wheel。不得从本机 Python、已装包、早前报告或 UI 删减根集合。
源码、解释器归档和锁声明图可分别审计；提供完整原始 wheel 缓存后，专用
`windows_mcp_dependency_input_inventory` 进一步核对每个 ZIP/RECORD、METADATA 的完整传递
闭包及 Requires-Python。宿主通用依赖入口仍仅接受原有 3.12/3.13；MCP 环境 v1/v2/v3 仍拒绝
未完成的 3.14 安装/加载配方。源码图与 wheel 元数据闭包分别记录，不能混为完整可运行应用。

MCP 数据审计另有独立 schema，仅额外识别旧生产器的 Root-Is-Purelib `True`/`False` 写法；
不修改 ZIP、原始 SHA 或 RECORD，不容许其他布尔写法、未知头或版本。通用 host 输入审计及
字节安装计划继续拒绝这些写法。v2 的此项数据兼容不授权安装、入口生成或加载。

载荷保留 `mcp-inputs` 原始源码/Python 归档、可选的完整 `mcp-wheels` 和可重算的
`windows-mcp-inputs.json`。缺项、额外成员、报告改写及依赖图变更均须按原始输入重新核对；
artifact/build 回执及 MCP scope 绑定这些字节，后续安装接受的 artifacts 绑定覆盖它们。
归档缓存可预先放入 `~/.cache/agentstrata/runtime-inputs/<SHA256>.tar.gz`，wheel 缓存位于
`~/.cache/agentstrata/mcp-wheels/<源码归档 SHA256>`；缓存自身不是权威，消费方不下载、修复或安装。
资源准备可显式传入 `--mcp-source-archive`、`--mcp-python-archive`、`--mcp-wheelhouse`。
重用匹配的原始资源时重新生成审计，不继承旧报告的接受含义。

v2 报告仅表示原始输入或 wheel 声明闭包 verified，项目安装、wheel 布局、前缀/入口、原生 DLL/ABI、
真实运行和独立安装接受保持未验证。runtime requirements 固定 incomplete。
新版策略的 bootstrap 在原 uv 准备之前拒绝实际 MCP 执行；`-InspectInputsOnly` 只读取载荷并返回
明确未验证运行接受的状态，不创建安装或 bootstrap 目录。它不是独立 verifier 或安全安装器，
不能替代发布权威、安装 ID/SID/目录身份绑定及全链运行验收；三处统一启动门禁保持拒绝。

v3 来源另行固定 `windows-mcp-source-copy/v1`，要求完整原始 wheel 集合。它按固定项目布局复制
包源码与许可，生成静态安装元数据和受限入口，合并依赖布局并检查整个目的树的路径、脚本及
包名碰撞。上游 `setuptools>=61` 仅作为未执行的构建声明记录；该配方不调用构建后端，也不
宣称复现上游 wheel。`windows-mcp-installation-plan.json` 由原始源码、源锁和 wheel 重算，
接入生产准备、读取、scope 和回执。v2 仍保留原数据审计语义；通用 host 安装入口不采用 MCP
旧式布尔头或 3.14 策略。布局计划及隔离数据副本不具备前缀、DLL/ABI、运行或安装接受的含义。

`core.mcp_materialize` 将同一来源的布局在全新私有实验目录复制并逐文件回读，保留失败现场；
它不创建 Python/venv 前缀或执行任何生成入口，阶段回执固定 `staged-data-only` 且不可授权运行。

插件的独立来源策略固定原始需求、完整哈希锁、上游与资产声明。生产读取必须重新核对完整
需求集合；可选的原始 wheel 数据按同一集合认证并验证完整元数据闭包。派生 build.json 及本机
包清单不登记新的 wheel 摘要；固定 PyTorch CPU URL 仅在插件来源策略中按原 URL、原锁哈希及
显式 Windows/Python ABI 核对，通用 host 继续拒绝直接 URL。

插件输入 v2 固定 `langdetect-paired-source-wheel-copy/v1`：同时认证原始源码 TAR、
Python-2 wheel 及已审查 setup.py 的独立摘要，逐文件核对两份归档中的包内容完全相同，
核对 Python-3 声明及 six 依赖。自有配方只重建 WHEEL、RECORD 和确定性 ZIP 容器；
不执行 setup.py、不调用上游构建后端，不将其称为供应方发布的 Python-3 wheel。
生产准备将原始 TAR 与 wheel 一起保存，读取时重新派生转换；运行声明绑定这两份原始输入，
不能用本机生成 wheel 或转换回执替代它们。v1 的 unresolved 源构建仍保持拒绝。

v2 另选插件专用的 `redundant-local-zip64/v1` 归档输入策略：普通局部头的 32 位大小、
CRC、压缩方法及标志必须先与中央目录一致，再允许单个 16 字节 ZIP64 扩展重复相同的
未压缩/压缩大小。大小哨兵、中央 ZIP64、流式记录、重复或不一致的扩展仍拒绝。
通用 ZIP、host 和 MCP 规则不采用此策略；它不形成安装映射或加载许可。
插件声明闭包保留全部原始需求及可选项，使用单独有限预算（最多 256 个根需求、每包
64 个可选项）；通用 host 的默认 128/32 及歧义拒绝不变。

插件输入 v3 进一步固定 `fixed-git-project-exact-patch/v1`。后端和前端分别按冻结
upstream/artifacts 原声明认证 Git revision、原始 TAR 和源码清单中的补丁摘要；全部成员经严格
清单与第二个流式读取器逐项核对后，才应用精确位置、行数及内容均匹配的普通编辑或 100644
新增文件补丁。不使用 Git、模糊匹配或上游构建脚本；二进制、重命名、删除、模式变化和
未支持的换行标记均拒绝。原始归档和补丁保留完整来源，计划记录每个输出文件的原始内容及
补丁摘要；项目和原始 wheel 可分别审计，缺少另一集合仍明确阻塞。

项目专用 `git_project_tar_inventory` 仅允许来源策略精确列举的有限 CJK 叶文件名；父目录为
ASCII，非 ASCII 字符限定无大小写和规范化变体的基本汉字。实际完整列表、大小写碰撞、路径别名、
链接、元数据、字节与成员预算均须通过，不能用其他 Unicode 字符替代。通用 TAR 和 MCP
仍采用原 ASCII 策略。文件名不重写，中文角色配置与第三方许可按原始内容保留。

生产 prepare-runtime 接受 `--plugin-wheelhouse`、配对的 `--plugin-sourcehouse` 及包含两份
原始项目 TAR 的 `--plugin-projecthouse`，在 vendor 构建前完成同一来源的审计。准备、载荷读取
和 runtime requirements 均重新消费原始归档与冻结补丁，重签目录摘要或派生报告不能替换输入。
可选 `--plugin-project-lab` 仅将重算的源码映射写入现有空私有实验目录：持有祖先和文件句柄，
独占创建并逐文件回读；按文件、字节、句柄及剩余空间预算拒绝，失败保留新建现场供复核。
阶段回执固定 source-data-only，不创建解释器前缀或运行应用。原生插件 provisioner 的直接入口
也先重新验证完整载荷和来源；这些尚未接受的原始输入不能进入旧项目复制、模型下载或客户端配置。

完整 wheel 声明闭包、项目源码映射与编译后前端、模型资产、工具、实际前缀、
DLL/ABI 及安装身份分别判断；后者未通过时仍保留 incomplete 并拒绝运行准备。
build_payload 仍可生成明确 incomplete 的源码/数据候选包，不能将它标为可运行的完整 Live2D 成品。

源码入口：[专用归档读取](../../src/chatcopilot/core/tar_inventory.py)、
[MCP 输入编译](../../src/chatcopilot/deployment/windows_mcp_inputs.py)、
[原始输入及拒绝回归](../../tests/unit/test_windows_mcp_inputs.py)。
插件入口：[原始输入编译与读取](../../src/chatcopilot/deployment/windows_plugin_inputs.py)、
[配对归档复制](../../src/chatcopilot/core/langdetect_wheel.py)及
[插件归档与有限预算攻击回归](../../tests/unit/test_plugin_archive_inputs.py)。
项目入口：[精确补丁与来源映射](../../src/chatcopilot/core/project_patch.py)、
[隔离文件落地](../../src/chatcopilot/core/project_materialize.py)、
[补丁与跨平台故障回归](../../tests/unit/test_project_patch.py)及
[生产读取和直接 provisioner 拒绝](../../tests/unit/test_plugin_project_inputs.py)。

`deployment.runtime_acceptance.validate_runtime_acceptance` 只消费外部已采信的回执与
完整作用域清单。调用方须从独立发布/接受渠道取得两个原始字节 pin 和可信安装绑定；
本机 JSON、自报摘要、文件系统暂存观察及候选声明均不能自行登记这些 pin。读取器先
认证字节，再检查封闭格式、完整作用域及输入摘要、各作用域计划/文件身份/验证摘要，
以及输入完整性、隔离前缀、入口、原生加载/ABI、应用运行、更新回滚和独立 review 的证据引用。
当前 incomplete 清单始终拒绝。它不签名、不签发接受、不执行引用证据中的测试，也不安装或启动软件。

安装绑定涵盖应用、安装 ID、受支持 Windows 用户 SID、安装根及数据根的宿主身份摘要、
应用源码、artifacts 和 launcher。根身份摘要由可信调用方取得，不是可写业务内容的快照；
更新时安装及数据身份保持一致，源码与 launcher 可以分别绑定新版本。

`deployment.runtime_update.RuntimeUpdateJournal` 复用 PrivateDatabase 的私有存储和 SQLite
事务，在专用 runtime-update.sqlite 中记录意图、期望代际、旧/目标独立接受摘要及历史观察。
重试必须复用同一意图；并发或过期请求拒绝。切换观察同样需要独立 pin，并绑定安装及意图。
观察目标时只记录 recorded 并增加代际，观察旧版本时记录 restored；其他结果保留
recovery_required，阻止新的切换，重启后须继续独立对账。只有紧邻当前代际的更新，才能
显式回到其独立接受的前一版本；旧意图、不同数据根及未知结果不能覆盖后续记录。

这是**接受消费契约与更新事务元数据**，不是已接入的 Windows 安装/回滚执行器。日志不
移动、删除或修复镜像，不改 M5 current.json，不控制服务，也不读取凭据或回滚业务数据。
它拒绝初始化或修复其他既有数据库。recorded 不表示运行健康或安装成功；日志自身不是
接受权威，也不提供对恶意本机所有者回退数据库的防护。生产 CLI 的 Windows update/rollback
仍不开放，原安装路径未调用该日志。受信 verifier、原子镜像切换、加载验收、外部代际采信、
失败恢复及真实升级/回滚仍须逐项接入；三处统一启动门禁保持拒绝。

源码入口：[作用域输入](../../src/chatcopilot/deployment/runtime_requirements.py)、
[生产 Python 输入](../../src/chatcopilot/deployment/windows_python_inputs.py)、
[接受消费](../../src/chatcopilot/deployment/runtime_acceptance.py)、
[更新日志](../../src/chatcopilot/deployment/runtime_update.py)。可复现的合成接受及原生私有
SQLite 回归不运行安装器或第三方程序：

```bash
.venv/bin/python -m pytest tests/unit/test_windows_python_inputs.py tests/unit/test_runtime_acceptance.py tests/unit/test_runtime_update.py tests/unit/test_desktop_studio.py tests/unit/test_native_application.py -q
```

#### 隔离 host 镜像安装与恢复

`deployment.isolated_runtime.IsolatedRuntimeInstallation` 已将原始输入暂存、独立接受消费、
实际数据镜像选择和更新日志接成同一条实验流程。它只在原生 Windows 的全新空私有目录
创建 data-only lab，不接管既有安装，不调用解释器、入口、pip、安装脚本或服务。重开 lab
必须提供调用方在创建时另行持有的 anchor，核对固定卷、SID、根/数据目录身份及严格角色；
anchor 仅约束写入位置，不是代码来源或运行接受权威。核心隔离树通过公开 owner_sid 属性提供已检查的属主身份。

`EnvironmentInputs` 保留原始不可变输入及独立环境 pin；先完成计划验证，再创建排他镜像
工作目录。失败镜像和记录均保留，不复用、清理或修复旧槽位。`IndependentAdmission`
只消费外部已采信的回执/requirements pin 和安装绑定；宿主 scope 必须同时绑定精确环境
输入根、计划和已落地文件身份。当前生产 requirements 仍为 incomplete，实验接口不会
替它补造接受；缺项或没有接受时保持未选择状态。

`core.python_environment_materialize.hold_python_environment_isolated` 在调用方操作期间
持有完整已验证镜像句柄，并在成功结束前复验成员集合、字节和身份；调用方异常保持原类型，
验证失败另记完整性错误。Windows 文件共享限制阻止对已持有文件写入/替换，新增成员仍由
结束前的布局检查发现。该上下文只保证这段验证操作的边界，不承诺释放句柄后的永久不可变。
原 v1/v2/v3 回读接口沿用它，计划格式和旧输入语义保持。

lab 的专用 SQLite selector 通过完整前值摘要执行原子比较替换，保留选择历史；读用接口
从选择记录定位调用方提供的同一份独立接受镜像，重新持有核验其真实文件。旧和目标镜像
在切换期间均受核验，用户可变数据位于独立 data 目录，不随镜像回退。更新日志可选绑定
selection_before_sha256；旧日志格式不被静默升级为新的隔离安装记录。

流程覆盖切换前、选择后日志确认前和确认后失败：未确认时恢复经独立核验的原选择；已经
确认的新版本须另建绑定前一版本的撤回意图，不回退代际。回滚中断后继续撤回，不能重新
激活被撤回候选。首次提交后没有前一版本时、恢复失败时，持久化 recovery fence 阻止
新切换和镜像读取，须显式完成或按已有意图恢复。限制只能由对应操作解除；回滚子意图接管
限制时核对原意图关系。未知选择、缺少精确接受、损坏或缺失的前版本均保留现场，不能猜成
恢复成功；显式继续目标也须重新核验其原始输入和接受。日志和 selector 仍不抵御恶意本机
所有者回退整个数据库，生产的外部代际及撤销权威尚待接入。

这已接入**隔离数据安装流程**，通过惰性合成归档及外部合成接受验证实际文件/SQLite 的
A→B→A、失败恢复和自有测试子进程突然退出。它不是实际安装器或第三方程序崩溃验收。
也可仅暂存真实固定 host 数据并验证接受缺项被拒绝。此层只重新核验 host 镜像；应用源码、
launcher、插件/MCP 的其他接受引用没有在这里变成已重新落地验证的完整应用环境。
生产 bootstrap、Windows update/rollback CLI 和启动门禁均未启用该实验路径；真实前缀、
入口、DLL/ABI、完整作用域验证、生产原子切换及独立运行接受仍须完成。

源码入口：[隔离编排](../../src/chatcopilot/deployment/isolated_runtime.py)、
[原子选择与恢复限制](../../src/chatcopilot/deployment/isolated_selection.py)、
[持有镜像验证](../../src/chatcopilot/core/python_environment_materialize.py)。回归使用新私有
测试目录，子进程只运行已有测试 Python 和自有故障脚本，不执行归档中的 Python 或包：

```bash
.venv/bin/python -m pytest tests/unit/test_isolated_runtime.py tests/unit/test_isolated_selection.py tests/unit/test_runtime_acceptance.py tests/unit/test_runtime_update.py tests/unit/test_python_environment.py -q
```

Linux 明确跳过 Windows 落地/句柄语义；原生 Windows 单列真实文件系统和子进程证据。
合成接受、文件完整性观察和 recorded 状态均不能替代真实软件运行或生产接受。

#### 生产准备接入条件

现有 `bootstrap-windows.ps1` 仍通过 uv 创建/同步环境并写入源码 `.pth`，不调用这个
受限接口。当前不改它，也不把已有环境扫描结果或其本机 JSON 变成来源授权。
接入须按以下依赖顺序继续：

1. 受信发布物先绑定校验器及其解释器/库字节，宿主从独立渠道采信环境 pin；不能先执行
   下载物，再让它证明自身可信。
2. 从已接受应用的实际源码、打包配置与所选 extras 导出完整根需求，绑定源码及构建身份；
   应用、插件和 MCP 的所有环境分别固定，不接受 UI 或候选任意缩小根集合。
3. 下载只作为固定字节输入，完成声明闭包及 v3 显式安装映射后，在全新私有槽位排他落地；保留失败
   现场，不把原目录的 `--clear`、同步或源码 `.pth` 当成已经受控的确定性转换。
4. 补完整前缀/脚本、应用源码注入、原生 DLL/进程加载与环境变量策略，并在获授权的隔离
   Windows 目标完成实际导入、ABI、运行和更新验收；本层没有执行这些步骤。
5. 独立接受回执再绑定应用、launcher、安装身份/SID、全部依赖输入、转换版本及验证结果，
   接受消费及更新日志契约已提供，实际发布/回滚执行器仍须保护旧版本和业务数据。文件系统观察摘要只用于暂存复验，不能代替回执。

应用 host 声明、固定 wheel 输入、完整缓存审计、安装映射及 v3 输入已接入原生构建和载荷读取；完整缓存可供原 bootstrap 离线消费，v3 隔离 provisioner 尚未接入。整条信任链
安全审查及真实验收完成前，UI、BFF 与原生启动门禁继续拒绝。

源码入口：[闭包校验](../../src/chatcopilot/core/dependency_closure.py)、
[v1/v2/v3 环境入口](../../src/chatcopilot/core/python_environment.py)与
[跨目标、循环和输入攻击回归](../../tests/unit/test_dependency_closure.py)。定向验证：

```bash
.venv/bin/python -m pytest tests/unit/test_wheel_installation.py tests/unit/test_windows_wheels.py tests/unit/test_dependency_closure.py tests/unit/test_python_environment.py tests/unit/test_python_materialize.py tests/unit/test_python_plan.py tests/unit/test_tar_inventory.py tests/unit/test_archive_inventory.py tests/unit/test_wheel_plan.py -q
```

实际固定发行版样例、跨平台计划和 Windows 文件落地证据留在交付记录，不能替代完整
应用依赖集或真实应用运行验收。

### Studio QQ 提示词交付

标准 QQ 配置与运行不依赖开发 worker。用户显式准备源码时，Studio 将公开引擎及当前应用的
BotSpec、Agent profile、提示词冻结为仅此应用的私有 Git 基线；该基线不带安装身份，也不是
AcceptedCandidate。准备不会切换正在使用的标准运行路径。应用的连接凭据、运行环境和私有
定义不进入源码。配置改变后旧基线失效，必须重新准备。

提示词改进使用该基线专属的 Harness、Eval socket 和结果目录，以及工作台冻结的独立验证
实现；首版只允许修改 identity.md 和 response.md，不改变渠道、工具、模型或 Gateway 权限。
导出仍调用既有 export_delivery，重新核对独立接受和基线、候选、确认的运行版本证据。
完整验收的普通应用源码可以生成此证据，不要求先伪造安装身份。

安装动作只能从该应用实际任务导出，再调用 install_bundle；不接受浏览器指定的目录或接受回执。
应用必须已停止，配置及来源仍匹配，安装不会自动启动。后续启动绑定安装源码、其相对 BotSpec
与私有环境；不得混用工作台的可变引擎。未知运行状态禁止重新生成端口和令牌，旧服务的归属与
启动参数不符时保留现场。配置变化或源码漂移会阻止运行已安装版本，不会悄悄回退到另一份源码。
历史开发结果保留在各基线的独立目录，旧候选不能覆盖新的基线。

此路径不开放仓库级全局 Eval/Harness，也不把本地准备、接受、安装或进程启动表示为真实模型
质量、QQ 连接或用户可见投递通过。源码安装后的提示词改进仍须显式操作，首版不是任意代码开发
或通用 M5 启用界面。

## 已验收版本启用与撤回

M5 使用 `HarnessController` 的准备、启用、查询、撤回、取消和对账入口。来源必须是当前协议下
已签发完整 `AcceptedCandidate` 的应用需求任务或绑定独立应用版本的本地反馈任务；实际候选快照、独立 Review、基线/候选/确认的
版本证据均重新核验。修改、取消、缺少版本事实或运行条件不一致的候选不能启用。
旧验收记录不会补造版本事实，需要重新验收后使用此入口。

目标是已登记的仓库部署实例，或显式选择的 `plugin:<plugin-id>:<agent-id>` 安装。
目标代码及应用配置必须与评测基线匹配，实际模型绑定和 Gateway 的 Python/依赖/native runtime
身份必须对应验收条件。不能借同名实例、模型或进程退出码复用旧接受结果。
插件目标仍要求与仓库评测应用配对；安装目录不会因此成为 M4 的任意可写配置来源。

每个实例在私有 `.agentstrata-versions/` 中保存基线和候选源码、内容清单、编译结果、切换记录与历史。
使用已安装解释器编译检查，然后由现有启动脚本或显式 `versioned` 服务选择源码版本和 Agent 配置。
`current.json` 只指向本安装的已准备版本，替换是原子的；程序依赖、凭据和可写实例数据保持原位置。
该目录及 Gateway 管理状态从 Agent 的文件资源视图中隐藏，不随源码同步或发布。

一次启用按以下顺序执行：

1. 记录绑定任务、候选和目标实例的意图；准备不停止服务。
2. 取得实例操作锁及准入锁，等待已运行任务、未确定执行和投递收尾。新的渠道输入继续进入已有持久队列，暂停认领；新的 RPC 回合返回 `gateway_updating`。
3. 等待桥接动作调用结束，停止已登记服务，并确认进程停止后替换版本指针。
4. 启动并核对实际进程、启动代际、回环监听端口、加载路径、代码/配置摘要及 runtime 环境，再记录启用成功。

启动后的进程记录实际加载事实，控制端另核对 PID、进程命令、监听归属和文件内容。
模型的最终文本、单次启动退出码和旧进程回执均不能替代这些事实。此核验说明版本已加载，
不代表真实模型能力或外部用户交付已经通过 M7 验收。

启用失败会尝试恢复原指针并重启上一版本；恢复失败记为 `recovery_required`，不能报告成功。
进程中断后先对账：已实际加载的目标版本可确认完成，已有撤回意图则继续撤回，不反向改判为启用成功。
取消请求和源任务失去启用资格会阻止继续切换。显式撤回只作用于当前版本，保留原操作及失败记录。
查询只读取记录与进程事实，不推进操作；`running_matches` 为未知时保留未知。

首版支持自有 Python 代码、桥接代码和冻结的公开 Agent 配置。依赖、上游补丁/构建资源及持久层变更
不通过普通切换入口，需要单独的构建或迁移/恢复验收。撤回程序版本不会撤销已发生的外部动作，
也不会覆盖后续业务数据。普通实例更新和插件重装须先撤回受管版本，避免两个更新路径同时生效。

源码入口：[事务](../../src/chatcopilot/deployment/activation_service.py)、[安装适配](../../src/chatcopilot/deployment/activation_runtime.py)、
[启动与身份](../../src/chatcopilot/core/release_runtime.py)、[准入](../../src/chatcopilot/core/release_gate.py)。
操作步骤见[启用与撤回](../guides/instances.md#启用与撤回已验收版本)。
插件专属目标及归属检查仍由[插件适配](../../src/chatcopilot/plugins/activation_runtime.py)提供。

凭据值不复制进版本，也不参与模型路由身份；保持同一认证引用的凭据轮换不改变代码版本。
一次切换仍冻结环境文件，期间出现并发修改时停止并保留诊断。已启用版本的显式撤回保留当前凭据，
不因为原需求任务后来取消而失去撤回能力。

## 两条受控交付路径

| 路径 | 授权与结果 |
| --- | --- |
| code-worker | Owner 明确调用独立代码任务，受信 worker 交付草稿 PR |
| Harness | 操作者启动正式修复或代码熵任务，冻结宿主在独立验收后交付正式 PR，并启用 GitHub 原生 squash 自动合并 |

## Harness

### 候选与发布资格

任务冻结远端 main、仓库、actor、任务分支与源码身份，在 `feat/harness-<task-id>` worktree 生成候选。
编程与审核 Agent 没有 Git 写权限或 GitHub 凭据。只有宿主签发的完整验收候选可以交付，正式提交
绑定审核过的精确文件、树和回归依据，不能用模型文本或进程退出码代替验收。

宿主执行独立审核、公开边界与秘密检查后，提交、普通推送并创建目标为 main 的正式 PR。
必需检查与审查不能绕过；不执行 Git hooks、强制推送或改写远端历史，不更新操作者本地 main，也不部署。
历史记录保持只读，不因重试获得新的发布权限；新请求不接受 `review_and_commit`。

### 对账与主干复验

重试先核对既有远端身份与交付意图，不重复创建提交或 PR。main 前进时先停止自动合并，恢复任务
worktree、整合 main，调用与首次修复相同的验收图重新验证和审核，通过后普通推送。
冲突、外部 head 变化或人工关闭自动合并会阻断自动进展，不能自动扩大修复范围。

明确取消在发布前阻止交付；已有 PR 时关闭自动合并并保留 PR，与合并竞争时以 GitHub 回执为准。
交付复验取消须确认外部测评收尾，不能改写已成立的修复结论或提前释放占用。
修复、交付、合并与清理分别记录，对账独立于 Console 页面。

### 归档与清理

PR 创建并核实提交后，本地任务工作区与分支可在归档验证后清理；失败任务也先归档再清理。
归档保留可恢复源码、Git bundle、补丁、验证证据和日志，不自动过期。活动进程、身份漂移、归档缺失
或损坏、现场外部修改及未归档内容均阻止删除。远端分支仅在 PR 已关闭或合并且 head 匹配登记提交时删除。

代码熵批次须等当前 PR 合并，或处理失败且 worker、验证与自动合并安全收尾后，再开始下一项。
实际交付与复验消耗批次预算，纯查询与等待不计时；预算耗尽仍可核对已发布 PR 并收尾，不能新发起复验。
配置、重试与定时器命令见[操作指南](../guides/harness.md#harness-pr-交付配置与恢复)。

## code-worker

Owner 明确调用 `start_code_task` 后，受信 worker 在独立的 `codex/<instance-id>/<task-id>` 分支
提交、非强制推送并创建草稿 PR。该授权不包含 merge、force-push、部署或修改操作者工作区。
执行隔离与认证边界见 [Agent](agent.md)，凭据准备见[外部服务指南](../guides/services.md#代码任务草稿-pr-凭据)。

## 源码入口

- [src/chatcopilot/harness/delivery.py](../../src/chatcopilot/harness/delivery.py)
- [src/chatcopilot/harness/delivery_archive.py](../../src/chatcopilot/harness/delivery_archive.py)
- [src/chatcopilot/harness/github_delivery.py](../../src/chatcopilot/harness/github_delivery.py)
- [src/chatcopilot/external_tools/repository_tasks](../../src/chatcopilot/external_tools/repository_tasks)
