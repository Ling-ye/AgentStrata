# Console 协作契约

修改控制台时结合 [前端工作流](ai-frontend.md)、[运行时契约](ai-contracts-runtime.md) 和 [根协作入口](../AGENTS.md) 阅读相关章节。

## 控制台前端约定

- 控制台是运维工作台，不是营销页：信息密集、安静、可扫描，优先支持重复运维操作和异常定位。
- Console 管理视图直接展示实例配置与私有观测原值，包括身份名单、凭据、环境引用和路径；不再次脱敏已有可读取字段。值仅进入私有存储及禁止缓存的 API，不进入公开源码或诊断导出；历史已省略字段不能补造。共享 artifact、Evaluation、群聊/工具授权及隐藏推理边界保持，契约见 `specs/console-operator-visible-values/spec.md`。
- 保持 React 18 + Rsbuild/Rspack + Arco Design + TanStack Query；不默认引入 Tailwind、shadcn、MUI、Storybook 或 Playwright 视觉测试等新栈。
- 触碰页面时使用原生 Arco API，不恢复旧 UI 语义兼容层。
- 文本、状态标签和按钮层级优先复用 `styles/tokens.css`、`styles/components.css` 与 `shared/ui/status.ts`。
- 修改后优先用当前 AI 环境已有浏览器工具检查桌面和窄屏；没有浏览器时至少完成构建并说明未做视觉验证。

### 控制台页面

| 页面 | 功能 |
| --- | --- |
| 总览 | 实例状态汇总 |
| 服务管理 | 按 Channel 接入与外部能力查看服务职责、状态、诊断与日志 |
| 机器人实例 | 任务 / 分层配置 / 运行状态 / 能力与工具，四个同级页签；任务内左侧列表、右侧流程，窄屏单列切换；服务日志统一入口 |
| 组件目录 | 按 tools / prompts / agents / context 四个 surface 统一浏览工具、提示词、Agent 和上下文组件（只读卡片） |
| 质量评测 | 新建评测 / 评测记录 / 任务集 |
| 设置 | 控制台本身 |

### 控制台 API

| 端点 | 方法 | 用途 |
| --- | --- | --- |
| `/api/bots/{id}/gateway-observation` | GET | 当前实例 Gateway run 和独立准入审计 |
| `/api/bots/{id}/gateway-observation/runs/{run_id}` | GET | 当前 run 的诊断事件、审批和交付回执 |
| `/api/catalog` | GET | 统一组件目录（tools + prompts + agents + context） |
| `/api/catalog/{item_id}` | GET | 单个目录条目 |
| `/api/bots/{id}/tools` | GET | 读取实例当前工具配置 |
| `/api/bots/{id}/tools` | PUT | 写回工具配置；`?apply=true` 时同步到运行实例并重启 |

### 工具配置编辑机制

- 前端 `BotToolEditor` 使用四面 DTO：`tools.packs/features/hide/mcp.servers` 与 `agents.presets/workflows`，并融合 inventory 诊断信息按 tools / prompts / agents / context 页签展示本地能力、MCP 健康、提示词、子代理预算、workflow 和上下文配置。
- 后端 `console/control/yaml_editor.py` 使用 `ruamel.yaml` round-trip 编辑 `bot.yaml` 和 `mcp/servers.yaml`，保留注释和格式；该依赖已声明在 `console/requirements.txt`，`deploy_console.sh` / `setup_console.sh` 安装时会一并装入 venv。
- `console/control/catalog.py` 通过 `component_catalog` 读取 tool pack / tool feature / MCP catalog / subagent preset / workflow DTO，并聚合提示词占位和上下文来源占位为统一 `CatalogItem`。
-  编辑后点「保存并重启」会先取得同实例 TaskManager 串行资格，再写入源仓配置并调用统一 `update_instance.sh`；该入口通常同步后快速应用配置并重启，只有依赖、安装脚本变化或实例 venv 缺失时才完整 bootstrap。仅「保存配置」同步写源仓。配置修改留在 WSL 源仓，由用户在 WSL git 工作区提交。

