"""通用飞书（lark-cli ``--as bot``）能力 domain。

提供通用飞书能力：云文档创建/追加、电子表格读写、多维表格
（Bitable）增删查、知识库/云盘检索、发送即时消息，以及一个只读的
``lark-cli api GET`` 逃生门。底层进程驱动统一复用
``chatcopilot.external_tools.shared.lark_cli``。

依赖约束：只依赖 ``external_tools.shared`` 与标准库；不 import
``chatcopilot.middleware.*`` / ``chatcopilot.platforms.*`` / ``chatcopilot.agent.*``。
"""

from chatcopilot.contracts.tool_packs import static_tool_provider
from chatcopilot.contracts.tools import ToolDef
from chatcopilot.external_tools.feishu.spec import TOOLS


def _select(*names: str) -> tuple[ToolDef, ...]:
    index = {tool.name: tool for tool in TOOLS}
    return tuple(index[name] for name in names)


TOOL_PROVIDER = static_tool_provider(
    "feishu",
    packs={
        "feishu.document": _select(
            "feishu_doc_create", "feishu_doc_append", "feishu_api_get"
        ),
        "feishu.sheet": _select(
            "feishu_sheet_read",
            "feishu_sheet_write",
            "feishu_sheet_append",
            "feishu_api_get",
        ),
        "feishu.bitable": _select(
            "feishu_bitable_query",
            "feishu_bitable_add",
            "feishu_bitable_update",
            "feishu_api_get",
        ),
        "feishu.wiki": _select(
            "feishu_wiki_search", "feishu_drive_search", "feishu_api_get"
        ),
        "feishu.messaging": _select("feishu_im_send", "feishu_api_get"),
    },
    module=__name__,
)

__all__ = ["TOOLS", "TOOL_PROVIDER"]
