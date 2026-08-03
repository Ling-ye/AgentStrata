"""Tests for subagent schema: input_schema merge and output_schema validation."""

from __future__ import annotations

import unittest

from chatcopilot.agent.subagents.result import validate_output
from chatcopilot.agent.subagents.task_pack import TaskPack, parse_task_pack, task_pack_schema


class InputSchemaTests(unittest.TestCase):
    def test_parse_collects_extra_fields(self):
        pack = parse_task_pack({
            "objective": "test",
            "custom_field": "custom_value",
            "another": 42,
        })
        self.assertEqual(pack.objective, "test")
        self.assertEqual(pack.extra["custom_field"], "custom_value")
        self.assertEqual(pack.extra["another"], 42)

    def test_extra_fields_in_to_dict(self):
        pack = TaskPack(objective="goal", extra={"region": "us-west"})
        d = pack.to_dict()
        self.assertEqual(d["objective"], "goal")
        self.assertEqual(d["region"], "us-west")

    def test_known_keys_not_in_extra(self):
        pack = parse_task_pack({
            "objective": "test",
            "user_intent": "help",
            "write_scope": "src/",
        })
        self.assertEqual(pack.extra, {})

    def test_workflow_depth_excluded_from_extra(self):
        pack = parse_task_pack({
            "objective": "test",
            "workflow_depth": 2,
        })
        self.assertNotIn("workflow_depth", pack.extra)

    def test_search_fields_are_structured_and_known(self):
        pack = parse_task_pack({
            "objective": "Find the latest Unity release notes",
            "domain": "technical",
            "target_sites": ["unity.com"],
            "time_window": "latest as of 2026-06-25",
            "required_fields": ["title", "url", "published_at", "version"],
            "cross_check": True,
        })

        self.assertEqual(pack.domain, "technical")
        self.assertEqual(pack.target_sites, ("unity.com",))
        self.assertEqual(pack.time_window, "latest as of 2026-06-25")
        self.assertEqual(pack.required_fields[-1], "version")
        self.assertTrue(pack.cross_check)
        self.assertEqual(pack.extra, {})

    def test_search_fields_do_not_pollute_generic_task_pack_schema(self):
        schema = task_pack_schema()

        self.assertNotIn("domain", schema)
        self.assertNotIn("target_sites", schema)
        self.assertNotIn("time_window", schema)
        self.assertNotIn("required_fields", schema)
        self.assertNotIn("cross_check", schema)


class OutputSchemaTests(unittest.TestCase):
    def test_no_schema_returns_empty(self):
        self.assertEqual(validate_output({"summary": "ok"}, None), [])
        self.assertEqual(validate_output({"summary": "ok"}, {}), [])

    def test_missing_required_key(self):
        schema = {
            "required": ["summary", "score"],
            "properties": {
                "summary": {"type": "string"},
                "score": {"type": "number"},
            },
        }
        warnings = validate_output({"summary": "ok"}, schema)
        self.assertEqual(len(warnings), 1)
        self.assertIn("score", warnings[0])

    def test_type_mismatch_warning(self):
        schema = {
            "properties": {
                "count": {"type": "integer"},
            },
        }
        warnings = validate_output({"count": "not_a_number"}, schema)
        self.assertEqual(len(warnings), 1)
        self.assertIn("count", warnings[0])
        self.assertIn("integer", warnings[0])

    def test_valid_payload_passes(self):
        schema = {
            "required": ["summary"],
            "properties": {
                "summary": {"type": "string"},
                "items": {"type": "array"},
            },
        }
        warnings = validate_output({"summary": "ok", "items": [1, 2]}, schema)
        self.assertEqual(warnings, [])

    def test_multiple_issues(self):
        schema = {
            "required": ["a", "b"],
            "properties": {
                "a": {"type": "string"},
                "b": {"type": "boolean"},
                "c": {"type": "integer"},
            },
        }
        warnings = validate_output({"c": "wrong"}, schema)
        self.assertTrue(len(warnings) >= 2)


if __name__ == "__main__":
    unittest.main()
