"""Generic Windows file system access tool pack from WSL.

This package exposes ``win_read_file`` / ``win_grep`` / ``win_glob`` tools that
read arbitrary files under a globally configured allow-list of root paths.

It is intentionally unaware of any "Unity project" concept. For project-aware
code search use the ``unity_codebase`` package instead.
"""
