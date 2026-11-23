"""Pydantic AI calls for this experiment only.

The agent has no tools that touch SWE routing, workflow, or the cost ledger.
Native schema output is refused before HTTP when the tag or host is cloud.
`retries=0` and `request_limit=1` are set on every run.
"""

from __future__ import annotations

import os
import time
from typing import Any

from pydantic import BaseModel

from hy4_research_policy.capability import OPENAI_BASE_URL, routes_to_cloud
from hy4_research_policy.structure import sanitize

REQUEST_LIMIT = 1


class NativeOutputRefused(RuntimeError):
    """Raised before a request when native schema output is not enforced."""


def _plain(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if hasattr(value, "model_dump"):
        return _plain(value.model_dump())
    return str(value)[:4000]


def _observation(messages: list[Any], usage: Any) -> dict[str, Any]:
    content: list[str] = []
    thinking: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    content_present = False
    thinking_present = False
    for message in messages:
        for part in getattr(message, "parts", []):
            kind = type(part).__name__.lower()
            if "think" in kind or "reason" in kind:
                thinking_present = True
                thinking.append(str(getattr(part, "content", "") or ""))
            elif hasattr(part, "tool_name"):
                tool_calls.append(
                    {
                        "function": {
                            "name": getattr(part, "tool_name", ""),
                            "arguments": _plain(getattr(part, "args", None)),
                        }
                    }
                )
            elif isinstance(getattr(part, "content", None), str):
                content_present = True
                content.append(part.content)
    details = getattr(usage, "details", {}) or {}
    recorded: dict[str, Any] = {}
    requests = getattr(usage, "requests", None)
    if isinstance(requests, int) and not isinstance(requests, bool):
        recorded["requests"] = requests
    for key, value in (
        ("input", getattr(usage, "input_tokens", None)),
        ("output", getattr(usage, "output_tokens", None)),
        ("cache_read", getattr(usage, "cache_read_tokens", None)),
    ):
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            recorded[key] = value
    if isinstance(details, dict) and details:
        recorded["details"] = {
            str(key): item for key, item in details.items() if isinstance(item, int) and not isinstance(item, bool)
        }
    return {
        "model": None,
        "content": "\n".join(content) if content_present else None,
        "content_present": content_present,
        "thinking": "\n".join(thinking) if thinking_present else None,
        "thinking_present": thinking_present,
        "tool_calls": tool_calls or None,
        "finish_reason": None,
        "usage": recorded,
    }


def run_structured(
    *,
    model_name: str,
    prompt: str,
    schema: type[BaseModel],
    mode: str,
    max_output: int,
    think: bool,
) -> dict[str, Any]:
    cloud = routes_to_cloud(model_name, OPENAI_BASE_URL)
    if mode == "native":
        raise NativeOutputRefused(
            "native structured output is refused before HTTP on this cloud route: "
            + ",".join(cloud["signals"])
        )
    if mode not in {"tool", "prompt"}:
        raise ValueError(f"unsupported pydantic mode {mode}")
    if not cloud["cloud"]:
        raise NativeOutputRefused("this adapter only calls the cloud tags under test")

    os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
    from pydantic_ai import Agent
    from pydantic_ai.models.ollama import OllamaModel
    from pydantic_ai.output import PromptedOutput, ToolOutput
    from pydantic_ai.providers.ollama import OllamaProvider
    from pydantic_ai.settings import ModelSettings
    from pydantic_ai.usage import UsageLimits

    provider = OllamaProvider(base_url=OPENAI_BASE_URL)
    model = OllamaModel(model_name, provider=provider)
    if mode == "tool":
        # The installed profile reports openai_supports_strict_tool_definition false.
        # Strictness is the Pydantic model, not an unsupported API flag.
        output = ToolOutput(schema, max_retries=0)
    else:
        output = PromptedOutput(schema)
    agent = Agent(model, output_type=output, retries=0)
    settings: ModelSettings = {"max_tokens": max_output, "temperature": 0, "thinking": think}
    started = time.perf_counter()
    try:
        result = agent.run_sync(
            prompt,
            model_settings=settings,
            usage_limits=UsageLimits(request_limit=REQUEST_LIMIT),
        )
    except NativeOutputRefused:
        raise
    except Exception as exc:
        elapsed = (time.perf_counter() - started) * 1000
        return {
            "http_status": 0,
            "latency_ms": round(elapsed, 3),
            "error_type": type(exc).__name__,
            "error": sanitize(str(exc)[:2000]),
            "observation": {
                "model": model_name,
                "content": None,
                "content_present": False,
                "thinking": None,
                "thinking_present": False,
                "tool_calls": None,
                "finish_reason": None,
                "usage": {},
            },
        }
    elapsed = (time.perf_counter() - started) * 1000
    usage = result.usage
    if callable(usage):
        usage = usage()
    observation = _observation(result.all_messages(), usage)
    observation["model"] = model_name
    return {
        "http_status": 200,
        "latency_ms": round(elapsed, 3),
        "error_type": None,
        "error": None,
        "profile_supports_json_schema_output": bool(
            model.profile.get("supports_json_schema_output")
        ),
        "observation": observation,
    }
