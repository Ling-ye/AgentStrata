# 部署、秘密与公开边界

修改安装、配置保存、公开扫描或发行流程时阅读。日常命令见 [认证与服务](../guides/services.md)，发布流程见 [发布指南](../guides/releasing.md)。

## 源码入口

- [scripts/check_public_repo.py](../../scripts/check_public_repo.py)
- [scripts/check_secrets.sh](../../scripts/check_secrets.sh) / [.gitleaksignore](../../.gitleaksignore)
- [scripts/verify_release_artifacts.py](../../scripts/verify_release_artifacts.py)
- [deploy/wsl](../../deploy/wsl)
- [src/chatcopilot/botspec](../../src/chatcopilot/botspec)

## 不要写绝对路径到代码或 YAML

- **不要写绝对路径到代码或 YAML**；机器路径走 env，secret 走 `bots/<id>/local.env` 或本机 credential store。

## 公开源码不携带私有身份或端点

tracked 文件、示例、测试和可达 Git 历史禁止真实凭据、组织/租户端点、文档 token、平台账号/群号、显示名、稳定平台身份、私有项目名或机器路径；公开维护者身份只有 `Lingye` / `lingye` 与 `616202172@qq.com`。 `DEFAULT_OWNERS` / `DEFAULT_ADMINS` 保持为空，角色只由部署 env 显式配置；Unity/Windows 根目录走 `CHATCOPILOT_UNITY_SAMPLE_GAME_ROOT`、`CHATCOPILOT_UNITY_PROJECTS`、`CHATCOPILOT_WINDOWS_FS_EXTRA_ROOTS` 或 `CHATCOPILOT_WINDOWS_FS_ALLOWLIST`，空 Windows allow-list 失败关闭。

固定的第三方许可原文和依赖证据文件保留上游公开署名、邮箱、仓库和网站；扫描器只对
明确列出的这些文件区分上游归属信息，许可证据门禁另行校验来源和内容哈希。此范围不
扩展到第三方目录中的任意文件，仍检查凭据、私有主机与文档、机器路径、私有字面量和
未允许的维护者仓库。

## 私有语义清单

组织名、私有域名、文档 ID 和项目代号等语义规则只通过 `scripts/check_public_repo.py --private-literals-file` 从仓库外载入；文件必须由当前用户拥有、mode `0600`、非符号链接、single-link、UTF-8 且每行一个非空唯一字面量。 私有清单和匹配报告禁止进入任何仓库；扫描输出不得打印字面量或命中路径。

## Standard WSL bridge host

`wsl.localhost` is the standard host-only bridge for Windows access to WSL files; exempt only that exact literal inside the `agentstrata-private-host` rule, never a path, suffix, or arbitrary `.localhost` allowlist.

## `local.env` 路径语义

`provision-env` 保持非执行解析边界，只展开值开头的 `~`、`$HOME`、`${HOME}` 为部署用户主目录；其他 shell 变量和命令替换不执行。

## 官方仓库坐标

`https://github.com/Ling-ye/AgentStrata` 是唯一允许包含 `Ling-ye` 的公开仓库坐标；不得由此放宽其他维护者仓库名或额外公开身份。

## 公开与发布门禁

当前索引、工作区和未忽略候选必须通过 `scripts/check_public_repo.py`；首次公开、可见性变更和 Release 还必须通过 `scripts/check_public_repo.py --history` 与 `scripts/check_secrets.sh history`。 首次公开根提交使用 `--strict-git-identities` 核对 author/committer/tagger header 邮箱；常规历史扫描允许 commit/tag message 中的外部仓库链接和联系或签名邮箱，但文件、路径、历史 blob、URI secret、真实私有文档链接和外部私有 literal 仍严格扫描，不能阻断 Dependabot 或合法外部贡献者。 Release 从签名 annotated tag 开始，自动化只创建草稿；不发布 PyPI、不部署、不合并、不修改源码。`docs/guides/releasing.md` 是唯一事实源。

## 敏感扫描测试夹具

Gitleaks 的私有主机规则只匹配有合法左边界的主机，敏感查询规则只匹配 URI 中的 `?key=` / `&key=`；拒绝性测试需要用分段字面量在运行时构造私网地址、私有域名或假 secret，禁止用宽泛路径 allowlist、`gitleaks:allow` 或提交真实敏感值绕过门禁。

已提交且已核实为合成夹具的历史误报，允许在 `.gitleaksignore` 中登记
`commit:file:rule:line` 精确 fingerprint，并注明理由及修正提交。此表只用于历史扫描，
不适用于当前索引、工作区或未跟踪候选；不能按整个提交、文件或规则忽略，更不能登记真实秘密。
该文件是 Harness 冻结政策，候选修复不能自行新增扫描豁免。
秘密扫描使用独立的 findings 退出码区分命中和工具故障；公开日志仅列规则计数。
显式 `--keep-report` 才保留仓库外 mode `0700` 目录中的私有诊断，禁止上传为公开 artifact。

## 全新公开基线

首次公开从审计后的 tracked-only 文件树创建单个无父根提交，不复制旧 commit、tag、branch、notes、replace refs、LFS、submodule 或 GitHub 元数据；禁止 `--mirror`、`--all` 和批量 `--tags` 推送。 完整流程与 Git 结构验收以 `docs/reference/publication.md` 为事实源，提交、推送、远端创建、可见性修改和归档由维护者执行。

## Release 构建边界

`requirements/release-build.txt` 是手工复核的五包、全哈希、Python 3.12 build-only 闭包，不由兼容 requirements 生成。测试、构建、正常安装和带写权限的 draft Release 必须分 job；原始 sdist 在解包前验证，最终 wheel/sdist/notes/checksum 受校验和与 attestation 绑定。

正常安装探针选择主包已声明的 `acp` extra，覆盖评测模块导入所需的 ACP 接口；
不取得模型凭据或调用模型。无依赖安装探针继续独立验证包内资源和基础 CLI。

可选 VTuber 插件独立构建 wheel/sdist，由 `scripts/plugin_artifacts.py` 严格比对包身份、
部署资源和法律文件，并从 sdist 重建。插件与主包分别验证，同入 Release 的 checksum
和 attestation；安装器从匹配 Release 获取，不引入 PyPI 发布。插件下载的第三方软件和
依赖留在用户目录，其固定来源、摘要及原始许可随安装记录和下载内容保留。

## 许可与对应源码

自有代码和文档采用 AGPL-3.0-only，作者与例外以根 [COPYRIGHT](../../COPYRIGHT) 和
[第三方声明](../../THIRD_PARTY_NOTICES.md) 为准。历史已授予的 MIT 权利继续有效。
独立选配的 Open-LLM-VTuber 前端与前端补丁使用上游单独许可，商业使用须核对其额外授权。

部署者将 `CHATCOPILOT_SOURCE_URL` 设置为实际运行版本的免费可获取源码归档或明确的
版本源码入口。官方版本使用对应 tag/commit 的完整仓库归档；下游改动必须提供自己的
修改版及所需构建、安装脚本、无秘密必要配置与依赖源码。秘密和用户数据不进入归档。
Python sdist 仅提供 Python 包源码，不包含完整 Console 项目。

机器人沿用 `bots/<id>/local.env` 与现有 provision 流程注入此变量。Console、独立 HTTP/MCP
及 CLI 使用各自进程环境；Console 的 systemd 部署可在现有 service drop-in 中设置
`Environment=CHATCOPILOT_SOURCE_URL=...`，使其对应 Console 自身运行的源码版本。

Console「许可与源码」、CLI `agentstrata source`、机器人 `/source`、HTTP `/legal`
和 MCP 初始化说明展示相同声明。未设置部署源码时明确显示项目仓库与缺失状态，不能
据此通过对外部署验收。`agentstrata source --check` 检查是否已配置；发布前仍需实际下载
并核对该地址与运行版本、构建脚本和第三方源码是否对应，配置存在不证明可用或完整。

依赖变化后刷新并复核许可材料；离线校验由 `scripts/third_party_notices.py` 完成。
wheel、sdist 和前端构建必须携带各自所需法律文件；发布说明附对应版本的源码链接。
许可切换的第一个 commit 和首个 Release 在维护者实际提交、发布时记录，不预造标识。
