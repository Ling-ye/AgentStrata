"""Shared project-level defaults for the platform runtime."""

from __future__ import annotations

import os
from pathlib import Path

# Public product name. The internal import namespace, environment prefix, and
# deployed instance paths intentionally remain compatible for the first public
# AgentStrata release.
PROJECT_NAME = "AgentStrata"
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
