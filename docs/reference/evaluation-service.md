# 测评生命周期与结果协议

修改进程监督、创建/取消/恢复、结果持久化或 Console BFF 时阅读。Evaluation 拥有独立生命周期，Console 不替它启动本地 manager 或改写终态。

## 源码入口

- [src/chatcopilot/evals/service](../../src/chatcopilot/evals/service)
- [src/chatcopilot/evals/trial_runner.py](../../src/chatcopilot/evals/trial_runner.py)
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

## Evaluation 独立生命周期

Agent Profile 对比和 BFCL / GAIA / IFEval Suite 只使用 `Evaluation`，以 `kind: comparison | suite` 区分；`chatcopilot.evals.application` 与本机 `chatcopilot.evals.service` 是活动 claim、受管 worker、lifecycle state 和更新 maintenance lease 的唯一 owner。

Console 只是通过同 UID Unix socket 调用服务的 UI/BFF，禁止在 `console.*` 中恢复 Evaluation manager、worker supervision、进程内 fallback 或旧 import facade。

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
