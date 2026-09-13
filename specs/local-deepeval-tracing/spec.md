---
id: local-deepeval-tracing
type: architecture
status: implemented
created: 2026-09-13
---

# DeepEval 本地执行记录

## Summary

机器人 run、Evaluation Trial 和 Harness 编程调用使用 DeepEval 4.2.2 的 trace/span
契约保存本地记录，Console 的三个原有详情入口按需读取正文。普通记录保留正文 30 天，
冻结证据独立保存。运行单元结束时归档，可捕获异常尽力保存，不承诺强制终止后的完整记录。

## Design

遵循 [四层运行时基线](../runtime-four-layer-definition/spec.md)、
[Evaluation 结果契约](../evaluation-result-pipeline/spec.md) 和
[Harness 契约](../evaluation-case-harness/spec.md)。Core 只提供事件转换、脱敏、文件归档和
读取；各宿主分别拥有执行生命周期和私有目录，Console 不跨库写入。Agent 不依赖 DeepEval
或存储宿主，事件在既有观察边界接入。SDK 只在固定版本适配模块中使用原生数据类型，禁用
dotenv、遥测、更新检查和自动上传，不启动评分模型，不修改全局 trace manager。

每份记录由原生 trace.json 和 artifacts/<sha256>.json 构成。大正文按逻辑字段拆分，记录
内相同正文去重，metadata.agentstrata 保存来源、顺序、原始状态、采集覆盖和引用清单。
正文先脱敏再散列，索引最后原子发布；不透明引用绑定所属任务、私有目录和内容摘要。
执行成功与记录完整性分别展示，provider 不可见不等于记录损坏。工具完整结果与模型投影
分别保存；Evaluation 在摘要投影前捕获上下文，经受监督 IPC 分块交付给 Core 写入。

旧记录不补造上下文。机器人缺归档时可冻结现有观测与用户反馈供 Harness 诊断，缺失材料
明确标注；旧无归档测评不改变原准入条件。新记录来源和回归冻结使用独立正文，冻结后不依赖来源
保留期限。普通记录失败不改变授权、执行或交付事实，验证所需证据缺失阻断 Harness 验收。
Console 的既有进度机制保持，归档在执行单元结束后可读，不新增记录中心或跨运行评分。

Gateway 归档异常在对应 run 元数据保存 error.code/type/message/detail，详情经过原采集范围
脱敏；缺少 DeepEval 明确使用 trace_dependency_missing，其他归档异常为 trace_archive_failed。
Console 只读展示，不改变业务终态。agent 依赖组与锁文件共同声明 DeepEval 4.2.2；部署
使用 uv sync --locked 并在实际安装环境完成无模型的归档保存、读取和正文核对，即使跳过其他验收也执行。

## Acceptance

- 原生模型/工具字段、调用关系、顺序和完整脱敏正文可重读；重复正文复用。
- 三种来源经各自公开端口查询，不暴露文件路径；非法引用、内容改动和正文缺失明确失败。
- 日常采集不发起网络或模型请求；取消、失败、过期和 provider 不透明状态准确呈现。
- 冻结案例保留自己的完整证据；完整 Target 组 checkpoint 和原成绩不受影响。
- 三个 Console 详情页共享组件、按需正文与桌面/窄屏布局。

## Verification

本轮依赖与归档故障修复验证（2026-09-13）：

- 复核到依赖声明已包含 DeepEval，但锁文件遗漏 agent 关联；旧实例环境的归档探针报 PackageNotFoundError。更新锁文件仅补入对应依赖关联，没有升级其他包。
- 全新仅安装 agent + acp 的环境和更新后的实际实例均为 DeepEval 4.2.2，归档生成、保存、读取和正文核对通过；探针没有调用模型或外部网络。
- 归档异常代码、异常类型、原因脱敏与原 run 状态保持均有回归覆盖；完整性校验失败仍拒绝，过期检查也覆盖无需独立 artifact 的小正文归档。
- 最终仓库 full 为 12/12 检查通过，Python 3871 passed、1 skipped；真实 QQ 回合未重放，旧缺失归档未补造。

实施前 SDK 合成探针验证了原生序列化往返、父子关系和 3,600,000 字节上下文保留，
网络调用为零。实现验证包括：

- 本地归档、HTTP 来源隔离、大正文服务分帧、真实 Trial 子进程传输与 Harness 来源的
  定向验收为 61 passed；其中 45 MiB 级步骤通过现有服务协议分块返回。
- Evaluation、监督、结果链路和 artifact guard 的定向回归为 219 passed。
- 前端为 20 files、185 tests passed，生产构建通过；真实浏览器使用受控 API 检查
  Harness 详情的懒加载、全文展开、筛选及 1360px/390px 布局，没有页面异常或横向溢出。
- 开发中一次 full 的全部 12 项检查通过，Python 为 3835 passed、1 skipped、154 subtests。
  后续接口补充以最终 full 的 manifest 和原始日志为准；本地记录在
  `.cache/local-traces/full-verified/`，浏览器记录在 `.cache/local-traces/browser-result.json`。

完整入口为 `PYTHONPATH=src .venv/bin/python scripts/check_repo.py full`。新文件尚未暂存时，
打包精确清单检查使用私有临时 Git 索引及对象目录，检查真实索引哈希不变，不暂存或提交。
这些检查不代表真实模型、QQ 或部署端到端验证。
