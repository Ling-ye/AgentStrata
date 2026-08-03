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
