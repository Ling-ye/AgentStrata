"""AgentStrata 多机器人运维控制台（平台级运维工具，独立于 src/chatcopilot 六层）。

- console.control  : 后端 <-> deploy 脚本 的 JSON 控制契约层（可被 CLI / 后端 / 其他客户端复用）
- console.backend  : FastAPI 后端，对外暴露 REST + SSE
- console.web      : React + TypeScript + Rsbuild/Rspack + Arco Design 前端
- console.systemd  : 每实例 systemd --user 服务模板与一次性注册脚本
"""

__all__ = ["control"]
