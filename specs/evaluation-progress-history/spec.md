---
id: evaluation-progress-history
type: feature
status: implemented
created: 2026-09-07
---

# 评测结果与进步趋势

## Summary

测评中心在现有 Agent 能力与 QQ 链路两条轨道上，展示单次通过情况、逐测试点结果和历史变化曲线。继续使用唯一 Evaluation service 与现有 artifact，不新增数据库或独立生命周期。Comparison 与其他 Suite 历史记录仍可查询、导出和操作。

## Design

- Application 在成功预检后、创建记录时，将创建时间、Git HEAD 与工作区是否有未提交内容保存到自己拥有的 `request.json`。Git 不可用时记录未知，不影响评测；历史记录不补写当前版本。只记录 commit、布尔状态和时间，不读取或展示 remote、分支、作者、文件名和 diff。该快照标识创建时代码，不承诺运行期间外部代码未变化。
- `GET /api/evals/evaluations` 与现有详情接口增加只读 `insights` 与 `source_revision` 投影。后端从已持久化 Trial 汇总通过、失败、异常、跳过、已记录与计划次数，详情仍按既有接口读取。无法确认的计数不补零为成功；记录标识重复、结果不完整或缺少必要元数据时明确标识。
- 通过率采用通过次数 / 全部已记录测试次数，分母包括失败、异常和跳过。每次重复执行分别计数，页面同时展示计划次数和已记录次数。生命周期 completed 与产品 verdict 分开展示，关键门禁失败保留原始 verdict，不合成为智能分数。
- 趋势按 Bot、Suite、测试定义与执行/判分实现、选择的 Case、模型/Backend/执行器/推理档位、重复次数、seed、选项及预算分组；不同测试条件不连线。Git、Agent runtime 与配置 fingerprint 作为每点元数据，允许观察代码和配置演进，同时标记变化。趋势是描述性观测，不等价于严格对照实验；现有 CLI compare/resume 约束保持。
- 曲线仅纳入 completed、记录数量完整、标识和结果有效、非 dry-run 且测试定义已记录的评测。取消、中断、进行中、缺少定义或不完整记录仍可在列表查看，并显示排除原因。模型提供方版本未锁定等限制使用已有快照事实，不补造历史版本。
- 前端保持开始测试、运行记录，并新增进步趋势。Bot 选择在公共顶部。单次详情以通过概览、能力分组和可筛选测试点表格为主；展开测试点能看到已脱敏的回答、判分理由和错误。原始摘要仍可展开，取消、重跑、删除和导出保留。
- 趋势可筛选轨道、时间范围和测试条件，支持通过率/耗时切换；每个点通过键盘或鼠标打开对应记录，表格同时提供时间、commit、工作区状态、模型与配置版本。仅一个点也显示，无数据使用明确空状态。
- 列表继续消费现有查询接口；趋势不为每个历史点请求完整正文。详情按所选 Evaluation ID 请求并轮询活动任务，切换记录后的旧响应不替换当前记录。刷新不改变选中记录。桌面和窄屏均可操作。

## Acceptance

- 单次成功、失败、异常、跳过与取消均能明确辨别；部分记录不形成虚假的 100% 完整通过率。
- 相同条件下多个不同 Git 或配置版本能形成时间曲线，鼠标/键盘可定位评测；不同 Case、判分实现、模型、重复次数、seed 或预算自动分组。
- 历史缺少 Git 时按真实时间显示“未记录”，读取历史时不调用 Git；重跑捕获新的版本，幂等重试不覆盖旧快照。
- 持久化仅由 Evaluation application 写入 request，Console 不创建 worker 或模型调用；现有结果、状态及操作契约保持。
- 覆盖重复 Trial、未知 outcome、缺失/部分正文、dry-run、测试定义变化和请求切换；没有有效趋势时展示原因。

## Verification

2026-09-07 本次验证：

- 前端：`npm --prefix console/web test`，8 个测试文件、77 项通过；`npm --prefix console/web run build` 的 TypeScript 检查和生产构建通过。
- Python：使用测试虚拟环境与独立临时目录运行 `test_evaluation_insights.py`、`test_evaluation_console.py`、`test_evaluations.py`、`test_evaluation_service_protocol.py` 及集成 `test_evaluation_service.py`，285 项通过。覆盖受管 worker 启动、版本字段严格校验、幂等创建、重跑版本、取消及重启恢复。
- SDD、架构检查、受影响 Python 文件 Ruff、观测投影及版本校验模块 mypy、公开仓库边界和 `git diff --check` 通过。
- 原生 Chrome / Playwright 使用生产构建与临时 Console BFF、真实本地 UDS Evaluation service、确定性持久化记录，验证曲线分组、通过率与耗时切换、单点曲线、键盘定位、历史版本缺口、测试点展开、失败重试、迟到响应隔离、活动详情更新及 390 px 窄屏。两组操作记录均无浏览器异常；重跑按钮的新 ID 导航通过拦截返回值验证，没有向服务发出该次 mutation。

首次扩大回归发现新增版本字段未被受管 bootstrap 接受；补充严格字段校验后，上述完整回归通过。浏览器验证同时修正了详情自动聚焦筛选框导致初始滚动的问题。

这些验证使用本地确定性数据和 dry-run / 替身执行器，不是商业模型或真实 QQ 端到端证据。本次未执行仓库全量测试矩阵，未部署、暂存或提交。
