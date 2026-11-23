"""One direct Ollama `/api/chat` call. This client never retries."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

from hy4_research_policy.capability import NATIVE_CHAT
from hy4_research_policy.structure import sanitize


def post_chat(body: dict[str, Any], *, timeout: float = 180) -> dict[str, Any]:
    request = urllib.request.Request(
        NATIVE_CHAT,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            elapsed = (time.perf_counter() - started) * 1000
            parsed = json.loads(raw.decode("utf-8"))
            payload = parsed if isinstance(parsed, dict) else {"body": parsed}
            return {
                "http_status": response.status,
                "latency_ms": round(elapsed, 3),
                "response": sanitize(payload),
            }
    except urllib.error.HTTPError as exc:
        elapsed = (time.perf_counter() - started) * 1000
        detail = exc.read(4000).decode("utf-8", errors="replace")
        return {
            "http_status": exc.code,
            "latency_ms": round(elapsed, 3),
            "response": {"error": sanitize(detail[:2000])},
        }
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        elapsed = (time.perf_counter() - started) * 1000
        return {
            "http_status": 0,
            "latency_ms": round(elapsed, 3),
            "response": {"error": type(exc).__name__},
        }


def observation_from_native(payload: dict[str, Any]) -> dict[str, Any]:
    message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
    usage = {}
    for source, target in (("prompt_eval_count", "input"), ("eval_count", "output")):
        if source in payload:
            usage[target] = payload[source]
    return {
        "model": payload.get("model"),
        "content": message.get("content") if isinstance(message.get("content"), str) else None,
        "content_present": "content" in message,
        "thinking": message.get("thinking") if isinstance(message.get("thinking"), str) else None,
        "thinking_present": "thinking" in message,
        "tool_calls": message.get("tool_calls") if isinstance(message.get("tool_calls"), list) else None,
        "finish_reason": payload.get("done_reason"),
        "usage": usage,
    }


def chat_body(
    model: str,
    prompt: str,
    *,
    max_output: int,
    think: bool,
    response_format: dict[str, Any] | None = None,
    tools: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "stream": False,
        "think": think,
        "messages": [{"role": "user", "content": prompt}],
        "options": {"num_predict": max_output, "temperature": 0, "seed": 0},
    }
    if response_format is not None:
        body["format"] = response_format
    if tools is not None:
        body["tools"] = tools
    return body
