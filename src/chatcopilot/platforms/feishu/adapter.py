"""飞书平台适配器：聚合 persona / 文件回传 / 后台通知 / 身份补全 / 部署渲染。

飞书 adapter 提供角色矩阵、私聊文件上传流水线和 OpenAPI 主动通知。
本模块把这些能力的入口收敛到一个 :class:`PlatformAdapter`
子类，并在模块级暴露 ``ADAPTER`` 供 ``platforms.registry`` 自动发现。

IM 长连接、附件下载、消息回传仍由外部 cc-connect 完成；本适配器只负责本仓库内
的装配与渲染逻辑。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from chatcopilot.platforms.base import PlatformAdapter, SecretSpec
from chatcopilot.platforms.feishu import notifier as _notifier
from chatcopilot.platforms.feishu import profile as _profile
from chatcopilot.platforms.feishu import sender as _sender

if TYPE_CHECKING:
    from chatcopilot.contracts.workspace import WorkspaceView as Workspace


class FeishuAdapter(PlatformAdapter):
    """cc-connect 后端的飞书适配器。"""

    name = "feishu"
    adapter_id = "feishu_acp"

    supports_role_matrix = True
    supports_user_files_pipeline = True
    supports_background_jobs = True

    # -- runtime: file delivery --------------------------------------------
    def resolve_sendable_paths(self, workspace: "Workspace", files: Sequence[str]) -> list[Path]:
        return _sender.resolve_sendable_paths(workspace, files)

    def send_files(
        self,
        files: Sequence[Path],
        *,
        message: str = "",
    ) -> str:
        return _sender.send_via_cc_connect(files, message=message)

    # -- runtime: background notification ----------------------------------
    def resolve_delivery_target(self, workspace: "Workspace") -> Any:
        return _notifier.resolve_delivery_target(workspace)

    def send_text(self, workspace: "Workspace", text: str, *, timeout: int = 10) -> Any:
        return _notifier.send_text_to_workspace(workspace, text, timeout=timeout)

    # -- runtime: identity --------------------------------------------------
    def resolve_user_display_name(self, user_id: str | None) -> str | None:
        return _profile.resolve_user_name_from_feishu(user_id)

    # -- deploy -------------------------------------------------------------
    def required_secrets(self) -> tuple[SecretSpec, ...]:
        return (
            SecretSpec("FEISHU_APP_ID", required=True, description="飞书自建应用 App ID（cli_ 开头）"),
            SecretSpec("FEISHU_APP_SECRET", required=True, description="飞书自建应用 App Secret"),
        )

    def render_cc_connect_section(self, env: Mapping[str, str]) -> str:
        app_id = env.get("FEISHU_APP_ID", "")
        app_secret = env.get("FEISHU_APP_SECRET", "")
        return (
            "[[projects.platforms]]\n"
            'type = "feishu"\n'
            "\n"
            "[projects.platforms.options]\n"
            f'app_id = "{app_id}"\n'
            f'app_secret = "{app_secret}"\n'
            "resolve_mentions = true\n"
            "\n"
        )

    def render_extra_files(self, env: Mapping[str, str], home: Path) -> dict[str, str]:
        cfg = {
            "apps": [
                {
                    "appId": env.get("FEISHU_APP_ID", ""),
                    "appSecret": env.get("FEISHU_APP_SECRET", ""),
                    "brand": "feishu",
                    "lang": "zh",
                    "users": [],
                }
            ]
        }
        cfg_path = home / ".lark-cli" / "config.json"
        return {str(cfg_path): json.dumps(cfg, ensure_ascii=False, indent=2)}


ADAPTER = FeishuAdapter()


__all__ = ["ADAPTER", "FeishuAdapter"]
