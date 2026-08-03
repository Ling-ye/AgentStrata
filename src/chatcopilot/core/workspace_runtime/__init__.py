"""per-session 用户私人空间：模型 + env 解析 + 清理 + 盘点 + 身份持久化。

cc-connect 为每个聊天会话 spawn AgentStrata agent 进程时，会把 chat 标识通过
环境变量注入；我们映射到一个独立子目录，保证不同用户/不同群聊互不串。本包是
middleware 与 agent 工具共享的运行期上下文：

- middleware 在 ``session/new`` 时调用 :func:`resolve_workspace` 创建实例
- agent 工具（list_workspace / read_text_head / send_files_to_user / ...）在 handler
  内通过 :func:`resolve_workspace` 拿到当前会话的 ``Workspace``

环境变量约定（由 cc-connect session.started hook + bot_wrapper.sh 注入）：

- ``CHATCOPILOT_WORKSPACE_ROOT``: 工作目录根
- ``CHATCOPILOT_CHAT_KIND``: ``p2p`` / ``group``
- ``CHATCOPILOT_CHAT_ID``: 聊天 ID
- ``CHATCOPILOT_USER_ID``: 发起用户 ID（飞书 open_id）
- ``CHATCOPILOT_USER_NAME``: 发起用户的飞书显示名
- ``CHATCOPILOT_WORKSPACE``: 显式指定整目录（最高优先级，调试用）

路径策略：

- ``p2p`` + ``user_id`` → ``<root>/p2p_<user_id>/``
- ``group`` + ``chat_id`` + ``user_id`` → ``<root>/group_<chat_id>/user_<user_id>/``
- 老兼容：``chat_id`` 在但 ``user_id`` 缺失 → ``<root>/<kind>_<chat_id>/``
- 全部缺失 → ``<root>/default/``
"""
from chatcopilot.core.workspace_runtime.cleanup import (
    CLEANUP_POLICIES,
    cleanup_workspace,
    cleanup_diagnostic_records,
    clear_workspace_files,
)
from chatcopilot.core.workspace_runtime.identity import (
    persist_workspace_identity,
)
from chatcopilot.core.workspace_runtime.inventory import (
    WorkspaceInventory,
    list_workspace_inventories,
)
from chatcopilot.core.workspace_runtime.model import (
    ATTACHMENTS_RELPATH,
    MEMORY_FILENAME,
    TRANSCRIPTS_DIRNAME,
    Workspace,
    describe_workspace,
    normalize_chat_kind,
)
from chatcopilot.core.workspace_runtime.resolver import (
    resolve_workspace,
    resolve_workspace_root,
)
from chatcopilot.core.workspace_runtime.service import (
    MiddlewareWorkspaceService,
)

__all__ = [
    "ATTACHMENTS_RELPATH",
    "CLEANUP_POLICIES",
    "MEMORY_FILENAME",
    "MiddlewareWorkspaceService",
    "TRANSCRIPTS_DIRNAME",
    "Workspace",
    "WorkspaceInventory",
    "cleanup_workspace",
    "cleanup_diagnostic_records",
    "clear_workspace_files",
    "describe_workspace",
    "list_workspace_inventories",
    "normalize_chat_kind",
    "persist_workspace_identity",
    "resolve_workspace",
    "resolve_workspace_root",
]
