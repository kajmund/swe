"""Bounded Hy4 vs DeepSeek Flash pilot through the local Ollama daemon.

The worst case is reserved before the first request: cache-miss peak prices,
measured prompt bytes divided by two, the requested max output, and one retry
per question. Verification is the local exact-quote check and costs nothing.
A plan above the protocol cap is not dispatched.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from hashlib import sha256
from decimal import Decimal
from pathlib import Path
from typing import Any

from hy4_research_policy.cost import compare_with_deepseek
from hy4_research_policy.freeze import load_corpus, load_protocol, load_questions
from hy4_research_policy.score import score_trace

MAX_OUTPUT_TOKENS = 1024
RETRY_LIMIT = 1
INPUT_BYTES_PER_TOKEN = 2
OLLAMA_CHAT = "http://127.0.0.1:11434/api/chat"
ARM_TAGS = {
    "deepseek-flash": "deepseek-v4.1-flash:cloud",
    "hy4": "hy4:cloud",
}
_MILLION = Decimal(1_000_000)


def input_token_bound(prompt: str) -> int:
    """Upper bound: one token per two UTF-8 bytes, never below one."""

    return max(1, (len(prompt.encode("utf-8")) + INPUT_BYTES_PER_TOKEN - 1) // INPUT_BYTES_PER_TOKEN)


def _peak_prices(arm: dict[str, Any]) -> tuple[str, str]:
    prices = arm["prices_usd_per_million"]
    return prices["input"], prices["output"]


def worst_call_usd(arm: dict[str, Any], *, input_tokens: int, max_output: int) -> Decimal:
    """Cache-miss peak price at the output ceiling. Cache savings are not assumed."""

    input_rate, output_rate = _peak_prices(arm)
    return (
        Decimal(input_tokens) * Decimal(input_rate) + Decimal(max_output) * Decimal(output_rate)
    ) / _MILLION


def pilot_plan(
    protocol: dict[str, Any],
    *,
    input_tokens: int,
    questions: int,
    max_output: int = MAX_OUTPUT_TOKENS,
    retries: int = RETRY_LIMIT,
) -> dict[str, Any]:
    arms = [arm for arm in protocol["arms"] if arm["id"] in ARM_TAGS and arm.get("available", True)]
    attempts = 1 + retries
    per_arm = []
    total = Decimal(0)
    for arm in arms:
        one = worst_call_usd(arm, input_tokens=input_tokens, max_output=max_output)
        arm_total = one * questions * attempts
        total += arm_total
        per_arm.append(
            {
                "arm_id": arm["id"],
                "model": ARM_TAGS[arm["id"]],
                "call_usd": format(one.normalize(), "f"),
                "attempts": attempts,
                "worst_usd": format(arm_total.normalize(), "f"),
            }
        )
    cap = Decimal(protocol["budget"]["usd_cap"])
    return {
        "questions": questions,
        "max_output_tokens": max_output,
        "retries_per_question": retries,
        "verification_usd": "0",
        "verification": "exact-quote-v1",
        "price_basis": "cache-miss peak, no cache credit",
        "input_token_bound": input_tokens,
        "arms": per_arm,
        "worst_usd": format(total.normalize(), "f"),
        "usd_cap": format(cap.normalize(), "f"),
        "fits": total <= cap,
    }


def build_prompt(question: dict[str, Any], corpus: dict[str, str]) -> str:
    documents = [
        {"source_id": source_id, "text": text}
        for source_id, text in sorted(corpus.items())
    ]
    payload = {
        "task": (
            "Decide the research policy for this question using only the corpus. "
            "Copy quotes exactly. Return one JSON object."
        ),
        "conclusions": ["ANSWERED", "CONTRADICTED", "INSUFFICIENT_EVIDENCE"],
        "tools": ["retrieve", "read_passage"],
        "rules": [
            "target_quotes and claims must be exact substrings of the named document.",
            "Every claim must also appear as a read_passage action.",
            "Use CONTRADICTED when the corpus states conflicting values.",
            "Use INSUFFICIENT_EVIDENCE when the corpus does not state the answer.",
            "answer_complete is true only for ANSWERED with every required quote read.",
        ],
        "response_schema": {
            "needs": [{"target_quotes": [{"source_id": "document id", "quote": "exact text"}]}],
            "actions": [
                {"tool": "retrieve", "gap": {"source_id": "document id", "quote": "exact text"}},
                {"tool": "read_passage", "gap": {"source_id": "document id", "quote": "exact text"}},
            ],
            "stop": {
                "conclusion": "ANSWERED",
                "answer_complete": True,
                "claims": [{"source_id": "document id", "quote": "exact text"}],
            },
        },
        "corpus": documents,
        "question_id": question["question_id"],
        "question": question["text"],
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _request_body(model: str, prompt: str) -> dict[str, Any]:
    return {
        "model": model,
        "stream": False,
        "think": False,
        "messages": [{"role": "user", "content": prompt}],
        "options": {"num_predict": MAX_OUTPUT_TOKENS, "temperature": 0, "seed": 0},
    }


def _price_period(moment: datetime) -> str:
    """Ollama off-peak is outside 12:00-18:00 UTC on weekdays."""

    if moment.weekday() >= 5:
        return "off_peak"
    return "peak" if 12 <= moment.hour < 18 else "off_peak"


_UNPARSED = {"source_id": "unparsed", "quote": "\u0000unparsed"}


def _parse_object(text: str) -> dict[str, Any] | None:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
    candidates = [cleaned]
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        candidates.append(cleaned[start : end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _content_json(payload: dict[str, Any]) -> dict[str, Any] | None:
    message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
    for field in ("content", "thinking"):
        parsed = _parse_object(str(message.get(field) or ""))
        if parsed is not None:
            return parsed
    return None


def _as_quote(item: Any) -> dict[str, str]:
    if isinstance(item, dict):
        source_id = item.get("source_id")
        quote = item.get("quote")
        if isinstance(source_id, str) and isinstance(quote, str):
            return {"source_id": source_id, "quote": quote}
    return dict(_UNPARSED)


def _steps_from_decision(decision: dict[str, Any]) -> list[dict[str, Any]]:
    needs_in = decision.get("needs") if isinstance(decision.get("needs"), list) else []
    needs = []
    for need in needs_in:
        targets_in = need.get("target_quotes") if isinstance(need, dict) else None
        targets = [_as_quote(target) for target in targets_in] if isinstance(targets_in, list) else [dict(_UNPARSED)]
        needs.append({"target_quotes": targets})
    actions = decision.get("actions") if isinstance(decision.get("actions"), list) else []
    stop = decision.get("stop") if isinstance(decision.get("stop"), dict) else {}
    claims_in = stop.get("claims") if isinstance(stop.get("claims"), list) else []
    steps: list[dict[str, Any]] = [{"type": "decompose", "needs": needs, "latency_ms": 0}]
    for action in actions:
        tool = str(action.get("tool") or "") if isinstance(action, dict) else "unparsed"
        gap = _as_quote(action.get("gap") if isinstance(action, dict) else None)
        steps.append({"type": "select_next", "tool": tool, "gap": gap, "latency_ms": 0})
        tool_step: dict[str, Any] = {"type": "tool", "tool": tool, "latency_ms": 0}
        if tool == "read_passage":
            tool_step["quote"] = gap
        steps.append(tool_step)
    steps.append(
        {
            "type": "stop",
            "conclusion": str(stop.get("conclusion") or ""),
            "answer_complete": bool(stop.get("answer_complete")),
            "claims": [_as_quote(claim) for claim in claims_in],
            "latency_ms": 0,
        }
    )
    return steps


def _call_record(payload: dict[str, Any], *, attempt: int, latency_ms: float, period: str) -> dict[str, Any]:
    """Keep provider token fields that are present and drop everything else."""

    record: dict[str, Any] = {
        "attempt": attempt,
        "latency_ms": round(latency_ms, 3),
        "price_period": period,
        "done_reason": payload.get("done_reason"),
    }
    for key in ("prompt_eval_count", "eval_count"):
        if key in payload:
            record[key] = payload[key]
    message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
    if "thinking" in message:
        record["reasoning_present"] = message.get("thinking") not in (None, "")
    return record


def _post(body: dict[str, Any]) -> tuple[int, dict[str, Any], float]:
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        OLLAMA_CHAT,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            raw = response.read()
            elapsed = (time.perf_counter() - started) * 1000
            parsed = json.loads(raw.decode("utf-8"))
            return response.status, parsed if isinstance(parsed, dict) else {}, elapsed
    except urllib.error.HTTPError as exc:
        elapsed = (time.perf_counter() - started) * 1000
        detail = exc.read(2000).decode("utf-8", errors="replace")
        return exc.code, {"error": detail[:500]}, elapsed
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        elapsed = (time.perf_counter() - started) * 1000
        return 0, {"error": type(exc).__name__}, elapsed


def _get_json(url: str) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            parsed = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _request_count(payload: dict[str, Any] | None) -> int | None:
    totals = payload.get("totals") if isinstance(payload, dict) else None
    count = totals.get("request_count") if isinstance(totals, dict) else None
    if isinstance(count, bool) or not isinstance(count, int):
        return None
    return count


def _note_input_overrun(
    payload: dict[str, Any],
    arm: dict[str, Any],
    *,
    bound: int,
    reserved: Decimal,
    cap: Decimal,
    protocol: dict[str, Any],
    questions: int,
) -> tuple[int, Decimal, str | None]:
    """Raise the input bound from an observed prompt and stop if the plan no longer fits."""

    observed = payload.get("prompt_eval_count")
    if isinstance(observed, bool) or not isinstance(observed, int) or observed <= bound:
        return bound, reserved, None
    extra = Decimal(observed - bound) * Decimal(_peak_prices(arm)[0]) / _MILLION
    reserved += extra
    bound = observed
    replanned = pilot_plan(protocol, input_tokens=bound, questions=questions)
    if reserved > cap or not replanned["fits"]:
        return bound, reserved, "observed prompt tokens exceed the reserved input bound"
    return bound, reserved, None


def run_pilot(output_dir: Path) -> dict[str, Any]:
    protocol = load_protocol()
    questions = load_questions(protocol)
    corpus = load_corpus(protocol)
    prompts = [build_prompt(question, corpus) for question in questions]
    bound = max(input_token_bound(prompt) for prompt in prompts)
    plan = pilot_plan(protocol, input_tokens=bound, questions=len(questions))
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "preflight.json").write_text(
        json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    if not plan["fits"]:
        raise SystemExit(
            f"pilot refused before dispatch: worst case {plan['worst_usd']} exceeds cap {plan['usd_cap']}"
        )
    usage_before = _get_json("http://127.0.0.1:11434/api/usage")
    before_count = _request_count(usage_before)
    cap = Decimal(plan["usd_cap"])
    reserved = Decimal(0)
    scores = []
    stopped = None
    if _get_json("http://127.0.0.1:11434/api/version") is None:
        stopped = "local Ollama daemon is not reachable"
    for arm_id, model in ARM_TAGS.items():
        if stopped:
            break
        arm = next(item for item in protocol["arms"] if item["id"] == arm_id)
        for question, prompt in zip(questions, prompts, strict=True):
            calls: list[dict[str, Any]] = []
            decision = None
            for attempt in range(1, RETRY_LIMIT + 2):
                moment = datetime.now(timezone.utc)
                period = _price_period(moment)
                worst = worst_call_usd(
                    arm, input_tokens=bound, max_output=MAX_OUTPUT_TOKENS
                )
                if reserved + worst > cap:
                    stopped = "remaining cap cannot hold the next max-output reservation"
                    break
                reserved += worst
                status, payload, latency_ms = _post(_request_body(model, prompt))
                bound, reserved, overrun = _note_input_overrun(
                    payload,
                    arm,
                    bound=bound,
                    reserved=reserved,
                    cap=cap,
                    protocol=protocol,
                    questions=len(questions),
                )
                observed_output = payload.get("eval_count")
                if isinstance(observed_output, int) and not isinstance(observed_output, bool):
                    if observed_output > MAX_OUTPUT_TOKENS:
                        stopped = "provider output exceeded num_predict"
                calls.append(
                    {
                        **_call_record(payload, attempt=attempt, latency_ms=latency_ms, period=period),
                        "http_status": status,
                    }
                )
                if stopped or overrun:
                    stopped = stopped or overrun
                    break
                if status == 0:
                    break
                if status == 200:
                    decision = _content_json(payload)
                    if decision is not None:
                        break
            trace = {
                "arm_id": arm_id,
                "question_id": question["question_id"],
                "model": model,
                "prompt_sha256": sha256(prompt.encode("utf-8")).hexdigest(),
                "max_output_tokens": MAX_OUTPUT_TOKENS,
                "calls": calls,
                "steps": _steps_from_decision(decision or {}),
            }
            scored = score_trace(trace, question, corpus, protocol)
            scores.append(scored)
            (output_dir / f"{arm_id}-{question['question_id']}.json").write_text(
                json.dumps(trace, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            if stopped:
                break
        if stopped:
            break
    usage_after = _get_json("http://127.0.0.1:11434/api/usage") if scores or stopped else None
    after_count = _request_count(usage_after)
    if before_count is None or after_count is None:
        quota_requests: int | str = "N/A"
    else:
        quota_requests = after_count - before_count
    report = {
        "stopped": stopped,
        "reserved_worst_usd": format(reserved.normalize(), "f"),
        "usd_cap": plan["usd_cap"],
        "verification": "exact-quote-v1",
        "verification_usd": "0",
        "quota": {
            "requests": quota_requests,
            "credits_consumed_usd": "N/A",
            "plan": "not present",
        },
        "scores": scores,
        "comparison": compare_with_deepseek(scores, protocol) if scores else None,
    }
    (output_dir / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return report


def main() -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = Path(__file__).resolve().parents[1] / "runs" / stamp
    report = run_pilot(output_dir)
    print(
        json.dumps(
            {
                "output": str(output_dir),
                "stopped": report["stopped"],
                "questions": len(report["scores"]),
                "reserved_worst_usd": report["reserved_worst_usd"],
                "quota_requests": report["quota"]["requests"],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
