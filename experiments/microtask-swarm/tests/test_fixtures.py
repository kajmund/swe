from __future__ import annotations

import json
import unittest

from microtask_swarm.contracts import Kind, TASK_KINDS
from microtask_swarm.fixtures import (
    FixtureError,
    all_microtask_specs,
    build_task_specs,
    load_fixtures,
)

from tests.support import CONFIG_PATH, ROOT


class FixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        self.suite = load_fixtures(
            ROOT / self.config["fixtures"]["gold"],
            expected_sha256=self.config["fixtures"]["sha256"],
        )

    def test_frozen_fixture_identity(self) -> None:
        self.assertEqual(self.suite.sha256, self.config["fixtures"]["sha256"])
        self.assertEqual(self.suite.item_count, 10)
        self.assertEqual(self.suite.microtask_count, 50)
        self.assertEqual(len(self.suite.domains), 5)
        self.assertEqual(
            set(self.suite.domains),
            {"biology", "engineering", "finance", "law", "medicine"},
        )

    def test_every_item_has_all_contracts(self) -> None:
        for item in self.suite.items:
            self.assertEqual(set(item.tasks), set(TASK_KINDS))
            self.assertTrue(item.passage)
            self.assertTrue(item.question)

    def test_every_gold_quote_is_an_exact_passage_substring(self) -> None:
        for item in self.suite.items:
            quotes = item.tasks[Kind.EXTRACTION].gold["quotes"]
            self.assertTrue(quotes)
            for quote in quotes:
                self.assertIn(quote, item.passage)

    def test_operation_gold_is_in_the_fixed_menu(self) -> None:
        for item in self.suite.items:
            operation = item.tasks[Kind.OPERATION].gold["operation"]
            self.assertIn(operation, self.suite.operation_menu)

    def test_task_inputs_do_not_include_metadata_or_gold(self) -> None:
        for item in self.suite.items:
            for kind, task in item.tasks.items():
                self.assertNotIn("gold", task.input_data)
                self.assertNotIn("item_id", task.input_data)
                self.assertNotIn("domain", task.input_data)
                self.assertNotIn(item.item_id, json.dumps(task.input_data))
                self.assertNotIn(item.domain, json.dumps(task.input_data))

    def test_spec_counts_by_mode(self) -> None:
        self.assertEqual(len(build_task_specs(self.suite, Kind.COMBINED)), 10)
        self.assertEqual(len(all_microtask_specs(self.suite)), 50)
        kinds = {spec.kind for spec in all_microtask_specs(self.suite)}
        self.assertEqual(kinds, set(TASK_KINDS))

    def test_hash_drift_is_rejected(self) -> None:
        with self.assertRaises(FixtureError):
            load_fixtures(
                ROOT / self.config["fixtures"]["gold"],
                expected_sha256="0" * 64,
            )


if __name__ == "__main__":
    unittest.main()
