---
id: file-token-limiter
type: refactor
status: implemented
created: 2026-09-10
---

## Summary

修复 N06：延迟发布的较早时间戳 token 不能挤掉已获准的任务，容量 2 不得出现 3 个持有者。

## Design

遵循 [四层基线](../runtime-four-layer-definition/spec.md)。限流器仍属于 Core，由既有 LLM
和重工具调用方使用，不增加运行时层、配置项或依赖。

- 对同一限流目录，用稳定的进程间锁串行化“清理失效 token、检查数量、创建并锁定 token”。
  获准后立即释放准入锁，实际任务仍可按配置容量并行。时间戳只作标识，不参与准入排序。
- 每个持有者在任务期间持有 token 的 OS 文件锁。TTL 只允许回收未被锁定的遗留 token，
  不能淘汰仍在运行的任务。崩溃释放 OS 锁后，遗留 token 按现有 TTL 回收。
- 保持 acquire()/release()/slot() 和返回 Path 的用法。release 只释放当前实例持有的 token，
  重复释放无副作用；fork 子进程不能释放父进程的 lease。
- 等待准入锁和等待容量共用 queue_timeout。Linux/WSL 使用 flock，Windows 使用 msvcrt
  字节锁；锁定失败不退化为线程锁或无锁执行。准入锁文件不能在使用期间删除。
- 同一限流目录的参与者需要使用一致版本与容量配置；不增加旧进程热兼容或数据迁移分支。

Windows 字节锁行为依据 [Python 文档](https://docs.python.org/3/library/msvcrt.html)。

## Acceptance

- 较早时间戳晚发布的反例、多个实例、多线程和独立进程竞争均不突破容量。
- 准入临界区被占用时仍遵守排队超时；异常创建不留下占位或文件描述符。
- 活跃但超过 TTL 的 token 不释放；崩溃遗留 token 可在 TTL 后回收。
- 正常/异常退出及重复 release 不影响其他持有者。

## Verification

先运行 test_concurrency.py 与直接调用方回归，再运行当前 tests/fast.txt 定义的 fast 及
git diff --check。只验证本地线程和进程，不调用真实模型或发送 QQ 消息。

实际验证：Linux/WSL 定向 25 passed；Windows Python 3.12.10 的独立进程探针峰值为 2，
活跃 TTL 与崩溃回收通过；fast 为 1096 passed、34 subtests，全部检查通过。
