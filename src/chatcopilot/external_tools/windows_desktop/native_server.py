"""Runs in native Windows Python; extends the pinned MCP with primary-screen observation."""
from __future__ import annotations

import importlib.metadata
import json
import os
import sys

TOOLS = ("ObservePrimary", "Snapshot", "Screenshot", "DisplayInventory", "App", "Click", "Type",
         "Scroll", "Move", "Shortcut", "WaitFor", "ControlStatus")
UPSTREAM_VERSION = "0.8.8"


def main():
    if sys.platform != "win32" or sys.version_info[:2] != (3, 14):
        raise RuntimeError("Desktop MCP requires its native Windows Python 3.14 environment")
    if importlib.metadata.version("windows-mcp") != UPSTREAM_VERSION:
        raise RuntimeError("Windows-MCP does not match the application's pinned version")
    os.environ["ANONYMIZED_TELEMETRY"] = "false"
    # Screens are sampled only by explicit tools or the user's perception toggle.
    os.environ["WINDOWS_MCP_WATCHDOG"] = "false"
    from windows_mcp import __main__ as upstream
    from windows_mcp.tools import control_policy
    from windows_mcp.tools._snapshot_helpers import capture_desktop_state
    from fastmcp.utilities.types import Image
    from mcp.types import ToolAnnotations

    # This one additional tool only reads a screenshot, exactly like Screenshot.
    control_policy.OTHER = control_policy.OTHER | {"ObservePrimary"}
    control_policy.CLASSIFIED_TOOLS = control_policy.CLASSIFIED_TOOLS | {"ObservePrimary"}
    server = upstream._build_mcp()

    @server.tool(name="ObservePrimary", description=(
        "Observe only the system's primary display. Returns the current image and physical bounds. "
        "Read-only, without mouse/keyboard actions or window contents from other displays."),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
    def observe_primary():
        desktop = upstream._get_desktop()
        display = next(value for value in desktop.get_displays() if value.primary)
        rect = display.rect
        region = [rect.left, rect.top, rect.right, rect.bottom]
        capture = capture_desktop_state(desktop, use_vision=True, use_dom=False, use_annotation=False,
            use_ui_tree=False, width_reference_line=None, height_reference_line=None,
            display=[display.index], region=region, tool_name="ObservePrimary")
        pixels = capture["screenshot_bytes"]
        if not pixels:
            raise RuntimeError("Primary display capture returned no image")
        return [json.dumps({"display": display.index, "primary": True, "region": region,
                            "coordinate_space": "physical virtual-desktop pixels"}),
                Image(data=pixels, format="png").to_image_content()]

    upstream.main(args=["serve", "--transport", "stdio", "--tools", ",".join(TOOLS),
                        "--control-policy", "relaxed"], prog_name="agentstrata-desktop-mcp")


if __name__ == "__main__":
    main()
