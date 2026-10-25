# 角色库与私有预设

生成插件 `0.2.0` 起提供“我的角色”“预设目录”和版本详情。插件仍独立于 AgentRuntime；
角色切换改变模型、Bot 身份/表达风格与音色，不创建新的聊天或长期记忆。安装前提及模型
质量边界见 [Live2D 指南](live2d.md)，数据与依赖契约见[角色库规格](../../specs/character-presets/spec.md)。

## 保存自己的角色

每个角色使用稳定 UUID，显示名可以修改，同名不会自动合并。开始生成前先保存草稿及
原始输入，成功后将模型、提示词、参考图、分层素材复制为独立版本。即使生成失败，
已接收的原稿也随角色备份保存。任务目录中的配置和日志
不收录；清理任务不影响已经保存的角色。保存阶段失败有独立提示，可稍后手动收录任务。

“保存草稿”只保存编辑内容；“保存新版本”保留当前选中版本的模型及创作资料，并使用
新的人设与声音。历史版本不会覆盖。选择历史版本后编辑，可以基于它另存新版。
下载的角色版本只读，通过“创建本地副本”取得新 ID，来源角色与版本会被记录。

默认数据位于生成插件安装目录：

| 位置 | 内容 |
| --- | --- |
| `data/jobs/` | 生成任务、输入、工作日志和阶段产物 |
| `data/characters/<id>/character.json` | 稳定身份、创建时间与来源 |
| `data/characters/<id>/draft.json` | 当前草稿 |
| `data/characters/<id>/draft-sources/<job>/` | 生成前保存的原始输入、提示词及人设快照 |
| `data/characters/<id>/versions/<version>/` | 不可变模型、预览、资料、创作源文件及摘要 |
| `data/characters/.catalog/` | 按源隔离的目录和预览缓存，不进入工程备份 |

升级和卸载保留 `data/`；没有自动清理历史版本。持久目录不等于备份，重要原稿请定期
导出到另一存储位置。更换插件根目录不会自动搬迁角色库，使用下述备份/恢复操作。

## CLI 与完整工程备份

所有 `characters` 命令的 `--root` 均为生成任务目录，角色库在其同级 `characters/`。
工作台与角色 CLI 复用同一文件锁，可以查询，冲突的保存操作会明确拒绝。

```bash
plugin_root="${XDG_DATA_HOME:-$HOME/.local/share}/agentstrata/plugins"
generator="$plugin_root/live2d-generator/runtime/venv/bin/agentstrata-live2d"
jobs="$plugin_root/live2d-generator/data/jobs"
config="$plugin_root/live2d-generator/config/generator.json"
"$generator" characters list --root "$jobs"
"$generator" characters collect --root "$jobs" --job JOB_ID
"$generator" characters show --root "$jobs" --id CHARACTER_ID
"$generator" characters backup --root "$jobs" --destination /path/to/new-backup
"$generator" characters restore --root "$jobs" --source /path/to/new-backup
```

`backup --id CHARACTER_ID` 只导出一个完整角色工程；省略 ID 导出整库。输出为包含原稿、
草稿、全部版本及 `backup.json` 摘要的目录，目标必须不存在且位于角色库之外。
恢复校验全部资料再检查冲突；相同 ID 的相同内容跳过，不同内容拒绝整个恢复请求。
不会携带客户端对话、记忆或凭据。备份可以很大，不受模型 ZIP 的 48 MiB 上传限制；
使用目录复制或自己的备份工具搬运即可，不需要 Git 或云同步服务。

编辑角色资料使用完整 JSON 文档，例如：

```json
{
  "name": "示例角色",
  "description": "温和的观星伙伴",
  "identity": "你是示例角色，喜欢观察星空。",
  "response_style": "使用简短、自然的中文交流。",
  "voice": {"provider": "edge_tts", "voice": "zh-CN-XiaoxiaoNeural"},
  "author": "Example",
  "license": "仅限个人使用，分享前需另行授权",
  "source": "原创角色资料"
}
```

```bash
"$generator" characters create --root "$jobs" --profile /path/to/profile.json
"$generator" characters save-draft --root "$jobs" --id CHARACTER_ID --profile /path/to/profile.json
"$generator" characters save-version --root "$jobs" --id CHARACTER_ID --base-version 1
"$generator" characters import-model --root "$jobs" --id CHARACTER_ID --bundle /path/to/model.zip
```

新角色导入模型时用 `--profile` 替代 `--id`。模型 ZIP 须有 `preview.png`；CLI 可以用
`--preview` 提供独立预览。生成已有角色用 `generate --character-id CHARACTER_ID`。
首版仅支持 Edge TTS 音色 ID，不导入语音服务地址、密钥、参考音频或训练权重。

## 成品预设与客户端应用

成品预设为 `agentstrata-character-preset-v1` ZIP，固定包含 `preset.json`、`profile.json`、
`avatar.zip` 和 `preview.png`。内部模型沿用原有模型包格式；外层摘要覆盖全部成品文件。
导出前填写身份、作者、许可、来源并保存版本。版本导出可复现，不包含原稿、提示词、
任务配置或日志；人设正文是你主动选择分享的内容，请检查后再分发。

```bash
"$generator" characters export --root "$jobs" --id CHARACTER_ID --version 1 --destination /path/to/preset.zip
"$generator" characters import --root "$jobs" --bundle /path/to/preset.zip
"$generator" characters fork --root "$jobs" --id CHARACTER_ID --version 1
agentstrata plugin stop open-llm-vtuber
"$generator" characters apply --root "$jobs" --id CHARACTER_ID --version 1 \
  --vtuber-root "$plugin_root/open-llm-vtuber"
agentstrata plugin start open-llm-vtuber
```

应用需要现有受管客户端与标准 Bot identity/style 文件绑定；用户自定义绑定不会被
静默覆盖。模型、动作映射、人设与音色作为一次事务更新，返回可撤回的 `backup` ID。
模型连接、Bot ID、工具权限、运行时人格及会话/记忆数据保持原样。
撤回沿用 `restore --backup BACKUP_ID --vtuber-root ...`，也可在工作台输入备份 ID。
应用和撤回前都必须停止客户端；已有后续配置编辑时拒绝覆盖。中断的 journal 会阻止
客户端启动，应先恢复。再次使用已安装模型可以直接应用对应角色版本。

工作台的目标客户端由本机 `config/runtime.env` 中的 `AGENTSTRATA_LIVE2D_VTUBER_ROOT`
指定，值为客户端安装目录的绝对路径；浏览器不能提交任意目标路径。

## 私有目录与按需下载

私有仓库坐标与 token 只保存在生成器的本机 `config/runtime.env`，不要写入源码、
角色预设或公开包。环境文件为非执行的环境键值配置，编辑后重新启动工作台。

```dotenv
AGENTSTRATA_LIVE2D_PRESET_REPOSITORY="owner/character-presets"
AGENTSTRATA_LIVE2D_PRESET_TOKEN=""
AGENTSTRATA_LIVE2D_VTUBER_ROOT=""
```

使用能读取目标仓库 Contents 的 token。浏览器只看到目录内容与配置状态，不读取 token。
GitHub API 请求携带认证；Release 资产重定向仅允许 GitHub 资产域名且不转发认证头，
临时下载 URL 不保存，传输日志不记录带签名的 URL 或响应头。

```bash
"$generator" characters refresh --root "$jobs" --config "$config"
"$generator" characters catalog --root "$jobs" --config "$config"
"$generator" characters download --root "$jobs" --config "$config" --id CHARACTER_ID --version 1
```

刷新从仓库 `main` 的 `catalog.json` 获取目录，成功后替换本机缓存；失败保留旧缓存。
角色模型按需下载、校验后加入角色库，不自动更新或应用。未认证、无权限、限流及下载
失败会明确报告，已保存角色不受影响。预览按需缓存；离线仍可浏览缓存和使用本地模型，
语音和 LLM 的联网要求不变。私有目录和缩略图不随公开插件 wheel 分发。

## 准备精选预设发布

生成任务不会自动上传。维护者从已检查的版本准备发布资料：

```bash
"$generator" characters release-files --root "$jobs" --id CHARACTER_ID --version 1 \
  --destination /path/to/release-files
```

新目录含 `preset.zip`、`preview.png` 与 `release.json`。将两个二进制附件上传到已确认
提交对应的私有 Release；保存返回的资产 ID，再生成目录条目与汇总目录：

```bash
"$generator" characters release-entry --root "$jobs" --release /path/to/release-files \
  --asset-id MODEL_ASSET_ID --preview-asset-id PREVIEW_ASSET_ID --destination /path/to/entry.json
"$generator" characters build-catalog --root "$jobs" --entry /path/to/entry.json \
  --destination /path/to/catalog.json
```

多个版本传入多个 `--entry`。目录索引由条目生成，模型与预览摘要来自实际上传文件；
不手工维护第二份摘要或临时链接。Git 仓库仅收录条目、介绍、来源、许可和目录，模型
放 Release 附件。发布后的版本不替换，修订创建新版本。仓库可见性、Git 提交和 Release
发布由维护者显式控制，插件不执行 Git 或取得发布权限。
