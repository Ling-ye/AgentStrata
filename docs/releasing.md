# AgentStrata 发布手册

本文是公开仓库后续版本发布的唯一事实源。首次公开基线的文件树与单根提交边界见
[`fresh-public-repository-bootstrap`](../specs/fresh-public-repository-bootstrap/spec.md)。
首次公开只包含 `0.1.0.dev0` 源码，不创建 tag 或 GitHub Release。

## 发布边界

- Release 必须从已经合并到 `main` 的版本准备 PR 开始；不直接在 tag 上修改源码。
- 只接受形如 `v1.2.3` 的、签名的 annotated tag。轻量 tag、未签名 tag 和预发布版本
  不进入当前 workflow。
- `.github/workflows/release.yml` 只验证、构建并创建 draft GitHub Release；它不合并
  PR、不发布 PyPI、不部署、不重启服务，也不修改源码。
- Git 提交、tag、push、workflow dispatch 与 draft Release 发布都由维护者执行。
- 发布构建只使用 tag checkout；不要从长期开发工作树打包，也不要推送 `--all`、
  `--tags` 或 `--mirror`。
- Release 前必须重新扫描完整可达历史。真实凭据一旦暴露，先撤销或轮换；删除文件或
  Git 对象不能恢复凭据安全性。

## 1. 准备版本 PR

在普通功能分支中完成以下变更：

1. 把 `pyproject.toml` 的版本从开发版本改为稳定版本，例如 `0.1.0`。
2. 把 `CHANGELOG.md` 的 `Unreleased` 内容移入带 UTC 日期的版本节，并重新保留空的
   `Unreleased` 节。
3. 确认 README、badge、包元数据和文档不声称尚未存在的 tag 或 Release。
4. 运行完整门禁；如果维护者有语义隐私清单，再额外运行本地私有字面量扫描。

```bash
.venv/bin/python scripts/check_public_repo.py
.venv/bin/python scripts/check_public_repo.py --history
bash scripts/check_secrets.sh history
.venv/bin/python scripts/check_sdd_specs.py
.venv/bin/python scripts/check_architecture.py
.venv/bin/python scripts/check_repo.py full
npm test --prefix console/web
git diff --check
```

私有语义清单必须在仓库外，归当前用户所有，权限为 `0600`，不是符号链接且只有一个
硬链接。扫描结果不会打印私有字面量或命中路径：

```bash
.venv/bin/python scripts/check_public_repo.py \
  --private-literals-file /absolute/path/to/private-literals.txt
```

PR 合并前确认 CI 全部通过，版本号、Changelog 日期和计划 tag 完全一致。

## 2. 创建并推送签名 tag

从最新的远端 `main` 创建 tag。下面命令由维护者本人执行：

```bash
git fetch origin main --no-tags
git switch main
git pull --ff-only origin main
git status --short
git tag -s v0.1.0 -m "发布 v0.1.0"
git verify-tag v0.1.0
git push origin refs/tags/v0.1.0
```

`git status --short` 必须为空。不要复用、移动或覆盖已经推送的版本 tag；发现 tag 指向
错误时停止发布并明确处理，不用 force-push 隐藏错误。

## 3. 运行 Release workflow

在 GitHub Actions 中选择 tag ref，并把同一 tag 作为 workflow 输入。也可以由维护者
执行：

```bash
gh workflow run release.yml --ref v0.1.0 -f tag=v0.1.0
gh run watch
```

workflow 会分别执行验证、构建和创建草稿三个权限隔离的 job：

- 核对仓库已公开、输入 tag 与运行 ref 相同、tag 为 GitHub 验证通过的签名 annotated
  tag，并且 tag commit 是 `main` 的祖先。
- 执行公开历史扫描、Gitleaks、release metadata 检查、完整仓库门禁和 Console 测试。
- 使用 `requirements/release-build.txt` 的全哈希 build-only 闭包，在 Python 3.10 上两次
  构建 wheel 与 sdist，验证内容、安装、从 sdist 重建 wheel 以及归一化产物一致性。
- 为 wheel、sdist、release notes 和 checksum 生成校验和与 attestation，并以
  `contents: write` 只创建 draft GitHub Release。

失败时不要创建替代 tag。修复源码后走新的版本 PR，并根据错误是否发生在公开 tag 前
决定使用新的补丁版本；已公开的版本标识不可重写。

## 4. 审核并发布草稿

维护者在 GitHub 上逐项核对：

- Release 的 target commit 与签名 tag commit 一致。
- workflow 的 verify、build、draft jobs 全部成功。
- wheel、sdist、release notes、checksum 与 attestation 文件齐全。
- `SHA256SUMS` 与下载后的资产一致，release notes 只来自该版本 Changelog。
- 产物中没有本地 env、凭据、私有配置、日志、数据库、缓存或未声明资源。

核对完成后手工发布 draft。当前发布流程不上传 PyPI；若未来引入包索引发布，必须先
建立独立规格、短期凭据和权限隔离 job，不能把写权限并入现有验证或构建 job。

## 部分失败恢复

- 验证或构建失败：修复源码并重新走版本 PR；不要手工拼装未经同一 workflow 验证的
  Release 资产。
- draft 创建失败但验证和构建成功：先确认没有同名 Release，再对同一 workflow run
  重试失败 job；禁止静默创建重复 Release。
- draft 已存在但资产不完整：保持 draft，核对 tag 和 run identity 后恢复上传；不删除
  或替换已经公开的 tag。
- draft 审核失败：保持不发布，记录失败原因；源码问题通过新 PR 修复。
