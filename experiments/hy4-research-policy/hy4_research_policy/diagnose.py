"""Schema diagnosis, then isolated tasks if one channel validates.

Every request is reserved against a fresh $2 cap before it is sent. A schema
failure is stored and is not retried. Semantic scores are omitted until the
schema validates.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any

from hy4_research_policy.adapter import NativeOutputRefused, run_structured
from hy4_research_policy.cost import _rates
from hy4_research_policy.freeze import load_corpus, load_protocol, load_questions, package_root
from hy4_research_policy.jev_quote import load_config, post_relation, request_body
from hy4_research_policy.native_chat import chat_body, observation_from_native, post_chat
from hy4_research_policy.pilot import ARM_TAGS, _price_period, input_token_bound, worst_call_usd
from hy4_research_policy.plan import (
    DIAGNOSIS_OUTPUT_TOKENS,
    DIAGNOSIS_USD_CAP,
    JEV_INPUT_USD_PER_MILLION,
    TASK_OUTPUT_TOKENS,
    diagnosis_plan,
)
from hy4_research_policy.schemas import PolicyDecision
from hy4_research_policy.structure import classify_output, sanitize
from hy4_research_policy.tasks import TASK_SCHEMAS, score_exact, task_prompt

_MILLION = Decimal(1_000_000)
PRIOR_RUN = "20261123T205650Z"


class Ledger:
    def __init__(self, cap: Decimal) -> None:
        self.cap = cap
        self.reserved = Decimal(0)
        self.reason: str | None = None

    def reserve(self, amount: Decimal) -> bool:
        if self.reason is not None:
            return False
        if self.reserved + amount > self.cap:
            self.reason = "remaining cap cannot hold the next reservation"
            return False
        self.reserved += amount
        return True


def _arm(protocol: dict[str, Any], arm_id: str) -> dict[str, Any]:
    return next(arm for arm in protocol["arms"] if arm["id"] == arm_id)


def _output_usd(arm: dict[str, Any], period: str, tokens: object) -> str:
    if not isinstance(tokens, int) or isinstance(tokens, bool):
        return "N/A"
    rates = _rates(arm, period)
    if rates is None:
        return "N/A"
    return format((Decimal(tokens) * Decimal(rates[1]) / _MILLION).normalize(), "f")


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(sanitize(payload), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _failed_questions(questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    prior = package_root() / "runs" / PRIOR_RUN
    failed = []
    for question in questions:
        path = prior / f"deepseek-flash-{question['question_id']}.json"
        trace = json.loads(path.read_text(encoding="utf-8"))
        conclusion = trace["steps"][-1].get("conclusion")
        if conclusion == "" and trace["calls"][0]["eval_count"] > 0:
            failed.append(question)
    return failed


def _over_cap(observation: dict[str, Any], max_output: int) -> bool:
    output = observation.get("usage", {}).get("output")
    requests = observation.get("usage", {}).get("requests")
    if isinstance(requests, int) and requests > 1:
        return True
    return isinstance(output, int) and not isinstance(output, bool) and output > max_output


def _apply_cached_stop(record: dict[str, Any], ledger: Ledger, max_output: int, reason: str) -> None:
    """Reapply the live stop when a saved call already exceeded its reservation."""

    if ledger.reason is not None:
        return
    classification = record.get("classification")
    usage = classification.get("usage") if isinstance(classification, dict) else None
    if not isinstance(usage, dict):
        usage = {}
    if record.get("hidden_retry") is True or _over_cap({"usage": usage}, max_output):
        ledger.reason = reason


def _record_native(
    *,
    name: str,
    arm_id: str,
    model: str,
    prompt: str,
    body: dict[str, Any],
    schema: type,
    channel: str,
    max_output: int,
    period: str,
    arm: dict[str, Any],
    ledger: Ledger,
    output_dir: Path,
    question: dict[str, Any] | None = None,
    task: str | None = None,
    corpus: dict[str, str] | None = None,
) -> dict[str, Any]:
    amount = worst_call_usd(
        arm,
        input_tokens=input_token_bound(prompt),
        max_output=max_output,
    )
    if not ledger.reserve(amount):
        return {"name": name, "skipped": ledger.reason}
    existing = output_dir / f"{name}.json"
    if existing.is_file():
        record = json.loads(existing.read_text(encoding="utf-8"))
        _apply_cached_stop(
            record,
            ledger,
            max_output,
            "provider output exceeded num_predict or a second request was observed",
        )
        return record
    sent = post_chat(body)
    payload = sent["response"] if isinstance(sent["response"], dict) else {}
    observation = observation_from_native(payload)
    classified = classify_output(
        observation,
        schema,
        requested_model=model,
        max_output=max_output,
        channel=channel,
    )
    exact = None
    if classified["structure_error"] is None and task and question and corpus is not None:
        exact = score_exact(task, classified["parsed"], question, corpus)
    record = {
        "name": name,
        "transport": "ollama-native-chat",
        "arm_id": arm_id,
        "model": model,
        "question_id": None if question is None else question["question_id"],
        "task": task,
        "prompt_sha256": sha256(prompt.encode("utf-8")).hexdigest(),
        "request": body,
        "http_status": sent["http_status"],
        "latency_ms": sent["latency_ms"],
        "price_period": period,
        "output_usd": _output_usd(arm, period, observation.get("usage", {}).get("output")),
        "actual_usd": "N/A",
        "classification": classified,
        "exact_quote": exact,
        "hidden_retry": False,
    }
    _write(output_dir / f"{name}.json", record)
    if _over_cap(observation, max_output):
        ledger.reason = "provider output exceeded num_predict or a second request was observed"
    return record


def _record_pydantic(
    *,
    name: str,
    mode: str,
    model: str,
    prompt: str,
    schema: type,
    max_output: int,
    think: bool,
    period: str,
    arm: dict[str, Any],
    ledger: Ledger,
    output_dir: Path,
    question: dict[str, Any],
) -> dict[str, Any]:
    amount = worst_call_usd(
        arm, input_tokens=input_token_bound(prompt) + 2000, max_output=max_output
    )
    if not ledger.reserve(amount):
        return {"name": name, "skipped": ledger.reason}
    existing = output_dir / f"{name}.json"
    if existing.is_file():
        record = json.loads(existing.read_text(encoding="utf-8"))
        _apply_cached_stop(
            record,
            ledger,
            max_output,
            "pydantic sent more than one request or output exceeded max_tokens",
        )
        return record
    try:
        sent = run_structured(
            model_name=model,
            prompt=prompt,
            schema=schema,
            mode=mode,
            max_output=max_output,
            think=think,
        )
    except NativeOutputRefused as exc:
        record = {"name": name, "transport": "pydantic-ai", "refused_before_http": str(exc)}
        _write(output_dir / f"{name}.json", record)
        return record
    observation = sent["observation"]
    channel = "tool" if mode == "tool" else "content"
    classified = classify_output(
        observation,
        schema,
        requested_model=model,
        max_output=max_output,
        channel=channel,
    )
    requests = observation.get("usage", {}).get("requests")
    record = {
        "name": name,
        "transport": "pydantic-ai",
        "mode": mode,
        "model": model,
        "question_id": question["question_id"],
        "prompt_sha256": sha256(prompt.encode("utf-8")).hexdigest(),
        "http_status": sent["http_status"],
        "latency_ms": sent["latency_ms"],
        "error_type": sent.get("error_type"),
        "error": sent.get("error"),
        "profile_supports_json_schema_output": sent.get("profile_supports_json_schema_output"),
        "price_period": period,
        "output_usd": _output_usd(arm, period, observation.get("usage", {}).get("output")),
        "actual_usd": "N/A",
        "classification": classified,
        "exact_quote": None,
        "hidden_retry": isinstance(requests, int) and requests > 1,
    }
    _write(output_dir / f"{name}.json", record)
    if record["hidden_retry"] or _over_cap(observation, max_output):
        ledger.reason = "pydantic sent more than one request or output exceeded max_tokens"
    return record


def _choose_mode(records: list[dict[str, Any]]) -> str | None:
    order = (
        ("direct-prompt", "direct_prompt"),
        ("direct-tools", "direct_tools"),
        ("pydantic-prompt", "pydantic_prompt"),
        ("pydantic-tool", "pydantic_tool"),
    )
    by_name = {record.get("name"): record for record in records}
    if "direct-prompt" not in by_name and "replay-multi_document_orbit-think-false" in by_name:
        by_name["direct-prompt"] = by_name["replay-multi_document_orbit-think-false"]
    for name, mode in order:
        record = by_name.get(name)
        if record and record.get("classification", {}).get("structure_error") is None:
            return mode
    return None


def run_diagnosis(output_dir: Path) -> dict[str, Any]:
    protocol = load_protocol()
    questions = load_questions(protocol)
    corpus = load_corpus(protocol)
    failed = _failed_questions(questions)
    replay_prompts = [task_prompt("decomposition", question, corpus) for question in questions]
    # The replay bound is the original combined prompt, which is larger.
    from hy4_research_policy.pilot import build_prompt

    original_prompts = {question["question_id"]: build_prompt(question, corpus) for question in questions}
    replay_bound = max(input_token_bound(prompt) for prompt in original_prompts.values())
    task_bound = max(input_token_bound(prompt) for prompt in replay_prompts)
    for task, task_schema in TASK_SCHEMAS.items():
        suffix = json.dumps(task_schema.model_json_schema())
        task_bound = max(
            task_bound,
            max(
                input_token_bound(task_prompt(task, question, corpus) + suffix) + 2000
                for question in questions
            ),
        )
    plan = diagnosis_plan(
        protocol, replay_input_tokens=replay_bound + 2000, task_input_tokens=task_bound
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    _write(output_dir / "preflight.json", plan)
    if not plan["fits"]:
        raise SystemExit(f"diagnosis refused before dispatch: {plan['worst_usd']} exceeds {plan['usd_cap']}")
    ledger = Ledger(DIAGNOSIS_USD_CAP)
    deepseek = _arm(protocol, "deepseek-flash")
    model = ARM_TAGS["deepseek-flash"]
    records: list[dict[str, Any]] = []
    period = _price_period(datetime.now(timezone.utc))
    focus = next(question for question in failed if question["question_id"] == "multi_document_orbit")
    focus_prompt = original_prompts[focus["question_id"]]
    for question in failed:
        if ledger.reason:
            break
        prompt = original_prompts[question["question_id"]]
        records.append(
            _record_native(
                name=f"replay-{question['question_id']}-think-false",
                arm_id="deepseek-flash",
                model=model,
                prompt=prompt,
                body=chat_body(model, prompt, max_output=DIAGNOSIS_OUTPUT_TOKENS, think=False),
                schema=PolicyDecision,
                channel="content",
                max_output=DIAGNOSIS_OUTPUT_TOKENS,
                period=period,
                arm=deepseek,
                ledger=ledger,
                output_dir=output_dir,
                question=question,
            )
        )
    if ledger.reason is None:
        records.append(
            _record_native(
                name="focus-think-true",
                arm_id="deepseek-flash",
                model=model,
                prompt=focus_prompt,
                body=chat_body(model, focus_prompt, max_output=DIAGNOSIS_OUTPUT_TOKENS, think=True),
                schema=PolicyDecision,
                channel="content",
                max_output=DIAGNOSIS_OUTPUT_TOKENS,
                period=period,
                arm=deepseek,
                ledger=ledger,
                output_dir=output_dir,
                question=focus,
            )
        )
    schema = PolicyDecision.model_json_schema()
    if ledger.reason is None:
        records.append(
            _record_native(
                name="direct-tools",
                arm_id="deepseek-flash",
                model=model,
                prompt=focus_prompt,
                body=chat_body(
                    model,
                    focus_prompt,
                    max_output=DIAGNOSIS_OUTPUT_TOKENS,
                    think=False,
                    tools=[
                        {
                            "type": "function",
                            "function": {
                                "name": "policy_decision",
                                "description": "Return the policy decision.",
                                "parameters": schema,
                            },
                        }
                    ],
                ),
                schema=PolicyDecision,
                channel="tool",
                max_output=DIAGNOSIS_OUTPUT_TOKENS,
                period=period,
                arm=deepseek,
                ledger=ledger,
                output_dir=output_dir,
                question=focus,
            )
        )
    if ledger.reason is None:
        records.append(
            _record_native(
                name="direct-format-probe",
                arm_id="deepseek-flash",
                model=model,
                prompt=focus_prompt,
                body=chat_body(
                    model,
                    focus_prompt,
                    max_output=DIAGNOSIS_OUTPUT_TOKENS,
                    think=False,
                    response_format=schema,
                ),
                schema=PolicyDecision,
                channel="content",
                max_output=DIAGNOSIS_OUTPUT_TOKENS,
                period=period,
                arm=deepseek,
                ledger=ledger,
                output_dir=output_dir,
                question=focus,
            )
        )
    for mode, name in (("tool", "pydantic-tool"), ("prompt", "pydantic-prompt")):
        if ledger.reason:
            break
        records.append(
            _record_pydantic(
                name=name,
                mode=mode,
                model=model,
                prompt=focus_prompt,
                schema=PolicyDecision,
                max_output=DIAGNOSIS_OUTPUT_TOKENS,
                think=False,
                period=period,
                arm=deepseek,
                ledger=ledger,
                output_dir=output_dir,
                question=focus,
            )
        )
    # Prompt JSON echoed the request. Native format did not enforce the schema.
    # The tool channel returned an object, so the isolated tasks use that channel.
    task_mode = None if ledger.reason else "direct_tools"
    task_records: list[dict[str, Any]] = []
    if task_mode is not None and ledger.reason is None:
        for arm_id in ("deepseek-flash", "hy4"):
            if ledger.reason:
                break
            arm = _arm(protocol, arm_id)
            tag = ARM_TAGS[arm_id]
            for task, task_schema in TASK_SCHEMAS.items():
                if ledger.reason:
                    break
                for question in questions:
                    if ledger.reason:
                        break
                    prompt = task_prompt(task, question, corpus)
                    period = _price_period(datetime.now(timezone.utc))
                    name = f"task-{arm_id}-{task}-{question['question_id']}"
                    if task_mode == "direct_tools":
                        task_records.append(
                            _record_native(
                                name=name,
                                arm_id=arm_id,
                                model=tag,
                                prompt=prompt,
                                body=chat_body(
                                    tag,
                                    prompt,
                                    max_output=TASK_OUTPUT_TOKENS,
                                    think=False,
                                    tools=[
                                        {
                                            "type": "function",
                                            "function": {
                                                "name": task,
                                                "description": task,
                                                "parameters": task_schema.model_json_schema(),
                                            },
                                        }
                                    ],
                                ),
                                schema=task_schema,
                                channel="tool",
                                max_output=TASK_OUTPUT_TOKENS,
                                period=period,
                                arm=arm,
                                ledger=ledger,
                                output_dir=output_dir,
                                question=question,
                                task=task,
                                corpus=corpus,
                            )
                        )
                    elif task_mode == "direct_prompt":
                        task_records.append(
                            _record_native(
                                name=name,
                                arm_id=arm_id,
                                model=tag,
                                prompt=prompt + "\n" + json.dumps(task_schema.model_json_schema()),
                                body=chat_body(
                                    tag,
                                    prompt + "\n" + json.dumps(task_schema.model_json_schema()),
                                    max_output=TASK_OUTPUT_TOKENS,
                                    think=False,
                                ),
                                schema=task_schema,
                                channel="content",
                                max_output=TASK_OUTPUT_TOKENS,
                                period=period,
                                arm=arm,
                                ledger=ledger,
                                output_dir=output_dir,
                                question=question,
                                task=task,
                                corpus=corpus,
                            )
                        )
                    else:
                        pydantic_mode = "tool" if task_mode == "pydantic_tool" else "prompt"
                        sent_record = _record_pydantic(
                            name=name,
                            mode=pydantic_mode,
                            model=tag,
                            prompt=prompt,
                            schema=task_schema,
                            max_output=TASK_OUTPUT_TOKENS,
                            think=False,
                            period=period,
                            arm=arm,
                            ledger=ledger,
                            output_dir=output_dir,
                            question=question,
                        )
                        if sent_record.get("classification", {}).get("structure_error") is None:
                            sent_record["exact_quote"] = score_exact(
                                task, sent_record["classification"]["parsed"], question, corpus
                            )
                            sent_record["task"] = task
                            _write(output_dir / f"{name}.json", sent_record)
                        task_records.append(sent_record)
    jev_records = _jev_pass(task_records, questions, ledger, output_dir)
    report = {
        "stopped": ledger.reason,
        "reserved_worst_usd": format(ledger.reserved.normalize(), "f"),
        "usd_cap": format(DIAGNOSIS_USD_CAP.normalize(), "f"),
        "validated_policy_mode": _choose_mode(records),
        "task_mode": task_mode,
        "diagnosis": [
            {
                "name": record.get("name"),
                "structure_error": (record.get("classification") or {}).get("structure_error"),
                "json_keys": (record.get("classification") or {}).get("json_keys"),
                "finish_reason": (record.get("classification") or {}).get("finish_reason"),
                "truncated": (record.get("classification") or {}).get("truncated"),
                "model_tag_match": (record.get("classification") or {}).get("model_tag_match"),
                "thinking_present": (record.get("classification") or {}).get("thinking_present"),
                "output_usd": record.get("output_usd"),
                "actual_usd": record.get("actual_usd", "N/A"),
                "latency_ms": record.get("latency_ms"),
            }
            for record in records
        ],
        "tasks": task_records,
        "jev": jev_records,
    }
    _write(output_dir / "report.json", report)
    return report


def _jev_pass(
    task_records: list[dict[str, Any]],
    questions: list[dict[str, Any]],
    ledger: Ledger,
    output_dir: Path,
) -> list[dict[str, Any]]:
    config = load_config()
    if config is None:
        return [{"status": "not_configured"}]
    by_id = {question["question_id"]: question for question in questions}
    rows = []
    seen: set[tuple[str, str, str]] = set()
    for record in task_records:
        parsed = (record.get("classification") or {}).get("parsed")
        if record.get("classification", {}).get("structure_error") or not isinstance(parsed, dict):
            continue
        question = by_id.get(record.get("question_id"))
        if question is None:
            continue
        for source_id, quote in _quotes(parsed):
            key = (record["arm_id"], source_id, quote)
            if key in seen or ledger.reason:
                continue
            seen.add(key)
            body = request_body(
                model=config["JEV_MODEL"],
                proposition=question["text"],
                evidence=quote,
                source_id=source_id,
            )
            amount = Decimal(input_token_bound(json.dumps(body))) * JEV_INPUT_USD_PER_MILLION / _MILLION
            if not ledger.reserve(amount):
                break
            sent = post_relation(config, body)
            rows.append(
                {
                    "arm_id": record["arm_id"],
                    "question_id": question["question_id"],
                    "source_id": source_id,
                    "http_status": sent["http_status"],
                    "latency_ms": sent["latency_ms"],
                    "jev": sent["response"],
                    "actual_usd": "N/A",
                }
            )
            if sent["http_status"] != 200:
                break
    _write(output_dir / "jev.json", {"calls": rows})
    return rows


def _quotes(parsed: dict[str, Any]) -> list[tuple[str, str]]:
    found = []
    for key in ("target_quotes", "unresolved", "claims"):
        for item in parsed.get(key) or []:
            if isinstance(item, dict) and isinstance(item.get("quote"), str):
                found.append((str(item.get("source_id", "")), item["quote"]))
    gap = parsed.get("gap")
    if isinstance(gap, dict) and isinstance(gap.get("quote"), str):
        found.append((str(gap.get("source_id", "")), gap["quote"]))
    return found


def main() -> None:
    resume = os.environ.get("DIAGNOSE_RESUME")
    if resume:
        output_dir = Path(resume)
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        output_dir = package_root() / "runs" / stamp
    report = run_diagnosis(output_dir)
    print(
        json.dumps(
            {
                "output": str(output_dir),
                "stopped": report["stopped"],
                "task_mode": report["task_mode"],
                "diagnosis": len(report["diagnosis"]),
                "tasks": len(report["tasks"]),
                "reserved_worst_usd": report["reserved_worst_usd"],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
