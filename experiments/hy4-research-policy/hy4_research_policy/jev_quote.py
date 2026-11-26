"""One System One relation call per quote. Exact-quote scoring does not read this."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from hashlib import sha256
from pathlib import Path
from typing import Any

from hy4_research_policy.structure import sanitize

_KEYS = ("TYPESAFE_API_KEY", "TYPESAFE_BASE_URL", "JEV_MODEL")
_ENV_FILE = Path("/Users/erikfremred/research/.env")


def load_config() -> dict[str, str] | None:
    found = {key: os.environ.get(key, "").strip() for key in _KEYS}
    if not all(found.values()) and _ENV_FILE.is_file():
        for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key in _KEYS and not found[key]:
                found[key] = value.strip().strip('"').strip("'")
    if not all(found.values()):
        return None
    return found


def request_body(*, model: str, proposition: str, evidence: str, source_id: str) -> dict[str, Any]:
    return {
        "model": model,
        "state": {
            "proposition": proposition,
            "evidence": evidence,
            "bindings": {"entities": [], "versions": []},
            "conditions": [],
            "requested_value": "",
            "negation": "",
            "modality": "",
            "cardinality": "",
            "temporal_scope": "",
            "dependencies": [],
            "source": {
                "source_id": source_id,
                "document_version": "frozen",
                "locator": "quote",
                "content_hash": sha256(evidence.encode("utf-8")).hexdigest(),
            },
            "context": {
                "research_question": proposition,
                "information_question": proposition,
                "answer_rule_id": "exact-quote-comparison",
                "role": "context_not_evidence",
            },
        },
        "questions": {
            "relation": {
                "type": "choice",
                "instructions": "Does the evidence establish the proposition for this research question?",
                "criteria": {
                    "SUPPORTS": "The evidence states the proposition for the question's subject.",
                    "CONTRADICTS": "The evidence states an incompatible value for the same subject.",
                    "INSUFFICIENT": "The evidence does not decide the proposition.",
                },
            }
        },
    }


def post_relation(config: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
    url = config["TYPESAFE_BASE_URL"].rstrip("/") + "/v1/systemone"
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {config['TYPESAFE_API_KEY']}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
            elapsed = (time.perf_counter() - started) * 1000
            return {
                "http_status": response.status,
                "latency_ms": round(elapsed, 3),
                "response": _choice(payload if isinstance(payload, dict) else {}),
            }
    except urllib.error.HTTPError as exc:
        elapsed = (time.perf_counter() - started) * 1000
        return {
            "http_status": exc.code,
            "latency_ms": round(elapsed, 3),
            "response": {"error": sanitize(exc.read(500).decode("utf-8", errors="replace"))},
        }
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        elapsed = (time.perf_counter() - started) * 1000
        return {"http_status": 0, "latency_ms": round(elapsed, 3), "response": {"error": type(exc).__name__}}


def _choice(payload: dict[str, Any]) -> dict[str, Any]:
    answers = payload.get("answers") if isinstance(payload.get("answers"), dict) else {}
    relation = answers.get("relation") if isinstance(answers.get("relation"), dict) else {}
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    kept = {
        "choice": relation.get("choice"),
        "confidence": relation.get("confidence"),
    }
    for key in ("input_tokens", "prompt_tokens", "output_tokens", "completion_tokens"):
        if key in usage:
            kept[key] = usage[key]
    return sanitize(kept)
