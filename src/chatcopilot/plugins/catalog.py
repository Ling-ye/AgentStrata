"""Explicit first-party software drivers. Tools still use the existing ToolRegistry."""
from chatcopilot.plugins.types import PluginDefinition
import os
from pathlib import Path

PLUGINS = {
    "live2d-generator": PluginDefinition(
        id="live2d-generator", package="agentstrata-live2d-generator", version="0.3.0",
        module="agentstrata_live2d.manage", python="3.12.14",
        description="Prompt, image or layered artwork to Live2D models; independent asset studio",
    ),
    "open-llm-vtuber": PluginDefinition(
        id="open-llm-vtuber", package="agentstrata-open-llm-vtuber", version="0.4.0",
        module="agentstrata_vtuber.manage", python="3.12.14",
        description="Live2D browser, text/push-to-talk dialogue, speech and avatar actions",
        requires_agent=True,
    ),
}


def default_root() -> Path:
    data = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return Path(os.environ.get("AGENTSTRATA_PLUGINS_ROOT", data / "agentstrata/plugins"))
