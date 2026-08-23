"""Unity project code search and skill-call tool packs.

This package serves two independent tool pack prompt declarations:

* ``unity.codebase.read``   - project-aware code retrieval tools
  (``unity_project_read`` / ``unity_project_search`` / ``unity_project_glob`` /
  ``unity_find_csharp_symbol``).
* ``unity.skills`` - thin wrappers around skill scripts shipped inside
  each registered Unity project (currently just ``unity_path_book``).

The two packs share configuration (``projects.yaml`` + ``UnityProjectConfig``)
because they operate against the same set of projects, but they are exposed as
independent tool packs and can be toggled separately in ``bot.yaml``.
"""

from chatcopilot.contracts.tool_packs import static_tool_provider
from chatcopilot.external_tools.unity_codebase.read_tools import TOOLS as READ_TOOLS
from chatcopilot.external_tools.unity_codebase.skill_tools import TOOLS as SKILL_TOOLS

TOOLS = [*READ_TOOLS, *SKILL_TOOLS]

TOOL_PROVIDER = static_tool_provider(
    "unity-codebase",
    packs={
        "unity.codebase.read": tuple(READ_TOOLS),
        "unity.skills": tuple(SKILL_TOOLS),
    },
    module=__name__,
)

__all__ = ["TOOLS", "TOOL_PROVIDER"]
