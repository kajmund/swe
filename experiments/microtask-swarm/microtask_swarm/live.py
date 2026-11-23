"""Live Ollama Cloud adapter, hard-gated behind an approved USD cap.

No paid dispatch is possible by default. ``check_live_gate`` refuses unless the
caller supplies a positive cap, a credential (or explicitly uses the local
daemon), and every selected model is verified available. ``preflight`` performs
only free metadata reads: GET /api/tags and POST /api/show.
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from microtask_swarm.executor import ChatRequest, ProviderResponse


class LiveDispatchRefused(RuntimeError):
    """A paid dispatch or live preflight was requested without authority."""


@dataclass(frozen=True, slots=True)
class LiveGate:
    approved_cap_usd: Decimal
    chat_endpoint: str
    tags_endpoint: str
    show_endpoint: str
    api_key: str | None
    use_local_daemon: bool
    price_period: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "approved_cap_usd": format(self.approved_cap_usd.normalize(), "f"),
            "chat_endpoint": self.chat_endpoint,
            "use_local_daemon": self.use_local_daemon,
            "credential": "present" if self.api_key else "absent",
            "price_period": self.price_period or "N/A",
        }


def check_live_gate(
    config: Mapping[str, Any],
    *,
    approved_cap_usd: str | None,
    environ: Mapping[str, str] | None = None,
    use_local_daemon: bool = False,
    now: datetime | None = None,
) -> LiveGate:
    environ = os.environ if environ is None else environ
    if approved_cap_usd is None or not str(approved_cap_usd).strip():
        raise LiveDispatchRefused(
            "no paid dispatch is approved: pass --approve-usd-cap with the "
            "explicit cap approved by the user"
        )
    try:
        cap = Decimal(str(approved_cap_usd))
    except InvalidOperation as exc:
        raise LiveDispatchRefused("--approve-usd-cap must be a decimal") from exc
    if not cap.is_finite() or cap <= 0:
        raise LiveDispatchRefused("--approve-usd-cap must be positive and finite")

    provider = config["provider"]
    api_key = environ.get("OLLAMA_API_KEY", "").strip() or None
    if not use_local_daemon and api_key is None:
        raise LiveDispatchRefused(
            "OLLAMA_API_KEY is absent and --use-local-daemon was not supplied"
        )
    if use_local_daemon:
        chat = "http://localhost:11434/api/chat"
        tags = "http://localhost:11434/api/tags"
        show = "http://localhost:11434/api/show"
    else:
        chat = str(provider["chat_endpoint"])
        tags = str(provider["tags_endpoint"])
        show = str(provider["show_endpoint"])
    return LiveGate(
        approved_cap_usd=cap,
        chat_endpoint=chat,
        tags_endpoint=tags,
        show_endpoint=show,
        api_key=api_key,
        use_local_daemon=use_local_daemon,
        price_period=price_period(now or datetime.now(UTC)),
    )


def price_period(now: datetime) -> str:
    """Ollama's published off-peak window: outside 12:00-18:00 UTC weekdays."""

    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    now = now.astimezone(UTC)
    if now.weekday() >= 5:
        return "off_peak"
    return "peak" if 12 <= now.hour < 18 else "off_peak"


def credential_status(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    environ = os.environ if environ is None else environ
    return {
        "OLLAMA_API_KEY": "present"
        if environ.get("OLLAMA_API_KEY", "").strip()
        else "absent"
    }


def _http_json(
    url: str,
    *,
    payload: Mapping[str, Any] | None,
    api_key: str | None,
    timeout: float = 30.0,
) -> Any:
    data = None
    headers = {"User-Agent": "swe-microtask-swarm-research/0.1"}
    if payload is not None:
        data = json.dumps(payload, sort_keys=True).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        raise LiveDispatchRefused(
            f"{url} returned HTTP {exc.code}: {body}"
        ) from exc
    except urllib.error.URLError as exc:
        raise LiveDispatchRefused(f"{url} is unreachable: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise LiveDispatchRefused(f"{url} returned non-JSON metadata") from exc


def preflight(
    config: Mapping[str, Any],
    gate: LiveGate,
    model_ids: Sequence[str],
) -> dict[str, Any]:
    """Free metadata checks: exact tags, digests, capabilities, thinking values."""

    by_id = {str(model["id"]): model for model in config["models"]}
    selected: list[Mapping[str, Any]] = []
    for model_id in model_ids:
        model = by_id.get(model_id)
        if model is None:
            raise LiveDispatchRefused(f"unknown model {model_id!r}")
        if not model.get("available", False):
            raise LiveDispatchRefused(
                f"model {model_id!r} is unavailable: {model.get('reason', 'no reason')}"
            )
        selected.append(model)
    catalog = _http_json(
        gate.tags_endpoint, payload=None, api_key=gate.api_key
    )
    names = {
        str(model.get("name")): str(model.get("digest", ""))[:12]
        for model in catalog.get("models", [])
        if isinstance(model, Mapping)
    }
    rows: list[dict[str, Any]] = []
    for model in selected:
        model_id = str(model["id"])
        api_model = str(model["api_model"])
        if api_model not in names:
            raise LiveDispatchRefused(
                f"exact tag {api_model!r} is absent from {gate.tags_endpoint}"
            )
        digest = names[api_model]
        expected = model.get("catalog_digest")
        if expected and digest != expected:
            raise LiveDispatchRefused(
                f"{api_model!r} digest {digest} does not match pinned {expected}"
            )
        show = _http_json(
            gate.show_endpoint,
            payload={"model": api_model},
            api_key=gate.api_key,
        )
        capabilities = show.get("capabilities", [])
        missing = set(model.get("capabilities", [])) - set(capabilities)
        if missing:
            raise LiveDispatchRefused(
                f"{api_model!r} is missing capabilities {sorted(missing)}"
            )
        thinking = show.get("thinking")
        if isinstance(thinking, Mapping):
            supported = thinking.get("values")
            selected = model.get("thinking", {}).get("selected")
            if supported is not None and selected not in supported:
                raise LiveDispatchRefused(
                    f"{api_model!r} does not support thinking value {selected!r}; "
                    f"supported values are {supported!r}"
                )
        rows.append(
            {
                "model_id": model_id,
                "api_model": api_model,
                "digest": digest,
                "capabilities": capabilities,
                "thinking": thinking,
                "context_length": _context_length(show),
            }
        )
    return {"catalog_count": len(names), "models": rows}


def _context_length(show: Mapping[str, Any]) -> int | None:
    info = show.get("model_info")
    if not isinstance(info, Mapping):
        return None
    for key, value in info.items():
        if str(key).endswith(".context_length") and isinstance(value, int):
            return value
    return None


class OllamaCloudClient:
    """Minimal stdlib client. It is only constructed by the live runner."""

    def __init__(
        self,
        *,
        gate: LiveGate,
        models: Mapping[str, Mapping[str, Any]],
    ) -> None:
        self.gate = gate
        self.models = models

    async def chat(self, request: ChatRequest) -> ProviderResponse:
        return await asyncio.to_thread(self._chat_sync, request)

    def _chat_sync(self, request: ChatRequest) -> ProviderResponse:
        model = self.models[request.model_id]
        payload: dict[str, Any] = {
            "model": str(model["api_model"]),
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user},
            ],
            "stream": False,
            "format": dict(request.schema),
            "options": {
                "temperature": 0,
                "num_predict": request.max_output_tokens,
                "num_ctx": 4096,
            },
        }
        think = _think_value(model)
        if think is not None:
            payload["think"] = think
        raw = _http_json(
            self.gate.chat_endpoint,
            payload=payload,
            api_key=self.gate.api_key,
            timeout=max(1.0, request.timeout_seconds),
        )
        message = raw.get("message")
        if not isinstance(message, Mapping) or not isinstance(message.get("content"), str):
            raise LiveDispatchRefused("Ollama response is missing message.content")
        prompt_tokens = raw.get("prompt_eval_count")
        output_tokens = raw.get("eval_count")
        cache_read = raw.get("prompt_eval_cached_count")
        return ProviderResponse(
            content=str(message["content"]),
            tokens={
                "input": prompt_tokens if isinstance(prompt_tokens, int) else None,
                "output": output_tokens if isinstance(output_tokens, int) else None,
                "reasoning": None,
                "cache_read": cache_read if isinstance(cache_read, int) else None,
                "cache_write": None,
                "source": "provider",
            },
            billed_usd=raw.get("billed_usd") if isinstance(raw.get("billed_usd"), str) else None,
            price_period=self.gate.price_period
            if model.get("price_period_required")
            else None,
        )


def _think_value(model: Mapping[str, Any]) -> Any:
    thinking = model.get("thinking")
    if not isinstance(thinking, Mapping):
        capabilities = model.get("capabilities", [])
        return None if "thinking" not in capabilities else False
    if thinking.get("off_supported"):
        return False
    selected = thinking.get("selected")
    if selected is not None:
        return selected
    values = thinking.get("values") or []
    return values[0] if values else None


__all__ = [
    "LiveDispatchRefused",
    "LiveGate",
    "OllamaCloudClient",
    "check_live_gate",
    "credential_status",
    "preflight",
    "price_period",
]
