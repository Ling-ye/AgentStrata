"""QQ platform adapter.

通过 cc-connect 的 ``[[projects.platforms]] type = "qq"`` 段，把本仓库的 ACP
server 与 QQ 桥接起来。cc-connect 不直接连 QQ，而是走 OneBot v11 协议连一个
本地 OneBot 实现（推荐 NapCat）：

    QQ Client <-> NapCat (OneBot v11) <-WebSocket-> cc-connect <-> ACP server

第一阶段只承载"纯问答骨架"——不启用 per-user workspace 隔离、不复用飞书的
角色矩阵 / 业务模式 / 私聊附件流水线。后续要扩展个性化、MCP、skill、文件回传
时再补。
"""
