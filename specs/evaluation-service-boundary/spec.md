---
id: evaluation-service-boundary
type: architecture
status: implemented
created: 2026-08-11
---

# Evaluation 独立服务边界

## Summary

AgentStrata 将 Evaluation 的应用生命周期和 worker supervision 从 Console 进程中迁出，由同仓库、同版本的本机 Evaluation service 统一负责。Console 继续提供现有质量评测 UI 和 `/api/evals/**` BFF，不再创建 manager、持有 worker 或在自身退出时改变 Evaluation 状态。

本次只完成 AgentStrata 原生 Evaluation 子系统的进程与代码所有权收敛，不引入外部评测引擎、实验追踪平台、第二套报告存储或独立前端应用。未来外部框架只能在单独规格中通过可选 adapter 或脱敏 exporter 接入。

## Design

`chatcopilot.evals.application` 是 Evaluation 应用控制面的唯一实现，拥有 Bot 引用解析、请求预检、活动 claim、生命周期状态、worker 启动与恢复、取消、删除、覆盖率、报告校验和 Suite catalog。该层不导入 `console.*`，并以显式 repository root 与 artifact root 运行。`chatcopilot.evals.service` 提供本机 Unix domain socket 服务、版本化 JSON 协议和同步 client；Console 只依赖 client 接口。

内部服务不监听 TCP。socket 默认位于 `XDG_RUNTIME_DIR` 下的用户私有目录，父目录与 socket 分别使用 `0700` 和 `0600`，拒绝符号链接、非 socket 占位、foreign owner 和宽松权限。协议限制请求大小、方法白名单和 JSON 对象形状；服务错误被映射为稳定的 not-found、blocked、conflict、invalid、unavailable 与 internal 分类。Console 在服务不可用时返回 `503`，不得回退为进程内 manager。

`start`、`rerun`、`cancel` 和 `delete` 使用两阶段 mutation 交付。Server 在任何 mutation 前先返回绑定 request ID、operation 与 Evaluation ID 的 accepted 帧；该帧未成功发送时不 dispatch。`start` 与 `rerun` 使用 client 生成的稳定 Evaluation ID，并把规范请求指纹持久化到 `request.json`；同 ID、同请求幂等返回，同 ID 请求漂移返回 conflict。Client 收到 accepted 后不再使用普通读取超时；若连接丢失，只能在同一次调用的有界恢复窗口内查询并用同一 ID 重试，使调用方不会收到失败响应后又在后台产生身份未知或重复的 Evaluation。`cancel` 与 `delete` 以目标 Evaluation ID 提供相同的可恢复语义，首次删除不存在记录仍返回 not-found。

WSL 部署增加独立的 `chatcopilot-evaluation.service` user unit。安装流程先安装并验证 Evaluation service，再启动或重启 Console。`--restart-only` 只重启 Console；`--update-only` 和已安装环境的安装修复在改变运行代码前，通过 UDS 在与 Evaluation 创建相同的跨进程锁内原子证明空闲并持久化 maintenance lease。Lease 存在期间创建在预检前和落盘前都被拒绝，并跨 Console web 构建、Evaluation service 重启、UDS health 与 Console 重启保持；成功后由同一 lease ID 释放，失败时 trap 尝试释放，service 不可达时 marker 保留并给出显式恢复命令。有活动记录、遗留 claim、未知 lifecycle、身份不明 worker、已安装 unit 未运行或无法证明空闲时拒绝更新。Console 页面只能通过 `systemd-run --user` 创建独立 transient unit 后触发 `--update-only`；`setsid` / `nohup` 不能脱离 Console service cgroup，禁止作为 fallback。Transient unit 无法创建时不得运行更新脚本或获取 maintenance lease，并返回可操作的手工命令。Evaluation worker 不通过 Console 的 cgroup、stdout pipe 或 lifespan 存活，Console 退出不发送任何 worker 信号。不涉及代码更新的 Evaluation service 重启依据权威 state、claim、PID 和唯一 `--output` 参数重新观察仍存活的 worker；PID 身份无法证明时保持 fail closed。

artifact 写入按文件分配唯一所有者：application service 写 `request.json`、`state.json`、活动 claim、maintenance lease 与合作式取消标记；Evaluation Core 写 `result.json`、`summary.md`、`progress.jsonl` 和逐 Trial 证据；worker 自身写脱敏 `run.log`。受管 worker 使用内部 managed 模式，验证 service bootstrap 后不重写 request 或 state。直接 CLI 运行仍由 Core 写完整本地记录。迁移不保留 `console.control.evaluations`、`console.control.evals` 或第二个 manager facade。

受管 worker 在执行 Core 前等待父子启动握手；application 必须先把 PID 持久化到 state 和 claim，再释放 worker。若 service 在 `Popen` 后、PID 落盘前退出，继承 pipe 关闭，worker 在创建 Core artifact 前自行退出。只有 argv 精确包含内部 `chatcopilot.evals.managed_worker` 模块并具有唯一匹配的绝对 `--output` 才能认领或发送信号，普通 standalone CLI 不属于受管 worker。

取消先创建受约束的取消标记并通知 worker，使 Core 在完整 Target group 边界写出 `cancelled` 结果；超时后才允许 supervisor 强制终止。任何信号仍要求 PID argv 中存在唯一 `--output`，且其规范路径与 Evaluation 目录完全一致。强制终止只能更新 service 所有的 lifecycle state，不得修改 Core 所有的 result、summary 或 progress。

Console 对外路径、前端模型和“新建评测 / 评测记录 / 任务集”信息架构保持不变。Profile、Suite、Case、数据准备、coverage、SSE、导出、取消、重跑与删除都经同一个 service client；Console 通用 TaskManager 仅呈现数据准备输出，不拥有 Evaluation worker。该服务是单机、单用户、共享本地文件系统设计；远程 evaluator、多副本和分布式 lease 不在本规格范围。

Suite 官方数据准备由独立 Python 子进程执行。Service 只在短临界区内取得 Bot 环境的私有快照，不在下载期间修改进程全局 `os.environ` 或持有全局环境锁；下载完成后才在短临界区内重新加载 Case。失败响应不得回传可能包含凭据的子进程 stdout 或 stderr。

受管根的既存祖先链不得包含符号链接。Evaluation 目录使用 `0700`，claim、取消标记和权威文件使用 `0600`；敏感读取通过安全打开后的同一文件描述符校验当前 UID、普通文件、单硬链接和记录 ID。报告在 UDS 与 Console HTTP 两段都按块传输；health 只读取受约束的 lifecycle state，不解析大型历史 result。

## Acceptance

- Console 进程启动、停止、更新或重启不会终止、取消、定态或释放正在运行的 Evaluation；Console 恢复后可继续查询和取消同一记录。
- Evaluation service 是 activity claim 和受管 worker 的唯一所有者；源码中不存在 Console manager、双 manager fallback 或旧 import facade。
- Console 的现有 `/api/evals/**` 行为与前端契约保持，服务不可用时明确返回 `503`，恢复后无需重建 Evaluation 记录。
- mutation 只有在 client 收到绑定操作与 Evaluation ID 的 accepted 帧后才执行；accepted 后断线可用同一 ID 恢复，重放不产生重复 Evaluation，请求漂移被拒绝。
- Profile、Suite、Case、官方数据准备、创建、列表、详情、Case evidence、coverage、事件流、报告导出、取消、重跑和删除均通过 Unix socket client 可用。
- Suite 官方数据准备不会在下载期间阻塞其他 Evaluation 请求的环境解析；慢准备与短 client timeout 并发时，不返回失败后再静默创建 Evaluation。
- socket、Evaluation 目录、claim、取消标记和权威 artifact 执行 owner、类型、权限、符号链接、记录 ID 与路径 containment 检查。
- 受管运行中 `request.json` 与 `state.json` 只由 application service 写；`result.json`、`summary.md`、`progress.jsonl` 和逐 Trial 证据只由 Core 写；`run.log` 由 worker 脱敏后直接写入。
- service 重启可重新观察身份匹配的活 worker；身份不明时不发送信号、不释放 claim、不把记录误判为终态。
- service 在 worker PID 持久化前退出时，启动握手保证 worker 在 Core 写入前退出，不产生不可追踪的活动 writer。
- 同一 Bot 仍跨线程和进程最多只有一个活动 Evaluation，创建阻断保持无副作用，取消与删除保持 fail closed。
- 直接 `agentstrata evals run` 的本地执行、严格请求、resume、Target fingerprint、checkpoint、脱敏和报告语义不回归；真实执行要求显式 `--output`，并拒绝受管 service 根目录。
- 运行代码更新只允许在 service 原子证明空闲并持有持久化 maintenance lease 时进行；整个更新窗口禁止新建 Evaluation，活 worker 不跨代码版本由新 service 恢复。
- 本次依赖和源码中不加入任何外部评测框架、追踪平台或 exporter。

## Verification

2026-08-11 完成以下验收：

- `python3 scripts/check_sdd_specs.py` 通过。
- `.venv/bin/python -m pytest tests/unit -q -k "eval or evaluation" --basetemp=/tmp/ev-unit-sEzBsB-1`：252 passed，1173 deselected。
- `.venv/bin/python -m pytest tests/integration -q -k "evaluation_service" --basetemp=/tmp/ev-int-sEzBsB-1`：1 passed，103 deselected。
- `.venv/bin/python -m pytest tests/unit/test_eval_console.py tests/unit/test_evals.py tests/unit/test_evaluation_console.py tests/unit/test_evaluation_service_deployment.py tests/unit/test_evaluation_service_protocol.py tests/unit/test_evaluations.py tests/integration/test_evaluation_service.py -q`：230 passed。真实进程场景覆盖 UDS 权限与协议、独立 service、Console BFF 断线恢复、mutation accepted 后同 ID 恢复、慢数据准备并发、请求漂移拒绝、原 PID 重新观察、双 Target 完整组边界取消、claim/取消标记权限、启动握手、SSE、多块报告导出和删除。
- Evaluation 部署与远端 Docker desired-state、固定端口、Component Catalog 和候选索引隔离交叉集合：96 passed。
- `cd console/web && npm test`：18 passed；`npm run build` 通过生产构建。
- `.venv/bin/python -m chatcopilot botspec validate bots/lingye-copilot-qq/bot.yaml` 通过。
- `.venv/bin/python scripts/check_repo.py fast`：1436 passed，1 skipped，39 subtests passed。
- 将 `HEAD + 本任务精确 delta` 物化到一次性候选仓后运行 `.venv/bin/python scripts/check_repo.py full`：wheel/sdist exact-member、隔离安装运行、完整测试集和 Console 生产构建均通过；操作者 worktree index 未被修改。
- `python3 scripts/check_public_repo.py` 通过 index、worktree 与 untracked 边界扫描。
- `bash scripts/check_secrets.sh changes` 使用脚本固定版本及 SHA-256 校验的 Gitleaks 通过。
- `bash -n console/setup_console.sh deploy/wsl/deploy_console.sh`、`deploy_console.sh --update-only --skip-web --dry-run` 和 `git diff --check` 通过。

Pytest 仅报告现有 FastAPI/Starlette `TestClient` 弃用警告，无失败和新增警告类别。
