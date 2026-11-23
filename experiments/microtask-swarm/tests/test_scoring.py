from __future__ import annotations

import unittest

from microtask_swarm.contracts import Kind, Verification
from microtask_swarm.results import TaskResult
from microtask_swarm.scoring import score_suite

from tests.support import spec

PASSAGE = (
    "The pump's controller shuts down the motor if discharge pressure exceeds 8 bar. "
    "A redundant pressure sensor provides a second signal."
)
GOLD_QUOTE = "The pump's controller shuts down the motor if discharge pressure exceeds 8 bar."


def _result(spec_, output, *, ok=True) -> TaskResult:
    return TaskResult(
        task_id=spec_.task_id,
        kind=str(spec_.kind),
        output=output,
        verification=Verification(ok, () if ok else ("bad",)),
        selected_models=("m",),
    )


class ScoringTests(unittest.TestCase):
    def test_relevance_accuracy(self) -> None:
        task = spec(
            Kind.RELEVANCE,
            input_data={"passage": PASSAGE, "query": "pressure"},
            gold={"relevant": True},
            passage=PASSAGE,
        )
        score = score_suite([task], {task.task_id: _result(task, {"relevant": True})})
        self.assertEqual(score["accuracy"], 1.0)
        self.assertEqual(score["by_contract"]["relevance"]["correct"], 1)
        self.assertEqual(score["answered_rate"], 1.0)
        self.assertEqual(score["verified_rate"], 1.0)

    def test_extraction_exact_quote_scores_full_coverage(self) -> None:
        task = spec(
            Kind.EXTRACTION,
            input_data={"passage": PASSAGE, "question": "pressure"},
            gold={"quotes": [GOLD_QUOTE]},
            passage=PASSAGE,
        )
        output = {"quotes": [GOLD_QUOTE]}
        score = score_suite([task], {task.task_id: _result(task, output)})
        self.assertEqual(score["accuracy"], 1.0)
        self.assertEqual(score["extraction"]["coverage"], 1.0)
        self.assertEqual(score["extraction"]["quote_precision"], 1.0)
        self.assertEqual(score["false_support"]["false_support_rate"], 0.0)
        self.assertEqual(score["extraction"]["exact_match_tasks"], 1)

    def test_unverified_quote_is_false_support(self) -> None:
        task = spec(
            Kind.EXTRACTION,
            input_data={"passage": PASSAGE, "question": "pressure"},
            gold={"quotes": [GOLD_QUOTE]},
            passage=PASSAGE,
        )
        output = {"quotes": ["The controller shuts down the motor"]}
        score = score_suite([task], {task.task_id: _result(task, output, ok=False)})
        self.assertEqual(score["accuracy"], 0.0)
        self.assertEqual(score["extraction"]["coverage"], 0.0)
        self.assertEqual(score["extraction"]["unverified_quotes"], 1)
        self.assertEqual(score["false_support"]["false_support_rate"], 1.0)

    def test_verified_but_unsupported_quote_is_false_support(self) -> None:
        task = spec(
            Kind.EXTRACTION,
            input_data={"passage": PASSAGE, "question": "pressure"},
            gold={"quotes": [GOLD_QUOTE]},
            passage=PASSAGE,
        )
        other = "A redundant pressure sensor provides a second signal."
        score = score_suite([task], {task.task_id: _result(task, {"quotes": [other]})})
        self.assertEqual(score["extraction"]["verified_quotes"], 1)
        self.assertEqual(score["extraction"]["unsupported_quotes"], 1)
        self.assertEqual(score["false_support"]["unsupported_quotes"], 1)
        self.assertEqual(score["extraction"]["coverage"], 0.0)
        self.assertEqual(score["false_support"]["false_support_rate"], 1.0)

    def test_empty_extraction_is_unanswered_not_false_support(self) -> None:
        task = spec(
            Kind.EXTRACTION,
            input_data={"passage": PASSAGE, "question": "pressure"},
            gold={"quotes": [GOLD_QUOTE]},
            passage=PASSAGE,
        )
        score = score_suite([task], {task.task_id: _result(task, {"quotes": []})})
        self.assertEqual(score["answered_rate"], 0.0)
        self.assertEqual(score["extraction"]["coverage"], 0.0)
        self.assertEqual(score["false_support"]["false_support_count"], 0)

    def test_combined_expands_to_five_contract_rows(self) -> None:
        task = spec(
            Kind.COMBINED,
            input_data={"passage": PASSAGE, "question": "pressure"},
            gold={
                "relevance": {"relevant": True},
                "entailment": {"label": "entailment"},
                "atomicity": {"atomic": True},
                "extraction": {"quotes": [GOLD_QUOTE]},
                "operation": {"operation": "extract_quote"},
            },
            passage=PASSAGE,
        )
        output = {
            "relevance": {"relevant": True},
            "entailment": {"label": "entailment"},
            "atomicity": {"atomic": False},
            "extraction": {"quotes": [GOLD_QUOTE]},
            "operation": {"operation": "extract_quote"},
        }
        score = score_suite([task], {task.task_id: _result(task, output)})
        self.assertEqual(score["scored_subtasks"], 5)
        self.assertEqual(score["correct"], 4)
        self.assertEqual(score["accuracy"], 0.8)
        self.assertEqual(score["by_contract"]["extraction"]["coverage"], 1.0)

    def test_invalid_contract_output_is_unanswered(self) -> None:
        task = spec(
            Kind.RELEVANCE,
            input_data={"passage": PASSAGE, "query": "pressure"},
            gold={"relevant": True},
            passage=PASSAGE,
        )
        output = {"relevant": True, "reason": "prose is forbidden"}
        score = score_suite([task], {task.task_id: _result(task, output, ok=False)})
        self.assertEqual(score["accuracy"], 0.0)
        self.assertEqual(score["answered_rate"], 0.0)

    def test_missing_result_scores_zero(self) -> None:
        task = spec(
            Kind.ENTAILMENT,
            input_data={"passage": PASSAGE, "hypothesis": "pressure"},
            gold={"label": "entailment"},
            passage=PASSAGE,
        )
        score = score_suite([task], {})
        self.assertEqual(score["accuracy"], 0.0)
        self.assertIn("missing result", score["rows"][0]["detail"])


if __name__ == "__main__":
    unittest.main()
