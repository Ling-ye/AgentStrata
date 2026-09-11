---
id: evaluation-case-harness
type: architecture
status: implemented
created: 2026-09-11
---

# 独立 AI Harness 修复工作台与测评结果存储

## Summary

控制台提供与测评中心并列的 AI Harness 修复页面，接收测评 ID 或绑定实例的机器人
任务 ID，每次只处理一个问题。测评中心负责测评，机器人任务流负责运行观测；来源
加载、自检、修复、复测和修复历史全部归 Harness，不在来源页面标记修复进度。
从已完成测评的单个失败 Case 手动发起修复。先确认当前代码仍有问题，再在专属本地
worktree 中调用 Codex，使用原题单复测。目标通过且原有通过项不退化才记录已修复；
标记绑定实际候选内容与验证记录，不修改原始成绩，不自动提交或发布。

## Design

遵循 [四层运行时基线](../runtime-four-layer-definition/spec.md) 与
[Evaluation 服务边界](../evaluation-service-boundary/spec.md)。Harness 是四层之外的可选
配套模块，独立命令与按需 systemd worker 拥有修复生命周期；Evaluation 只拥有测评与
结果，Console 只组合公开查询和操作。Evaluation 不依赖 Harness，Harness 核心仅通过
验证和编程两个执行端口协作，适配器不读取 Evaluation 私有状态或直接写它的数据库。

来源适配与执行分开：Evaluation 来源通过公开服务加载失败 Case/Target；机器人任务
来源通过绑定实例的 Gateway 观测 reader 读取 run、事件、有界正文和执行配置。Console
只负责可信实例解析与端口装配，不把任意客户端日志、路径或 Console 运维任务 ID
当成机器人证据。Harness 不回写来源记录，观测缺失、过期、截断或运行未结束均明确
记录自检受阻。历史和来源关联从 Harness 自己的数据库查询，不要求来源服务在线。

日常任务没有可靠的现成 Case 时，在独立准备阶段生成一个本地 pytest 复现测试和
依据说明；准备 Agent 只能写测试草案目录，产品代码只读。测试必须产生真实断言失败，
导入错误、空测试、跳过或环境异常不算复现。宿主冻结测试字节与标识；修复 Agent
只能改候选产品代码。相同冻结测试及受信 tests/unit 在无网络、无实例状态和凭据的
隔离进程中执行，保护基线中每个通过项。不能安全本地复现的外部任务明确受阻，
不得重放生产消息或将生成测试通过描述为真实平台端到端恢复。

新增测评的结构化请求、Trial、结果和采集详情由 Evaluation 服务校验后幂等写入自己的
SQLite 数据库；文件仍承载 Core 的原始证据与恢复检查点。历史文件只读，不批量导入。
结果入库失败不改变原始执行结果，重启可对已登记的新记录补写。Harness 的独立 SQLite
只保存任务和尝试，保存证据引用和必要快照，不建通用问题库，不做跨库事务。

通用候选测评接口接收同仓库受管 worktree 的内容身份与调用方稳定请求 ID；服务冻结
源码后启动隔离 worker。Case、评分及测评实现使用服务的受信版本，不能由候选覆盖；
每轮创建独立 Evaluation，保留代码、定义、配置与产物身份。定义不兼容或来源漂移时
明确阻断，不将修改代码后的运行当作原 Evaluation 的 resume。

修复任务绑定来源 Evaluation/Case/Target，默认从当前仓库 HEAD 创建分支。保护集合是
来源批次已通过项与当前基线新增通过项的并集；其他原失败项不要求修好。沿用原重复
次数，默认最多三个候选，预算在启动时固定。重试恢复本任务基线，保留失败补丁。
验收期间工作区冻结，结果绑定候选摘要。未复现、受阻、取消和失败与已修复分别记录。

同一来源条件、失败特征及代码基线的活动任务幂等返回；已有已验证补丁提供历史关联，
新失败不能被旧修复标记覆盖。已修复仅表示 worktree 验收通过，前端展示未合入状态。
取消和继续核验任务进程与候选身份，已提交的子测评用稳定 ID 查回，不盲目重放。
后台 worker 脱离 Console cgroup，运行时源码使用私有冻结副本；Agent 只可写候选产品
源码，凭据由租约提供，验收、Harness、测试定义和 Git 控制目录不可被它修改。

## Acceptance

- 不启动 Harness 时普通测评、入库和查询正常；关闭 Console 不终止 Harness。
- 新测评入库保留原始成绩、工具轨迹、错误和证据，历史记录仍可查询。
- 当前版本未复现时不启动编程执行器；目标和保护集通过才标记已修复。
- 复测实际加载冻结候选源码，评分与 Case 不能被候选更改。
- 重复请求、断线与恢复不产生重复活动任务或子测评，失败入库可幂等补写。
- 不同失败特征不误合并，旧修复与新失败同时可见，失效补丁不自动复用。
- CLI、Console、数据库和 worker 共享相同公开控制入口与状态语义。
- 测评页不导入或轮询 Harness；独立页面支持来源预览、单 Case 选择、全过程和历史。
- 机器人任务的测试准备与产品修复拥有不同写入范围，测试冻结后不得改变验收标准。
- 原工作区、暂存状态和秘密边界保持，不自动提交、推送或发布。

## Verification

2026-09-11，在独立 worktree 使用 CPython 3.13.15 与本地 Node.js 20.20.2 验证：

- 私有临时索引下的 `python scripts/check_repo.py full`：12 项全部通过；
  全量 Python 为 3433 passed、1 skipped、10 warnings、121 subtests passed。
  包含 wheel/sdist 精确成员及隔离安装验证、架构、SDD、类型、Ruff、公开边界和前端构建。
  主工作区及开发分支真实 index 的 SHA-256 前后相同；未执行提交或推送。
- Harness、Console BFF、Evaluation 协议/读取与架构定向集合：72 passed。
  包含真实隔离 Git worktree、候选源码导入、受管 dry-run 子进程与 SQLite 存储，
  并使用受控编程/测评端口验证修复、回归拒绝、中断恢复、幂等与取消。
- `npm --prefix console/web test`：16 files、154 tests passed。
- `bash scripts/check_secrets.sh changes`：私有候选索引、工作区及未忽略候选变更扫描通过。
- 新模块 mypy 检查通过；架构检查确认 Evaluation 不反向依赖 Harness，核心不直连私有执行器。
- 实际生产前端配合受控 API 完成 1360px 与 390px 浏览器交互检查：正确选择 Case/Target、
  提交修复、展示验证状态与报告、再次发起，未出现 pageerror。截图和结果位于
  工作区 `.cache/harness-verification/`。

这些结果不包含真实 Codex/商用模型修复、systemd worker 的实际部署运行或真实 QQ
端到端。

后续修复 QQ 合成预检的分流错误：通用 Suite 预检原先将 QQ 确定性 driver 也送入
DeepEval，导致出现 `judge.quality.enabled must be explicitly declared`。现在与执行器
保持一致，仅为 `agent_isolated` / `agent_configured` 执行 Agent 评分预检；未更改 QQ
Case、断言或质量评分标准。QQ quick/full/security 预检及非 dry-run 合成 full 7/7
已通过，受管候选源码与结果入库回归一并覆盖。

本次跟进验证：QQ/Harness 定向回归 33 passed；仓库 `fast` 9 项检查全部通过，
其中核心测试 1097 passed、2 warnings、34 subtests passed。真实 index 前后哈希不变。
非 dry-run QQ full 的 7/7 报告保存在工作区 `.cache/qq-preflight-fix/qq-full/`，
仍只证明本地合成链路，不代表真实 QQ 或商业模型端到端。


独立工作台跟进验证（2026-09-11）：

- 仓库 `full` 的 12 项检查全部通过；Python 全量为 3455 passed、1 skipped、
  10 warnings、121 subtests passed。真实主工作区与 worktree 的 index 哈希前后相同。
- 前端独立模块 16 files、156 tests passed，生产构建通过。
- 实际 bubblewrap + pytest 子进程验证了：环境变量隔离、产品只读、取消终止子进程、
  基线断言失败、冻结测试在候选代码上通过，以及原有单元测试结果逐项保留。
  Gateway 真实观测数据库读取前后文件内容相同。编程准备/修复使用受控执行器，
  不将这些检查描述成真实 Codex 模型修复。
- 受控 API 下的浏览器检查覆盖 1360px / 390px、两种来源、Case/Target 选择、
  自检与复测记录、刷新恢复选中任务、明确结果后的新请求，以及测评页无 Harness 请求。
- 验证报告及浏览器截图位于工作区 `.cache/harness-independent/`；没有部署或真实 QQ
  发信，没有调用商用模型。原 QQ 合成预检修复保留并由完整回归覆盖。
