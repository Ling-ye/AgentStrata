"""通用飞书（lark-cli ``--as bot``）能力 domain。

提供通用飞书能力：云文档创建/追加、电子表格读写、多维表格
（Bitable）增删查、知识库/云盘检索、发送即时消息，以及一个只读的
``lark-cli api GET`` 逃生门。底层进程驱动统一复用
``chatcopilot.external_tools.shared.lark_cli``。

依赖约束：只依赖 ``external_tools.shared`` 与标准库；不 import
``chatcopilot.middleware.*`` / ``chatcopilot.platforms.*`` / ``chatcopilot.agent.*``。
"""
