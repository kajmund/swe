from __future__ import annotations

import asyncio
import threading
import unittest
from datetime import UTC, datetime
from decimal import Decimal
from unittest import mock

from microtask_swarm import cli
from microtask_swarm.estimate import estimate_matrix
from microtask_swarm.executor import ChatRequest, ProviderResponse
from microtask_swarm.contracts import Kind, output_schema
from microtask_swarm.live import (
    LiveDispatchRefused,
    OllamaCloudClient,
    _think_value,
    check_live_gate,
    preflight,
    price_period,
)

from tests.support import load_test_config


class LiveGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_test_config()

    def test_no_cap_refuses(self) -> None:
        with self.assertRaises(LiveDispatchRefused):
            check_live_gate(self.config, approved_cap_usd=None)
        with self.assertRaises(LiveDispatchRefused):
            check_live_gate(self.config, approved_cap_usd="0")

    def test_missing_credential_refuses(self) -> None:
        with self.assertRaises(LiveDispatchRefused):
            check_live_gate(
                self.config, approved_cap_usd="3.00", environ={}
            )

    def test_explicit_cap_and_credential_allow_gate(self) -> None:
        gate = check_live_gate(
            self.config,
            approved_cap_usd="3.00",
            environ={"OLLAMA_API_KEY": "secret"},
        )
        self.assertEqual(str(gate.approved_cap_usd), "3.00")
        self.assertEqual(gate.api_key, "secret")
        self.assertEqual(gate.chat_endpoint, "https://ollama.com/api/chat")

    def test_local_daemon_does_not_need_a_key(self) -> None:
        gate = check_live_gate(
            self.config,
            approved_cap_usd="3.00",
            environ={},
            use_local_daemon=True,
        )
        self.assertIsNone(gate.api_key)
        self.assertTrue(gate.chat_endpoint.startswith("http://localhost"))

    def test_price_period_window(self) -> None:
        self.assertEqual(
            price_period(datetime(2026, 11, 23, 13, 0, tzinfo=UTC)), "peak"
        )
        self.assertEqual(
            price_period(datetime(2026, 11, 23, 10, 0, tzinfo=UTC)), "off_peak"
        )
        self.assertEqual(
            price_period(datetime(2026, 11, 21, 13, 0, tzinfo=UTC)), "off_peak"
        )

    def test_think_values_follow_verified_capabilities(self) -> None:
        models = {model["id"]: model for model in self.config["models"]}
        self.assertEqual(_think_value(models["gpt-oss-20b"]), "low")
        self.assertIs(_think_value(models["nemotron-3-super"]), False)
        self.assertIs(_think_value(models["mimo-v2.6-flash"]), False)
        self.assertIs(_think_value(models["deepseek-v4.1-flash"]), False)

    def test_preflight_checks_exact_tag_digest_and_capabilities(self) -> None:
        gate = check_live_gate(
            self.config,
            approved_cap_usd="3.00",
            environ={"OLLAMA_API_KEY": "secret"},
        )
        catalog = {
            "models": [
                {"name": "gpt-oss:20b", "digest": "05afbac4bad6"},
                {"name": "nemotron-3-super", "digest": "da11955bb451"},
            ]
        }
        show_gpt = {
            "capabilities": ["completion", "tools", "thinking"],
            "thinking": {"values": ["low", "medium", "high"], "default": "medium"},
            "model_info": {"gptoss.context_length": 131072},
        }
        show_nemotron = {
            "capabilities": ["completion", "thinking", "tools"],
            "thinking": {"values": [False, True], "default": True},
            "model_info": {"nemotron_h_moe.context_length": 262144},
        }

        def fake_http(url, *, payload, api_key, timeout=30.0):
            if payload is None:
                return catalog
            return show_gpt if payload["model"] == "gpt-oss:20b" else show_nemotron

        with mock.patch("microtask_swarm.live._http_json", side_effect=fake_http):
            result = preflight(
                self.config, gate, ["gpt-oss-20b", "nemotron-3-super"]
            )
        self.assertEqual(result["catalog_count"], 2)
        self.assertEqual(result["models"][0]["digest"], "05afbac4bad6")
        self.assertEqual(result["models"][0]["context_length"], 131072)

    def test_preflight_refuses_digest_drift(self) -> None:
        gate = check_live_gate(
            self.config,
            approved_cap_usd="3.00",
            environ={"OLLAMA_API_KEY": "secret"},
        )
        catalog = {"models": [{"name": "gpt-oss:20b", "digest": "deadbeef0000"}]}
        with mock.patch(
            "microtask_swarm.live._http_json", return_value=catalog
        ):
            with self.assertRaises(LiveDispatchRefused):
                preflight(self.config, gate, ["gpt-oss-20b"])

    def test_preflight_refuses_unavailable_model_without_network(self) -> None:
        gate = check_live_gate(
            self.config,
            approved_cap_usd="3.00",
            environ={"OLLAMA_API_KEY": "secret"},
        )
        with mock.patch(
            "microtask_swarm.live._http_json", side_effect=AssertionError("network")
        ):
            with self.assertRaises(LiveDispatchRefused):
                preflight(self.config, gate, ["nemotron-3-nano"])

    def test_cli_live_without_cap_refuses_before_network(self) -> None:
        with mock.patch(
            "microtask_swarm.live._http_json", side_effect=AssertionError("network")
        ):
            self.assertEqual(cli.main(["live"]), 2)

    def test_chat_request_is_strict_and_uses_verified_thinking_control(self) -> None:
        gate = check_live_gate(
            self.config,
            approved_cap_usd="3.00",
            environ={"OLLAMA_API_KEY": "secret"},
        )
        models = {model["id"]: model for model in self.config["models"]}
        client = OllamaCloudClient(gate=gate, models=models)
        captured: dict = {}

        def fake_http(url, *, payload, api_key, timeout=30.0):
            captured["url"] = url
            captured["payload"] = payload
            return {
                "message": {"content": '{"relevant":true}'},
                "prompt_eval_count": 1000,
                "prompt_eval_cached_count": 250,
                "eval_count": 12,
            }

        request = ChatRequest(
            request_id="r1",
            task_id="t1",
            kind=Kind.RELEVANCE,
            model_id="gpt-oss-20b",
            system="system",
            user='{"typed":true}',
            schema=output_schema(Kind.RELEVANCE),
            max_output_tokens=32,
            passage="passage",
            input_data={"passage": "passage", "query": "q"},
            attempt=1,
            timeout_seconds=10.0,
        )
        with mock.patch("microtask_swarm.live._http_json", side_effect=fake_http):
            response = client._chat_sync(request)
        payload = captured["payload"]
        self.assertEqual(payload["model"], "gpt-oss:20b")
        self.assertEqual(payload["stream"], False)
        self.assertEqual(payload["think"], "low")
        self.assertEqual(payload["format"], output_schema(Kind.RELEVANCE))
        self.assertEqual(payload["options"]["temperature"], 0)
        self.assertEqual(payload["options"]["num_predict"], 32)
        self.assertEqual(response.tokens["cache_read"], 250)
        self.assertFalse(response.tokens["cache_read_assumed_miss"])
        self.assertEqual(response.tokens["input"], 1000)
        self.assertEqual(response.tokens["output"], 12)
        self.assertIsNone(response.billed_usd)

    def test_missing_cache_count_is_an_explicit_cache_miss(self) -> None:
        gate = check_live_gate(
            self.config,
            approved_cap_usd="3.00",
            environ={"OLLAMA_API_KEY": "secret"},
        )
        models = {model["id"]: model for model in self.config["models"]}
        client = OllamaCloudClient(gate=gate, models=models)

        def fake_http(url, *, payload, api_key, timeout=30.0):
            return {
                "message": {"content": '{"relevant":true}'},
                "prompt_eval_count": 100,
                "eval_count": 5,
            }

        request = ChatRequest(
            request_id="r3",
            task_id="t3",
            kind=Kind.RELEVANCE,
            model_id="mimo-v2.6-flash",
            system="system",
            user="{}",
            schema=output_schema(Kind.RELEVANCE),
            max_output_tokens=32,
            passage="passage",
            input_data={"passage": "passage", "query": "q"},
            attempt=1,
            timeout_seconds=10.0,
        )
        with mock.patch("microtask_swarm.live._http_json", side_effect=fake_http):
            response = client._chat_sync(request)
        self.assertEqual(response.tokens["cache_read"], 0)
        self.assertTrue(response.tokens["cache_read_assumed_miss"])

    def test_reasoning_off_is_sent_when_supported(self) -> None:
        gate = check_live_gate(
            self.config,
            approved_cap_usd="3.00",
            environ={"OLLAMA_API_KEY": "secret"},
        )
        models = {model["id"]: model for model in self.config["models"]}
        client = OllamaCloudClient(gate=gate, models=models)
        captured: dict = {}

        def fake_http(url, *, payload, api_key, timeout=30.0):
            captured["payload"] = payload
            return {
                "message": {"content": '{"relevant":true}'},
                "prompt_eval_count": 10,
                "eval_count": 2,
            }

        request = ChatRequest(
            request_id="r2",
            task_id="t2",
            kind=Kind.RELEVANCE,
            model_id="nemotron-3-super",
            system="system",
            user="{}",
            schema=output_schema(Kind.RELEVANCE),
            max_output_tokens=32,
            passage="passage",
            input_data={"passage": "passage", "query": "q"},
            attempt=1,
            timeout_seconds=10.0,
        )
        with mock.patch("microtask_swarm.live._http_json", side_effect=fake_http):
            client._chat_sync(request)
        self.assertIs(captured["payload"]["think"], False)

    def test_selected_matrix_changes_the_approval_estimate(self) -> None:
        baseline = estimate_matrix(self.config)
        selected = estimate_matrix(
            self.config,
            model_ids=["deepseek-v4.1-flash", "gpt-oss-20b", "mimo-v2.6-flash", "nemotron-3-super"],
            concurrency_levels=list(self.config["concurrency_levels"]),
        )
        self.assertEqual(selected["all_in_estimated_usd"], baseline["all_in_estimated_usd"])
        wider = estimate_matrix(
            self.config,
            concurrency_levels=[1, 2, 4, 8, 16, 32, 64, 128],
        )
        self.assertGreater(
            Decimal(wider["all_in_estimated_usd"]),
            Decimal(baseline["all_in_estimated_usd"]),
        )
        self.assertEqual(wider["assumptions"]["concurrency_levels"], 8)
        one_model = estimate_matrix(
            self.config, model_ids=["nemotron-3-super"], modes=["single_call"]
        )
        self.assertLess(
            Decimal(one_model["all_in_estimated_usd"]),
            Decimal(baseline["all_in_estimated_usd"]),
        )

    def test_oversized_live_sweep_refuses_before_network(self) -> None:
        with mock.patch.dict("os.environ", {"OLLAMA_API_KEY": "secret"}, clear=False):
            with mock.patch(
                "microtask_swarm.live._http_json", side_effect=AssertionError("network")
            ):
                code = cli.main(
                    [
                        "live",
                        "--approve-usd-cap",
                        "1.20",
                        "--concurrency",
                        "1",
                        "2",
                        "4",
                        "8",
                        "16",
                        "32",
                        "64",
                        "128",
                    ]
                )
        self.assertEqual(code, 2)

    def test_cancelled_chat_joins_the_worker_before_returning(self) -> None:
        gate = check_live_gate(
            self.config,
            approved_cap_usd="3.00",
            environ={"OLLAMA_API_KEY": "secret"},
        )
        models = {model["id"]: model for model in self.config["models"]}
        client = OllamaCloudClient(gate=gate, models=models)
        started = threading.Event()
        release = threading.Event()
        phase = {"value": "init"}

        def _blocked(_request: ChatRequest) -> ProviderResponse:
            phase["value"] = "waiting"
            started.set()
            self.assertTrue(release.wait(2))
            phase["value"] = "finished"
            return ProviderResponse(content="{}", tokens={"input": 1, "output": 1})

        request = ChatRequest(
            request_id="r3",
            task_id="t3",
            kind=Kind.RELEVANCE,
            model_id="nemotron-3-super",
            system="system",
            user="{}",
            schema=output_schema(Kind.RELEVANCE),
            max_output_tokens=32,
            passage="passage",
            input_data={"passage": "passage", "query": "q"},
            attempt=1,
            timeout_seconds=10.0,
        )

        async def _run() -> None:
            with mock.patch.object(client, "_chat_sync", side_effect=_blocked):
                task = asyncio.create_task(client.chat(request))
                await asyncio.to_thread(started.wait, 2)
                task.cancel()
                await asyncio.sleep(0.05)
                self.assertEqual(phase["value"], "waiting")
                self.assertFalse(task.done())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
