---
id: evaluation-agentbench-local-environment
type: feature
status: implemented
created: 2026-09-12
---

# AgentBench FC 本地环境接入

## Summary

为 AgentBench FC 提供可重复的本地 DB/OS 环境准备，导出真实 worker 对应的题目目录，修正 Controller 启动协议并增加在线只读预检。目录不再只检查环境变量字符串就宣称可用。

## Design

遵循 [四层运行时基线](../runtime-four-layer-definition/spec.md)。AgentRL Controller、Redis 与 task worker 是独立测评环境，不进入机器人 Channel/Gateway/Application/Agent 消息链。Evaluation 仍拥有 Trial、取消和结果，调用现有 Agent 配置完成环境任务；不运行上游模型、训练器或第二套测评生命周期。

- 固定 AgentBench 与 AgentRL 源码 revision 和下载哈希，使用独立 Docker Compose 项目。Controller 只向本机回环发布端口，worker/Redis 不对主机发布端口；只有受信 worker 挂载 Docker socket，任务容器不挂载宿主数据或凭据。
- 当前机器采用单 worker、单任务并发的 DB/OS 配置。上游 DB 默认 32 GiB buffer pool，改为明确的本地低内存环境参数；OS 使用官方 Ubuntu 镜像源并保留任务脚本。修改只影响资源与部署参数，不改题目、参考答案或评分代码，版本与实际镜像 ID 写入准备回执。
- 通过实际 worker 类加载官方数据、导出 task/index/input/source_revision，并与 Controller 的题目索引核对。仅导出题目预览，不把参考答案或验收脚本交给 Agent。KG/ALFWorld/WebShop 不冒充本次已准备范围。
- AgentRL start_sample 响应是 messages/tools，运行状态由后续 interact 返回；按操作分别校验，禁止把缺少终态或评分信息的 interact 当成功。收到 session_id 即登记清理责任，异常仍尝试回收。
- 只读预检调用 list_workers/get_indices，核验选定 task/index 和活跃 worker，目录展示未准备与繁忙原因。预检不启动 task、容器或模型；创建与执行前复检实际条件。
- 私有 local.env 保存实际数据路径与 Controller URL；示例只列无秘密配置项。既有服务、暂存区、历史成绩不改写；新环境由显式准备命令管理。

## Acceptance

- 本机 DB/OS Controller、worker 和数据目录可用，完整可选题目与上游索引一致。
- 正常启动不因 start_sample 缺 finish 字段而失败；interact 缺终态/评分信息仍报错。
- 未配置、连接失败、无活跃 worker、缺索引均在模型调用前明确阻断。
- 至少完成 DB/OS 各一条真实环境交互及原生判分/取消，模型替身与真实环境证据分开记录。
- 相关测试、仓库检查与本机配置核验通过，不宣称完整 AgentBench 榜单成绩。

## Verification

2026-09-12 完成：本机固定 AgentBench `d1e4a10db08c87075c78972e48ecc182be03e2d5` 与 AgentRL worker `6a73409d31ba695d383b978a8ad3ef400d90c054`，DB 300 + OS 144 共 444 题。独立 Controller、Redis、DB/OS worker 及所需容器可用；真实环境分别核查正确、错误答案及取消。Native Agent 使用受控模型，在真实 DB 环境完成两次工具调用及原生评分；这不是真实付费模型能力证明。

本次随 LLM 测评维护更新激活目录在线预检；实际 Console API 在 DB 占用前/中/释放后分别显示 444/144/444。浏览器可选 DB/OS 题目并形成精确请求，写请求在验证侧截获。新增环境测试纳入测评相关回归；本轮 857 passed，仓库 fast 1113 passed + 34 subtests，安装包资源验证通过。未准备 KG/ALFWorld/WebShop，不产出 AgentBench 完整榜单成绩。
