from __future__ import annotations

import unittest

from chatcopilot.external_tools.shared.env_template import expand_env_template, expand_in_tree


class EnvTemplateTests(unittest.TestCase):
    def test_substitute_known_variable(self) -> None:
        env = {"FOO": "bar"}
        self.assertEqual(expand_env_template("${FOO}/baz", environ=env), "bar/baz")

    def test_unknown_variable_without_default_expands_to_empty(self) -> None:
        env: dict[str, str] = {}
        self.assertEqual(expand_env_template("${NOPE}/x", environ=env), "/x")

    def test_default_used_when_variable_missing(self) -> None:
        env: dict[str, str] = {}
        self.assertEqual(
            expand_env_template("${MISSING:-/mnt/f/SampleGame}", environ=env),
            "/mnt/f/SampleGame",
        )

    def test_default_used_when_variable_empty(self) -> None:
        env = {"FOO": ""}
        self.assertEqual(expand_env_template("${FOO:-default}", environ=env), "default")

    def test_set_variable_overrides_default(self) -> None:
        env = {"FOO": "real"}
        self.assertEqual(expand_env_template("${FOO:-default}", environ=env), "real")

    def test_multiple_templates_in_same_string(self) -> None:
        env = {"A": "1", "B": "2"}
        self.assertEqual(expand_env_template("${A}-${B}-${C:-3}", environ=env), "1-2-3")

    def test_expand_in_tree_walks_lists_and_dicts(self) -> None:
        env = {"ROOT": "/mnt/f/proj"}
        tree = {
            "root": "${ROOT}",
            "globs": ["${ROOT}/Assets", "${ROOT}/Packages"],
            "nested": {"path": "${ROOT}/sub"},
            "literal": "no template here",
            "number": 42,
        }
        result = expand_in_tree(tree, environ=env)
        self.assertEqual(
            result,
            {
                "root": "/mnt/f/proj",
                "globs": ["/mnt/f/proj/Assets", "/mnt/f/proj/Packages"],
                "nested": {"path": "/mnt/f/proj/sub"},
                "literal": "no template here",
                "number": 42,
            },
        )


if __name__ == "__main__":
    unittest.main()
