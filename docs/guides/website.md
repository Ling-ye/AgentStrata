# 产品入口网站

[AgentStrata 产品网站](https://ling-ye.github.io/AgentStrata/)提供产品介绍、演示视频、公开源码与操作文档入口。
页面由 GitHub Pages 托管，站点源码位于 [website](../../website/index.html)，发布入口为
[Product website 工作流](../../.github/workflows/pages.yml)。它遵循[持续集成基线](../../specs/ci-validation/spec.md)
的现有检查边界；运行后端、模型连接、身份准入和 Console 仍按各自指南配置。

## 内容与素材

网站只包含静态 HTML、CSS、JavaScript 和明确选择的公开素材。页面不运行 Agent 后端，
不接受模型凭据，不声称提供托管聊天。安装与使用步骤链接到现有指南，避免维护第二套部署命令。
没有公开发行包时，Windows 入口指向真实的构建与使用说明，源码 ZIP 明确标为源码。

首页和产品页使用 V7 演示、封面和章节，在同目录托管视频。片中实际接入了本机 Live2D 桌宠，开发、验收和打包部分用步骤说明呈现。逐章说明、口播与字幕见[演示文字页](../../website/demo.html)。Console 图片沿用根 README 的公开示例，角色与第三方许可入口保留在页脚。

公开内容检查按精确路径、文件摘要和误报类型登记已审阅的媒体。历史素材和当前页面选用的素材分别维护；更换文件或字节后应重新审阅。凭据、身份和私有字面量继续检查。
规则和回归案例分别见 [公开检查](../../scripts/check_public_repo.py) 与
[边界测试](../../tests/unit/test_public_repo_check.py)。

## 本地预览

在仓库根目录运行：

```bash
python3 -m http.server 8080 --bind 127.0.0.1 --directory website
```

打开终端给出的本机地址，核对桌面与手机尺寸、键盘导航、菜单、视频播放和章节跳转、
「开始使用」、两个产品页、FAQ、资源与文档链接；禁用 JavaScript 时主要介绍、视频和文档链接仍可用。
图片、脚本和视频使用相对路径，支持 GitHub Pages 的项目路径。

## 发布与恢复

仓库 Pages 选择 GitHub Actions 作为发布源。主分支中网站或发布工作流变更触发部署，
也可手动运行工作流。工作流只打包 `website`，不上传仓库、工作区或用户配置。
Pages 的写权限和 OIDC 权限仅授予发布 job；既有分支保护与 CI 保持生效。

按仓库贡献流程提交并通过要求的检查后合并。部署成功后检查公开入口、视频、文字说明、
源码和文档链接。恢复时通过正常审查流程撤回相应网站提交，再重新部署；不回退应用数据或模型配置。
