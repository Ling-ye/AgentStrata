---
id: console-operator-visible-values
type: architecture
status: implemented
created: 2026-09-08
---

# Console 配置与观测原值

## Summary

Console 面向部署者和代码拥有者，直接展示所管理实例的配置与已有观测原值。权限名单显示完整 QQ 群号、用户号；环境引用、凭据、端点和目录显示实际配置值，不增加解锁、掩码或来源切换操作。

## Design

- 当前分层配置从实例配置及环境读取原值。权限策略包含 `QQ_ALLOW_FROM`、`QQ_ALLOW_GROUPS`、Owner 和 Admin 名单；环境引用解析实际值，未设置与显式空值区分。模型前缀与 MCP 环境引用同样解析，不倾倒无关进程环境。
- 新生成的 Gateway 私有观测配置和宿主可见正文保留原值，Console 查询不再次脱敏。任务与步骤仍按真实 ID 读取执行时快照，配置变化不回填历史。旧记录中已经省略或替换的内容不能还原，界面说明历史采集限制。
- 复用现有观测索引和详情文件，不迁移或改写旧数据。配置记录标明原值可见性；独立的有界复制保留深度、节点、字符串与 JSON 限制。私有文件的 owner、权限、链接、实例/任务归属、保留期限及缺口标记继续校验。宿主未提供的内部状态与隐藏推理不采集。
- Legacy task/job 的 Console 读取不再额外替换已有正文；其原有共享 artifact 写入契约和导出的诊断包继续脱敏。Evaluation 读取现有服务提供的报告，不改写原报告或另建证据副本。服务日志已有原文展示保持。
- 配置向导通过 Console 自己的字段投影展示已保存值，使用普通文本控件；共享 provisioning CLI schema 和 mutation receipt 仍不包含值。保存空白保持原值的既有语义不变。
- 所有 Console API 响应禁止缓存，配置原值不写浏览器持久化存储，真实私有值不进入公开源码、构建夹具或测试证据。测试仅使用合成值。
- 沿用回环监听及部署者维护的访问边界。Console 当前没有 HTTP operator 登录，不能把“部署者使用”描述成已经实现的身份认证；非回环访问仍需要已有部署文档要求的认证代理和隔离。本次不扩展监听、不部署、不修改聊天、工具或群成员的权限。

## Acceptance

- 当前配置可直接看到完整名单与环境值，覆盖多值、空值、通配符、模型、MCP、端点、凭据、路径和不同实例。
- 配置和环境变化在刷新后可见，历史任务仍显示当时值；旧快照缺失不被补造。
- 新私有任务输入、工具参数/结果、上下文及关联日志的已有字段保持原值，隐藏推理仍排除；Console 不再次替换 Legacy 可读取字段。
- 所有字段在页面直接阅读，无掩码开关；前端显示未设置、已留空、历史未记录和采集失败等真实状态。
- 单元和 API 测试验证实际值贯穿采集、存储、查询与展示，同时覆盖无缓存、跨实例/任务拒绝、文件安全和读取上限。实际浏览器验证桌面/窄屏及当前/历史配置不串值。
- 运行相关 Python、前端测试、生产构建、SDD、架构、公开信息与差异检查，报告实际命令和边界。保持未暂存、未提交，不部署。

## Verification

2026-09-08，WSL Python 与 Windows 原生 Chrome 的本次验证：

- `PYTHONPATH=src /tmp/agentstrata-gateway-fix-venv/bin/python -m pytest tests -q --basetemp=/tmp/asg-operator-final`：2948 passed、1 skipped、133 subtests passed。包括原值复制与公开脱敏分离、真实私有配置文件、运行端独立记录、正文与快照读取、环境修改/删除/覆盖、历史不可变、跨实例/任务拒绝、文件安全、配额和清理。首次全量运行仅失败于本规格使用了无效状态名，修正后已重新执行全集通过。
- `npm --prefix console/web test`：9 个文件、84 项测试通过。`npm --prefix console/web run build`：TypeScript 与生产构建通过。
- `PYTHONPATH=src:. /tmp/agentstrata-gateway-fix-venv/bin/python /tmp/asg-operator-browser-fixture.py` 启动隔离后端；Windows Node 执行 `/tmp/asg-operator-browser.cjs`，真实 Chrome 对生产构建完成 7 组检查：完整名单和空值、环境变量搜索、模型/MCP/平台凭据与路径、配置向导明文及自动生成值只读展示、任务配置与正文、修改 local.env 后历史不变、旧快照限制、两个实例与 390px 页面。数据经过环境文件、ObservationRecorder、SQLite、HTTP 和 DOM，无响应替换；未发生浏览器异常、失败 API、可缓存 API 或 HTTP mutation 请求。截图和结果保存在 `/tmp/asg-operator-browser-evidence/`。
- `python3 scripts/check_sdd_specs.py`、`python3 scripts/check_architecture.py`、`python3 scripts/check_public_repo.py`、`git diff --check` 通过；架构检查为 519 modules、1583 static edges、0 cycles。
- `PYTHONPATH=src /tmp/agentstrata-gateway-fix-venv/bin/python scripts/check_component_catalog.py` 通过：25 packs、70 static tools、4 MCP entries、4 subagents、0 workflows。
- `PYTHONPATH=src /tmp/agentstrata-gateway-fix-venv/bin/python -m ruff check src tests scripts console` 通过；同解释器运行 `-m mypy src/chatcopilot/contracts src/chatcopilot/agent/session_protocol.py`，26 个源文件通过。

本次浏览器只使用合成名单、凭据和隔离实例，没有读取真实凭据作截图，没有调用真实模型或发送 QQ 消息。未部署或重启现有服务，未进行打包、发布或数据迁移；已有历史缺失值无法恢复。旧版验证结果不作为本次验收依据。交付保持未暂存、未提交。
