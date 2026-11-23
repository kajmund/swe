from __future__ import annotations

import unittest

from microtask_swarm.budget import SweycoinMeter
from microtask_swarm.contracts import Kind
from microtask_swarm.dag import ScheduledTask, compile_layers
from microtask_swarm.executor import SwarmExecutor
from microtask_swarm.fixtures import build_task_specs, load_fixtures
from microtask_swarm.runner import load_config

from tests.support import CONFIG_PATH, ROOT, ScriptedProvider, spec

PASSAGE = (
    "The pump's controller shuts down the motor if discharge pressure exceeds 8 bar. "
    "A redundant pressure sensor provides a second signal."
)
GOLD_QUOTE = "The pump's controller shuts down the motor if discharge pressure exceeds 8 bar."


def _config():
    return load_config(CONFIG_PATH)


def _models(config):
    return {model["id"]: model for model in config["models"]}


def _executor(config, provider, *, concurrency=4, max_attempts=1):
    return SwarmExecutor(
        provider=provider,
        models=_models(config),
        contracts=config["contracts"],
        concurrency=concurrency,
        max_attempts=max_attempts,
        timeout_seconds=1.0,
        meter=SweycoinMeter(250000),
        retry_backoff_ms=0,
        default_price_period="peak",
    )


def _relevance_specs(count: int):
    config = _config()
    suite = load_fixtures(
        ROOT / config["fixtures"]["gold"],
        expected_sha256=config["fixtures"]["sha256"],
    )
    return build_task_specs(suite, Kind.RELEVANCE)[:count]


class ExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_bounded_concurrency_never_exceeds_the_limit(self) -> None:
        config = _config()
        specs = _relevance_specs(10)
        provider = ScriptedProvider({}, default={"relevant": True}, delay_ms=2.0)
        executor = _executor(config, provider, concurrency=3)
        execution = await executor.run(
            specs, mode="distributed_independent", model_ids=["gpt-oss-20b"]
        )
        self.assertEqual(execution.call_count, 10)
        self.assertGreaterEqual(execution.peak_in_flight, 2)
        self.assertLessEqual(execution.peak_in_flight, 3)
        self.assertTrue(all(result.verification.ok for result in execution.results.values()))

    async def test_retry_is_counted_and_charged_separately(self) -> None:
        config = _config()
        task = spec(
            Kind.RELEVANCE,
            input_data={"passage": PASSAGE, "query": "pressure"},
            gold={"relevant": True},
            passage=PASSAGE,
            task_id="retry-task",
        )
        provider = ScriptedProvider(
            {
                ("retry-task", "gpt-oss-20b", 1): RuntimeError("transient"),
                ("retry-task", "gpt-oss-20b", 2): {"relevant": True},
            }
        )
        executor = _executor(config, provider, concurrency=1, max_attempts=2)
        execution = await executor.run(
            [task], mode="distributed_independent", model_ids=["gpt-oss-20b"]
        )
        result = execution.results["retry-task"]
        self.assertTrue(result.verification.ok)
        self.assertEqual(execution.retry_count, 1)
        self.assertEqual(execution.call_count, 2)
        tariffs = config["contracts"]["relevance"]["sweycoins_milli"]
        self.assertEqual(executor.meter.spent_milli, tariffs * 2)

    async def test_prose_is_rejected_by_the_contract(self) -> None:
        config = _config()
        task = spec(
            Kind.RELEVANCE,
            input_data={"passage": PASSAGE, "query": "pressure"},
            gold={"relevant": True},
            passage=PASSAGE,
            task_id="prose-task",
        )
        provider = ScriptedProvider(
            {}, default={"relevant": True, "reason": "free prose"}
        )
        executor = _executor(config, provider, concurrency=1, max_attempts=1)
        execution = await executor.run(
            [task], mode="distributed_independent", model_ids=["gpt-oss-20b"]
        )
        result = execution.results["prose-task"]
        self.assertFalse(result.verification.ok)
        self.assertEqual(result.selected_models, ())
        self.assertIn("extra keys", result.error or "")

    async def test_competition_selects_first_verified_and_cancels_the_rest(self) -> None:
        config = _config()
        task = spec(
            Kind.EXTRACTION,
            input_data={"passage": PASSAGE, "question": "pressure"},
            gold={"quotes": [GOLD_QUOTE]},
            passage=PASSAGE,
            task_id="compete-task",
        )
        provider = ScriptedProvider(
            {("compete-task", "gpt-oss-20b", 1): {"quotes": [GOLD_QUOTE]}},
            default={"quotes": []},
            delay_by_model={
                "gpt-oss-20b": 0.0,
                "nemotron-3-super": 50.0,
                "mimo-v2.6-flash": 50.0,
                "deepseek-v4.1-flash": 50.0,
            },
        )
        executor = _executor(config, provider, concurrency=4, max_attempts=1)
        execution = await executor.run(
            [task],
            mode="heterogeneous_competition",
            model_ids=[
                "gpt-oss-20b",
                "nemotron-3-super",
                "mimo-v2.6-flash",
                "deepseek-v4.1-flash",
            ],
        )
        result = execution.results["compete-task"]
        self.assertEqual(result.selected_models, ("gpt-oss-20b",))
        self.assertTrue(result.verification.ok)
        self.assertEqual(result.cancelled_candidates, 3)
        self.assertTrue(result.early_cancelled)
        self.assertEqual(execution.cancelled_count, 3)
        self.assertEqual(execution.early_cancelled_tasks, 1)

    async def test_competition_with_no_verified_candidate_fails_closed(self) -> None:
        config = _config()
        task = spec(
            Kind.EXTRACTION,
            input_data={"passage": PASSAGE, "question": "pressure"},
            gold={"quotes": [GOLD_QUOTE]},
            passage=PASSAGE,
            task_id="bad-task",
        )
        provider = ScriptedProvider({}, default={"quotes": ["not in the passage"]})
        executor = _executor(config, provider, concurrency=2, max_attempts=1)
        execution = await executor.run(
            [task],
            mode="heterogeneous_competition",
            model_ids=["gpt-oss-20b", "nemotron-3-super"],
        )
        result = execution.results["bad-task"]
        self.assertEqual(result.selected_models, ())
        self.assertIn("no candidate passed", result.error or "")

    async def test_unavailable_model_is_rejected_before_dispatch(self) -> None:
        config = _config()
        task = _relevance_specs(1)[0]
        executor = _executor(config, ScriptedProvider({}), concurrency=1)
        with self.assertRaises(ValueError):
            await executor.run(
                [task], mode="distributed_independent", model_ids=["nemotron-3-nano"]
            )

    async def test_dag_layers_are_deterministic(self) -> None:
        plan = compile_layers(
            [
                ScheduledTask("publish", depends_on=("lint", "test")),
                ScheduledTask("lint"),
                ScheduledTask("test", depends_on=("cache",)),
                ScheduledTask("cache"),
            ],
            concurrency=4,
        )
        self.assertEqual(plan.layers, (("lint", "cache"), ("test",), ("publish",)))
        self.assertTrue(plan.source)


if __name__ == "__main__":
    unittest.main()
