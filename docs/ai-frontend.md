# AI 前端设计与修复工作流

这份文档是 Codex、Cursor 和其他代码 AI 修改控制台前端时的共享入口。控制台前端当前技术栈是 React 18 + Rsbuild/Rspack + Arco Design + TanStack Query。

## 适用范围

- 修改 `console/web/**` 的页面、组件、样式和交互状态。
- 排查布局错位、文本溢出、按钮不可用、状态展示错误、弹窗/抽屉异常、表格滚动异常。
- 设计或重排控制台页面的信息层级。

## 默认策略

1. 先读现有实现：相关页面、组件、`console/web/src/styles.css`、`console/web/package.json` 和已有 Arco 用法。
2. 复用现有栈：React 18、Rsbuild、Arco、TanStack Query；不要默认引入 Tailwind、shadcn、Ant Design、Storybook、Chromatic 或新的视觉测试依赖。
3. 先修业务路径：确认页面任务、主要操作、成功/加载/空/错误/禁用状态，再做视觉调整；组件迁移必须保留旧页面的信息字段和操作入口。
4. 保持控制台气质：信息密集、安静、可扫描，避免营销页式 Hero、氛围背景、渐变球、插画装饰和卡片套卡片。
5. 使用真实浏览器验证：能启动前端时检查桌面和窄屏；没有浏览器工具时至少完成构建并说明未做视觉验证。
6. 触碰页面时必须使用原生 Arco API；旧 UI 语义兼容层已移除，不给新代码继续承载旧 Semi JSX 语义。
7. 徽标与状态标签内部始终横向单行展示：空间不足时按整枚标签换行，单枚超长文本使用省略号并通过 `title` 或 `Tooltip` 提供全文，禁止把中英文字符压成竖排。

## 设计工作流

1. 识别用户和任务：程序员/运维要完成什么操作，异常时先看哪里。
2. 拆信息结构：摘要、列表、详情、操作区、反馈区分开设计。
3. 选组件：优先 Arco 的 `Button`、`Tag`、`Tabs`、`Table`、`Modal`、`Drawer`、`Descriptions`、`Typography`、`Space`、`Tooltip`；共享组件只保留 `PageSection`、日志/任务抽屉、可调整列宽表格 hook 这类业务语义。
4. 定状态：正常、loading、empty、error、disabled、长文本、窄屏都要有合理表现。
5. 控制样式外溢：共享样式放 `styles/**`；局部样式保持小而清晰。

## 数据与状态

- 服务端读取、轮询、刷新优先走 TanStack Query。
- SSE 日志流和任务流继续用专用 hook，不塞进 Query。
- 表格列宽、抽屉打开状态、case 选择等局部 UI 状态留在组件内。

## 快速验证

```powershell
cd console/web
npm run build
```

只改文档或规则时，至少运行：

```powershell
git diff --check
```

能启动前端时：

```powershell
cd console/web
npm run dev
```

检查项：信息层级、对齐、文本溢出、按钮尺寸、loading/empty/error、抽屉和表格滚动、桌面宽度和窄屏宽度；验收以当前可见页面和可交互元素为准，不用隐藏 DOM 文本代替视觉检查。
