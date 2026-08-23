"""Development tools: file operations, shell execution.

Git operations are provided by the external mcp-server-git MCP server.
"""

from chatcopilot.contracts.tool_packs import static_tool_provider
from chatcopilot.external_tools.dev.adapter_tools import TOOLS as ADAPTER_TOOLS
from chatcopilot.external_tools.dev.code_task_tools import TOOLS as CODE_TASK_TOOLS
from chatcopilot.external_tools.dev.file_tools import TOOLS as FILE_TOOLS
from chatcopilot.external_tools.dev.shell_tools import TOOLS as SHELL_TOOLS

TOOLS = [*FILE_TOOLS, *SHELL_TOOLS, *CODE_TASK_TOOLS, *ADAPTER_TOOLS]

TOOL_PROVIDER = static_tool_provider(
    "dev",
    packs={
        "dev.files": tuple(FILE_TOOLS),
        "dev.shell": tuple(SHELL_TOOLS),
        "dev.code_tasks": tuple([*CODE_TASK_TOOLS, *ADAPTER_TOOLS]),
    },
    module=__name__,
)

__all__ = ["TOOLS", "TOOL_PROVIDER"]
