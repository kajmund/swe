from __future__ import annotations

import json
import unittest

from microtask_swarm.contracts import (
    ContractViolation,
    Kind,
    assert_input_is_typed,
    parse_json_output,
    render_prompt,
    validate_output,
    verify_output,
)
from microtask_swarm.fixtures import all_microtask_specs, build_task_specs, load_fixtures

from tests.support import CONFIG_PATH, ROOT


class ContractTests(unittest.TestCase):
    def test_valid_outputs_are_accepted(self) -> None:
        self.assertEqual(
            validate_output(Kind.RELEVANCE, {"relevant": True})["relevant"], True
        )
        self.assertEqual(
            validate_output(Kind.ENTAILMENT, {"label": "neutral"})["label"],
            "neutral",
        )
        self.assertFalse(validate_output(Kind.ATOMICITY, {"atomic": False})["atomic"])
        self.assertEqual(
            validate_output(Kind.OPERATION, {"operation": "extract_quote"})[
                "operation"
            ],
            "extract_quote",
        )
        self.assertEqual(
            validate_output(Kind.EXTRACTION, {"quotes": ["a long enough quote"]})[
                "quotes"
            ],
            ["a long enough quote"],
        )

    def test_extra_keys_and_prose_are_rejected(self) -> None:
        with self.assertRaises(ContractViolation):
            validate_output(
                Kind.RELEVANCE, {"relevant": True, "reason": "because"}
            )
        with self.assertRaises(ContractViolation):
            validate_output(Kind.ENTAILMENT, {"label": "entailment", "prose": "x"})

    def test_types_are_strict(self) -> None:
        with self.assertRaises(ContractViolation):
            validate_output(Kind.RELEVANCE, {"relevant": 1})
        with self.assertRaises(ContractViolation):
            validate_output(Kind.ATOMICITY, {"atomic": "false"})

    def test_enums_are_closed(self) -> None:
        with self.assertRaises(ContractViolation):
            validate_output(Kind.ENTAILMENT, {"label": "maybe"})
        with self.assertRaises(ContractViolation):
            validate_output(Kind.OPERATION, {"operation": "summarize"})

    def test_extraction_requires_exact_supported_quote(self) -> None:
        passage = "The controller shuts down the motor if pressure exceeds 8 bar."
        good = verify_output(
            Kind.EXTRACTION,
            {"quotes": ["The controller shuts down the motor if pressure exceeds 8 bar."]},
            passage=passage,
        )
        self.assertTrue(good.ok)
        bad = verify_output(
            Kind.EXTRACTION,
            {"quotes": ["the controller shuts down the motor"]},
            passage=passage,
        )
        self.assertFalse(bad.ok)
        self.assertEqual(len(bad.unverified_quotes), 1)

    def test_extraction_rejects_duplicates_and_short_quotes(self) -> None:
        with self.assertRaises(ContractViolation):
            validate_output(
                Kind.EXTRACTION, {"quotes": ["a long enough quote", "a long enough quote"]}
            )
        with self.assertRaises(ContractViolation):
            validate_output(Kind.EXTRACTION, {"quotes": ["short"]})

    def test_combined_requires_five_exact_children(self) -> None:
        combined = {
            "relevance": {"relevant": True},
            "entailment": {"label": "entailment"},
            "atomicity": {"atomic": True},
            "extraction": {"quotes": ["a long enough quote"]},
            "operation": {"operation": "extract_quote"},
        }
        validate_output(Kind.COMBINED, combined)
        with self.assertRaises(ContractViolation):
            validate_output(
                Kind.COMBINED, {**combined, "extra": {"relevant": True}}
            )
        with self.assertRaises(ContractViolation):
            validate_output(Kind.COMBINED, {"relevance": {"relevant": True}})

    def test_prompts_never_contain_gold_keys_or_item_metadata(self) -> None:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        suite = load_fixtures(
            ROOT / config["fixtures"]["gold"],
            expected_sha256=config["fixtures"]["sha256"],
        )
        specs = list(all_microtask_specs(suite)) + list(
            build_task_specs(suite, Kind.COMBINED)
        )
        for spec in specs:
            system, user = render_prompt(spec.kind, spec.input_data)
            self.assertNotIn("gold", spec.input_data)
            payload = json.loads(user)
            self.assertEqual(set(payload), {"contract", "input", "output_schema"})
            self.assertNotIn("gold", payload["input"])
            self.assertNotIn("item_id", payload["input"])
            self.assertNotIn("domain", payload["input"])
            self.assertNotIn(spec.item_id, user)
            self.assertNotIn(spec.domain, user)
            self.assertNotIn("Return only", user)
            self.assertIn("typed microtask worker", system)

    def test_gold_bearing_input_is_rejected_before_rendering(self) -> None:
        with self.assertRaises(ContractViolation):
            assert_input_is_typed({"passage": "x", "gold": {"relevant": True}})
        with self.assertRaises(ContractViolation):
            assert_input_is_typed({"nested": [{"label": "entailment"}]})

    def test_json_parser_is_strict(self) -> None:
        self.assertEqual(parse_json_output('{"relevant":true}'), {"relevant": True})
        for text in ('```json\n{"relevant":true}\n```', "Here: {\"relevant\":true}", ""):
            with self.assertRaises(ContractViolation):
                parse_json_output(text)


if __name__ == "__main__":
    unittest.main()
