# 测评生命周期与结果协议

修改进程监督、创建/取消/恢复、结果持久化或 Console BFF 时阅读。Evaluation 拥有独立生命周期，Console 不替它启动本地 manager 或改写终态。

## 源码入口

- [src/chatcopilot/evals/service](../../src/chatcopilot/evals/service)
- [src/chatcopilot/evals/trial_runner.py](../../src/chatcopilot/evals/trial_runner.py)
- [src/chatcopilot/evals/trial_supervisor.py](../../src/chatcopilot/evals/trial_supervisor.py)
- [src/chatcopilot/evals/result_codec.py](../../src/chatcopilot/evals/result_codec.py)
- [src/chatcopilot/evals/application](../../src/chatcopilot/evals/application)
- [tests/integration/test_evaluation_service.py](../../tests/integration/test_evaluation_service.py)

## Evaluation v2 结果链路

单题统一通过 `trial_runner` 获取执行观测并调用评分器，插件不能组装最终 Trial。
  `models` 与 `result_codec` 是结果和编解码的唯一契约；无错误为 null，未评分分数为 null。
  执行和评分快照经既有 observation 通道保存，结果校验异常不能丢失先前已验证证据或冒充 Judge 异常。
  单题失败/异常继续；结果契约、协议、权威持久化和清理失败停止整批，完整 Target 组检查点规则保持。
  预期只从受信题目/fixture 投影，运行前冻结，不注入被测模型；结果页只读当时快照。
  旧记录只归档导出，不迁移、补判、恢复或作为 Harness 来源；新来源排除测评系统故障。
  部署更新同时保护 Harness 创建/恢复和 Evaluation 维护窗口。规格见 `docs/reference/evaluation-service.md`。

插件只提供执行观测和自己的附加 metadata。Core 最后写入 `expectation`、`judge_evidence`、
工具轨迹、任务/后置状态、usage 与 Case 来源，同名插件字段不能覆盖这些权威内容。
插件的执行报错与独立评分结论分别保存；任何自报的通过说明都不能替代实际 `assessment`。
配置快照中的 `activation_model_identity` 绑定配置化 Agent 的实际模型路由与认证引用身份，
仅保存摘要，不保存明文凭据；Harness 将它与版本快照一起纳入验证回执，供 M5 核对目标运行条件。

源码评测沿用服务 `start(code_source={path, sha256})`、冻结请求及受管 worker。Core 在校验输出目录后，
从服务请求和 `.sources` 回执取得版本，绑定实际执行树、Target、runtime 与桥接组件，并在 Trial 前后核验。
提交源码与覆盖受信验证器后的执行源码分别记录；候选对评分代码的改动不进入本次运行的评分实现。
版本记录保存在现有 `config_snapshot.version_snapshot`，不增加数据库或第二套生命周期。
完整对照规则见 [版本绑定与基线候选对照](evaluation.md#版本绑定与基线候选对照)。

## Evaluation 独立生命周期

Agent Profile 对比和 BFCL / GAIA / IFEval Suite 只使用 `Evaluation`，以 `kind: comparison | suite` 区分；`chatcopilot.evals.application` 与本机 `chatcopilot.evals.service` 是活动 claim、受管 worker、lifecycle state 和更新 maintenance lease 的唯一 owner。

`EvaluationApplication` 显式接收 `EvaluationWorkerPort`，由服务 runtime 注入本地进程实现。
进程创建、启动门、身份观测、等待与终止属于 worker 实现；请求冻结、claim、维护租约、
取消决定和结果收尾仍属于应用服务。启动门只有在 state 与 claim 发布成功后才放行，
发布失败会关闭管道，不能先运行 Core 再补写身份。

Console 只是通过同 UID Unix socket 调用服务的 UI/BFF，禁止在 `console.*` 中恢复 Evaluation manager、worker supervision、进程内 fallback 或旧 import facade。

`EvaluationServiceClient.preview(bot_id=..., request=...)` 是只读预检，返回实际 Target 与冻结条件，
不创建 Evaluation、worker、报告或副作用。Harness 的需求入口用它绑定被测实例，随后 `start`
仍按相同条件重新核验；查询不能启动测评或推进任务。

Console 启停和重启不得发送 worker 信号或改写 Evaluation 终态；运行代码更新必须在与创建相同的跨进程锁内原子证明 idle 并持久化 maintenance marker，整个构建、Evaluation 重启、UDS health 和 Console 重启窗口都拒绝新 Evaluation，结束后才释放；服务不可达、状态不明或已安装 unit 未运行时 fail closed。

Console 页面触发自身更新时只允许 `systemd-run --user` 创建独立 transient unit；`setsid` / `nohup` 仍属于 Console service cgroup，禁止作为降级路径，transient unit 无法创建时必须在运行更新脚本和获取 maintenance lease 前失败。

服务不可用时 BFF 明确返回 `503`，不得降级为本地 manager。

## Evaluation artifact 所有权

创建必须先完成无副作用预检，阻断时返回结构化 `code/message/checks`，不创建报告目录或子进程。

Application 唯一写 `request.json`、`state.json`、活动 claim 和取消标记；Evaluation Core 唯一写 `result.json`、`summary.md`、`progress.jsonl` 和逐 Trial 证据；managed worker 只写脱敏 `run.log`。

Worker 必须等待父子启动握手，只有 PID 同时持久化到 state 与 claim 后才能执行 Core；握手前 service 退出时 worker 必须自行退出。

受管进程退出前禁止删除、重跑或为同 Bot 创建下一条；worker PID 只有在 argv 精确包含内部 managed-worker 模块、且唯一 `--output` 与 Evaluation 目录规范路径匹配时才可发送信号，身份不明时 fail closed。

Evaluation 根的既存祖先、目录、claim、取消标记和权威 artifact 必须拒绝符号链接，并校验 owner、inode 类型、`0700` / `0600`、单硬链接、记录 ID 与 containment。

评测数据统一位于 `reports/evals/evaluations/<evaluation-id>/`，禁止恢复 `/api/evals/experiments`、`/api/evals/runs` 或第二套报告根。

## Evaluation mutation 交付

`start` / `rerun` / `cancel` / `delete` 必须在任何 mutation 前由 UDS server 返回绑定 request ID、operation 和 Evaluation ID 的 accepted 帧；未成功发送 accepted 时不得 dispatch。`start` / `rerun` 使用 client 生成的稳定 Evaluation ID 和规范请求指纹实现同请求幂等恢复，同 ID 请求漂移必须 conflict；accepted 后断线只能用同一 ID 有界查询或重试，禁止产生身份未知的重复 Evaluation。Suite 官方数据准备在显式子进程内使用私有环境快照，不得在下载期间修改全局 `os.environ` 或长期持有进程级环境锁。

## Standalone Evaluation 隔离

`evals run` 默认写入 `reports/evals/manual/<evaluation-id>`，允许显式 `--output`，并拒绝写入 `CHATCOPILOT_EVALUATION_ROOT` 或默认 `reports/evals/evaluations/` 受管根；standalone/CI 记录使用 `reports/evals/manual/` 等独立目录，不能绕过 service claim 写受管 artifact。

## Evaluation Trial 监督

正式 Trial 必须在独立 `spawn` 子进程执行，期限取 Case timeout 与 Evaluation 剩余 max-wall 的最小值；取消、期限或预算终止并回收 Trial 进程组，Linux/WSL 必须绑定父死保护。只有同一 Case/attempt 的完整 Target 组可写 checkpoint；中断的不完整组及 workspace 必须丢弃，不能参与 resume、compare 或通过率。

若期限在 supervisor 建立进程组前到达，清理等待收到它的 `ready` 帧后补发终止信号；
该帧确认清理 handler 与 subreaper 已就绪。已有清理宽限期和完整回收证明仍须满足。
