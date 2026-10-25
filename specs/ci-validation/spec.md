---
id: ci-validation
type: architecture
status: implemented
created: 2026-10-20
---

# 持续集成与本地验证

## Summary

CI 与本地共用仓库检查入口，验证公开边界、Python 和 Console。环境准备失败必须修复环境，
不能通过删除回归、吞掉退出码或跳过安全测试使检查变绿。运行结果留在 CI 或交付记录中。

## Design

检查范围与开发命令以[开发指南](../../docs/guides/development.md)为唯一正文，
公开扫描遵循[公开边界](../../docs/reference/publication.md)，
变更基准遵循[文档治理](../documentation-governance/spec.md)。

CI 使用固定 Ubuntu 版本、明确的 Python 支持边界和 Node 主版本；Python 从 uv.lock
严格同步，前端从 package-lock.json 安装。检查入口分别提供 Python、Console 和二者组合的
完整检查，避免 workflow 复制本地门禁命令清单。
Python 支持范围为 3.12–3.13；日常 CI 在 3.13 上执行主程序完整检查，发行构建与安装
使用最低支持版本 3.12。可选 VTuber 另设 Python 3.12 兼容 job，与本地共用脚本验证
固定上游和安装包，使用替代语音及 Agent，不取得模型凭据。3.12 不运行主程序全量回归；
3.11 及更早版本不再满足新版安装条件。

Linux 测试依赖必须实际可用。仅在一次性 GitHub runner 上安装系统工具和 Bubblewrap 的
AppArmor userns profile，不修改部署宿主策略，不用 skip 替代隔离测试。

公开历史扫描保留全部历史；已审阅的合成夹具误报只能按提交、文件、规则和行号的精确
fingerprint 豁免。新提交及同文件的其他命中继续失败。公开日志只提供规则计数；
详细报告必须显式请求并存于仓库外私有临时目录。

CI 的 GitHub token 仅有 `contents: read` 权限，不持久化 Git 凭据，不取得部署凭据。
核心 Python 完整回归、Console 和历史安全扫描在主分支、PR 与手动运行中始终执行，
workflow 不使用顶层路径过滤。可选插件与桌面验证按已知依赖路径选择，变更基准和重命名
复用现有 Git 处理；缺失基准、未分类路径按全部扩展处理。手动运行选择全部扩展并构建
Windows 安装包，日常相关桌面改动仅做依赖安装、单测和原生 Electron smoke。
持续启用的检查名保持稳定；新增、移除或更名
检查须由维护者同步分支保护。Release 的验证环境复用 CI 准备流程，签名 tag、独立构建和草稿发布边界不变。

复制或逐文件比对 Git 元数据的测试仓库在首次提交前关闭仓库级自动维护，避免后台维护
锁与夹具操作竞争。生产仓库设置不变，必要文件丢失仍然报错。
CI 使用检查入口的私有 report-dir，在 Summary 只展示状态、失败用例标识与跳过原因，
不上传原始失败正文、日志或秘密扫描详情；重复检查不得复用旧 JUnit 证据。

## Acceptance

- 当前工作树通过仍不足以覆盖历史；历史扫描继续检出新增或未经审阅的命中。
- 主包锁文件覆盖 Python 3.12 和 3.13，锁文件过期时同步失败；3.11 安装被包元数据拒绝。
- 本地 full 覆盖 CI 的 Python 与 Console 检查，包含前端测试。
- 缺失系统能力、依赖安装失败、检查失败和取消不被报告为成功。
- 失败有可定位的检查名和诊断，公共日志不泄漏秘密扫描内容或命中路径。
- 扩展选择覆盖删除、跨目录重命名和未知基准；核心检查不因变更范围被跳过。
- Windows 仅验证模式不请求打包，默认完整构建仍生成安装包；失败和进程终止保留非零结果。

## Verification

验证 profile 组合、workflow 权限与锁文件同步、PR/push 基准传递和失败诊断。
对精确历史例外和新增命中分别运行固定版本的 Gitleaks，随后执行完整仓库检查。
本地验证不代表 GitHub runner 或正式发布执行成功；远端结果需提交后独立确认。
