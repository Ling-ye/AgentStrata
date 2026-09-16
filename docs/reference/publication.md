# 部署、秘密与公开边界

修改安装、配置保存、公开扫描或发行流程时阅读。日常命令见 [认证与服务](../guides/services.md)，发布流程见 [发布指南](../guides/releasing.md)。

## 源码入口

- [scripts/check_public_repo.py](../../scripts/check_public_repo.py)
- [scripts/verify_release_artifacts.py](../../scripts/verify_release_artifacts.py)
- [deploy/wsl](../../deploy/wsl)
- [src/chatcopilot/botspec](../../src/chatcopilot/botspec)

## 不要写绝对路径到代码或 YAML

- **不要写绝对路径到代码或 YAML**；机器路径走 env，secret 走 `bots/<id>/local.env` 或本机 credential store。

## 公开源码不携带私有身份或端点

tracked 文件、示例、测试和可达 Git 历史禁止真实凭据、组织/租户端点、文档 token、平台账号/群号、显示名、稳定平台身份、私有项目名或机器路径；公开维护者身份只有 `Lingye` / `lingye` 与 `616202172@qq.com`。 `DEFAULT_OWNERS` / `DEFAULT_ADMINS` 保持为空，角色只由部署 env 显式配置；Unity/Windows 根目录走 `CHATCOPILOT_UNITY_SAMPLE_GAME_ROOT`、`CHATCOPILOT_UNITY_PROJECTS`、`CHATCOPILOT_WINDOWS_FS_EXTRA_ROOTS` 或 `CHATCOPILOT_WINDOWS_FS_ALLOWLIST`，空 Windows allow-list 失败关闭。

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

## 全新公开基线

首次公开从审计后的 tracked-only 文件树创建单个无父根提交，不复制旧 commit、tag、branch、notes、replace refs、LFS、submodule 或 GitHub 元数据；禁止 `--mirror`、`--all` 和批量 `--tags` 推送。 完整流程与 Git 结构验收以 `docs/reference/publication.md` 为事实源，提交、推送、远端创建、可见性修改和归档由维护者执行。

## Release 构建边界

`requirements/release-build.txt` 是手工复核的六包、全哈希、Python 3.10 build-only 闭包，不由兼容 requirements 生成。测试、构建、正常安装和带写权限的 draft Release 必须分 job；原始 sdist 在解包前验证，最终 wheel/sdist/notes/checksum 受校验和与 attestation 绑定。
