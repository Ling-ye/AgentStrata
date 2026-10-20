"""Shared project-level defaults for the platform runtime."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

# Public product name. The internal import namespace, environment prefix, and
# deployed instance paths intentionally remain compatible for the first public
# AgentStrata release.
PROJECT_NAME = "AgentStrata"
PROJECT_REPOSITORY = "https://github.com/Ling-ye/AgentStrata"
PROJECT_LICENSE = "AGPL-3.0-only"
PROJECT_COPYRIGHT = "Copyright (C) 2026 Lingye"
PROJECT_SLUG = "chatcopilot"
# Storage and deployment directories remain a compatibility contract. Public
# branding must not silently move existing runtime state or caches.
COMPAT_DATA_DIRNAME = "ChatCopilot"

ENV_PREFIX = "CHATCOPILOT"
CHAT_ENV_PREFIX = f"{ENV_PREFIX}_CHAT"

CONFIG_DIRNAME = f".{PROJECT_SLUG}"
SECRET_ENV_FILENAME = f".{PROJECT_SLUG}.env"
WORKSPACE_DIRNAME = f"{PROJECT_SLUG}-workspaces"
LOG_DIRNAME = f"{PROJECT_SLUG}-logs"
LIMIT_DIRNAME = f"{PROJECT_SLUG}-limits"
ASSET_CACHE_DIRNAME = "feishu_asset_cache"

DEFAULT_HOME = Path(
    os.environ.get("CHATCOPILOT_HOME", Path.home() / COMPAT_DATA_DIRNAME)
).expanduser()
DEFAULT_CONFIG_DIR = Path(os.environ.get("CHATCOPILOT_CONFIG_DIR", Path.home() / CONFIG_DIRNAME)).expanduser()


def legal_info() -> dict[str, str | bool]:
    """Public legal information; never derive source from a private checkout."""
    source = os.environ.get(f"{ENV_PREFIX}_SOURCE_URL", "").strip()
    if source:
        url = urlsplit(source)
        if (url.scheme not in {"https", "http"} or not url.hostname
                or url.username is not None or url.password is not None
                or any(c.isspace() for c in source)):
            raise ValueError("CHATCOPILOT_SOURCE_URL must be a public HTTP(S) source URL without credentials")
    return {
        "project": PROJECT_NAME,
        "copyright": PROJECT_COPYRIGHT,
        "license": PROJECT_LICENSE,
        "repository_url": PROJECT_REPOSITORY,
        "source_url": source or PROJECT_REPOSITORY,
        "source_configured": bool(source),
        "license_url": f"{PROJECT_REPOSITORY}/blob/main/LICENSE",
        "notices_url": f"{PROJECT_REPOSITORY}/blob/main/THIRD_PARTY_NOTICES.md",
        "warranty": "本程序不提供任何担保；你可以依照 AGPL-3.0-only 再分发和修改。",
    }


def format_legal_info() -> str:
    info = legal_info()
    source_label = "本部署对应源码" if info["source_configured"] else "项目仓库（尚未设置本部署对应源码）"
    return (
        f"{info['project']} · {info['copyright']}\n"
        f"许可：{info['license']}\n{info['warranty']}\n"
        f"{source_label}：{info['source_url']}\n"
        f"许可全文：{info['license_url']}\n第三方声明：{info['notices_url']}"
    )


def source_notice() -> str:
    info = legal_info()
    label = "源码" if info["source_configured"] else "项目源码（本部署源码未设置）"
    return f"{PROJECT_NAME} · {PROJECT_LICENSE} · {label}：{info['source_url']}（/source 查看许可与署名）"


def source_command_reply(text: str) -> str | None:
    parts = text.strip().split()
    if not parts or parts[0].lower() != "/source":
        return None
    return format_legal_info() if len(parts) == 1 else "用法：/source"
